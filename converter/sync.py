#!/usr/bin/env python3
"""Local-only MOZE snapshot ingestion. No network calls, no source writes."""
import argparse
from contextlib import closing
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile

REPO = Path(__file__).resolve().parents[1]
PATTERN = re.compile(r'MOZE_4\.0_(\d{4}-\d{2}-\d{2})_(\d{2}):(\d{2}):(\d{2})\.zip')
SAFE = {'AHAccount','AHAccountGroup','AHAccountPayment','AHBudget','AHCategory',
        'AHClassification','AHCurrency','AHCurrencyConversion','AHExchangeRate',
        'AHInstallment','AHRecord','AHProject','AHPeriod','AHTransfer','AHTag',
        'AHTarget','AHReport','AHPackage','AHBonusReward','AHCreditSharing'}

# Compatibility contract, not a row-count heuristic: an empty MOZE account is valid.
# New Realm versions require explicit review before they can replace a good snapshot.
SUPPORTED_REALM_VERSION = 202
CORE_FIELDS = {
    'AHRecord': {'dateString':'string', 'date':'date', 'price':'double',
                 'total':'double', 'type':'int', 'isEnabled':'bool',
                 'isEvent':'bool', 'isRefund':'bool', 'transferID':'string?',
                 'account':'AHAccount?', 'currency':'AHCurrency?',
                 'project':'AHProject?', 'classification':'AHClassification?'},
    'AHAccount': {'originalAmount':'double', 'mainCurrency':'AHCurrency?'},
    'AHCategory': {},
    'AHClassification': {'category':'AHCategory?'},
    'AHCurrency': {'decimalPlace':'int'},
    'AHProject': {'mainCurrency':'AHCurrency?'},
}

def validate_schema(version, definitions):
    if version != SUPPORTED_REALM_VERSION:
        raise ValueError('Unsupported MOZE Realm schema version')
    schemas = {s['name']:s for s in definitions}
    if len(schemas) != len(definitions):
        raise ValueError('Duplicate Realm schema names')
    for name, fields in CORE_FIELDS.items():
        schema = schemas.get(name, {})
        key = 'code' if name == 'AHCurrency' else 'identifier'
        if schema.get('primaryKey') != key or schema.get('embedded', False):
            raise ValueError('Unsupported MOZE core model')
        required = {key:'string', 'isDeleted':'bool', **fields}
        if name != 'AHCurrency': required['name'] = 'string'
        for field, signature in required.items():
            prop = schema.get('properties', {}).get(field, {})
            optional = signature.endswith('?')
            kind = signature.rstrip('?')
            expected = 'object' if kind.startswith('AH') else kind
            if (prop.get('type') != expected or prop.get('optional') != optional
                    or (expected == 'object' and prop.get('objectType') != kind)):
                raise ValueError('Unsupported MOZE core field')
    return schemas

def emit(data):
    print(json.dumps({'api_version':1,'ok':True,'data':data},ensure_ascii=False))

def stamp(path):
    m = PATTERN.fullmatch(path.name)
    if not m:
        return None
    return dt.datetime.fromisoformat(m[1]+'T'+':'.join(m.groups()[1:])).isoformat()

def private_dir(path):
    if path.is_symlink():
        raise ValueError('Private directory must not be a symlink')
    path.mkdir(mode=0o700,parents=True,exist_ok=True)
    os.chmod(path,0o700)

def validate_paths(source, data, mirror=None):
    for p in [data] + ([mirror] if mirror else []):
        if p == Path.home() or p == Path('/') or p in source.parents or p == source or source in p.parents:
            raise ValueError('Storage and MOZE source directories must be separate')
        if p == REPO or REPO in p.parents:
            raise ValueError('Private storage must be outside the repository')
        if any((parent/'.git').exists() for parent in [p,*p.parents]):
            raise ValueError('Private storage must be outside Git worktrees')
    if mirror and (mirror == data or mirror in data.parents or data in mirror.parents):
        raise ValueError('Mirror and local storage must be separate')

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
    return h.hexdigest()

