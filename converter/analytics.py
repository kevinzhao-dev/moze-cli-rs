#!/usr/bin/env python3
"""Bounded, read-only financial analytics over the allowlisted snapshot view."""
import argparse
import base64
from collections import Counter, defaultdict
import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

MAX_SCAN = 250000
EXCLUSION_SCOPE = (
    'Matching records; invalid_date is snapshot-wide. unknown_tag_match_* counts '
    'otherwise-matching excluded records whose tag encoding prevents determining tag membership.'
)
KINDS = {'category': 'AHCategory', 'subcategory': 'AHClassification',
         'account': 'AHAccount', 'project': 'AHProject', 'currency': 'AHCurrency',
         'tag': 'AHTag'}
GROUPS = set(KINDS) - {'currency', 'tag'} | {'store', 'month', 'day'}
METRICS = {'gross_expense', 'refund', 'net_expense', 'count'}
FILTERS = {'query', 'category_ids', 'subcategory_ids', 'account_ids', 'project_ids',
           'currency_codes', 'include_descendants', 'names', 'stores', 'tags', 'tags_mode'}
SORTS = {
    'summarize_spending': {'dimension', 'net_expense', 'source_total', 'count'},
    'compare_spending': {'dimension', 'net_expense_difference', 'source_total_difference', 'count_difference'},
    'search_transactions': {'date_desc', 'date_asc', 'amount_desc'},
}


def rank_groups(groups, sort_by, totals_key='totals'):
    """Rank before truncation, never compare values of different currencies."""
    if sort_by == 'dimension':
        return groups
    totals = [total for group in groups for total in group[totals_key]]
    if sort_by not in ('count', 'count_difference'):
        if len({t['currency'] for t in totals}) > 1:
            fail('Monetary sorting requires a single currency; use filters.currency_codes')
    if any(t[sort_by] is None for t in totals):
        fail('Requested sorting metric is unavailable; inspect unknown amounts and semantics', 'UNAVAILABLE_METRIC')

    def magnitude(group):
        value = sum((Decimal(str(t[sort_by])) for t in group[totals_key]), Decimal(0))
        return abs(value) if totals_key == 'differences' else value

    return sorted(groups, key=magnitude, reverse=True)


class AnalyticsError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code.upper()


def fail(message, code='invalid_argument'):
    raise AnalyticsError(code, message)


def date(value):
    if not isinstance(value, str):
        fail('Dates must be YYYY-MM-DD strings')
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        fail('Dates must be valid YYYY-MM-DD strings')
    if parsed.isoformat() != value:
        fail('Dates must be YYYY-MM-DD strings')
    return value


def keys(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        fail('Unknown fields or invalid object')


def string(value, label):
    if not isinstance(value, str) or not value or len(value) > 512:
        fail(label + ' must be a nonempty string of at most 512 characters')
    return value


def strings(value, label):
    if not isinstance(value, list) or len(value) > 100:
        fail(label + ' must be an array of at most 100 strings')
    if label in ('names', 'stores', 'tags'):
        # Source literals have no 512-character schema restriction. The transports
        # bound the complete request; identifiers and search terms stay compact.
        if any(not isinstance(v, str) or not v for v in value):
            fail(label + ' must contain nonempty strings')
        return value
    return [string(v, label) for v in value]


def parse_tags(raw):
    """Only decode explicit arrays; MOZE's opaque strings have no assumed delimiter."""
    if raw is None or raw == '':
        return [], 'empty'
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):
            return [], 'unverified'
    if not isinstance(raw, list) or any(not isinstance(tag, str) or not tag for tag in raw):
        return [], 'unverified'
    return sorted(set(raw)), 'verified' if raw else 'empty'


def token(value):
    result = base64.urlsafe_b64encode(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).decode()
    if len(result) > 1000000:
        fail('Query token exceeds supported size')
    return result


def untoken(value):
    if not isinstance(value, str) or len(value) > 1000000:
        fail('Invalid cursor or drilldown token')
    try:
        result = json.loads(base64.b64decode(value, altchars=b'-_', validate=True))
    except (ValueError, UnicodeError):
        fail('Invalid cursor or drilldown token')
    if not isinstance(result, dict):
        fail('Invalid cursor or drilldown token')
    return result


def semantics_config(value):
    if value is None:
        return None
    if isinstance(value, (str, Path)):
        try:
            value = json.loads(Path(value).read_text())
        except (OSError, ValueError):
            fail('Cannot read semantics configuration', 'invalid_semantics')
    required = {'version', 'evidence', 'expense_types', 'income_types', 'amount_field',
                'amount_convention', 'refund_policy'}
    keys(value, required)
    if set(value) != required:
        fail('Semantics configuration requires all documented fields', 'invalid_semantics')
    string(value['version'], 'version')
    string(value['evidence'], 'evidence')
    for key in ('expense_types', 'income_types'):
        if not isinstance(value[key], list) or any(type(x) is not int for x in value[key]):
            fail('Source enum mappings must be integer arrays', 'invalid_semantics')
    if set(value['expense_types']) & set(value['income_types']):
        fail('Expense and income mappings must be disjoint', 'invalid_semantics')
    if (value['amount_field'] not in ('total', 'price') or value['amount_convention'] != 'magnitude'
            or value['refund_policy'] != 'flag_magnitude'):
        fail('Unsupported monetary semantics', 'invalid_semantics')
    return value


