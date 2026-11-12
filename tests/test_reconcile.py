import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from warehouse.catalog import connect, load_bundle, load_file
from warehouse.reconcile import (
    INDETERMINATE,
    MATCHED,
    TRUE_DIFFERENCE,
    WITHIN_TOLERANCE,
    ReconcileError,
    affected_channels,
    explain,
    run_reconcile,
    verify_run,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / 'examples'
SOURCE = EXAMPLES / 'source.json'
EXPECTED = json.loads((EXAMPLES / 'expected_reconcile.json').read_text(encoding='utf-8'))

PARAMS = {
    'charge_watermark': '2026-11-04',
    'settlement_watermark': '2026-11-04',
    'mapping_version': 'v1',
    'tolerances': {'CNY': '0.05', 'USD': '0.05'},
    'precision': {'CNY': 2, 'USD': 2},
}


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.connection = connect(Path(self.temporary.name) / 'warehouse.sqlite3')
        load_file(self.connection, SOURCE, 'main')

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def run_v1(self, run_id='run-v1', **overrides):
        params = dict(PARAMS)
        params.update(overrides)
        return run_reconcile(self.connection, run_id, **params)

    def results(self, run_id):
        return [dict(row) for row in self.connection.execute(
            'SELECT package, channel, business_date, currency, receivable, settled, difference, category FROM recon_results WHERE run_id=? ORDER BY package, channel, business_date, currency',
            (run_id,))]

    def partition(self, run_id, package):
        rows = [row for row in self.results(run_id) if row['package'] == package]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def allocations(self, run_id):
        return [dict(row) for row in self.connection.execute(
            'SELECT * FROM recon_allocations WHERE run_id=? ORDER BY charge_line, package', (run_id,))]

    def status(self, run_id):
        row = self.connection.execute('SELECT status FROM recon_runs WHERE run_id=?', (run_id,)).fetchone()
        return None if row is None else row['status']

    def test_summary_and_partitions_match_expected(self):
        summary = self.run_v1()
        self.assertEqual(summary, EXPECTED['summary'])
        self.assertEqual(self.results('run-v1'), EXPECTED['partitions'])

    def test_split_order_conservation_and_residual_owner(self):
        self.run_v1()
        allocations = self.allocations('run-v1')
        by_line = {}
        for row in allocations:
            by_line.setdefault(row['charge_line'], {})[row['package']] = row
        # 拆单 o1：10.01 按权重 1:1 量化，尾差 -0.01 稳定落到包裹号最小的 p1
        self.assertEqual(by_line['c1']['p1']['allocated'], '5.00')
        self.assertEqual(by_line['c1']['p1']['residual'], 1)
        self.assertEqual(by_line['c1']['p2']['allocated'], '5.01')
        self.assertEqual(by_line['c1']['p2']['residual'], 0)
        # 拆单 o2：20.00 按数量权重拆三包，尾差落到 p3
        self.assertEqual([by_line['c2'][p]['allocated'] for p in ('p3', 'p4', 'p5')], ['6.66', '6.67', '6.67'])
        self.assertEqual(by_line['c2']['p3']['residual'], 1)
        self.assertEqual(by_line['c2']['p3']['weight'], '1')
        # 逐行守恒：各包裹分摊合计恒等于收费行金额
        for line, amount in (('c1', '10.01'), ('c2', '20.00'), ('c3', '30.00'), ('c6', '100.00')):
            total = sum(Decimal(row['allocated']) for row in allocations if row['charge_line'] == line)
            self.assertEqual(total, Decimal(amount))

    def test_multi_currency_partitions_not_mixed(self):
        self.run_v1()
        results = self.results('run-v1')
        self.assertEqual({row['currency'] for row in results}, {'CNY', 'USD'})
        usd = [row for row in results if row['currency'] == 'USD']
        self.assertEqual(len(usd), 1)
        self.assertEqual(usd[0]['category'], MATCHED)
        self.assertEqual(usd[0]['receivable'], '30.00')

    def test_duplicate_settlement_lines_accumulate(self):
        self.run_v1()
        p4 = self.partition('run-v1', 'p4')
        self.assertEqual(p4['settled'], '6.67')
        self.assertEqual(p4['category'], MATCHED)
        lines = self.connection.execute(
            "SELECT line FROM recon_settlements WHERE run_id='run-v1' AND package='p4' ORDER BY line").fetchall()
        self.assertEqual([row['line'] for row in lines], ['s4', 's5'])

    def test_settlement_redelivery_not_double_counted(self):
        first = self.run_v1()
        load_file(self.connection, EXAMPLES / 'redelivery.json', 'redelivery')
        count = self.connection.execute('SELECT COUNT(*) FROM settlements').fetchone()[0]
        self.assertEqual(count, 10)
        self.assertEqual(self.run_v1(), first)
        self.assertEqual(self.partition('run-v1', 'p4')['settled'], '6.67')

    def test_conflicting_settlement_line_blocks_release(self):
        with self.assertRaises(ValueError):
            load_file(self.connection, EXAMPLES / 'conflict.json', 'conflict')
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM ingest_batches WHERE batch='conflict'").fetchone()[0], 0)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM settlements').fetchone()[0], 10)
        self.assertEqual(self.connection.execute("SELECT amount FROM settlements WHERE line='s4'").fetchone()['amount'], '4.00')

    def test_zero_amount_differs_from_missing_record(self):
        self.run_v1()
        p8 = self.partition('run-v1', 'p8')
        self.assertEqual(p8['settled'], '0.00')
        self.assertEqual(p8['difference'], '-9.99')
        self.assertEqual(p8['category'], TRUE_DIFFERENCE)
        p7 = self.partition('run-v1', 'p7')
        self.assertIsNone(p7['settled'])
        self.assertIsNone(p7['difference'])
        self.assertEqual(p7['category'], INDETERMINATE)

    def test_tolerance_boundary_includes_equality(self):
        self.run_v1()
        p2 = self.partition('run-v1', 'p2')
        self.assertEqual(p2['difference'], '0.05')
        self.assertEqual(p2['category'], WITHIN_TOLERANCE)

    def test_missing_tolerance_is_indeterminate(self):
        self.run_v1('run-no-tol', tolerances={})
        self.assertEqual(self.partition('run-no-tol', 'p2')['category'], INDETERMINATE)
        self.assertEqual(self.partition('run-no-tol', 'p9')['category'], INDETERMINATE)

    def test_indeterminate_blocks_pass(self):
        summary = self.run_v1()
        self.assertFalse(summary['passed'])
        self.assertEqual(summary['categories'][INDETERMINATE], 3)
        self.assertEqual(summary['categories'][TRUE_DIFFERENCE], 2)

    def test_watermark_excludes_later_rows(self):
        self.run_v1()
        self.assertNotIn('p11', {row['package'] for row in self.results('run-v1')})
        self.assertNotIn('c7', {row['charge_line'] for row in self.allocations('run-v1')})
        later = self.run_v1('run-later', charge_watermark='2026-11-05', settlement_watermark='2026-11-05')
        self.assertEqual(later['partitions'], 11)
        self.assertEqual(self.partition('run-later', 'p11')['category'], MATCHED)

    def test_missing_mapping_fails_run_and_leaves_no_complete_batch(self):
        load_file(self.connection, EXAMPLES / 'broken_source.json', 'broken')
        with self.assertRaises(ReconcileError):
            self.run_v1('run-broken')
        self.assertEqual(self.status('run-broken'), 'failed')
        self.assertEqual(self.results('run-broken'), [])
        self.assertEqual(self.allocations('run-broken'), [])
        load_bundle(self.connection, {'channel_mappings': [
            {'version': 'v1', 'source_channel': 'mystery', 'target_channel': 'mystery-x', 'effective_from': '2026-01-01'}]}, 'fix')
        summary = self.run_v1('run-broken')
        self.assertEqual(self.status('run-broken'), 'completed')
        self.assertEqual(summary['partitions'], 11)

    def test_insufficient_weight_fails(self):
        load_bundle(self.connection, {
            'charges': [{'line': 'cb2', 'order_id': 'ob2', 'channel': 'old-truck', 'business_date': '2026-11-01', 'currency': 'CNY', 'amount': '3.00'}],
            'packages': [{'package': 'pb2', 'order_id': 'ob2', 'units': 1}, {'package': 'pb3', 'order_id': 'ob2', 'units': 1}],
            'allocation_rules': [{'order_id': 'ob2', 'mode': 'explicit'}],
            'allocation_weights': [{'order_id': 'ob2', 'package': 'pb2', 'weight': '1'}],
        }, 'short-weight')
        with self.assertRaises(ReconcileError):
            self.run_v1('run-short-weight')
        self.assertEqual(self.status('run-short-weight'), 'failed')

    def test_zero_total_weight_fails(self):
        load_bundle(self.connection, {
            'charges': [{'line': 'cb3', 'order_id': 'ob3', 'channel': 'old-truck', 'business_date': '2026-11-01', 'currency': 'CNY', 'amount': '3.00'}],
            'packages': [{'package': 'pb4', 'order_id': 'ob3', 'units': 1}],
            'allocation_rules': [{'order_id': 'ob3', 'mode': 'explicit'}],
            'allocation_weights': [{'order_id': 'ob3', 'package': 'pb4', 'weight': '0'}],
        }, 'zero-weight')
        with self.assertRaises(ReconcileError):
            self.run_v1('run-zero-weight')
        self.assertEqual(self.status('run-zero-weight'), 'failed')

    def test_missing_allocation_rule_fails(self):
        load_bundle(self.connection, {
            'charges': [{'line': 'cb4', 'order_id': 'ob4', 'channel': 'old-truck', 'business_date': '2026-11-01', 'currency': 'CNY', 'amount': '3.00'}],
            'packages': [{'package': 'pb5', 'order_id': 'ob4', 'units': 1}],
        }, 'no-rule')
        with self.assertRaises(ReconcileError):
            self.run_v1('run-no-rule')
        self.assertEqual(self.status('run-no-rule'), 'failed')

    def test_duplicate_mapping_blocked(self):
        with self.assertRaises(sqlite3.IntegrityError):
            load_bundle(self.connection, {'channel_mappings': [
                {'version': 'v1', 'source_channel': 'old-express', 'target_channel': 'other', 'effective_from': '2026-01-01'}]}, 'dup-mapping')
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM ingest_batches WHERE batch='dup-mapping'").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM channel_mappings WHERE version='v1'").fetchone()[0], 3)

    def test_reentry_same_input_is_idempotent(self):
        first = self.run_v1()
        second = self.run_v1()
        self.assertEqual(first, second)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM recon_runs').fetchone()[0], 1)
        self.assertEqual(len(self.results('run-v1')), 10)

    def test_reentry_conflicting_input_rejected(self):
        self.run_v1()
        with self.assertRaises(ValueError):
            self.run_v1(charge_watermark='2026-11-03')
        self.assertEqual(self.status('run-v1'), 'completed')
        self.assertEqual(len(self.results('run-v1')), 10)

    def test_deterministic_rerun_same_difference_and_residual(self):
        self.run_v1('run-a')
        self.run_v1('run-b')
        self.assertEqual(self.results('run-a'), self.results('run-b'))
        residual_a = [(row['charge_line'], row['package']) for row in self.allocations('run-a') if row['residual']]
        residual_b = [(row['charge_line'], row['package']) for row in self.allocations('run-b') if row['residual']]
        self.assertEqual(residual_a, residual_b)
        self.assertEqual(residual_a, [('c1', 'p1'), ('c2', 'p3')])

    def test_mapping_revision_recomputes_only_affected(self):
        self.run_v1()
        load_file(self.connection, EXAMPLES / 'mapping_v2.json', 'mapping-v2')
        self.assertEqual(affected_channels(self.connection, 'v1', 'v2'), {'legacy-express', 'old-express'})
        self.run_v1('run-v2', mapping_version='v2', reuse_from='run-v1')
        self.run_v1('run-v2-full', mapping_version='v2')
        self.assertEqual(self.results('run-v2'), self.results('run-v2-full'))
        v1_truck = [row for row in self.results('run-v1') if row['channel'] == 'truck']
        v2_truck = [row for row in self.results('run-v2') if row['channel'] == 'truck']
        self.assertEqual(v1_truck, v2_truck)
        channels = {row['channel'] for row in self.results('run-v2')}
        self.assertIn('express-main', channels)
        self.assertNotIn('express', channels)
        keep_v1 = [row for row in self.allocations('run-v1') if row['source_channel'] == 'old-truck']
        keep_v2 = [row for row in self.allocations('run-v2') if row['source_channel'] == 'old-truck']
        strip = lambda rows: [{key: value for key, value in row.items() if key != 'run_id'} for row in rows]
        self.assertEqual(strip(keep_v1), strip(keep_v2))
        self.assertTrue(verify_run(self.connection, 'run-v2')['ok'])

    def test_reuse_requires_same_watermarks_and_inputs(self):
        self.run_v1()
        load_file(self.connection, EXAMPLES / 'mapping_v2.json', 'mapping-v2')
        with self.assertRaises(ValueError):
            self.run_v1('run-v2', mapping_version='v2', reuse_from='run-v1', charge_watermark='2026-11-03')
        load_bundle(self.connection, {'settlements': [
            {'line': 's11', 'package': 'p1', 'channel': 'old-express', 'business_date': '2026-11-01', 'currency': 'CNY', 'amount': '0.01'}]}, 'late-settlement')
        with self.assertRaises(ValueError):
            self.run_v1('run-v2-late', mapping_version='v2', reuse_from='run-v1')

    def test_verify_passes_and_detects_tampering(self):
        self.run_v1()
        verified = verify_run(self.connection, 'run-v1')
        self.assertTrue(verified['ok'])
        self.assertEqual([check['ok'] for check in verified['checks']], [True] * len(verified['checks']))
        self.connection.execute("UPDATE recon_allocations SET allocated='9.99' WHERE run_id='run-v1' AND charge_line='c1' AND package='p1'")
        verified = verify_run(self.connection, 'run-v1')
        self.assertFalse(verified['ok'])
        self.assertFalse(verified['checks'][0]['ok'])

    def test_explain_drills_back_to_lines_steps_and_rules(self):
        self.run_v1()
        detail = explain(self.connection, 'run-v1', package='p2', channel='express', business_date='2026-11-01', currency='CNY')
        self.assertEqual([row['line'] for row in detail['charges']], ['c1'])
        self.assertEqual([row['line'] for row in detail['settlements']], ['s2'])
        self.assertEqual(len(detail['allocations']), 1)
        step = detail['allocations'][0]
        self.assertEqual(step['allocated'], '5.01')
        self.assertEqual(step['weight'], '1')
        self.assertEqual(step['residual'], 0)
        self.assertEqual(detail['rules']['mappings'][0]['target_channel'], 'express')
        self.assertEqual(detail['rules']['allocation_rules'][0]['mode'], 'explicit')
        self.assertEqual(detail['rules']['tolerance'], '0.05')
        self.assertEqual(detail['result']['category'], WITHIN_TOLERANCE)

    def test_explain_rejects_unknown_run_or_partition(self):
        self.run_v1()
        with self.assertRaises(ValueError):
            explain(self.connection, 'missing', package='p1', channel='express', business_date='2026-11-01', currency='CNY')
        with self.assertRaises(ValueError):
            explain(self.connection, 'run-v1', package='nope', channel='express', business_date='2026-11-01', currency='CNY')


if __name__ == '__main__':
    unittest.main()
