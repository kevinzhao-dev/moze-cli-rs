"""Deterministic acceptance workflows; independent live agent runs use the same fixture."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from agent_eval_fixture import prepare

ROOT = Path(__file__).resolve().parents[1]
PERIOD = {'from': '2026-01-01', 'to': '2026-03-31'}


class AgentWorkflows(unittest.TestCase):
    def test_discover_then_filter_without_reading_raw_records(self):
        with tempfile.TemporaryDirectory(prefix='moze-agent-workflow-') as directory:
            db, semantics = prepare(directory)
            snapshot = None
            calls = []

            def call(tool, args, expected_code=0):
                nonlocal snapshot
                if snapshot:
                    args = {**args, 'snapshot_id': snapshot}
                result = subprocess.run([
                    str(ROOT/'target/debug/moze-rs'), '--db', str(db), 'query', tool,
                    '--args', '-', '--semantics', str(semantics)],
                    input=json.dumps(args), capture_output=True, text=True, timeout=15)
                calls.append(tool)
                self.assertEqual(result.returncode, expected_code, result.stdout + result.stderr)
                body = json.loads(result.stdout)
                if expected_code:
                    self.assertFalse(body['ok'])
                    return body['error']
                self.assertTrue(body['ok'])
                snapshot = body['data']['snapshot_id']
                return body['data']

            categories = call('list_entities', {'kind': 'category', 'query': '居家'})
            self.assertEqual(len(categories['items']), 1)
            category = categories['items'][0]['id']
            catalog = call('list_entities', {'kind': 'subcategory', 'parent_id': category, 'period': PERIOD})
            self.assertEqual({v['name']: v['usage_count'] for v in catalog['items']},
                             {'電費': 3, '水費': 1, '房租': 1, '瓦斯費': 0})
            self.assertIsNone(catalog['next_cursor'])
            accounts = call('resolve_entities', {'query': '生活帳戶', 'kinds': ['account']})
            account = accounts['candidates'][0]['id']
            facets = call('get_facets', {'period': PERIOD, 'filters': {'account_ids': [account]},
                                         'dimensions': ['name', 'store', 'tag']})
            self.assertEqual(facets['matching_count'], 5)
            self.assertEqual({v['value']: v['count'] for v in facets['facets']['name']['values']},
                             {'電費': 3, '水費': 1, '房租': 1})
            self.assertEqual(facets['facets']['store']['missing_count'], 1)
            self.assertEqual(facets['facets']['tag']['unknown_count'], 1)
            self.assertEqual(facets['facets']['tag']['missing_count'], 1)
            name_filter = next(v['filter'] for v in facets['facets']['name']['values'] if v['value'] == '電費')
            store_filter = next(v['filter'] for v in facets['facets']['store']['values'] if v['value'] == '北方公用')
            summary = call('summarize_spending', {'period': PERIOD,
                'filters': {'category_ids': [category], **name_filter, **store_filter}})
            total = summary['totals'][0]
            self.assertEqual((total['currency'], total['gross_expense'], total['refund'], total['net_expense']),
                             ('USD', '100.00', '5.00', '95.00'))
            evidence = call('search_transactions', {'drilldown_token': summary['groups'][0]['drilldown_token']})
            self.assertEqual({v['id'] for v in evidence['rows']}, {'p1', 'p2', 'p3'})
            error = call('summarize_spending', {'period': PERIOD,
                'filters': {'account_ids': [account], 'tags': ['居家', '必要'], 'tags_mode': 'all'}}, expected_code=1)
            self.assertEqual(error['code'], 'UNVERIFIED_TAG_ENCODING')
            self.assertEqual(len(calls), 7)


if __name__ == '__main__':
    unittest.main()
