"""Regression cases confirmed by the pre-merge review; synthetic data only."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from analytics_fixture import SEMANTICS, build_snapshot, record

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'converter'))
import analytics

JAN = {'from': '2026-01-01', 'to': '2026-01-31'}
FEB = {'from': '2026-02-01', 'to': '2026-02-28'}


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='moze-review-fix-')
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name)/'synthetic.sqlite3'

    def query(self, tool, args):
        return analytics.dispatch(self.db, tool, args, SEMANTICS)

    def test_token_work_scales_with_returned_groups_not_matching_groups(self):
        rows = [record(f'{month}-{i}', i + 1, store=f'{i:03d}', dateString=f'2026.{month}.15')
                for month in ('01', '02') for i in range(500)]
        build_snapshot(self.db, rows)
        filters = {'account_ids': ['checking'] + ['x'*500+str(i) for i in range(99)]}
        original = analytics.token
        token_sizes = []

        def measure_token(value):
            encoded = original(value)
            token_sizes.append(len(encoded))
            return encoded

        # Count actual serialized token bytes without allocating the old 33MB+
        # intermediate result. Both endpoints must scale with their output limit.
        with patch.object(analytics, 'token', side_effect=measure_token):
            summary = self.query('summarize_spending', {
                'period': JAN, 'filters': filters, 'group_by': ['store'], 'limit': 1,
                'sort_by': 'net_expense'})
            self.assertEqual(len(token_sizes), 1)
            self.assertLess(sum(token_sizes), 100_000)
            self.assertEqual(summary['group_count'], 500)
            self.assertEqual(summary['totals'][0]['net_expense'], '125250.00')
            self.assertEqual(summary['groups'][0]['dimensions']['store'], '499')
            token_sizes.clear()
            comparison = self.query('compare_spending', {
                'period': FEB, 'baseline_period': JAN, 'filters': filters,
                'group_by': ['store'], 'limit': 1})
            self.assertLessEqual(len(token_sizes), 4)  # two summaries, two evidence links
            self.assertLess(sum(token_sizes), 400_000)
            self.assertEqual(comparison['contribution_count'], 500)
            self.assertTrue(comparison['contributions_truncated'])
            self.assertEqual(comparison['differences'][0]['net_expense_difference'], '0.00')

        for token, expected_month in [
            (summary['groups'][0]['drilldown_token'], '01'),
            (comparison['contributions'][0]['current_drilldown_token'], '02'),
            (comparison['contributions'][0]['baseline_drilldown_token'], '01'),
            (comparison['current']['groups'][0]['drilldown_token'], '02'),
        ]:
            evidence = self.query('search_transactions', {'drilldown_token': token})
            self.assertEqual(len(evidence['rows']), 1)
            self.assertEqual(evidence['rows'][0]['date'][5:7], expected_month)

    def test_exclusion_counts_obey_known_tag_filters(self):
        build_snapshot(self.db, [
            record('wanted', tags=['wanted']),
            record('other-disabled', tags=['other'], isEnabled=False),
            record('wanted-disabled', tags=['wanted'], isEnabled=False),
            record('other-transfer', tags=['other'], transferID='t'),
            record('wanted-transfer', tags=['wanted'], transferID='t'),
        ])
        for tool, extra in [('summarize_spending', {}), ('search_transactions', {}),
                            ('get_facets', {'dimensions': ['name']})]:
            with self.subTest(tool=tool):
                result = self.query(tool, {'period': JAN, 'filters': {'tags': ['wanted']}, **extra})
                self.assertEqual(result['excluded_counts'], {'disabled': 1, 'transfer': 1})
                empty = self.query(tool, {'period': JAN, 'filters': {'tags': []}, **extra})
                self.assertEqual(empty['excluded_counts'], {})

    def test_unknown_excluded_tags_are_separate_from_matching_exclusions(self):
        build_snapshot(self.db, [record('wanted', tags=['wanted']),
                                 record('opaque-disabled', tags='wanted,other', isEnabled=False)])
        result = self.query('summarize_spending', {'period': JAN, 'filters': {'tags': ['wanted']}})
        self.assertEqual(result['totals'][0]['net_expense'], '10.00')
        self.assertEqual(result['excluded_counts'], {'unknown_tag_match_disabled': 1})
        self.assertIn('unknown_tag_match_*', result['exclusion_scope'])
        empty = self.query('summarize_spending', {'period': JAN, 'filters': {'tags': []}})
        self.assertEqual(empty['excluded_counts'], {})

    def test_long_discovered_literals_roundtrip_in_all_exact_fields(self):
        name, store, tag = '費'*513, 'Store'*150, 'tag'*180
        build_snapshot(self.db, [record('match', name=name, store=store, tags=[tag]),
                                 record('other', name='unrelated', store='other', tags=['other'])])
        facets = self.query('get_facets', {'period': JAN, 'dimensions': ['name', 'store', 'tag']})
        combined = {}
        for dimension, literal in [('name', name), ('store', store), ('tag', tag)]:
            selected = next(value for value in facets['facets'][dimension]['values'] if value['value'] == literal)
            filters = selected['filter']
            combined.update(filters)
            summary = self.query('summarize_spending', {'period': JAN, 'filters': filters})
            self.assertEqual(summary['totals'][0]['net_expense'], '10.00')
            evidence = self.query('search_transactions', {'drilldown_token': summary['groups'][0]['drilldown_token']})
            self.assertEqual([row['id'] for row in evidence['rows']], ['match'])
        summary = self.query('summarize_spending', {'period': JAN, 'filters': combined})
        self.assertEqual(summary['totals'][0]['count'], 1)


if __name__ == '__main__':
    unittest.main()
