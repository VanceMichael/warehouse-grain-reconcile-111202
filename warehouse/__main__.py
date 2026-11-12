import argparse
import json
from pathlib import Path

from .catalog import connect, load_file
from .domain import overview
from .reconcile import explain, run_reconcile, verify_run
from .store import ObjectStore


def main():
    parser = argparse.ArgumentParser(description='本地数据集合装载、查询与仓库对账')
    parser.add_argument('command', choices=['import', 'inspect', 'export', 'reconcile', 'results', 'explain', 'verify', 'runs'])
    parser.add_argument('--db', required=True)
    parser.add_argument('--input')
    parser.add_argument('--batch')
    parser.add_argument('--objects')
    parser.add_argument('--run')
    parser.add_argument('--charge-watermark')
    parser.add_argument('--settlement-watermark')
    parser.add_argument('--mapping-version')
    parser.add_argument('--rules')
    parser.add_argument('--reuse-from')
    parser.add_argument('--package')
    parser.add_argument('--channel')
    parser.add_argument('--date')
    parser.add_argument('--currency')
    args = parser.parse_args()

    def require(*names):
        missing = ['--' + name for name in names if getattr(args, name.replace('-', '_')) is None]
        if missing:
            parser.error(f"{args.command} 需要参数: {', '.join(missing)}")

    if args.command == 'import':
        require('input', 'batch')
    if args.command == 'export':
        require('objects')
    if args.command == 'reconcile':
        require('run', 'charge-watermark', 'settlement-watermark', 'mapping-version', 'rules')
    if args.command in ('results', 'verify'):
        require('run')
    if args.command == 'explain':
        require('run', 'package', 'channel', 'date', 'currency')

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    connection = connect(args.db)
    try:
        if args.command == 'import':
            result = {'changed': load_file(connection, args.input, args.batch)}
        elif args.command == 'inspect':
            result = overview(connection)
        elif args.command == 'export':
            records = overview(connection)
            result = {'object': ObjectStore(args.objects).put(records), 'rows': len(records)}
        elif args.command == 'reconcile':
            rules = json.loads(Path(args.rules).read_text(encoding='utf-8'))
            result = run_reconcile(
                connection, args.run,
                charge_watermark=args.charge_watermark,
                settlement_watermark=args.settlement_watermark,
                mapping_version=args.mapping_version,
                tolerances=rules.get('tolerances', {}),
                precision=rules.get('precision', {}),
                reuse_from=args.reuse_from)
        elif args.command == 'results':
            result = [dict(row) for row in connection.execute(
                'SELECT package, channel, business_date, currency, receivable, settled, difference, category FROM recon_results WHERE run_id=? ORDER BY package, channel, business_date, currency',
                (args.run,))]
        elif args.command == 'explain':
            result = explain(connection, args.run, package=args.package, channel=args.channel, business_date=args.date, currency=args.currency)
        elif args.command == 'verify':
            result = verify_run(connection, args.run)
        else:
            result = [
                {'run_id': row['run_id'], 'status': row['status'], 'summary': json.loads(row['summary'])}
                for row in connection.execute('SELECT run_id, status, summary FROM recon_runs ORDER BY run_id')]
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    finally:
        connection.close()


if __name__ == '__main__':
    main()
