import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
import zipfile

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('sync',ROOT/'converter/sync.py')
sync=importlib.util.module_from_spec(spec);spec.loader.exec_module(sync)

class Pipeline(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='moze-test-')
        self.root=Path(self.temp.name);self.source=self.root/'source';self.source.mkdir()
        self.data=self.root/'data'
        subprocess.run(['node',str(ROOT/'tests/make-fixture.mjs'),str(self.root)],check=True,timeout=30)
        self.add(1)
    def tearDown(self): self.temp.cleanup()
    def add(self,n):
        p=self.source/f'MOZE_4.0_2026-01-0{n}_12:00:00.zip'
        with zipfile.ZipFile(p,'w') as z:
            z.write(self.root/f'{n}.realm','moze.realm')
            z.writestr('../escape','must never extract')
        return p
    def run_sync(self): return sync.sync(self.source,self.data,'node')
    def cli(self,*args,ok=True):
        p=subprocess.run([str(ROOT/'target/debug/moze-rs'),'--db',str(self.data/'finance.sqlite3'),*args],capture_output=True,text=True)
        self.assertEqual(p.returncode==0,ok,p.stdout)
        return json.loads(p.stdout)
    def test_roundtrip_update_and_readonly_cli(self):
        source_hash=sync.digest(next(self.source.glob('*.zip')))
        first=self.run_sync();self.assertEqual(first['status'],'updated')
        self.assertEqual(self.run_sync()['status'],'unchanged')
        self.assertEqual(sync.digest(next(self.source.glob('*.zip'))),source_hash)
        self.assertFalse((self.data/'escape').exists())
        db=self.data/'finance.sqlite3';before=sync.digest(db)
        page=self.cli('list','AHRecord','--limit','2')['data']
        self.assertEqual(page['next_offset'],2)
        self.assertEqual(page['rows'][0]['category'],{'$ref':'AHCategory','id':'category'})
        self.assertEqual(page['rows'][0]['info'],{'x':1.25})
        self.assertEqual(page['rows'][0]['date'],{'$date':'2026-01-01T00:00:00.000Z'})
        self.assertEqual(len(self.cli('list','AHRecord','--id','record-1')['data']['rows']),1)
        self.assertEqual(len(self.cli('list','AHRecord','--from','2026-01-01','--to','2026-01-01')['data']['rows']),3)
        self.assertEqual(len(self.cli('list','AHRecord','--from','2026-01-02')['data']['rows']),0)
        self.cli('list','AHAppConfig',ok=False)
        self.cli('list','AHRecord','--limit','501',ok=False)
        self.cli('list','AHRecord','--snapshot','wrong',ok=False)
        self.assertEqual(sync.digest(db),before)
        self.add(2);self.run_sync()
        self.cli('list','AHRecord','--snapshot',first['sha256'],ok=False)
        self.assertEqual(len(self.cli('list','AHRecord')['data']['rows']),2)
        self.assertEqual(len(self.cli('list','AHRecord','--include-deleted')['data']['rows']),3)
        with sqlite3.connect(db) as c:
            self.assertEqual(c.execute('select count(*) from objects').fetchone()[0],5)
            self.assertEqual(c.execute('select count(*) from links').fetchone()[0],3)
        self.assertEqual(len(list((self.data/'snapshots').glob('*.sqlite3'))),2)
    def test_corruption_preserves_current(self):
        self.run_sync();db=self.data/'finance.sqlite3';before=sync.digest(db)
        (self.source/'MOZE_4.0_2026-01-03_12:00:00.zip').write_bytes(b'broken')
        with self.assertRaises(zipfile.BadZipFile): self.run_sync()
        self.assertEqual(sync.digest(db),before)
    def test_rollback_rejected(self):
        second=self.add(2);self.run_sync();second.unlink()
        with self.assertRaisesRegex(ValueError,'rollback'): self.run_sync()
    def test_storage_boundaries(self):
        with self.assertRaises(ValueError): sync.validate_paths(self.source,ROOT/'private')
        with self.assertRaises(ValueError): sync.validate_paths(self.source,self.source/'out')
        with self.assertRaises(ValueError): sync.validate_paths(self.source,self.root)
    def test_dangling_link_rejected(self):
        export=self.root/'bad.jsonl'
        subprocess.run(['node',str(ROOT/'converter/export.mjs'),str(self.root/'1.realm'),str(export)],check=True,timeout=30)
        rows=[json.loads(line) for line in export.read_text().splitlines()]
        for row in rows:
            if row.get('type')=='AHRecord':
                row['data']['category']['id']='missing'
                break
        export.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        with self.assertRaisesRegex(ValueError,'Unresolved'): sync.build(export,self.root/'bad.sqlite3',{})

if __name__=='__main__': unittest.main()
