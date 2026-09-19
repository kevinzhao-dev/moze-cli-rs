"""Reproducible synthetic ledger for discovery workflow acceptance and agent evaluation."""
import json
from pathlib import Path
from analytics_fixture import SEMANTICS, build_snapshot, record, ref


def prepare(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    db = directory/'synthetic.sqlite3'
    profile = directory/'semantics.json'
    extra = {
        'AHCategory': [
            {'identifier': 'home', 'name': '居家', 'isDeleted': False},
            {'identifier': 'travel', 'name': '旅行', 'isDeleted': False},
        ],
        'AHClassification': [
            {'identifier': identifier, 'name': name, 'isDeleted': False, 'isArchived': archived,
             'category': ref('AHCategory', category)}
            for identifier, name, category, archived in [
                ('home-power', '電費', 'home', False), ('home-water', '水費', 'home', False),
                ('home-rent', '房租', 'home', False), ('home-unused', '瓦斯費', 'home', False),
                ('home-old', '舊電話費', 'home', True), ('travel-power', '電費', 'travel', False),
            ]
        ],
        'AHAccount': [{'identifier': 'living', 'name': '生活帳戶', 'isDeleted': False,
                       'mainCurrency': ref('AHCurrency', 'USD')}],
        'AHTag': [{'identifier': f'tag-{i}', 'text': name, 'isDeleted': False, 'relatedID': 'unknown'}
                  for i, name in enumerate(['居家', '必要', '未用'])],
    }
    rows = []
    for identifier, name, amount, day, subcategory, store, tags, refund in [
        ('p1', '電費', 40, '2026.01.15', 'home-power', '北方公用', ['居家', '必要', '居家'], False),
        ('p2', '電費', 60, '2026.02.15', 'home-power', '北方公用', '["居家"]', False),
        ('p3', '電費', -5, '2026.03.15', 'home-power', '北方公用', ['居家'], True),
        ('w1', '水費', 20, '2026.01.16', 'home-water', '南方公用', [], False),
        ('r1', '房租', 200, '2026.01.01', 'home-rent', '', '居家|必要', False),
        ('t1', '電費', 300, '2026.01.20', 'travel-power', '北方公用', ['旅行'], False),
    ]:
        rows.append(record(identifier, amount, name=name, dateString=day, store=store, tags=tags,
                           isRefund=refund, classification=ref('AHClassification', subcategory),
                           account=ref('AHAccount', 'living' if subcategory.startswith('home-') else 'checking')))
    rows.append(record('disabled', 999, name='電費', store='北方公用', isEnabled=False,
                       classification=ref('AHClassification', 'home-power'), account=ref('AHAccount', 'living')))
    build_snapshot(db, rows, extra_entities=extra)
    profile.write_text(json.dumps(SEMANTICS))
    return db, profile


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    db, profile = prepare(parser.parse_args().directory)
    print(json.dumps({'db': str(db), 'semantics': str(profile)}))
