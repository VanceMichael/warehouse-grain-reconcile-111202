"""仓库对账层。

订单应收先按约定权重分摊到包裹，再与承运商逐包裹实结核对：
差额 = 实结 - 应收，按（包裹、归一渠道、业务日期、币种）汇总。
一次运行固定双方水位与映射版本；失败不留下被当成完整的批次。
"""

import hashlib
import json
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .catalog import encode, transaction

MATCHED = 'matched'
WITHIN_TOLERANCE = 'within_tolerance'
TRUE_DIFFERENCE = 'true_difference'
INDETERMINATE = 'indeterminate'
CATEGORIES = (MATCHED, WITHIN_TOLERANCE, TRUE_DIFFERENCE, INDETERMINATE)

DEFAULT_PRECISION = 2

RESULT_COLUMNS = ('package', 'channel', 'business_date', 'currency', 'receivable', 'settled', 'difference', 'category')
ALLOCATION_COLUMNS = ('charge_line', 'order_id', 'package', 'weight', 'share', 'allocated', 'residual', 'source_channel', 'channel', 'business_date', 'currency')
SETTLEMENT_COLUMNS = ('line', 'package', 'source_channel', 'channel', 'business_date', 'currency', 'amount')
RUN_TABLES = ('recon_results', 'recon_allocations', 'recon_settlements', 'recon_runs')


class ReconcileError(ValueError):
    """对账输入或规则不满足约束，本次发布被阻断。"""


def _check_date(value, field):
    if not isinstance(value, str):
        raise ReconcileError(f'{field} 需要 ISO 日期文本，实际为 {value!r}')
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ReconcileError(f'{field} 需要 ISO 日期文本，实际为 {value!r}')


def _decimal(value, field):
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise ReconcileError(f'{field} 需要十进制文本，实际为 {value!r}')


def _sha256(payload):
    return hashlib.sha256(encode(payload)).hexdigest()


def _mapping_view(connection, version):
    rows = [dict(row) for row in connection.execute(
        'SELECT source_channel, target_channel, effective_from FROM channel_mappings WHERE version=? ORDER BY source_channel, effective_from',
        (version,))]
    timeline = defaultdict(list)
    for row in rows:
        timeline[row['source_channel']].append((row['effective_from'], row['target_channel']))

    def resolve(source_channel, business_date):
        options = [(effective, target) for effective, target in timeline.get(source_channel, []) if effective <= business_date]
        if not options:
            return None
        latest = max(effective for effective, _ in options)
        targets = {target for effective, target in options if effective == latest}
        if len(targets) > 1:
            raise ReconcileError(f'渠道 {source_channel} 在映射版本 {version} 下存在重复映射')
        return targets.pop()

    return resolve, rows


def affected_channels(connection, version_a, version_b):
    """两个映射版本之间解析结果不同的来源渠道，即修订影响面。"""

    def timeline(version):
        rows = connection.execute(
            'SELECT source_channel, target_channel, effective_from FROM channel_mappings WHERE version=? ORDER BY source_channel, effective_from',
            (version,)).fetchall()
        grouped = defaultdict(list)
        for row in rows:
            grouped[row['source_channel']].append((row['effective_from'], row['target_channel']))
        return {source: sorted(entries) for source, entries in grouped.items()}

    before = timeline(version_a)
    after = timeline(version_b)
    return {source for source in set(before) | set(after) if before.get(source) != after.get(source)}


def _collect_inputs(connection, charge_watermark, settlement_watermark, mapping_version):
    charges = [dict(row) for row in connection.execute(
        'SELECT * FROM charges WHERE business_date <= ? ORDER BY line', (charge_watermark,))]
    settlements = [dict(row) for row in connection.execute(
        'SELECT * FROM settlements WHERE business_date <= ? ORDER BY line', (settlement_watermark,))]
    packages = [dict(row) for row in connection.execute('SELECT * FROM packages ORDER BY package')]
    resolve, mappings = _mapping_view(connection, mapping_version)
    rules = {row['order_id']: row['mode'] for row in connection.execute('SELECT order_id, mode FROM allocation_rules ORDER BY order_id')}
    weights = defaultdict(dict)
    for row in connection.execute('SELECT order_id, package, weight FROM allocation_weights ORDER BY order_id, package'):
        weights[row['order_id']][row['package']] = row['weight']
    return {
        'charges': charges,
        'settlements': settlements,
        'packages': packages,
        'mappings': mappings,
        'rules': rules,
        'weights': {order: dict(entries) for order, entries in weights.items()},
        'resolve': resolve,
    }