class Snapshot:
    def __init__(self, path, semantics):
        self.sem = semantics_config(semantics)
        self.con = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
        self.con.execute('PRAGMA query_only=ON')
        self.con.execute('BEGIN')
        try:
            self.meta = {k: json.loads(v) for k, v in self.con.execute('SELECT key,value FROM metadata')}
            self.entities = {}
            for kind, collection in KINDS.items():
                rows = self.con.execute('SELECT id,data FROM agent_objects WHERE type=? LIMIT ?', (collection, MAX_SCAN + 1)).fetchall()
                if len(rows) > MAX_SCAN:
                    fail('Entity scan limit exceeded', 'scan_limit')
                self.entities[kind] = {str(json.loads(i)): json.loads(v) for i, v in rows}
        except Exception:
            self.con.close()
            raise

    def close(self):
        self.con.close()

    def provenance(self, args):
        sha = self.meta.get('sha256')
        if not isinstance(sha, str) or not sha:
            fail('Snapshot provenance is missing', 'snapshot_error')
        if args.get('snapshot_id') is not None and args['snapshot_id'] != sha:
            fail('Snapshot changed; restart analysis', 'snapshot_mismatch')
        return {'snapshot_id': sha, 'backup_time': self.meta.get('backup_time'),
                'date_basis': 'MOZE dateString local calendar date; inclusive endpoints',
                'semantics_version': self.sem['version'] if self.sem else 'unverified',
                'semantics_fingerprint': self.fingerprint(),
                'coverage': {'snapshot_scan_complete': False, 'bank_completeness': 'unknown'},
                'exclusion_scope': EXCLUSION_SCOPE,
                'warnings': ([] if self.sem else ['Source enum and amount semantics are unverified; expense metrics are unavailable.'])}

    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.sem, sort_keys=True).encode()).hexdigest()

    def entity(self, kind, ref):
        if not isinstance(ref, dict) or ref.get('$ref') != KINDS[kind]:
            return None
        identifier = str(ref.get('id'))
        data = self.entities[kind].get(identifier)
        return {'id': identifier, 'name': data.get('text' if kind == 'tag' else 'name', identifier) if data else None}

    def normalize(self, identifier, r):
        sub = self.entity('subcategory', r.get('classification'))
        subdata = self.entities['subcategory'].get(sub['id'], {}) if sub else {}
        currency = self.entity('currency', r.get('currency'))
        result = {'id': str(json.loads(identifier)), 'name': r.get('name'), 'store': r.get('store'),
                  'description': r.get('desc'), 'source_type': r.get('type'),
                  'category': self.entity('category', subdata.get('category')), 'subcategory': sub,
                  'account': self.entity('account', r.get('account')),
                  'project': self.entity('project', r.get('project')),
                  'currency': currency['id'] if currency else None,
                  'is_refund': r.get('isRefund'), 'transfer_id': r.get('transferID'),
                  'source_price': str(r.get('price')), 'source_total': str(r.get('total')),
                  'event_type': r.get('eventType'), 'happen_type': r.get('happenType')}
        raw = r.get('dateString', '')
        result['tags'], result['tags_status'] = parse_tags(r.get('tags'))
        try:
            result['date'] = date(raw[:10].replace('.', '-'))
        except (AnalyticsError, AttributeError):
            result['date'] = None
        return result

    def records(self, args):
        period = args.get('period')
        if period is not None:
            keys(period, {'from', 'to'})
            if set(period) != {'from', 'to'} or date(period['from']) > date(period['to']):
                fail('period requires ordered from and to dates')
        filters = args.get('filters', {})
        keys(filters, FILTERS)
        if filters.get('include_descendants') is False:
            fail('Categories contain subcategories; include_descendants=false is unsupported')
        if 'tags_mode' in filters and 'tags' not in filters:
            fail('tags_mode requires tags')
        for k, v in filters.items():
            if k == 'include_descendants':
                if type(v) is not bool:
                    fail('include_descendants must be boolean')
            elif k == 'query':
                string(v, 'query')
            elif k == 'tags_mode':
                if not isinstance(v, str) or v not in ('any', 'all'):
                    fail('tags_mode must be any or all')
            else:
                strings(v, k)
        as_of = date(args.get('as_of', str(self.meta.get('backup_time', ''))[:10]))
        excluded = Counter()
        rows = self.con.execute("SELECT id,data FROM agent_objects WHERE type='AHRecord' ORDER BY id LIMIT ?", (MAX_SCAN + 1,)).fetchall()
        if len(rows) > MAX_SCAN:
            fail('Record scan limit exceeded; no partial total returned', 'scan_limit')
        out = []
        for identifier, raw in rows:
            r = json.loads(raw, parse_float=Decimal)
            n = self.normalize(identifier, r)
            if n['date'] is None:
                excluded['invalid_date'] += 1
                continue
            if period and not period['from'] <= n['date'] <= period['to']:
                continue
            if not self.matches(n, filters, check_tags=False):
                continue
            if 'tags' in filters and (not filters['tags'] or n['tags_status'] != 'unverified'):
                if not self.matches(n, filters):
                    continue
            reason = ('deleted' if r.get('isDeleted') is not False else
                      'disabled' if r.get('isEnabled') is not True else
                      'event_template' if r.get('isEvent') is not False else
                      'future' if n['date'] > as_of else
                      'transfer' if r.get('transferID') or r.get('isTransferIn') is True else None)
            if reason:
                if 'tags' in filters and n['tags_status'] == 'unverified':
                    excluded['unknown_tag_match_' + reason] += 1
                else:
                    excluded[reason] += 1
                continue
            if not self.matches(n, filters):
                continue
            kind = 'unknown'
            if self.sem and type(r.get('type')) is int:
                if r['type'] in self.sem['expense_types']:
                    kind = 'refund' if r.get('isRefund') is True else 'expense' if r.get('isRefund') is False else 'unknown'
                elif r['type'] in self.sem['income_types']:
                    kind = 'income'
            n['kind'] = kind
            try:
                amount = Decimal(str(r.get(self.sem['amount_field'] if self.sem else 'total')))
                if not amount.is_finite() or abs(amount) >= Decimal('1e18'):
                    raise InvalidOperation
                n['_amount'] = amount
            except (InvalidOperation, ValueError):
                n['_amount'] = None
                n['kind'] = 'unknown'
            out.append(n)
        return out, dict(excluded), as_of

    @staticmethod
    def matches(n, filters, check_tags=True):
        for key, field in [('category_ids', 'category'), ('subcategory_ids', 'subcategory'),
                           ('account_ids', 'account'), ('project_ids', 'project')]:
            if key in filters and (n[field] or {}).get('id') not in filters[key]:
                return False
        if 'currency_codes' in filters and n['currency'] not in filters['currency_codes']:
            return False
        for key, field in [('names', 'name'), ('stores', 'store')]:
            if key in filters and n[field] not in filters[key]:
                return False
        if 'query' in filters:
            texts = [n.get('name'), n.get('store'), n.get('description'),
                     (n['category'] or {}).get('name'), (n['subcategory'] or {}).get('name')]
            if filters['query'].casefold() not in ' '.join(x for x in texts if isinstance(x, str)).casefold():
                return False
        if check_tags and 'tags' in filters:
            if not filters['tags']:
                return False
            if n['tags_status'] == 'unverified':
                fail('Eligible records have unsupported tag encodings; exact tag filtering cannot be complete',
                     'UNVERIFIED_TAG_ENCODING')
            selected = set(filters['tags'])
            available = set(n['tags'])
            if filters.get('tags_mode', 'any') == 'all':
                if not selected <= available:
                    return False
            elif not selected & available:
                return False
        return True

    def list_entities(self, args, provenance):
        """Enumerate declared entities, preserving unused values and source hierarchy."""
        request = dict(args)
        offset = 0
        if 'cursor' in request:
            keys(request, {'cursor', 'limit', 'snapshot_id'})
            state = untoken(request['cursor'])
            keys(state, {'tool', 'request', 'snapshot_id', 'semantics_fingerprint', 'offset'})
            if state.get('tool') != 'list_entities':
                fail('Cursor belongs to another tool')
            if state.get('semantics_fingerprint') != self.fingerprint():
                fail('Semantics changed; restart discovery', 'semantics_mismatch')
            if request.get('snapshot_id') and request['snapshot_id'] != state.get('snapshot_id'):
                fail('Conflicting snapshots', 'snapshot_mismatch')
            if state.get('snapshot_id') != provenance['snapshot_id']:
                fail('Snapshot changed; restart discovery', 'snapshot_mismatch')
            if not isinstance(state.get('request'), dict):
                fail('Invalid cursor request')
            limit_override = request.get('limit')
            request = dict(state['request'])
            keys(request, {'kind', 'parent_id', 'query', 'include_unused', 'include_archived', 'period', 'as_of', 'limit', 'snapshot_id'})
            request['snapshot_id'] = state.get('snapshot_id')
            if limit_override is not None:
                request['limit'] = limit_override
            offset = state.get('offset')
            if type(offset) is not int or not 0 <= offset <= MAX_SCAN:
                fail('Invalid cursor offset')
            provenance = self.provenance(request)
        kind = request.get('kind')
        if not isinstance(kind, str) or kind not in KINDS:
            fail('kind must be a supported entity kind')
        limit = request.get('limit', 20)
        if type(limit) is not int or not 1 <= limit <= 100:
            fail('limit must be an integer from 1 to 100')
        for option in ('include_unused', 'include_archived'):
            if option in request and type(request[option]) is not bool:
                fail(option + ' must be boolean')
        if 'parent_id' in request:
            string(request['parent_id'], 'parent_id')
            if kind != 'subcategory':
                fail('parent_id is supported only for subcategories')
        query_text = string(request['query'], 'query').casefold() if 'query' in request else None
        rows, excluded, as_of = self.records({k: request[k] for k in ('period', 'as_of') if k in request})
        usage = Counter()
        if kind != 'tag':
            for row in rows:
                value = row.get(kind)
                identifier = value if kind == 'currency' else (value or {}).get('id')
                if identifier is not None:
                    usage[identifier] += 1
        elif request.get('include_unused') is False:
            fail('Tag ownership is unverified; include_unused=false cannot determine declared tag usage', 'UNVERIFIED_TAG_LINKS')
        items = []
        for identifier, obj in self.entities[kind].items():
            if obj.get('isDeleted') is True:
                continue
            flags = {field: obj[field] for field in ('isHidden', 'isArchived') if field in obj}
            archived = any(value is True for value in flags.values())
            if archived and not request.get('include_archived', False):
                continue
            name = obj.get('text', obj.get('name', identifier)) if kind == 'tag' else obj.get('name', identifier)
            if query_text is not None and query_text not in str(name).casefold():
                continue
            parent = self.entity('category', obj.get('category')) if kind == 'subcategory' else None
            if 'parent_id' in request and (parent or {}).get('id') != request['parent_id']:
                continue
            count = None if kind == 'tag' else usage[identifier]
            if request.get('include_unused', True) is False and count == 0:
                continue
            items.append({'kind': kind, 'id': identifier, 'name': name, 'parent': parent,
                          'is_archived': archived, 'archive_source_flags': flags, 'usage_count': count})
        items.sort(key=lambda item: (str(item['name']).casefold(), item['id']))
        pinned = {**request, 'as_of': as_of, 'snapshot_id': provenance['snapshot_id']}
        next_state = {'tool': 'list_entities', 'request': pinned, 'snapshot_id': provenance['snapshot_id'],
                      'semantics_fingerprint': self.fingerprint(), 'offset': offset + limit}
        return {**provenance, 'coverage': {**provenance['coverage'], 'snapshot_scan_complete': True},
                'items': items[offset:offset + limit], 'total_count': len(items),
                'warnings': provenance['warnings'] + (['Declared tag usage is unknown; tag-to-transaction ownership is unverified.'] if kind == 'tag' else []),
                'next_cursor': token(next_state) if offset + limit < len(items) else None,
                'as_of': as_of, 'effective_period': request.get('period'), 'excluded_counts': excluded,
                'usage_definition': 'Eligible source record count; declared tag usage is unknown because ownership is unverified.',
                'archive_definition': 'Source isHidden or isArchived is true; missing flags do not assert source archival status.'}

    def facets(self, args, provenance):
        """Discover observed values while retaining every requested filter."""
        request = dict(args)
        offset = 0
        if 'cursor' in request:
            keys(request, {'cursor', 'limit', 'snapshot_id'})
            state = untoken(request['cursor'])
            keys(state, {'tool', 'request', 'snapshot_id', 'semantics_fingerprint', 'offset'})
            if state.get('tool') != 'get_facets':
                fail('Cursor belongs to another tool')
            if state.get('semantics_fingerprint') != self.fingerprint():
                fail('Semantics changed; restart discovery', 'semantics_mismatch')
            if request.get('snapshot_id') and request['snapshot_id'] != state.get('snapshot_id'):
                fail('Conflicting snapshots', 'snapshot_mismatch')
            if state.get('snapshot_id') != provenance['snapshot_id']:
                fail('Snapshot changed; restart discovery', 'snapshot_mismatch')
            if not isinstance(state.get('request'), dict):
                fail('Invalid cursor request')
            limit_override = request.get('limit')
            request = dict(state['request'])
            keys(request, {'period', 'filters', 'dimensions', 'limit', 'as_of', 'snapshot_id'})
            request['snapshot_id'] = state.get('snapshot_id')
            if limit_override is not None:
                request['limit'] = limit_override
            offset = state.get('offset')
            if type(offset) is not int or not 0 <= offset <= MAX_SCAN:
                fail('Invalid cursor offset')
            provenance = self.provenance(request)
        if not request.get('period'):
            fail('period is required')
        dimensions = request.get('dimensions')
        allowed = {'category', 'subcategory', 'account', 'project', 'currency', 'name', 'store', 'tag'}
        if (not isinstance(dimensions, list) or not 1 <= len(dimensions) <= 5
                or any(not isinstance(d, str) or d not in allowed for d in dimensions)
                or len(set(dimensions)) != len(dimensions)):
            fail('dimensions must contain one to five distinct supported dimensions')
        limit = request.get('limit', 20)
        if type(limit) is not int or not 1 <= limit <= 100:
            fail('limit must be an integer from 1 to 100')
        rows, excluded, as_of = self.records(request)
        facets = {}
        has_more = False
        fields = {'category': 'category_ids', 'subcategory': 'subcategory_ids',
                  'account': 'account_ids', 'project': 'project_ids', 'currency': 'currency_codes',
                  'name': 'names', 'store': 'stores', 'tag': 'tags'}
        for dimension in dimensions:
            counts = Counter()
            values = {}
            missing = 0
            unknown = 0
            for row in rows:
                if dimension == 'tag':
                    if row.get('tags_status') == 'unverified':
                        unknown += 1
                        continue
                    entries = row.get('tags', [])
                    if not isinstance(entries, list):
                        unknown += 1
                        continue
                else:
                    entries = [row.get(dimension)]
                usable = [entry for entry in entries if entry is not None and entry != '']
                if not usable:
                    missing += 1
                seen = set()
                for value in usable:
                    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
                    if encoded not in seen:
                        counts[encoded] += 1
                        values[encoded] = value
                        seen.add(encoded)
            ordered = sorted(counts, key=lambda key: (-counts[key], key))
            has_more = has_more or offset + limit < len(ordered)
            page = []
            for encoded in ordered[offset:offset + limit]:
                value = values[encoded]
                filter_value = value.get('id') if isinstance(value, dict) else value
                page.append({'value': value, 'count': counts[encoded], 'filter': {fields[dimension]: [filter_value]}})
            facets[dimension] = {'values': page, 'missing_count': missing, 'unknown_count': unknown,
                                 'distinct_count': len(ordered), 'truncated': offset + limit < len(ordered)}
        pinned = {**request, 'as_of': as_of, 'snapshot_id': provenance['snapshot_id']}
        next_state = {'tool': 'get_facets', 'request': pinned, 'snapshot_id': provenance['snapshot_id'],
                      'semantics_fingerprint': self.fingerprint(), 'offset': offset + limit}
        return {**provenance, 'coverage': {**provenance['coverage'], 'snapshot_scan_complete': True},
                'facets': facets, 'matching_count': len(rows), 'excluded_counts': excluded,
                'warnings': provenance['warnings'] + ([f"{facets['tag']['unknown_count']} eligible records have unsupported tag encodings; tag options and counts are incomplete."] if 'tag' in facets and facets['tag']['unknown_count'] else []),
                'as_of': as_of, 'effective_period': request['period'], 'effective_filters': request.get('filters', {}),
                'next_cursor': token(next_state) if has_more else None,
                'count_definition': 'Eligible source records per value, deduplicated within each record; multi-valued tag counts are not additive.',
                'filter_behavior': 'conjunctive: all requested filters remain applied to every dimension'}

    def money(self, value, currency):
        places = self.entities['currency'].get(currency, {}).get('decimalPlace')
        if type(places) is not int or not 0 <= places <= 8:
            return str(value)
        return format(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), 'f')

    def aggregate(self, rows):
        buckets = defaultdict(list)
        for row in rows:
            buckets[row['currency']].append(row)
        results = []
        for currency, items in sorted(buckets.items(), key=lambda x: str(x[0])):
            unknown = sum(r['kind'] == 'unknown' or r['_amount'] is None for r in items)
            expense = sum((abs(r['_amount']) for r in items if r['kind'] == 'expense' and r['_amount'] is not None), Decimal(0))
            refund = sum((abs(r['_amount']) for r in items if r['kind'] == 'refund' and r['_amount'] is not None), Decimal(0))
            scale = self.entities['currency'].get(currency, {}).get('decimalPlace')
            scale_known = type(scale) is int and 0 <= scale <= 8
            valid = self.sem is not None and unknown == 0 and currency is not None and scale_known
            results.append({'currency': currency, 'count': len(items), 'unknown_count': unknown,
                            'invalid_amount_count': sum(r['_amount'] is None for r in items),
                            'currency_scale_verified': scale_known,
                            'source_total': self.money(sum((r['_amount'] for r in items if r['_amount'] is not None), Decimal(0)), currency) if all(r['_amount'] is not None for r in items) else None,
                            'source_total_definition': 'signed source amount; not spending',
                            'gross_expense': self.money(expense, currency) if valid else None,
                            'refund': self.money(refund, currency) if valid else None,
                            'net_expense': self.money(expense - refund, currency) if valid else None})
        return results

    @staticmethod
    def dimensions(row, groups):
        result = {}
        for g in groups:
            result[g] = row['date'][:7] if g == 'month' else row['date'] if g == 'day' else row[g]
        return result

    def drilldown(self, summary, dimensions):
        return token({'snapshot_id': summary['snapshot_id'],
                      'semantics_fingerprint': summary['semantics_fingerprint'],
                      'period': summary['effective_period'], 'filters': summary['effective_filters'],
                      'as_of': summary['as_of'], 'dimensions': dimensions})

    def summary(self, args, provenance, all_groups=False):
        rows, excluded, as_of = self.records(args)
        groups = args.get('group_by', ['category'])
        if not isinstance(groups, list) or len(groups) > 2 or any(not isinstance(g, str) or g not in GROUPS for g in groups) or len(set(groups)) != len(groups):
            fail('group_by must contain at most two supported distinct dimensions')
        metrics = args.get('metrics', sorted(METRICS))
        if not isinstance(metrics, list) or any(not isinstance(m, str) or m not in METRICS for m in metrics):
            fail('Unsupported metrics')
        buckets = defaultdict(list)
        for r in rows:
            buckets[json.dumps(self.dimensions(r, groups), sort_keys=True)].append(r)
        grouped = []
        for label, values in sorted(buckets.items()):
            dims = json.loads(label)
            grouped.append({'dimensions': dims, 'totals': self.aggregate(values)})
        # Comparisons rank their differences after forming the full group union.
        ordering = args.get('sort_by', 'dimension') if not all_groups else 'dimension'
        grouped = rank_groups(grouped, ordering)
        limit = len(grouped) if all_groups else args.get('limit', 20)
        remaining_labels = {json.dumps(g['dimensions'], sort_keys=True) for g in grouped[limit:]}
        result = {**provenance, 'coverage': {**provenance['coverage'], 'snapshot_scan_complete': True}, 'as_of': as_of, 'effective_period': args.get('period'),
                'effective_filters': args.get('filters', {}),
                'excluded_counts': excluded,
                'totals': self.aggregate(rows), 'groups': grouped[:limit], 'group_count': len(grouped),
                'groups_truncated': len(grouped) > limit,
                'remaining_totals': self.aggregate([r for label in remaining_labels for r in buckets[label]]),
                'sort_by': ordering,
                'requested_metrics': metrics, 'metrics_policy': 'Summary selects requested metrics; comparison retains core metrics needed for differences.', 'metric_definition': {'amount_field': self.sem['amount_field'] if self.sem else 'total',
                                      'refund_date': 'refund transaction date', 'rounding': 'sum then currency decimalPlace, HALF_UP',
                                      'count': 'eligible source records, including income and unknown kinds'}}
        if not all_groups:
            for group in result['groups']:
                group['drilldown_token'] = self.drilldown(result, group['dimensions'])
        return result


