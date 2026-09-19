"""Synthetic MCP wire tests: lifecycle, recovery, schema and dispatch boundaries."""
import io
import json
from pathlib import Path
import subprocess
import sqlite3
from contextlib import closing
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from converter import mcp_server as mcp
from analytics_fixture import SEMANTICS, build_snapshot, record


def request(method, params=None, request_id=1):
    result = {'jsonrpc': '2.0', 'method': method, 'id': request_id}
    if params is not None:
        result['params'] = params
    return result


def initialize(version='2025-11-25'):
    return request('initialize', {'protocolVersion': version, 'capabilities': {},
                                 'clientInfo': {'name': 'synthetic-test', 'version': '1'}})


READY = {'jsonrpc': '2.0', 'method': 'notifications/initialized'}


class McpTests(unittest.TestCase):
    def ready_server(self):
        server = mcp.Server(Path('/synthetic/database'))
        server.handle(initialize())
        server.handle(READY)
        return server

    def test_protocol_versions_and_lifecycle(self):
        for requested in (*mcp.PROTOCOL_VERSIONS, '2099-01-01'):
            server = mcp.Server(Path('/synthetic/database'))
            self.assertEqual(server.handle(request('tools/list'))['error']['code'], -32002)
            result = server.handle(initialize(requested))['result']
            self.assertEqual(result['protocolVersion'], requested if requested in
                             mcp.PROTOCOL_VERSIONS else mcp.PROTOCOL_VERSIONS[-1])
            self.assertEqual(result['capabilities'], {'tools': {'listChanged': False}})
            self.assertIsNone(server.handle(READY))
            tools = server.handle(request('tools/list'))['result']['tools']
            self.assertEqual(len(tools), 8)
            self.assertTrue(all(t['annotations']['readOnlyHint'] for t in tools))
            self.assertTrue(all(t['inputSchema']['additionalProperties'] is False for t in tools))
            self.assertEqual(server.handle(request('initialize'))['error']['code'], -32600)

    def test_invalid_envelopes_params_unknown_methods_notifications(self):
        server = self.ready_server()
        for bad in ([], {}, {'jsonrpc': '2.0', 'method': 'ping', 'id': True},
                    {'jsonrpc': '2.0', 'method': 'ping', 'id': None}):
            self.assertEqual(server.handle(bad)['error']['code'], -32600)
        self.assertIsNone(server.handle({'jsonrpc': '2.0', 'method': 'unknown'}))
        self.assertEqual(server.handle(request('unknown'))['error']['code'], -32601)
        self.assertEqual(server.handle(request('ping', []))['error']['code'], -32602)
        self.assertEqual(server.handle(request('tools/call', {'name': 'sql'}))['error']['code'], -32602)
        self.assertEqual(server.handle(request('tools/list', {'cursor': 'x'}))['error']['code'], -32602)
        self.assertEqual(server.handle(request('tools/call', {'name': 'get_context',
                                                          'arguments': []}))['error']['code'], -32602)
        self.assertEqual(server.handle(request('ping'))['result'], {})

    def test_results_and_sanitized_unexpected_errors(self):
        server = self.ready_server()
        with patch.object(mcp.analytics, 'dispatch', return_value={'total': '123.45'}) as dispatch:
            result = server.handle(request('tools/call', {'name': 'get_context'}))['result']
            self.assertFalse(result['isError'])
            self.assertEqual(json.loads(result['content'][0]['text']), result['structuredContent'])
            dispatch.assert_called_once_with(Path('/synthetic/database'), 'get_context', {}, None)
        with patch.object(mcp.analytics, 'dispatch', side_effect=RuntimeError('/private/secret')):
            result = server.handle(request('tools/call', {'name': 'get_context'}))['result']
            self.assertTrue(result['isError'])
            self.assertNotIn('/private', json.dumps(result))
        with patch.object(mcp.analytics, 'dispatch',
                          side_effect=mcp.analytics.AnalyticsError('snapshot_changed', 'Retry with current snapshot')):
            result = server.handle(request('tools/call', {'name': 'get_context'}))['result']
            self.assertTrue(result['isError'])
            self.assertEqual(result['structuredContent']['error']['code'], 'SNAPSHOT_CHANGED')

    def test_bounded_lines_and_parse_errors_recover(self):
        ping = json.dumps(request('ping')).encode() + b'\n'
        for malformed, code in ((b'{broken\n', -32700), (b'\xff\n', -32700),
                                (b'{"x":NaN}\n', -32700),
                                (b'[' * 2000 + b'\n', -32700),
                                (b'x' * (mcp.MAX_LINE_BYTES * 3) + b'\n', -32600)):
            with self.subTest(code=code, size=len(malformed)):
                output = io.StringIO()
                mcp.serve(Path('/synthetic/database'), input_stream=io.BytesIO(malformed + ping),
                          output_stream=output)
                messages = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual(len(messages), 2)
                self.assertEqual(messages[0]['error']['code'], code)
                self.assertEqual(messages[1]['result'], {})

    def test_subprocess_synthetic_electricity_and_validation(self):
        with tempfile.TemporaryDirectory(prefix='moze-mcp-test-') as tmp:
            db = Path(tmp) / 'synthetic.sqlite3'
            semantics = Path(tmp) / 'semantics.json'
            semantics.write_text(json.dumps(SEMANTICS))
            build_snapshot(db, [record('expense-1', 12), record('expense-2', 8)])
            messages = [initialize(), READY,
                request('tools/call', {'name': 'summarize_spending', 'arguments': {
                    'period': {'from': '2026-01-01', 'to': '2026-01-31'},
                    'filters': {'query': '電費'}, 'group_by': ['month'],
                    'as_of': '2026-03-31'}}, 2),
                request('tools/call', {'name': 'get_context', 'arguments': {'sql': 'SELECT 1'}}, 3),
                request('tools/call', {'name': 'get_transaction', 'arguments': {'id': 'expense-1'}}, 4)]
            process = subprocess.run([sys.executable, str(ROOT / 'converter/mcp_server.py'),
                                      '--db', str(db), '--semantics', str(semantics)],
                input=''.join(json.dumps(m) + '\n' for m in messages),
                capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(process.stderr, '')
            replies = [json.loads(line) for line in process.stdout.splitlines()]
            self.assertFalse(replies[1]['result']['isError'], replies[1])
            self.assertIn('20.00', json.dumps(replies[1]['result']['structuredContent']))
            self.assertTrue(replies[2]['result']['isError'])
            self.assertFalse(replies[3]['result']['isError'], replies[3])
            self.assertNotIn('SYNTHETIC_SECRET', process.stdout)
            self.assertNotIn(tmp, process.stdout)

    def test_discovery_tools_and_exact_filters_over_mcp(self):
        with tempfile.TemporaryDirectory(prefix='moze-mcp-facets-test-') as tmp:
            db = Path(tmp) / 'synthetic.sqlite3'
            semantics = Path(tmp) / 'semantics.json'
            semantics.write_text(json.dumps(SEMANTICS))
            build_snapshot(db, [record('record-a', 12, name='Exact Bill', store='Exact Store', tags=['home', 'utility']),
                                record('record-b', 8, name='Other Bill', store='Other Store', tags='opaque,unknown')])
            with closing(sqlite3.connect(db)) as con, con:
                con.execute('INSERT INTO objects VALUES (?,?,?)', ('AHTag', json.dumps('tag-home'),
                    json.dumps({'identifier': 'tag-home', 'name': 'Configured home', 'isDeleted': False})))
                con.execute('DROP VIEW agent_objects')
                con.execute("CREATE VIEW agent_objects AS SELECT * FROM objects WHERE type IN "
                            "('AHCurrency','AHCategory','AHClassification','AHAccount','AHProject','AHRecord','AHTag')")
            period = {'from': '2026-01-01', 'to': '2026-01-31'}
            messages = [initialize(), READY,
                request('tools/call', {'name': 'list_entities', 'arguments': {'kind': 'category'}}, 2),
                request('tools/call', {'name': 'list_entities', 'arguments': {'kind': 'tag'}}, 3),
                request('tools/call', {'name': 'get_facets', 'arguments': {'period': period, 'dimensions': ['name', 'store', 'tag']}}, 4),
                request('tools/call', {'name': 'summarize_spending', 'arguments': {'period': period,
                    'filters': {'names': ['Exact Bill'], 'stores': ['Exact Store'], 'tags': ['home', 'utility'], 'tags_mode': 'all'}}}, 5),
                request('tools/call', {'name': 'summarize_spending', 'arguments': {'period': period,
                    'filters': {'tags_mode': 'all'}}}, 6)]
            process = subprocess.run([sys.executable, str(ROOT / 'converter/mcp_server.py'),
                                      '--db', str(db), '--semantics', str(semantics)],
                input=''.join(json.dumps(m) + '\n' for m in messages),
                capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(process.stderr, '')
            replies = [json.loads(line) for line in process.stdout.splitlines()]
            for reply in replies[1:5]:
                self.assertFalse(reply['result']['isError'], reply)
            self.assertIn('Synthetic food', json.dumps(replies[1]))
            self.assertIn('Configured home', json.dumps(replies[2]))
            self.assertIsNone(replies[2]['result']['structuredContent']['items'][0]['usage_count'])
            self.assertIn('Exact Store', json.dumps(replies[3]))
            self.assertEqual(replies[3]['result']['structuredContent']['facets']['tag']['unknown_count'], 1)
            self.assertIn('12.00', json.dumps(replies[4]))
            self.assertTrue(replies[5]['result']['isError'])
            self.assertNotIn('SYNTHETIC_SECRET', process.stdout)

    def test_rust_cli_mcp_stdout_is_only_protocol(self):
        binary = ROOT / 'target/debug/moze-rs'
        if not binary.exists():
            self.skipTest('Run cargo build --locked to test Rust entrypoint')
        with tempfile.TemporaryDirectory(prefix='moze-mcp-cli-test-') as tmp:
            db = Path(tmp) / 'synthetic.sqlite3'
            build_snapshot(db)
            messages = [initialize(), READY, request('tools/list', request_id=2)]
            process = subprocess.run([str(binary), '--db', str(db), 'mcp'],
                input=''.join(json.dumps(m) + '\n' for m in messages),
                capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            replies = [json.loads(line) for line in process.stdout.splitlines()]
            self.assertEqual([r['id'] for r in replies], [1, 2])
            self.assertEqual(len(replies[1]['result']['tools']), 8)

    def test_subprocess_protocol_and_missing_database_error(self):
        with tempfile.TemporaryDirectory(prefix='moze-mcp-test-') as tmp:
            absent = Path(tmp) / 'absent.sqlite3'
            messages = [initialize(), READY, request('tools/list', request_id=2),
                        request('tools/call', {'name': 'get_context'}, 3), request('ping', request_id=4)]
            process = subprocess.run([sys.executable, str(ROOT / 'converter/mcp_server.py'),
                                      '--db', str(absent)],
                                     input=''.join(json.dumps(m) + '\n' for m in messages),
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(process.stderr, '')
            replies = [json.loads(line) for line in process.stdout.splitlines()]
            self.assertEqual([r['id'] for r in replies], [1, 2, 3, 4])
            self.assertTrue(replies[2]['result']['isError'])
            self.assertNotIn(tmp, process.stdout)
            self.assertFalse(absent.exists())


if __name__ == '__main__':
    unittest.main()