def atomic_copy(source, target):
    # mkstemp makes a private file on the destination filesystem.
    fd, tmp = tempfile.mkstemp(prefix='.pending-',dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as out, source.open('rb') as inp:
            shutil.copyfileobj(inp,out); out.flush(); os.fsync(out.fileno())
        os.replace(tmp,target)
    finally:
        Path(tmp).unlink(missing_ok=True)

def build(export, db, metadata):
    con = sqlite3.connect(db)
    try:
        con.executescript('''
        PRAGMA journal_mode=DELETE;
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE source_schema(type TEXT PRIMARY KEY, definition TEXT NOT NULL);
        CREATE TABLE objects(type TEXT NOT NULL,id TEXT NOT NULL,data TEXT NOT NULL,
          PRIMARY KEY(type,id));
        CREATE TABLE links(source_type TEXT,source_id TEXT,property TEXT,target_type TEXT,target_id TEXT);
        CREATE INDEX links_target ON links(target_type,target_id);
        CREATE VIEW agent_objects AS SELECT * FROM objects WHERE type IN ('''+','.join("'"+t+"'" for t in sorted(SAFE))+''');
        CREATE VIEW transactions AS SELECT id,
          json_extract(data,'$.name') AS name,
          replace(substr(json_extract(data,'$.dateString'),1,10),'.','-') AS date,
          json_extract(data,'$.date.$date') AS timestamp_utc,
          json_extract(data,'$.type') AS source_type,
          json_extract(data,'$.price') AS price,
          json_extract(data,'$.total') AS total,
          json_extract(data,'$.currency.id') AS currency,
          json_extract(data,'$.account.id') AS account_id,
          json_extract(data,'$.project.id') AS project_id,
          json_extract(data,'$.classification.id') AS classification_id,
          json_extract(data,'$.isDeleted') AS is_deleted,
          json_extract(data,'$.isEvent') AS is_event,
          json_extract(data,'$.isEnabled') AS is_enabled,
          data AS source_json
          FROM objects WHERE type='AHRecord';
        PRAGMA user_version=1;
        ''')
        with export.open() as f:
            header=json.loads(next(f))
            if header.get('kind')!='schema' or header.get('version')!=1: raise ValueError('Unsupported export')
            schemas=validate_schema(header.get('realm_schema_version'), header['schemas'])
            for name,s in schemas.items(): con.execute('INSERT INTO source_schema VALUES(?,?)',(name,json.dumps(s)))
            def links(v,t,i,p):
                if isinstance(v,dict):
                    if '$ref' in v:
                        con.execute('INSERT INTO links VALUES(?,?,?,?,?)',(t,i,p,v['$ref'],json.dumps(v['id'],ensure_ascii=False)))
                    else:
                        for k,x in v.items(): links(x,t,i,p+'.'+k)
                elif isinstance(v,list):
                    for k,x in enumerate(v): links(x,t,i,p+'['+str(k)+']')
            for line in f:
                r=json.loads(line);t=r['type'];i=json.dumps(r['id'],ensure_ascii=False)
                if t not in schemas or set(r['data'])!=set(schemas[t]['properties']): raise ValueError('Schema mismatch')
                con.execute('INSERT INTO objects VALUES(?,?,?)',(t,i,json.dumps(r['data'],ensure_ascii=False,allow_nan=False)))
                links(r['data'],t,i,'')
        missing=con.execute('SELECT count(*) FROM links l LEFT JOIN objects o ON l.target_type=o.type AND l.target_id=o.id WHERE o.type IS NULL').fetchone()[0]
        if missing: raise ValueError('Unresolved Realm object references')
        metadata.update(schema_version=1,realm_schema_version=header['realm_schema_version'],imported_at=dt.datetime.now(dt.timezone.utc).isoformat())
        for k,v in metadata.items(): con.execute('INSERT INTO metadata VALUES(?,?)',(k,json.dumps(v)))
        con.commit()
        if con.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('SQLite integrity check failed')
        return dict(con.execute('SELECT type,count(*) FROM objects GROUP BY type'))
    finally:
        con.close()

def sync(source,data,node,mirror=None):
    import zipfile
    source=source.resolve();data=data.resolve();mirror=mirror.resolve() if mirror else None
    validate_paths(source,data,mirror)
    private_dir(data)
    lock_path=data/'sync.lock'
    if lock_path.is_symlink(): raise ValueError('Invalid lock path')
    with lock_path.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        candidates=[(stamp(p),p) for p in source.iterdir() if stamp(p) and not p.is_symlink() and p.is_file()]
        if not candidates: raise ValueError('No downloaded MOZE 4 backups found')
        date,latest=max(candidates)
        for p in source.iterdir():
            if p.name.startswith('.') and p.name.endswith('.icloud'):
                pending=stamp(Path(p.name[1:-7]))
                if pending and pending>=date: raise ValueError('Newest iCloud backup is not downloaded')
        current=data/'finance.sqlite3'
        old={}
        if current.exists():
            with closing(sqlite3.connect(current.as_uri()+'?mode=ro',uri=True)) as c:
                old={k:json.loads(v) for k,v in c.execute('SELECT key,value FROM metadata')}
            if date<old['backup_time']: raise ValueError('Refusing source rollback')
        with tempfile.TemporaryDirectory(prefix='.import-',dir=data) as tmp:
            tmp=Path(tmp); archive=tmp/'source.zip'; before=latest.stat()
            shutil.copyfile(latest,archive)
            after=latest.stat()
            sha=digest(archive)
            if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns) or digest(latest)!=sha:
                raise ValueError('Source changed during read; retry later')
            if sha==old.get('sha256'):
                # Existing snapshots may have been produced by an older importer.
                with closing(sqlite3.connect(current.as_uri()+'?mode=ro',uri=True)) as c:
                    definitions=[json.loads(row[0]) for row in c.execute('SELECT definition FROM source_schema')]
                    validate_schema(old.get('realm_schema_version'), definitions)
                if mirror:
                    private_dir(mirror); atomic_copy(current,mirror/(sha+'.sqlite3'))
                return {'status':'unchanged','backup_time':date,'sha256':sha}
            if date==old.get('backup_time'): raise ValueError('Same timestamp has different backup contents')
            with zipfile.ZipFile(archive) as z:
                entries=[i for i in z.infolist() if i.filename=='moze.realm']
                if len(entries)!=1 or entries[0].file_size>512*1024*1024: raise ValueError('Invalid or oversized Realm member')
                # Never extract archive paths. Only copy the known member to a fixed path.
                with z.open(entries[0]) as inp,(tmp/'moze.realm').open('wb') as out: shutil.copyfileobj(inp,out)
            subprocess.run([node,str(REPO/'converter/export.mjs'),str(tmp/'moze.realm'),str(tmp/'export.jsonl')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=180)
            counts=build(tmp/'export.jsonl',tmp/'new.sqlite3',{'backup_time':date,'sha256':sha})
            private_dir(data/'archives'); private_dir(data/'snapshots')
            atomic_copy(archive,data/'archives'/(sha+'.zip'))
            atomic_copy(tmp/'new.sqlite3',data/'snapshots'/(sha+'.sqlite3'))
            # Publishing only happens after all validation; failed imports leave current intact.
            atomic_copy(tmp/'new.sqlite3',current)
            if mirror:
                private_dir(mirror); atomic_copy(current,mirror/(sha+'.sqlite3'))
            return {'status':'updated','backup_time':date,'sha256':sha,'counts':counts}

def main():
    os.umask(0o077)
    p=argparse.ArgumentParser()
    p.add_argument('--source',type=Path,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--node',default='node');p.add_argument('--mirror',type=Path)
    a=p.parse_args()
    try: emit(sync(a.source,a.data_dir,a.node,a.mirror))
    except Exception:
        print(json.dumps({'api_version':1,'ok':False,'error':{'code':'SYNC_FAILED','message':'Import or mirror failed. Source unchanged; current DB is either previous or fully validated. Check local paths, dependencies, available backups and storage.'}}))
        raise SystemExit(1)

if __name__=='__main__': main()