def _order_weights(order_id, packages, mode, weights):
    """返回 {包裹: 权重}；权重不足、模式未知一律阻断，不做均分。"""
    if mode == 'explicit':
        table = weights.get(order_id, {})
        unknown = sorted(set(table) - {package['package'] for package in packages})
        if unknown:
            raise ReconcileError(f'订单 {order_id} 的分摊权重指向未知包裹: {unknown}')
        result = {}
        for package in packages:
            name = package['package']
            if name not in table:
                raise ReconcileError(f'订单 {order_id} 的包裹 {name} 缺少分摊权重，拒绝均分')
            weight = _decimal(table[name], f'订单 {order_id} 包裹 {name} 的分摊权重')
            if weight < 0:
                raise ReconcileError(f'订单 {order_id} 包裹 {name} 的分摊权重为负')
            result[name] = weight
        return result
    if mode == 'units':
        # 数量仅作为权重规则的一种可选依据，不直接当作金额
        return {package['package']: Decimal(package['units']) for package in packages}
    raise ReconcileError(f'订单 {order_id} 的分摊规则模式未知: {mode!r}')


def _split_amount(amount, weights, precision):
    """按权重把金额量化分摊到包裹；尾差稳定落到权重最大（并列取包裹号最小）的成员。"""
    quantum = Decimal(1).scaleb(-precision)
    total = sum(weights.values())
    if total <= 0:
        raise ReconcileError('分摊权重合计必须为正，拒绝均分')
    shares = {}
    allocated = {}
    for name in sorted(weights):
        raw = amount * weights[name] / total
        shares[name] = raw
        allocated[name] = raw.quantize(quantum, rounding=ROUND_HALF_UP)
    residual = amount - sum(allocated.values())
    residual_member = None
    if residual != 0:
        residual_member = min(weights, key=lambda name: (-weights[name], name))
        allocated[residual_member] += residual
    return shares, allocated, residual_member


def _build_partitions(params, inputs, reuse):
    for row in inputs['mappings']:
        _check_date(row['effective_from'], '映射生效日期')
    precision = params['precision']
    tolerances = params['tolerances']
    resolve = inputs['resolve']
    packages_by_order = defaultdict(list)
    for package in inputs['packages']:
        packages_by_order[package['order_id']].append(package)

    allocations = []
    settlement_lines = []
    charges = inputs['charges']
    settlements = inputs['settlements']
    if reuse is not None:
        # 映射修订只重算受影响渠道：未受影响的分摊与结算明细整体继承
        affected = reuse['affected']
        allocations.extend(row for row in reuse['allocations'] if row['source_channel'] not in affected)
        settlement_lines.extend(row for row in reuse['settlements'] if row['source_channel'] not in affected)
        charges = [row for row in charges if row['channel'] in affected]
        settlements = [row for row in settlements if row['channel'] in affected]

    for charge in charges:
        _check_date(charge['business_date'], f"收费行 {charge['line']} 的业务日期")
        amount = _decimal(charge['amount'], f"收费行 {charge['line']} 的金额")
        order_packages = packages_by_order.get(charge['order_id'])
        if not order_packages:
            raise ReconcileError(f"订单 {charge['order_id']} 存在收费但缺少包裹记录")
        mode = inputs['rules'].get(charge['order_id'])
        if mode is None:
            raise ReconcileError(f"订单 {charge['order_id']} 缺少分摊规则，拒绝均分")
        channel = resolve(charge['channel'], charge['business_date'])
        if channel is None:
            raise ReconcileError(f"收费行 {charge['line']} 的渠道 {charge['channel']} 在映射版本 {params['mapping_version']} 下缺失，拒绝猜测")
        order_weights = _order_weights(charge['order_id'], order_packages, mode, inputs['weights'])
        shares, allocated, residual_member = _split_amount(amount, order_weights, precision.get(charge['currency'], DEFAULT_PRECISION))
        for name in sorted(order_weights):
            allocations.append({
                'charge_line': charge['line'],
                'order_id': charge['order_id'],
                'package': name,
                'weight': str(order_weights[name]),
                'share': str(shares[name]),
                'allocated': str(allocated[name]),
                'residual': 1 if name == residual_member else 0,
                'source_channel': charge['channel'],
                'channel': channel,
                'business_date': charge['business_date'],
                'currency': charge['currency'],
            })

    for settlement in settlements:
        _check_date(settlement['business_date'], f"结算行 {settlement['line']} 的业务日期")
        amount = _decimal(settlement['amount'], f"结算行 {settlement['line']} 的金额")
        channel = resolve(settlement['channel'], settlement['business_date'])
        if channel is None:
            raise ReconcileError(f"结算行 {settlement['line']} 的渠道 {settlement['channel']} 在映射版本 {params['mapping_version']} 下缺失，拒绝猜测")
        settlement_lines.append({
            'line': settlement['line'],
            'package': settlement['package'],
            'source_channel': settlement['channel'],
            'channel': channel,
            'business_date': settlement['business_date'],
            'currency': settlement['currency'],
            'amount': str(amount),
        })

    receivable = defaultdict(Decimal)
    for row in allocations:
        receivable[(row['package'], row['channel'], row['business_date'], row['currency'])] += Decimal(row['allocated'])
    settled = defaultdict(Decimal)
    for row in settlement_lines:
        settled[(row['package'], row['channel'], row['business_date'], row['currency'])] += Decimal(row['amount'])

    results = []
    for package, channel, business_date, currency in sorted(set(receivable) | set(settled)):
        key = (package, channel, business_date, currency)
        expected = receivable.get(key)
        actual = settled.get(key)
        if expected is None or actual is None:
            # 缺侧数据无法判断；没有记录与金额确为零分开表达
            category = INDETERMINATE
            difference = None
        else:
            difference = actual - expected
            if difference == 0:
                category = MATCHED
            elif currency in tolerances:
                category = WITHIN_TOLERANCE if abs(difference) <= _decimal(tolerances[currency], '容差') else TRUE_DIFFERENCE
            else:
                category = INDETERMINATE
        results.append({
            'package': package,
            'channel': channel,
            'business_date': business_date,
            'currency': currency,
            'receivable': None if expected is None else str(expected),
            'settled': None if actual is None else str(actual),
            'difference': None if difference is None else str(difference),
            'category': category,
        })
    return results, allocations, settlement_lines


