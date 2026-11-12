import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from warehouse.store import ObjectStore

EXAMPLES = ROOT / 'examples'
RECONCILE_ARGS = ('--charge-watermark', '2026-11-04', '--settlement-watermark', '2026-11-04', '--rules', str(EXAMPLES / 'rules.json'))


def execute(*arguments, expect_failure=False):
    command = [sys.executable, '-m', 'warehouse', *arguments]
    print('$ ' + ' '.join(command), flush=True)
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    if expect_failure:
        lines = result.stderr.strip().splitlines()
        print(lines[-1] if lines else '(无输出)', flush=True)
        if result.returncode == 0:
            raise RuntimeError('预期失败的命令却成功返回')
        return None
    if result.returncode != 0:
        print(result.stderr, flush=True)
        raise RuntimeError('命令执行失败')
    print(result.stdout.strip(), flush=True)
    return json.loads(result.stdout)


def main():
    with tempfile.TemporaryDirectory() as temporary:
        database = str(Path(temporary) / 'warehouse.sqlite3')
        objects = str(Path(temporary) / 'objects')

        print('== 导入合成输入：收费、包裹、结算明细、渠道映射、分摊规则 ==', flush=True)
        if not execute('import', '--db', database, '--input', str(EXAMPLES / 'source.json'), '--batch', 'main')['changed']:
            raise RuntimeError('首次导入没有写入')

        print('== 现有查询：订单包裹数量 ==', flush=True)
        execute('inspect', '--db', database)

        print('== 对账：拆单、多币种、重复明细、缺侧数据（固定双方水位与映射版本 v1） ==', flush=True)
        summary = execute('reconcile', '--db', database, '--run', 'run-v1', '--mapping-version', 'v1', *RECONCILE_ARGS)
        if summary['categories'] != {'matched': 4, 'within_tolerance': 1, 'true_difference': 2, 'indeterminate': 3}:
            raise RuntimeError('对账分类计数与预期不符')
        if summary['passed']:
            raise RuntimeError('存在无法判断分区时不应判定通过')

        print('== 可重入：同标识同输入重跑直接返回原结果，不重复写入 ==', flush=True)
        if execute('reconcile', '--db', database, '--run', 'run-v1', '--mapping-version', 'v1', *RECONCILE_ARGS) != summary:
            raise RuntimeError('同输入重跑结果不一致')

        print('== 分区结果：差额 = 实结 - 应收，缺侧为 null 而非零 ==', flush=True)
        execute('results', '--db', database, '--run', 'run-v1')

        print('== 钻取：容差边界分区 p2（差额 0.05 恰等于容差，判容差内） ==', flush=True)
        execute('explain', '--db', database, '--run', 'run-v1', '--package', 'p2', '--channel', 'express', '--date', '2026-11-01', '--currency', 'CNY')

        print('== 钻取：金额确为零的 p8（与无结算记录分开表达） ==', flush=True)
        execute('explain', '--db', database, '--run', 'run-v1', '--package', 'p8', '--channel', 'truck', '--date', '2026-11-03', '--currency', 'CNY')

        print('== 自动校验：逐行分配守恒与各类差额，而非仅比较总额 ==', flush=True)
        if not execute('verify', '--db', database, '--run', 'run-v1')['ok']:
            raise RuntimeError('对账校验未通过')

        print('== 结算行重投：同内容幂等吸收，不重复计入 ==', flush=True)
        execute('import', '--db', database, '--input', str(EXAMPLES / 'redelivery.json'), '--batch', 'redelivery')
        connection = sqlite3.connect(database)
        try:
            if connection.execute('SELECT COUNT(*) FROM settlements').fetchone()[0] != 10:
                raise RuntimeError('结算行重投被重复计入')
        finally:
            connection.close()
        if execute('reconcile', '--db', database, '--run', 'run-v1', '--mapping-version', 'v1', *RECONCILE_ARGS) != summary:
            raise RuntimeError('重投后对账结果发生变化')

        print('== 结算行标识相同而内容不同：阻断该次发布 ==', flush=True)
        execute('import', '--db', database, '--input', str(EXAMPLES / 'conflict.json'), '--batch', 'conflict', expect_failure=True)

        print('== 映射修订：v2 仅重算受影响渠道的分区，未受影响明细整体继承 ==', flush=True)
        execute('import', '--db', database, '--input', str(EXAMPLES / 'mapping_v2.json'), '--batch', 'mapping-v2')
        execute('reconcile', '--db', database, '--run', 'run-v2', '--mapping-version', 'v2', '--reuse-from', 'run-v1', *RECONCILE_ARGS)
        execute('reconcile', '--db', database, '--run', 'run-v2-full', '--mapping-version', 'v2', *RECONCILE_ARGS)
        if execute('results', '--db', database, '--run', 'run-v2') != execute('results', '--db', database, '--run', 'run-v2-full'):
            raise RuntimeError('增量重算与全量重算结果不一致')
        print('增量重算与全量重算逐行一致', flush=True)

        print('== 失败语义：映射缺失的运行不被当成完整批次 ==', flush=True)
        broken = str(Path(temporary) / 'broken.sqlite3')
        execute('import', '--db', broken, '--input', str(EXAMPLES / 'broken_source.json'), '--batch', 'main')
        execute('reconcile', '--db', broken, '--run', 'run-broken', '--mapping-version', 'v1', *RECONCILE_ARGS, expect_failure=True)
        runs = execute('runs', '--db', broken)
        if [row['status'] for row in runs] != ['failed']:
            raise RuntimeError('失败运行未被标记为 failed')
        if execute('results', '--db', broken, '--run', 'run-broken') != []:
            raise RuntimeError('失败运行留下了结果行')

        print('== 导出与对象校验（既有功能） ==', flush=True)
        exported = execute('export', '--db', database, '--objects', objects)
        if ObjectStore(objects).read(exported['object']) != execute('inspect', '--db', database):
            raise RuntimeError('导出与查询结果不符')
        print('全部演示与校验通过', flush=True)


if __name__ == '__main__':
    main()