def _dispatch(db_path, tool_name, arguments, semantics=None):
    common = {'snapshot_id', 'as_of'}
    query = {'period', 'filters', 'limit'}
    allowed = {'get_context': common,
               'resolve_entities': common | {'query', 'kinds', 'limit'},
               'list_entities': common | {'kind', 'parent_id', 'query', 'include_unused', 'include_archived', 'period', 'limit', 'cursor'},
               'get_facets': common | query | {'dimensions', 'cursor'},
               'summarize_spending': common | query | {'group_by', 'metrics', 'sort_by'},
               'compare_spending': common | query | {'group_by', 'metrics', 'baseline_period', 'sort_by'},
               'search_transactions': common | query | {'cursor', 'drilldown_token', 'sort_by'},
               'get_transaction': common | {'id'}}
    if tool_name not in allowed:
        fail('Unknown analytics tool')
    keys(arguments, allowed[tool_name])
    args = dict(arguments)
    if 'sort_by' in args and (not isinstance(args['sort_by'], str) or args['sort_by'] not in SORTS.get(tool_name, set())):
        fail('Unsupported sort_by for this tool')
    if tool_name in ('summarize_spending', 'compare_spending') and not args.get('period'):
        fail('period is required')
    if 'limit' in args and (type(args['limit']) is not int or not 1 <= args['limit'] <= 100):
        fail('limit must be an integer from 1 to 100')
    if 'as_of' in args:
        date(args['as_of'])
    try:
        snapshot = Snapshot(db_path, semantics)
    except (sqlite3.Error, OSError, ValueError) as error:
        raise AnalyticsError('SNAPSHOT_ERROR', 'Cannot open a valid analytics snapshot') from error
    try:
        provenance = snapshot.provenance(args)
        if tool_name == 'list_entities':
            return snapshot.list_entities(args, provenance)
        if tool_name == 'get_facets':
            return snapshot.facets(args, provenance)
        if tool_name == 'get_context':
            observed, exclusions, as_of = snapshot.records(args)
            observed_dates = [r['date'] for r in observed]
            return {**provenance, 'coverage': {**provenance['coverage'], 'snapshot_scan_complete': True}, 'observed_eligible_date_range': {'from': min(observed_dates) if observed_dates else None, 'to': max(observed_dates) if observed_dates else None}, 'as_of': as_of, 'excluded_counts': exclusions, 'argument_fields': {name: sorted(fields) for name, fields in allowed.items()}, 'supported_tools': sorted(allowed), 'supported_group_by': sorted(GROUPS),
                    'supported_entity_kinds': sorted(KINDS),
                    'supported_facet_dimensions': sorted(set(KINDS) | {'name', 'store'}),
                    'supported_filters': sorted(FILTERS),
                    'supported_metrics': sorted(METRICS), 'currency_mode': 'separate',
                    'semantics': snapshot.sem, 'limits': {'scan_records': MAX_SCAN, 'result_limit': 100},
                    'balance_support': 'unvalidated; opening amounts and caches are not current balances'}
        if tool_name == 'resolve_entities':
            query_text = string(args.get('query'), 'query').casefold()
            kinds = args.get('kinds', list(KINDS))
            if not isinstance(kinds, list) or any(not isinstance(k, str) or k not in KINDS for k in kinds):
                fail('Unsupported entity kinds')
            candidates = []
            for kind in kinds:
                for identifier, obj in snapshot.entities[kind].items():
                    name = obj.get('text' if kind == 'tag' else 'name', identifier)
                    if obj.get('isDeleted') is True or query_text not in str(name).casefold():
                        continue
                    item = {'kind': kind, 'id': identifier, 'name': name,
                            'exact_match': query_text == str(name).casefold()}
                    if kind == 'subcategory':
                        item['category'] = snapshot.entity('category', obj.get('category'))
                    candidates.append(item)
            candidates.sort(key=lambda c: (not c['exact_match'], c['kind'], c['id']))
            return {**provenance, 'candidates': candidates[:args.get('limit', 20)],
                    'candidate_count': len(candidates), 'truncated': len(candidates) > args.get('limit', 20),
                    'ambiguous': len(candidates) > 1}
        if tool_name == 'summarize_spending':
            result = snapshot.summary(args, provenance)
            if 'metrics' in args:
                for totals in [result['totals'], result['remaining_totals']] + [g['totals'] for g in result['groups']]:
                    for total in totals:
                        for metric in METRICS - set(args['metrics']):
                            total.pop(metric, None)
            return result
        if tool_name == 'compare_spending':
            if not args.get('period') or not args.get('baseline_period'):
                fail('compare_spending requires period and baseline_period')
            current = snapshot.summary(args, provenance, all_groups=True)
            baseline = snapshot.summary({**args, 'period': args['baseline_period']}, provenance, all_groups=True)
            def difference(now, before):
                n = {t['currency']: t for t in now}
                b = {t['currency']: t for t in before}
                result = []
                for currency in sorted(n.keys() | b.keys(), key=str):
                    x, y = n.get(currency), b.get(currency)
                    a = x['net_expense'] if x else '0'
                    z = y['net_expense'] if y else '0'
                    delta = Decimal(a) - Decimal(z) if a is not None and z is not None else None
                    result.append({'currency': currency,
                                   'count_difference': (x['count'] if x else 0) - (y['count'] if y else 0),
                                   'net_expense_difference': snapshot.money(delta, currency) if delta is not None else None,
                                   'source_total_difference': snapshot.money(Decimal(x['source_total'] if x else '0') - Decimal(y['source_total'] if y else '0'), currency) if (x is None or x['source_total'] is not None) and (y is None or y['source_total'] is not None) else None,
                                   'source_total_definition': 'signed source amount difference; not spending',
                                   'percent_change': str((delta / abs(Decimal(z)) * 100).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
                                   if delta is not None and Decimal(z) != 0 else None})
                return result
            now_groups = {json.dumps(g['dimensions'], sort_keys=True): g for g in current['groups']}
            old_groups = {json.dumps(g['dimensions'], sort_keys=True): g for g in baseline['groups']}
            contributions = []
            for label in sorted(now_groups.keys() | old_groups.keys()):
                now = now_groups.get(label, {})
                old = old_groups.get(label, {})
                contributions.append({'dimensions': json.loads(label),
                                      'differences': difference(now.get('totals', []), old.get('totals', []))})
            contributions = rank_groups(contributions, args.get('sort_by', 'dimension'), 'differences')
            limit = args.get('limit', 20)
            contribution_count = len(contributions)
            contributions = contributions[:limit]
            for group in contributions:
                label = json.dumps(group['dimensions'], sort_keys=True)
                for prefix, summary, source_groups in [('current', current, now_groups), ('baseline', baseline, old_groups)]:
                    group[prefix + '_drilldown_token'] = (
                        snapshot.drilldown(summary, group['dimensions']) if label in source_groups else None)
            for summary in (current, baseline):
                summary['groups_truncated'] = len(summary['groups']) > limit
                summary['groups'] = summary['groups'][:limit]
                for group in summary['groups']:
                    group['drilldown_token'] = snapshot.drilldown(summary, group['dimensions'])
                summary.pop('remaining_totals', None)
            return {**provenance, 'coverage': current['coverage'], 'current': current, 'baseline': baseline,
                    'contribution_order': args.get('sort_by', 'dimension'),
                    'contributions': contributions, 'contribution_count': contribution_count,
                    'contributions_truncated': contribution_count > limit,
                    'differences': difference(current['totals'], baseline['totals']),
                    'comparison_warning': 'Periods are explicit; partial periods and seasonality require interpretation.'}
        if tool_name == 'get_transaction':
            identifier = string(args.get('id'), 'id')
            row = snapshot.con.execute("SELECT id,data FROM agent_objects WHERE type='AHRecord' AND id=?",
                                       (json.dumps(identifier, ensure_ascii=False),)).fetchone()
            if row is None:
                fail('Transaction not found', 'not_found')
            raw = json.loads(row[1], parse_float=Decimal)
            if raw.get('isDeleted') is not False:
                fail('Transaction not found', 'not_found')
            normalized = snapshot.normalize(row[0], raw)
            normalized.update(is_enabled=raw.get('isEnabled'), is_event=raw.get('isEvent'))
            eligible, exclusions, as_of = snapshot.records(args)
            matched = next((r for r in eligible if r['id'] == identifier), None)
            normalized['kind'] = matched['kind'] if matched else 'excluded'
            normalized['included_in_eligible_records'] = matched is not None
            related = []
            candidates = snapshot.con.execute("SELECT id,data FROM agent_objects WHERE type='AHRecord' ORDER BY id LIMIT ?", (MAX_SCAN + 1,)).fetchall()
            if len(candidates) > MAX_SCAN:
                fail('Record scan limit exceeded', 'scan_limit')
            for other_id, other_json in candidates:
                other = json.loads(other_json, parse_float=Decimal)
                if other_id == row[0] or other.get('isDeleted') is not False:
                    continue
                evidence = []
                if raw.get('transferID') and other.get('transferID') == raw['transferID']:
                    evidence.append('same transferID')
                for field, value in other.items():
                    if isinstance(value, dict) and value.get('$ref') == 'AHRecord' and value.get('id') == identifier:
                        evidence.append('incoming AHRecord reference: ' + field)
                for field, value in raw.items():
                    if isinstance(value, dict) and value.get('$ref') == 'AHRecord' and str(value.get('id')) == str(json.loads(other_id)):
                        evidence.append('outgoing AHRecord reference: ' + field)
                if evidence:
                    related.append({'transaction': snapshot.normalize(other_id, other), 'relationship_evidence': evidence})
            return {**provenance, 'transaction': normalized, 'as_of': as_of,
                    'related_transactions': related[:100], 'related_count': len(related),
                    'related_truncated': len(related) > 100,
                    'warnings': provenance['warnings'] + ['Detail may be excluded from spending; inspect flags and source semantics.']}
        dimensions = {}
        offset = 0
        if 'drilldown_token' in args:
            if any(k in args for k in ('period', 'filters', 'as_of', 'cursor')):
                fail('drilldown_token cannot be combined with query overrides or cursor')
            drill = untoken(args['drilldown_token'])
            keys(drill, {'snapshot_id', 'semantics_fingerprint', 'period', 'filters', 'as_of', 'dimensions'})
            if drill.get('semantics_fingerprint') != snapshot.fingerprint():
                fail('Semantics changed; restart analysis', 'semantics_mismatch')
            if args.get('snapshot_id') and args['snapshot_id'] != drill.get('snapshot_id'):
                fail('Conflicting snapshots', 'snapshot_mismatch')
            args.update({k: drill.get(k) for k in ('snapshot_id', 'period', 'filters', 'as_of')})
            dimensions = drill.get('dimensions', {})
            keys(dimensions, GROUPS)
            provenance = snapshot.provenance(args)
        if 'cursor' in args:
            if any(k in args for k in ('period', 'filters', 'as_of', 'drilldown_token', 'sort_by')):
                fail('cursor cannot be combined with query overrides')
            cursor = untoken(args['cursor'])
            keys(cursor, {'snapshot_id', 'semantics_fingerprint', 'period', 'filters', 'as_of', 'dimensions', 'offset', 'sort_by'})
            if cursor.get('semantics_fingerprint') != snapshot.fingerprint():
                fail('Semantics changed; restart analysis', 'semantics_mismatch')
            if args.get('snapshot_id') and args['snapshot_id'] != cursor.get('snapshot_id'):
                fail('Conflicting snapshots', 'snapshot_mismatch')
            args.update({k: cursor.get(k) for k in ('snapshot_id', 'period', 'filters', 'as_of')})
            args['sort_by'] = cursor.get('sort_by', 'date_desc')
            if not isinstance(args['sort_by'], str) or args['sort_by'] not in SORTS['search_transactions']:
                fail('Invalid cursor sort order')
            dimensions = cursor.get('dimensions', {})
            keys(dimensions, GROUPS)
            offset = cursor.get('offset')
            if type(offset) is not int or not 0 <= offset <= MAX_SCAN:
                fail('Invalid cursor offset')
            provenance = snapshot.provenance(args)
        rows, excluded, as_of = snapshot.records(args)
        rows = [r for r in rows if snapshot.dimensions(r, list(dimensions)) == dimensions]
        ordering = args.get('sort_by', 'date_desc')
        rows.sort(key=lambda r: (r['date'], r['id']), reverse=ordering != 'date_asc')
        if ordering == 'amount_desc':
            if len({r['currency'] for r in rows}) > 1:
                fail('Monetary sorting requires a single currency; use filters.currency_codes')
            if any(r['_amount'] is None or r['currency'] is None for r in rows):
                fail('Amount sorting requires known amounts and currency', 'UNAVAILABLE_METRIC')
            rows.sort(key=lambda r: abs(r['_amount']), reverse=True)
        limit = args.get('limit', 20)
        page = [{k: v for k, v in r.items() if not k.startswith('_')} for r in rows[offset:offset + limit]]
        cursor = {'snapshot_id': provenance['snapshot_id'], 'semantics_fingerprint': snapshot.fingerprint(),
                  'period': args.get('period'), 'filters': args.get('filters', {}), 'as_of': as_of,
                  'dimensions': dimensions, 'offset': offset + limit, 'sort_by': ordering}
        return {**provenance, 'coverage': {**provenance['coverage'], 'snapshot_scan_complete': True}, 'rows': page, 'matching_count': len(rows), 'excluded_counts': excluded,
                'as_of': as_of, 'effective_period': args.get('period'), 'sort_by': ordering,
                'next_cursor': token(cursor) if offset + limit < len(rows) else None}
    finally:
        snapshot.close()


def dispatch(db_path, tool_name, arguments, semantics=None):
    with localcontext() as context:
        context.prec = 64
        return _dispatch(db_path, tool_name, arguments, semantics)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--tool', required=True)
    parser.add_argument('--args', default='{}')
    parser.add_argument('--semantics')
    args = parser.parse_args()
    try:
        try:
            text = sys.stdin.buffer.read(1048577).decode('utf-8') if args.args == '-' else args.args
            if len(text.encode('utf-8')) > 1048576:
                fail('Query arguments exceed 1048576 bytes')
            arguments = json.loads(text)
        except ValueError:
            fail('Arguments must be valid JSON')
        result = dispatch(args.db, args.tool, arguments, args.semantics)
        print(json.dumps({'ok': True, 'data': result}, ensure_ascii=False))
        return 0
    except AnalyticsError as error:
        print(json.dumps({'ok': False, 'error': {'code': error.code, 'message': str(error)}}))
        return 2 if error.code == 'INVALID_ARGUMENT' else 1
    except (ValueError, TypeError, sqlite3.Error, OSError, KeyError):
        print(json.dumps({'ok': False, 'error': {'code': 'analytics_error', 'message': 'Cannot analyze snapshot or arguments'}}))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