def _purge_run(connection, run_id):
    for table in RUN_TABLES:
        connection.execute(f'DELETE FROM {table} WHERE run_id=?', (run_id,))


def _insert_rows(connection, table, run_id, columns, rows):
    names = ', '.join(['run_id', *columns])
    placeholders = ', '.join('?' for _ in ('run_id', *columns))
    statement = f'INSERT INTO {table} ({names}) VALUES ({placeholders})'
    for row in rows:
        connection.execute(statement, [run_id, *[row[column] for column in columns]])


def _prepare_reuse(connection, reuse_from, params, fingerprint):
    old = connection.execute('SELECT status, params, fingerprint FROM recon_runs WHERE run_id=?', (reuse_from,)).fetchone()
    if old is None or old['status'] != 'completed':
        raise ValueError(f'增量复用需要一次已完成的运行，实际为 {reuse_from}')
    old_params = json.loads(old['params'])
    for key in ('charge_watermark', 'settlement_watermark', 'tolerances', 'precision'):
        if old_params.get(key) != params[key]:
            raise ValueError('映射修订增量复用要求两侧水位与币种规则一致')
    if json.loads(old['fingerprint'])['inputs'] != json.loads(fingerprint)['inputs']:
        raise ValueError('两侧数据已变化，映射修订不能增量复用，请全量重算')
    affected = affected_channels(connection, old_params['mapping_version'], params['mapping_version'])
    allocations = [dict(row) for row in connection.execute(
        f"SELECT {', '.join(ALLOCATION_COLUMNS)} FROM recon_allocations WHERE run_id=?", (reuse_from,))]
    settlements = [dict(row) for row in connection.execute(
        f"SELECT {', '.join(SETTLEMENT_COLUMNS)} FROM recon_settlements WHERE run_id=?", (reuse_from,))]
    return {'affected': affected, 'allocations': allocations, 'settlements': settlements}


