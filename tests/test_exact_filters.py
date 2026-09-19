"""Literal field filters are composable and never guess opaque tag encodings."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from analytics_fixture import SEMANTICS, build_snapshot, record

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'converter'))
import analytics

PERIOD = {'from': '2026-01-01', 'to': '2026-01-31'}


class ExactFilters(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='moze-exact-test-')
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name)/'synthetic.sqlite3'

    def query(self, filters, tool='search_transactions', **extra):
        return analytics.dispatch(self.db, tool, {'period': PERIOD, 'filters': filters, **extra}, SEMANTICS)

    def ids(self, filters):
        return {row['id'] for row in self.query(filters)['rows']}

    def test_or_within_field_and_across_fields(self):
        build_snapshot(self.db, [
            record('a', name='電費', store='North', tags=['home', 'required', 'home']),
            record('b', name='水費', store='North', tags='["home"]'),
            record('c', name='電費', store='South', tags=['required']),
            record('d', name='電費補繳', store='North', tags=[]),
        ])
        self.assertEqual(self.ids({'names': ['電費', '水費'], 'stores': ['North']}), {'a', 'b'})
        self.assertEqual(self.ids({'tags': ['home', 'required']}), {'a', 'b', 'c'})
        self.assertEqual(self.ids({'tags': ['home', 'required'], 'tags_mode': 'all'}), {'a'})
        self.assertEqual(self.ids({'names': ['電費'], 'stores': ['North'], 'tags': ['home']}), {'a'})
        for field in ('names', 'stores', 'tags'):
            self.assertEqual(self.ids({field: []}), set())

    def test_case_whitespace_wildcards_quotes_are_literal(self):
        literal = "費用_%' OR 1=1 --\n☀"
        build_snapshot(self.db, [record('exact', name=literal, store=' A '),
                                 record('similar', name=literal.lower(), store='a'),
                                 record('ordinary', name='費用')])
        self.assertEqual(self.ids({'names': [literal], 'stores': [' A ']}), {'exact'})
        self.assertEqual(self.ids({'names': ['費用%']}), set())
        self.assertEqual(self.ids({'stores': ['A']}), set())

    def test_unknown_tags_fail_only_for_otherwise_eligible_candidates(self):
        build_snapshot(self.db, [record('valid', name='wanted', tags=['home']),
                                 record('opaque', name='other', tags='home,required'),
                                 record('disabled', name='wanted', tags='?', isEnabled=False),
                                 record('future', name='wanted', tags='?', dateString='2026.09.01')])
        with self.assertRaises(analytics.AnalyticsError) as error:
            self.ids({'tags': ['home']})
        self.assertEqual(error.exception.code, 'UNVERIFIED_TAG_ENCODING')
        self.assertEqual(self.ids({'names': ['wanted'], 'tags': ['home']}), {'valid'})
        rows = self.query({})['rows']
        self.assertEqual(next(row for row in rows if row['id'] == 'opaque')['tags_status'], 'unverified')

    def test_invalid_shapes_and_tags_mode_without_selection(self):
        build_snapshot(self.db)
        for filters in ({'names': '電費'}, {'stores': [None]}, {'tags': [1]},
                        {'tags_mode': 'all'}, {'tags': ['home'], 'tags_mode': ['any']},
                        {'tags': ['home'], 'tags_mode': 'guess'}):
            with self.subTest(filters=filters), self.assertRaises(analytics.AnalyticsError) as error:
                self.query(filters)
            self.assertEqual(error.exception.code, 'INVALID_ARGUMENT')

    def test_tag_decoding_keeps_structured_values_and_does_not_split(self):
        cases = [(['a,b', 'a,b', 'x y'], ['a,b', 'x y'], 'verified'),
                 ('["a,b", "x y"]', ['a,b', 'x y'], 'verified'),
                 ('a,b', [], 'unverified'), ('["a", 3]', [], 'unverified'),
                 ('{"tags": ["a"]}', [], 'unverified'), ('', [], 'empty'),
                 (None, [], 'empty'), ('[]', [], 'empty')]
        for raw, tags, status in cases:
            with self.subTest(raw=raw):
                self.assertEqual(analytics.parse_tags(raw), (tags, status))

    def test_exact_filters_survive_aggregation_drilldown_and_cursor(self):
        build_snapshot(self.db, [record(str(i), i, name='Exact', store='One', tags=['a', 'b'])
                                 for i in range(5)] + [record('wrong', 999, name='Exact suffix')])
        summary = self.query({'names': ['Exact'], 'stores': ['One'], 'tags': ['a', 'b'], 'tags_mode': 'all'},
                             tool='summarize_spending', group_by=['category'])
        self.assertEqual(summary['totals'][0]['net_expense'], '10.00')
        first = analytics.dispatch(self.db, 'search_transactions', {
            'drilldown_token': summary['groups'][0]['drilldown_token'], 'limit': 2}, SEMANTICS)
        second = analytics.dispatch(self.db, 'search_transactions', {'cursor': first['next_cursor']}, SEMANTICS)
        ids = {row['id'] for row in first['rows'] + second['rows']}
        self.assertEqual(ids, {str(i) for i in range(5)})

    def test_resolve_declared_tag_text_not_identifier(self):
        build_snapshot(self.db, extra_entities={'AHTag': [
            {'identifier': 'synthetic-tag', 'text': '居家', 'isDeleted': False, 'relatedID': 'opaque'}]})
        result = analytics.dispatch(self.db, 'resolve_entities', {'query': '居家', 'kinds': ['tag']})
        self.assertEqual(result['candidates'][0]['name'], '居家')
        self.assertEqual(result['candidates'][0]['id'], 'synthetic-tag')


if __name__ == '__main__':
    unittest.main()
