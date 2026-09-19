"""Integer fidelity checks against disposable, entirely synthetic Realm files."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = r"""
import Realm from 'realm';
const [file, kind, text] = process.argv.slice(1);
const number = Number(text);
let schema, create;
if (kind === 'reference' || kind === 'primary') {
  schema = [
    {name:'AHAccount', primaryKey:'identifier', properties:{identifier:'int'}},
    {name:'AHRecord', primaryKey:'identifier', properties:{identifier:'string', account:'AHAccount'}}
  ];
  create = realm => {
    const account = realm.create('AHAccount', {identifier:number});
    if (kind === 'reference') realm.create('AHRecord', {identifier:'synthetic', account});
  };
} else {
  const types = {scalar:'int', list:'int[]', set:'int<>', dictionary:'int{}'};
  schema = [{name:'AHRecord', primaryKey:'identifier', properties:{identifier:'string', amount:types[kind]}}];
  const amount = kind === 'scalar' ? number : kind === 'dictionary' ? {synthetic:number} : [number];
  create = realm => realm.create('AHRecord', {identifier:'synthetic', amount});
}
const realm = new Realm({path:file, schema});
realm.write(() => create(realm));
realm.close();
process.exit(0);
"""


class IntegerExport(unittest.TestCase):
    def export(self, kind, number):
        with tempfile.TemporaryDirectory(prefix='moze-integer-test-') as directory:
            realm = Path(directory) / 'synthetic.realm'
            output = Path(directory) / 'synthetic.jsonl'
            subprocess.run(
                ['node', '--input-type=module', '-e', FIXTURE, str(realm), kind, str(number)],
                cwd=ROOT, check=True, capture_output=True, text=True, timeout=30,
            )
            result = subprocess.run(
                ['node', str(ROOT / 'converter/export.mjs'), str(realm), str(output)],
                cwd=ROOT, capture_output=True, text=True, timeout=30,
            )
            rows = [json.loads(line) for line in output.read_text().splitlines()] if result.returncode == 0 else []
            return result, rows

    def test_unsafe_integers_rejected_in_every_typed_position(self):
        for kind in ('scalar', 'list', 'set', 'dictionary', 'primary', 'reference'):
            for number in (2**53, -(2**53)):
                with self.subTest(kind=kind, number=number):
                    result, _ = self.export(kind, number)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('Realm export failed', result.stderr)

    def test_safe_boundaries_roundtrip_including_reference_ids(self):
        for kind in ('scalar', 'list', 'set', 'dictionary', 'primary', 'reference'):
            for number in (2**53 - 1, -(2**53 - 1)):
                with self.subTest(kind=kind, number=number):
                    result, rows = self.export(kind, number)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    objects = {row['type']: row for row in rows if row['kind'] == 'object'}
                    if kind in ('primary', 'reference'):
                        self.assertEqual(objects['AHAccount']['id'], number)
                        if kind == 'reference':
                            self.assertEqual(objects['AHRecord']['data']['account'], {'$ref': 'AHAccount', 'id': number})
                    else:
                        expected = number if kind == 'scalar' else {'synthetic': number} if kind == 'dictionary' else [number]
                        self.assertEqual(objects['AHRecord']['data']['amount'], expected)


if __name__ == '__main__':
    unittest.main()