def run_reconcile(connection, run_id, *, charge_watermark, settlement_watermark, mapping_version, tolerances, precision, reuse_from=None):
    """执行一次对账；同标识同输入幂等返回，失败只留 failed 记录，可修正后重跑。"""
    if not run_id:
        raise ValueError('需要运行标识')
    _check_date(charge_watermark, '收费水位')
    _check_date(settlement_watermark, '结算水位')
    params = {
        'charge_watermark': charge_watermark,
        'settlement_watermark': settlement_watermark,
        'mapping_version': mapping_version,
        'tolerances': {str(key): str(value) for key, value in sorted(tolerances.items())},
        'precision': {str(key): int(value) for key, value in sorted(precision.items())},
        'reuse_from': reuse_from,
    }
    inputs = _collect_inputs(connection, charge_watermark, settlement_watermark, mapping_version)
    fingerprint = json.dumps({
        'params': _sha256(params),
        'inputs': _sha256({'charges': inputs['charges'], 'settlements': inputs['settlements'], 'packages': inputs['packages']}),
        'rules': _sha256({'mappings': inputs['mappings'], 'allocation_rules': inputs['rules'], 'allocation_weights': inputs['weights']}),
    }, sort_keys=True)
    existing = connection.execute('SELECT status, fingerprint, summary FROM recon_runs WHERE run_id=?', (run_id,)).fetchone()
    if existing is not None and existing['status'] == 'completed':
        if existing['fingerprint'] == fingerprint:
            return json.loads(existing['summary'])
        raise ValueError(f'运行 {run_id} 已完成，参数或输入不一致，拒绝覆盖')
    try:
        reuse = _prepare_reuse(connection, reuse_from, params, fingerprint) if reuse_from is not None else None
        results, allocations, settlement_lines = _build_partitions(params, inputs, reuse)
        counts = {category: 0 for category in CATEGORIES}
        for result in results:
            counts[result['category']] += 1
        summary = {
            'run': run_id,
            'partitions': len(results),
            'categories': counts,
            'passed': counts[INDETERMINATE] == 0 and counts[TRUE_DIFFERENCE] == 0,
        }
        with transaction(connection):
            _purge_run(connection, run_id)
            connection.execute(
                'INSERT INTO recon_runs (run_id, fingerprint, params, status, summary) VALUES (?, ?, ?, ?, ?)',
                (run_id, fingerprint, json.dumps(params, ensure_ascii=False, sort_keys=True), 'completed', json.dumps(summary, ensure_ascii=False, sort_keys=True)))
            _insert_rows(connection, 'recon_results', run_id, RESULT_COLUMNS, results)
            _insert_rows(connection, 'recon_allocations', run_id, ALLOCATION_COLUMNS, allocations)
            _insert_rows(connection, 'recon_settlements', run_id, SETTLEMENT_COLUMNS, settlement_lines)
    except Exception as exc:
        with transaction(connection):
            _purge_run(connection, run_id)
            connection.execute(
                'INSERT INTO recon_runs (run_id, fingerprint, params, status, summary) VALUES (?, ?, ?, ?, ?)',
                (run_id, fingerprint, json.dumps(params, ensure_ascii=False, sort_keys=True), 'failed', json.dumps({'error': str(exc)}, ensure_ascii=False, sort_keys=True)))
        raise
    return summary


def explain(connection, run_id, *, package, channel, business_date, currency):
    """从分区差额钻回收费行、结算行、分摊步骤与规则。"""
    run = connection.execute('SELECT status, params FROM recon_runs WHERE run_id=?', (run_id,)).fetchone()
    if run is None:
        raise ValueError(f'运行不存在: {run_id}')
    if run['status'] != 'completed':
        raise ValueError(f'运行 {run_id} 未完成，不能钻取')
    params = json.loads(run['params'])
    result = connection.execute(
        'SELECT package, channel, business_date, currency, receivable, settled, difference, category FROM recon_results WHERE run_id=? AND package=? AND channel=? AND business_date=? AND currency=?',
        (run_id, package, channel, business_date, currency)).fetchone()
    if result is None:
        raise ValueError('该分区不在此次运行结果中')
    allocations = [dict(row) for row in connection.execute(
        f"SELECT {', '.join(ALLOCATION_COLUMNS)} FROM recon_allocations WHERE run_id=? AND package=? AND channel=? AND business_date=? AND currency=? ORDER BY charge_line",
        (run_id, package, channel, business_date, currency))]
    settlement_lines = [dict(row) for row in connection.execute(
        f"SELECT {', '.join(SETTLEMENT_COLUMNS)} FROM recon_settlements WHERE run_id=? AND package=? AND channel=? AND business_date=? AND currency=? ORDER BY line",
        (run_id, package, channel, business_date, currency))]
    charge_lines = sorted({row['charge_line'] for row in allocations})
    charges = []
    if charge_lines:
        marks = ', '.join('?' for _ in charge_lines)
        charges = [dict(row) for row in connection.execute(f'SELECT * FROM charges WHERE line IN ({marks}) ORDER BY line', charge_lines)]
    sources = sorted({row['source_channel'] for row in allocations} | {row['source_channel'] for row in settlement_lines})
    mappings = []
    if sources:
        marks = ', '.join('?' for _ in sources)
        mappings = [dict(row) for row in connection.execute(
            f'SELECT version, source_channel, target_channel, effective_from FROM channel_mappings WHERE version=? AND source_channel IN ({marks}) ORDER BY source_channel, effective_from',
            [params['mapping_version'], *sources])]
    order_ids = sorted({row['order_id'] for row in allocations})
    allocation_rules = []
    allocation_weights = []
    if order_ids:
        marks = ', '.join('?' for _ in order_ids)
        allocation_rules = [dict(row) for row in connection.execute(f'SELECT order_id, mode FROM allocation_rules WHERE order_id IN ({marks}) ORDER BY order_id', order_ids)]
        allocation_weights = [dict(row) for row in connection.execute(f'SELECT order_id, package, weight FROM allocation_weights WHERE order_id IN ({marks}) ORDER BY order_id, package', order_ids)]
    return {
        'run': run_id,
        'partition': {'package': package, 'channel': channel, 'business_date': business_date, 'currency': currency},
        'result': dict(result),
        'charges': charges,
        'settlements': settlement_lines,
        'allocations': allocations,
        'rules': {
            'mappings': mappings,
            'allocation_rules': allocation_rules,
            'allocation_weights': allocation_weights,
            'tolerance': params['tolerances'].get(currency),
            'precision': params['precision'].get(currency, DEFAULT_PRECISION),
        },
    }


