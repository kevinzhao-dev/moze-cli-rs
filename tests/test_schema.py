"""Schema compatibility failures must not replace a valid private snapshot."""
import copy
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('schema_sync', ROOT/'converter/sync.py')
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class SchemaCompatibility(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='moze-schema-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'source'
        self.source.mkdir()
        self.data = self.root/'data'
        subprocess.run(['node', str(ROOT/'tests/make-fixture.mjs'), str(self.root)],
                       check=True, capture_output=True, timeout=30)
        with zipfile.ZipFile(self.source/'MOZE_4.0_2026-01-01_12:00:00.zip', 'w') as z:
            z.write(self.root/'1.realm', 'moze.realm')
        sync.sync(self.source, self.data, 'node')
        self.db = self.data/'finance.sqlite3'
        with closing(sqlite3.connect(self.db)) as c:
            self.schemas = [json.loads(row[0]) for row in
                            c.execute('SELECT definition FROM source_schema')]

    def test_unrelated_realm_preserves_current(self):
        before = sync.digest(self.db)
        code = """import Realm from 'realm';
const r = new Realm({path:process.argv[1],schemaVersion:202,
schema:[{name:'OtherApp',primaryKey:'id',properties:{id:'string'}}]});
r.close(); process.exit(0);"""
        unrelated = self.root/'other.realm'
        subprocess.run(['node', '--input-type=module', '-e', code, str(unrelated)],
                       cwd=ROOT, check=True, capture_output=True, timeout=30)
        with zipfile.ZipFile(self.source/'MOZE_4.0_2026-01-02_12:00:00.zip', 'w') as z:
            z.write(unrelated, 'moze.realm')
        with self.assertRaisesRegex(ValueError, 'Unsupported MOZE'):
            sync.sync(self.source, self.data, 'node')
        self.assertEqual(sync.digest(self.db), before)
        self.assertEqual(len(list((self.data/'snapshots').glob('*.sqlite3'))), 1)

    def test_version_missing_model_field_type_and_link_target_rejected(self):
        for change in ('version', 'model', 'field', 'type', 'target', 'nullable'):
            with self.subTest(change=change):
                schemas = copy.deepcopy(self.schemas)
                version = 202
                record = next(s for s in schemas if s['name'] == 'AHRecord')
                if change == 'version': version = 203
                if change == 'model': schemas.remove(record)
                if change == 'field': del record['properties']['dateString']
                if change == 'type': record['properties']['price']['type'] = 'string'
                if change == 'target': record['properties']['account']['objectType'] = 'AHProject'
                if change == 'nullable': record['properties']['price']['optional'] = True
                export = self.root/f'{change}.jsonl'
                export.write_text(json.dumps({'kind':'schema', 'version':1,
                    'realm_schema_version':version, 'schemas':schemas})+'\n')
                with self.assertRaisesRegex(ValueError, 'Unsupported MOZE'):
                    sync.build(export, self.root/f'{change}.sqlite3', {})

    def test_supported_empty_dataset_is_valid(self):
        export = self.root/'empty.jsonl'
        export.write_text(json.dumps({'kind':'schema', 'version':1,
            'realm_schema_version':202, 'schemas':self.schemas})+'\n')
        self.assertEqual(sync.build(export, self.root/'empty.sqlite3', {}), {})

    def test_unchanged_legacy_unsupported_database_is_not_mirrored(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("DELETE FROM source_schema WHERE type='AHRecord'")
        before = sync.digest(self.db)
        mirror = self.root/'mirror'
        with self.assertRaisesRegex(ValueError, 'Unsupported MOZE'):
            sync.sync(self.source, self.data, 'node', mirror)
        self.assertEqual(sync.digest(self.db), before)
        self.assertFalse(mirror.exists())


if __name__ == '__main__':
    unittest.main()
