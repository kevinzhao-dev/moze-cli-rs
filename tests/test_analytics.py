"""Behavioral contracts for local, source-aware financial analysis."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from analytics_fixture import SEMANTICS, build_snapshot, record, ref

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'converter'))
import analytics

PERIOD = {'from': '2026-01-01', 'to': '2026-01-31'}


class Analytics(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='moze-analytics-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'synthetic.sqlite3'

    def build(self, rows=None):
        build_snapshot(self.db, rows)

    def query(self, tool='summarize_spending', args=None, semantics=SEMANTICS):
        return analytics.dispatch(self.db, tool, args if args is not None else {'period': PERIOD}, semantics=semantics)

    def total(self, result, currency='USD'):
        return next(item for item in result['totals'] if item['currency'] == currency)

    def test_electricity_one_call_resolves_category_and_sums_all_rows(self):
        self.build([record(f'cost-{i:04d}', 0.1) for i in range(501)] + [
            record('other', 500, name='Synthetic lunch', store='',
                   classification=ref('AHClassification', 'lunch'))])
        before = self.db.read_bytes()
        data = self.query(args={'period': PERIOD, 'filters': {'query': '電費'}, 'group_by': ['month']})
        self.assertEqual(self.total(data)['net_expense'], '50.10')
        self.assertEqual(self.total(data)['count'], 501)
        self.assertEqual(len(data['groups']), 1)
        self.assertTrue(data['coverage']['snapshot_scan_complete'])
        self.assertEqual(before, self.db.read_bytes())
        self.assertNotIn('SYNTHETIC_SECRET', json.dumps(data))

    def test_unknown_semantics_never_claim_expense(self):
        self.build([record('positive', 15), record('negative', -2)])
        data = self.query(semantics=None)
        total = self.total(data)
        self.assertEqual(total['source_total'], '13.00')
        self.assertIsNone(total['net_expense'])
        self.assertEqual(total['unknown_count'], 2)
        self.assertTrue(data['warnings'])

    def test_refunds_transfers_disabled_events_and_future(self):
        self.build([
            record('expense', 100), record('refund', -20, isRefund=True),
            record('transfer-out', 800, transferID='transfer'),
            record('transfer-in', 800, transferID='transfer', isTransferIn=True),
            record('disabled', 500, isEnabled=False), record('event', 700, isEvent=True),
            record('deleted', 600, isDeleted=True),
            record('future', 900, dateString='2026.04.01-12:00:00'),
        ])
        data = self.query(args={'period': {'from': '2026-01-01', 'to': '2026-12-31'}})
        total = self.total(data)
        self.assertEqual(total['gross_expense'], '100.00')
        self.assertEqual(total['refund'], '20.00')
        self.assertEqual(total['net_expense'], '80.00')
        self.assertEqual(total['count'], 2)
        self.assertEqual(sum(data['excluded_counts'].values()), 6)

    def test_currencies_remain_separate_and_round_per_currency(self):
        self.build([record('usd', 1.005), record('jpy', 100.5, currency=ref('AHCurrency', 'JPY'))])
        data = self.query()
        self.assertEqual(self.total(data)['net_expense'], '1.01')
        self.assertEqual(self.total(data, 'JPY')['net_expense'], '101')

    def test_mixed_known_unknown_does_not_hide_uncertainty(self):
        self.build([record('known', 10), record('unknown', 20, type=999)])
        total = self.total(self.query())
        self.assertIsNone(total['net_expense'])
        self.assertEqual(total['unknown_count'], 1)
        self.assertEqual(total['source_total'], '30.00')

    def test_aggregate_limit_is_not_transaction_limit(self):
        self.build([record('jan', 1), record('feb', 2, dateString='2026.02.01'),
                    record('mar', 3, dateString='2026.03.01')])
        data = self.query(args={'period': {'from': '2026-01-01', 'to': '2026-03-31'},
                                'group_by': ['month'], 'limit': 1})
        self.assertEqual(self.total(data)['net_expense'], '6.00')
        self.assertEqual(data['group_count'], 3)
        self.assertTrue(data['groups_truncated'])
        self.assertEqual(len(data['groups']), 1)

    def test_drilldown_and_snapshot_and_policy_consistency(self):
        self.build([record('a', 1), record('b', 2)])
        data = self.query(args={'period': PERIOD, 'group_by': ['subcategory']})
        token = data['groups'][0]['drilldown_token']
        detail = self.query('search_transactions', {'drilldown_token': token})
        self.assertEqual(len(detail['rows']), 2)
        changed = dict(SEMANTICS, expense_types=[70])
        with self.assertRaises(analytics.AnalyticsError):
            self.query('search_transactions', {'drilldown_token': token}, changed)
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE metadata SET value=? WHERE key='sha256'", (json.dumps('new-snapshot'),))
        with self.assertRaises(analytics.AnalyticsError):
            self.query('search_transactions', {'drilldown_token': token})

    def test_invalid_arguments_not_empty_results(self):
        self.build()
        cases = [
            {'period': {'from': '2026-02-30', 'to': '2026-03-01'}},
            {'period': {'from': '2026-03-01', 'to': '2026-01-01'}},
            {'period': PERIOD, 'limit': 0}, {'period': PERIOD, 'limit': 101},
            {'period': PERIOD, 'filters': {'sql': 'SELECT 1'}},
            {'period': PERIOD, 'group_by': ['password']},
            {'period': PERIOD, 'currency_mode': 'guess'},
            {'period': PERIOD, 'snapshot_id': 'wrong'},
        ]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(analytics.AnalyticsError):
                self.query(args=args)

    def test_profile_validation(self):
        self.build()
        for change in [dict(income_types=[71]), dict(amount_convention='guess'), dict(evidence='')]:
            with self.subTest(change=change), self.assertRaises(analytics.AnalyticsError):
                self.query(semantics=dict(SEMANTICS, **change))

    def test_cli_end_to_end_and_error_codes(self):
        self.build()
        profile = self.root / 'semantics.json'
        profile.write_text(json.dumps(SEMANTICS))
        command = [str(ROOT/'target/debug/moze-rs'), '--db', str(self.db),
                   'query', 'summarize_spending', '--args', json.dumps({'period': PERIOD}),
                   '--semantics', str(profile)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.total(json.loads(result.stdout)['data'])['net_expense'], '10.00')
        result = subprocess.run(command[:4] + ['no_such_tool'], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)['ok'])
        self.assertEqual(result.returncode, 2)

    def test_missing_database_stays_missing(self):
        with self.assertRaises(analytics.AnalyticsError):
            self.query()
        self.assertFalse(self.db.exists())

    def test_pagination_preserves_filter_and_never_repeats_records(self):
        self.build([record(f'cost-{i:03d}', i) for i in range(43)] + [
            record('unrelated', 999, name='Other', store='', desc='',
                   classification=ref('AHClassification', 'lunch'))])
        args = {'period': PERIOD, 'filters': {'query': '電費'}, 'limit': 7}
        identifiers = []
        while True:
            result = self.query('search_transactions', args)
            identifiers.extend(row['id'] for row in result['rows'])
            if result['next_cursor'] is None:
                break
            args = {'cursor': result['next_cursor'], 'limit': 7}
        self.assertEqual(len(identifiers), 43)
        self.assertEqual(len(set(identifiers)), 43)
        self.assertNotIn('unrelated', identifiers)

    def test_schema_can_request_only_one_allowlisted_collection(self):
        self.build()
        base = [str(ROOT/'target/debug/moze-rs'), '--db', str(self.db), 'schema']
        result = subprocess.run(base + ['AHRecord'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual([r['name'] for r in json.loads(result.stdout)['data']['collections']], ['AHRecord'])
        result = subprocess.run(base + ['AHAppConfig'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('SYNTHETIC_SECRET', result.stdout)

    def test_comparison_includes_groups_only_present_in_one_period(self):
        self.build([
            record('jan-electricity', 40),
            record('feb-electricity', 60, dateString='2026.02.10'),
            record('jan-lunch', 10, classification=ref('AHClassification', 'lunch')),
        ])
        result = self.query('compare_spending', {
            'period': {'from': '2026-02-01', 'to': '2026-02-28'},
            'baseline_period': PERIOD, 'group_by': ['subcategory'],
        })
        self.assertEqual(result['differences'][0]['net_expense_difference'], '10.00')
        self.assertEqual(result['differences'][0]['percent_change'], '20.00')
        changes = {group['dimensions']['subcategory']['id']: group
                   for group in result['contributions']}
        self.assertEqual(changes['electricity']['differences'][0]['net_expense_difference'], '20.00')
        self.assertEqual(changes['lunch']['differences'][0]['net_expense_difference'], '-10.00')
        self.assertIsNone(changes['lunch']['current_drilldown_token'])
        detail = self.query('search_transactions', {
            'drilldown_token': changes['lunch']['baseline_drilldown_token']})
        self.assertEqual([row['id'] for row in detail['rows']], ['jan-lunch'])

    def test_unknown_comparison_exposes_only_source_change(self):
        self.build([record('jan', 10), record('feb', 20, dateString='2026.02.10')])
        data = self.query('compare_spending', {
            'period': {'from': '2026-02-01', 'to': '2026-02-28'},
            'baseline_period': PERIOD}, semantics=None)
        self.assertIsNone(data['differences'][0]['net_expense_difference'])
        self.assertEqual(data['differences'][0]['source_total_difference'], '10.00')

    def test_invalid_amount_and_missing_scale_are_not_zero_or_valid_expenses(self):
        self.build([record('invalid', None), record('missing-scale', 20,
                    currency=ref('AHCurrency', 'NO_SCALE'))])
        result = self.query()
        self.assertIsNone(self.total(result)['source_total'])
        self.assertEqual(self.total(result)['invalid_amount_count'], 1)
        self.assertIsNone(self.total(result, 'NO_SCALE')['net_expense'])
        self.assertFalse(self.total(result, 'NO_SCALE')['currency_scale_verified'])

    def test_scan_limit_is_an_error_not_a_partial_total(self):
        self.build([record('one'), record('two'), record('three')])
        with patch.object(analytics, 'MAX_SCAN', 2):
            with self.assertRaises(analytics.AnalyticsError) as error:
                self.query()
        self.assertEqual(error.exception.code, 'SCAN_LIMIT')

    def test_malformed_arrays_are_explicit_argument_errors(self):
        self.build()
        for extra in [{'group_by': [{}]}, {'metrics': [[]]}, {'filters': []},
                      {'period': None}, {'period': {}}, {'group_by': 'category'}]:
            with self.subTest(extra=extra), self.assertRaises(analytics.AnalyticsError):
                self.query(args={'period': PERIOD, **extra})

    def test_entity_resolution_and_get_transaction_evidence(self):
        self.build([record('out', transferID='same'),
                    record('in', transferID='same', isTransferIn=True), record('expense')])
        result = self.query('resolve_entities', {'query': '電費', 'kinds': ['subcategory']})
        self.assertEqual(result['candidates'][0]['id'], 'electricity')
        self.assertEqual(result['candidates'][0]['category']['id'], 'utilities')
        result = self.query('get_transaction', {'id': 'out'})
        self.assertEqual(result['transaction']['kind'], 'excluded')
        self.assertEqual(result['related_count'], 1)
        self.assertEqual(result['related_transactions'][0]['transaction']['id'], 'in')
        result = self.query('get_transaction', {'id': 'expense'})
        self.assertEqual(result['transaction']['kind'], 'expense')

    def test_read_context_and_semantics_changes_do_not_modify_snapshot(self):
        self.build()
        before = self.db.read_bytes()
        context = self.query('get_context', {})
        self.assertEqual(context['observed_eligible_date_range'], {'from': '2026-01-15', 'to': '2026-01-15'})
        self.assertEqual(context['snapshot_id'], 'synthetic-snapshot-1')
        self.query(semantics=dict(SEMANTICS, expense_types=[91]))
        self.assertEqual(self.db.read_bytes(), before)

    def test_ranking_before_limit_and_omitted_totals(self):
        self.build([record('small', 1), record('large', 90,
                   classification=ref('AHClassification', 'lunch'))])
        result = self.query(args={'period': PERIOD, 'group_by': ['subcategory'],
                                  'sort_by': 'net_expense', 'limit': 1})
        self.assertEqual(result['groups'][0]['dimensions']['subcategory']['id'], 'lunch')
        self.assertEqual(result['remaining_totals'][0]['net_expense'], '1.00')
        self.assertEqual(self.total(result)['net_expense'], '91.00')

    def test_comparison_ranks_absolute_change_before_limit(self):
        self.build([record('jan', 100, classification=ref('AHClassification', 'lunch')),
                    record('feb', 5, dateString='2026.02.10')])
        data = self.query('compare_spending', {
            'period': {'from': '2026-02-01', 'to': '2026-02-28'}, 'baseline_period': PERIOD,
            'group_by': ['subcategory'], 'sort_by': 'net_expense_difference', 'limit': 1})
        self.assertEqual(data['contributions'][0]['dimensions']['subcategory']['id'], 'lunch')
        self.assertEqual(data['contributions'][0]['differences'][0]['net_expense_difference'], '-100.00')
        self.assertEqual(data['contribution_count'], 2)

    def test_monetary_sort_refuses_mixed_currencies(self):
        self.build([record('usd', 10), record('jpy', 1000, currency=ref('AHCurrency', 'JPY'))])
        for tool, ordering in [('summarize_spending', 'source_total'), ('search_transactions', 'amount_desc')]:
            with self.subTest(tool=tool), self.assertRaises(analytics.AnalyticsError):
                self.query(tool, {'period': PERIOD, 'sort_by': ordering})
        result = self.query(args={'period': PERIOD, 'sort_by': 'source_total',
                                  'filters': {'currency_codes': ['USD']}})
        self.assertEqual(self.total(result)['source_total'], '10.00')

    def test_amount_sort_survives_cursor(self):
        self.build([record('a', 1), record('b', -50), record('c', 7)])
        first = self.query('search_transactions', {'period': PERIOD, 'sort_by': 'amount_desc', 'limit': 1})
        self.assertEqual(first['rows'][0]['id'], 'b')
        second = self.query('search_transactions', {'cursor': first['next_cursor'], 'limit': 1})
        self.assertEqual(second['rows'][0]['id'], 'c')
        self.assertEqual(second['sort_by'], 'amount_desc')

    def test_large_valid_filter_tokens_roundtrip(self):
        self.build()
        args = {'period': PERIOD, 'filters': {'account_ids': ['checking'] + ['x'*200+str(i) for i in range(99)]}}
        summary = self.query(args=args)
        token = summary['groups'][0]['drilldown_token']
        self.assertGreater(len(token), 20000)
        result = self.query('search_transactions', {'drilldown_token': token})
        self.assertEqual(result['rows'][0]['id'], 'expense')

    def test_cli_stdin_supports_large_tokens_without_shell_argument_limits(self):
        self.build()
        args = {'period': PERIOD, 'filters': {'account_ids': ['checking'] + ['x'*500+str(i) for i in range(99)]}}
        summary = self.query(args=args, semantics=None)
        token = summary['groups'][0]['drilldown_token']
        self.assertGreater(len(token), 65536)
        result = subprocess.run([str(ROOT/'target/debug/moze-rs'), '--db', str(self.db),
                                 'query', 'search_transactions', '--args', '-'],
                                input=json.dumps({'drilldown_token': token}),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout)['data']['rows'][0]['id'], 'expense')


if __name__ == '__main__':
    unittest.main()