def verify_run(connection, run_id):
    """自动校验逐行分配守恒与各类差额，而非仅比较总额。"""
    run = connection.execute('SELECT status, params, summary FROM recon_runs WHERE run_id=?', (run_id,)).fetchone()
    if run is None:
        raise ValueError(f'运行不存在: {run_id}')
    if run['status'] != 'completed':
        raise ValueError(f'运行 {run_id} 未完成，不能校验')
    params = json.loads(run['params'])
    summary = json.loads(run['summary'])
    results = [dict(row) for row in connection.execute(f"SELECT {', '.join(RESULT_COLUMNS)} FROM recon_results WHERE run_id=?", (run_id,))]
    allocations = [dict(row) for row in connection.execute(f"SELECT {', '.join(ALLOCATION_COLUMNS)} FROM recon_allocations WHERE run_id=?", (run_id,))]
    settlement_lines = [dict(row) for row in connection.execute(f"SELECT {', '.join(SETTLEMENT_COLUMNS)} FROM recon_settlements WHERE run_id=?", (run_id,))]

    checks = []

    by_line = defaultdict(Decimal)
    for row in allocations:
        by_line[row['charge_line']] += Decimal(row['allocated'])
    conservation = True
    for line, total in sorted(by_line.items()):
        charge = connection.execute('SELECT amount FROM charges WHERE line=?', (line,)).fetchone()
        if charge is None or total != Decimal(charge['amount']):
            conservation = False
    checks.append({'name': '逐行分配守恒', 'ok': conservation, 'lines': len(by_line)})

    receivable = defaultdict(Decimal)
    for row in allocations:
        receivable[(row['package'], row['channel'], row['business_date'], row['currency'])] += Decimal(row['allocated'])
    settled = defaultdict(Decimal)
    for row in settlement_lines:
        settled[(row['package'], row['channel'], row['business_date'], row['currency'])] += Decimal(row['amount'])

    amounts = True
    difference = True
    categories = True
    counts = {category: 0 for category in CATEGORIES}
    tolerances = params['tolerances']
    for row in results:
        key = (row['package'], row['channel'], row['business_date'], row['currency'])
        expected = receivable.get(key)
        actual = settled.get(key)
        if (expected is None) != (row['receivable'] is None) or (expected is not None and str(expected) != row['receivable']):
            amounts = False
        if (actual is None) != (row['settled'] is None) or (actual is not None and str(actual) != row['settled']):
            amounts = False
        if expected is None or actual is None:
            if row['difference'] is not None:
                difference = False
            expected_category = INDETERMINATE
        else:
            gap = actual - expected
            if row['difference'] is None or row['difference'] != str(gap):
                difference = False
            if gap == 0:
                expected_category = MATCHED
            elif row['currency'] in tolerances:
                expected_category = WITHIN_TOLERANCE if abs(gap) <= Decimal(tolerances[row['currency']]) else TRUE_DIFFERENCE
            else:
                expected_category = INDETERMINATE
        if row['category'] != expected_category:
            categories = False
        counts[row['category']] += 1
    checks.append({'name': '分区应收实结合计', 'ok': amounts, 'partitions': len(results)})
    checks.append({'name': '差额等于实结减应收', 'ok': difference})
    checks.append({'name': '分类与容差边界', 'ok': categories})

    passed = counts[INDETERMINATE] == 0 and counts[TRUE_DIFFERENCE] == 0
    consistent = summary.get('categories') == counts and summary.get('partitions') == len(results) and summary.get('passed') == passed
    checks.append({'name': '汇总与通过判定', 'ok': consistent})
    return {'run': run_id, 'ok': all(check['ok'] for check in checks), 'checks': checks}
