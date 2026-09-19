"""Invalid input must never look like a successful empty financial query."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

BINARY = Path(__file__).resolve().parents[1] / 'target/debug/moze-rs'

class Arguments(unittest.TestCase):
    def run_cli(self, *args):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([str(BINARY), '--db', str(Path(tmp)/'missing.sqlite3'), *args], capture_output=True, text=True)
        return result.returncode, json.loads(result.stdout)

    def test_invalid_input_before_database_access(self):
        cases = [
            ('list', 'AHRecord', '--from', day)
            for day in ['2026-99-99', '2026-02-30', '2025-02-29', '1900-02-29', '2026-04-31', '2026-01-00', '0000-01-01', '2026-1-01']
        ] + [
            ('list', 'AHRecord', '--limit', '0'),
            ('list', 'AHRecord', '--limit', '501'),
            ('list', 'AHRecord', '--from', '2026-03-01', '--to', '2026-02-01'),
            ('list', 'AHAccount', '--from', '2026-01-01'),
            ('sync',), # --db is invalid for sync
        ]
        for args in cases:
            with self.subTest(args=args):
                code, body = self.run_cli(*args)
                self.assertEqual(code, 2)
                self.assertFalse(body['ok'])
                self.assertEqual(body['error']['code'], 'INVALID_ARGUMENT')

    def test_valid_dates_reach_database_operation(self):
        for day in ['2000-02-29', '2024-02-29', '2026-02-28', '2026-04-30', '2026-12-31']:
            with self.subTest(day=day):
                code, body = self.run_cli('list', 'AHRecord', '--from', day)
                self.assertEqual(code, 1)
                self.assertEqual(body['error']['code'], 'OPERATION_FAILED')
