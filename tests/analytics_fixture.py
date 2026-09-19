"""Small synthetic snapshots; no values or identifiers from private backups."""
import json
import sqlite3
from contextlib import closing

SEMANTICS = {
    'version': 'synthetic-v1',
    'evidence': 'Synthetic fixture enum contract only; not MOZE enum documentation.',
    'expense_types': [71],
    'income_types': [72],
    'amount_field': 'total',
    'amount_convention': 'magnitude',
    'refund_policy': 'flag_magnitude',
}


def ref(collection, identifier):
    return {'$ref': collection, 'id': identifier}


def record(identifier, amount=10, **overrides):
    result = {
        'identifier': identifier, 'name': 'Synthetic electricity', 'store': 'Synthetic utility',
        'desc': '', 'total': amount, 'price': amount, 'type': 71,
        'dateString': '2026.01.15-18:00:00', 'isDeleted': False, 'isEnabled': True,
        'isEvent': False, 'isRefund': False, 'transferID': None,
        'isTransferIn': False, 'eventType': 0, 'happenType': 0,
        'currency': ref('AHCurrency', 'USD'),
        'account': ref('AHAccount', 'checking'),
        'classification': ref('AHClassification', 'electricity'),
        'project': None, 'tags': '',
    }
    result.update(overrides)
    return result


def build_snapshot(path, records=None, snapshot='synthetic-snapshot-1', extra_entities=None):
    entities = {
        'AHCurrency': [
            {'code': 'USD', 'decimalPlace': 2, 'isDeleted': False},
            {'code': 'JPY', 'decimalPlace': 0, 'isDeleted': False},
        ],
        'AHCategory': [
            {'identifier': 'utilities', 'name': 'Synthetic utilities', 'isDeleted': False},
            {'identifier': 'food', 'name': 'Synthetic food', 'isDeleted': False},
        ],
        'AHClassification': [
            {'identifier': 'electricity', 'name': '電費', 'isDeleted': False,
             'category': ref('AHCategory', 'utilities')},
            {'identifier': 'lunch', 'name': 'Synthetic lunch', 'isDeleted': False,
             'category': ref('AHCategory', 'food')},
        ],
        'AHAccount': [
            {'identifier': 'checking', 'name': 'Synthetic checking', 'isDeleted': False,
             'mainCurrency': ref('AHCurrency', 'USD')},
        ],
        'AHProject': [],
        'AHTag': [],
        'AHRecord': records if records is not None else [record('expense')],
    }
    for kind, values in (extra_entities or {}).items():
        entities.setdefault(kind, []).extend(values)
    with closing(sqlite3.connect(path)) as con, con:
        con.executescript('''
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE source_schema(type TEXT PRIMARY KEY, definition TEXT NOT NULL);
            CREATE TABLE objects(type TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                                 PRIMARY KEY(type,id));
            CREATE VIEW agent_objects AS SELECT * FROM objects WHERE type IN
                ('AHCurrency','AHCategory','AHClassification','AHAccount','AHProject','AHTag','AHRecord');
        ''')
        for key, value in {
            'sha256': snapshot, 'backup_time': '2026-03-31T12:00:00',
            'imported_at': '2026-03-31T04:30:00+00:00',
            'schema_version': 1, 'realm_schema_version': 202,
        }.items():
            con.execute('INSERT INTO metadata VALUES (?,?)', (key, json.dumps(value)))
        for kind, rows in entities.items():
            con.execute('INSERT INTO source_schema VALUES (?,?)', (kind, json.dumps({
                'name': kind, 'primaryKey': 'code' if kind == 'AHCurrency' else 'identifier',
                'properties': {},
            })))
            for row in rows:
                identifier = row.get('identifier', row.get('code'))
                con.execute('INSERT INTO objects VALUES (?,?,?)',
                            (kind, json.dumps(identifier), json.dumps(row)))
        con.execute('INSERT INTO objects VALUES (?,?,?)',
                    ('AHAppConfig', '"config"', '{"password":"SYNTHETIC_SECRET"}'))
