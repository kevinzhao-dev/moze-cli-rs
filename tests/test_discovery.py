"""Synthetic discovery and facet behavior, including pinned pagination."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from analytics_fixture import SEMANTICS, build_snapshot, record, ref

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'converter'))
import analytics

PERIOD = {'from': '2026-01-01', 'to': '2026-01-31'}


class Discovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='moze-discovery-')
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'synthetic.sqlite3'

    def query(self, tool, args):
        return analytics.dispatch(self.db, tool, args, SEMANTICS)

    def insert_entity(self, kind, identifier, **values):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute('INSERT INTO objects VALUES (?,?,?)',
                               (kind, json.dumps(identifier), json.dumps(dict(identifier=identifier, isDeleted=False, **values))))

    def test_declared_unused_values_and_usage_count(self):
        build_snapshot(self.db)
        result = self.query('list_entities', {'kind': 'subcategory'})
        self.assertEqual(result['total_count'], 2)
        values = {item['id']: item for item in result['items']}
        self.assertEqual(values['electricity']['usage_count'], 1)
        self.assertEqual(values['lunch']['usage_count'], 0)
        self.assertEqual(values['electricity']['parent']['id'], 'utilities')
        used = self.query('list_entities', {'kind': 'subcategory', 'include_unused': False})
        self.assertEqual([item['id'] for item in used['items']], ['electricity'])

    def test_parent_and_query_filters(self):
        build_snapshot(self.db)
        result = self.query('list_entities', {'kind': 'subcategory', 'parent_id': 'utilities', 'query': '電'})
        self.assertEqual([item['id'] for item in result['items']], ['electricity'])
        with self.assertRaises(analytics.AnalyticsError):
            self.query('list_entities', {'kind': 'account', 'parent_id': 'utilities'})

    def test_declared_pagination_pins_snapshot_and_request(self):
        build_snapshot(self.db)
        first = self.query('list_entities', {'kind': 'subcategory', 'limit': 1})
        second = self.query('list_entities', {'cursor': first['next_cursor']})
        self.assertEqual(second['total_count'], 2)
        self.assertNotEqual(first['items'][0]['id'], second['items'][0]['id'])
        self.assertIsNone(second['next_cursor'])
        with self.assertRaises(analytics.AnalyticsError):
            self.query('list_entities', {'cursor': first['next_cursor'], 'query': 'changed'})
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("UPDATE metadata SET value=? WHERE key='sha256'", (json.dumps('replacement'),))
        with self.assertRaises(analytics.AnalyticsError):
            self.query('list_entities', {'cursor': first['next_cursor']})

    def test_archived_entities_opt_in(self):
        build_snapshot(self.db)
        self.insert_entity('AHAccount', 'old', name='Old account', isHidden=True)
        result = self.query('list_entities', {'kind': 'account'})
        self.assertEqual(result['total_count'], 1)
        result = self.query('list_entities', {'kind': 'account', 'include_archived': True})
        old = next(item for item in result['items'] if item['id'] == 'old')
        self.assertTrue(old['is_archived'])
        self.assertEqual(old['usage_count'], 0)

    def test_observed_facets_retain_conjunctive_filters(self):
        build_snapshot(self.db, [record('one', name='A', store='Store A'),
                                 record('two', name='B', store='Store B'),
                                 record('three', name='C', store='Store C', classification=ref('AHClassification', 'lunch'))])
        result = self.query('get_facets', {'period': PERIOD, 'dimensions': ['subcategory', 'name', 'store'],
                                          'filters': {'subcategory_ids': ['electricity']}})
        self.assertEqual(result['matching_count'], 2)
        self.assertEqual(result['facets']['subcategory']['distinct_count'], 1)
        self.assertEqual({entry['value'] for entry in result['facets']['name']['values']}, {'A', 'B'})
        self.assertEqual(result['facets']['store']['values'][0]['filter'], {'stores': ['Store A']})

    def test_facets_pagination_count_and_missing(self):
        build_snapshot(self.db, [record('a', store='X'), record('b', store='Y'),
                                 record('c', store='X'), record('d', store='')])
        first = self.query('get_facets', {'period': PERIOD, 'dimensions': ['store', 'project'], 'limit': 1})
        self.assertEqual(first['facets']['store']['values'][0]['count'], 2)
        self.assertEqual(first['facets']['store']['missing_count'], 1)
        self.assertEqual(first['facets']['project']['missing_count'], 4)
        second = self.query('get_facets', {'cursor': first['next_cursor']})
        self.assertEqual(second['facets']['store']['values'][0]['value'], 'Y')
        self.assertIsNone(second['next_cursor'])
        with self.assertRaises(analytics.AnalyticsError):
            self.query('list_entities', {'cursor': first['next_cursor']})

    def test_tags_deduplicate_and_opaque_tags_are_unknown(self):
        build_snapshot(self.db, [record('one', tags=['work', 'work', 'home']),
                                 record('two', tags='opaque,unverified'), record('three', tags='')])
        result = self.query('get_facets', {'period': PERIOD, 'dimensions': ['tag']})
        facet = result['facets']['tag']
        self.assertTrue(any('tag encodings' in warning for warning in result['warnings']))
        self.assertEqual(facet['unknown_count'], 1)
        self.assertEqual(facet['missing_count'], 1)
        self.assertEqual({entry['value']: entry['count'] for entry in facet['values']}, {'home': 1, 'work': 1})

    def test_malformed_cursor_request_is_structured_error(self):
        build_snapshot(self.db)
        first = self.query('list_entities', {'kind': 'subcategory', 'limit': 1})
        state = analytics.untoken(first['next_cursor'])
        for change in ({'request': 3}, {'snapshot_id': None}):
            with self.subTest(change=change), self.assertRaises(analytics.AnalyticsError):
                self.query('list_entities', {'cursor': analytics.token(dict(state, **change))})

    def test_invalid_facets_raise(self):
        build_snapshot(self.db)
        for args in ({'period': PERIOD, 'dimensions': []},
                     {'period': PERIOD, 'dimensions': ['name', 'name']},
                     {'period': PERIOD, 'dimensions': ['password']},
                     {'dimensions': ['name']}):
            with self.subTest(args=args), self.assertRaises(analytics.AnalyticsError):
                self.query('get_facets', args)

    def test_empty_and_duplicate_name_catalogs_preserve_source_identity(self):
        build_snapshot(self.db, records=[], extra_entities={'AHClassification': [
            {'identifier': 'other-power', 'name': '電費', 'isDeleted': False,
             'category': ref('AHCategory', 'food')},
            {'identifier': 'deleted', 'name': 'Removed', 'isDeleted': True}]})
        catalog = self.query('list_entities', {'kind': 'subcategory', 'query': '電費'})
        self.assertEqual({row['id'] for row in catalog['items']}, {'electricity', 'other-power'})
        self.assertEqual({row['parent']['id'] for row in catalog['items']}, {'utilities', 'food'})
        self.assertEqual({row['usage_count'] for row in catalog['items']}, {0})
        all_items = self.query('list_entities', {'kind': 'subcategory', 'include_archived': True})
        self.assertNotIn('deleted', {row['id'] for row in all_items['items']})
        projects = self.query('list_entities', {'kind': 'project'})
        self.assertEqual(projects['items'], [])
        self.assertIsNone(projects['next_cursor'])
        facets = self.query('get_facets', {'period': PERIOD, 'dimensions': ['name']})
        self.assertEqual(facets['matching_count'], 0)
        self.assertEqual(facets['facets']['name']['distinct_count'], 0)
        self.assertIsNone(facets['next_cursor'])

    def test_discovery_tokens_reject_policy_changes(self):
        build_snapshot(self.db, [record('a', name='A'), record('b', name='B')])
        for tool, args in [('list_entities', {'kind': 'subcategory', 'limit': 1}),
                           ('get_facets', {'period': PERIOD, 'dimensions': ['name'], 'limit': 1})]:
            cursor = self.query(tool, args)['next_cursor']
            with self.subTest(tool=tool), self.assertRaises(analytics.AnalyticsError) as error:
                analytics.dispatch(self.db, tool, {'cursor': cursor}, dict(SEMANTICS, expense_types=[99]))
            self.assertEqual(error.exception.code, 'SEMANTICS_MISMATCH')


if __name__ == '__main__':
    unittest.main()
