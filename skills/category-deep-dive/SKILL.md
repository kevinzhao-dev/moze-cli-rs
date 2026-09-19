---
name: category-deep-dive
description: Investigate a MOZE spending category or named expense such as electricity, including totals, monthly history, and the transactions behind a change.
---

# Category deep dive

Use the local moze-rs MCP tools, or `./target/release/moze-rs query TOOL --args JSON`
from this repository. Read [README.md](../../README.md#agent-interface) for setup and
parameters when needed.

For a simple named-expense request, start with one `summarize_spending` call with an
explicit period, `filters.query`, and month grouping. For example:

```sh
./target/release/moze-rs query summarize_spending --args '{"period":{"from":"2025-09-19","to":"2026-09-19"},"filters":{"query":"電費"},"group_by":["month"]}'
```

Replace example dates with the user's requested period. Text matching can include notes
and linked names; label it as a text search. When the user means an exact category or
overlapping matches could change the answer, use `resolve_entities` and filter by its
stable IDs. Check parent/child scope instead of silently choosing an ambiguous name.

When vocabulary is unknown, browse `list_entities` for declared categories or
subcategories instead of guessing names. For a period's observed transaction names,
stores, or tags, use `get_facets` with the relevant dimensions. Catalog entries can
exist without usage; observed facets do not enumerate unused categories. Follow cursors
if a complete vocabulary is needed, use `limit` at most 100, and reuse the snapshot
for follow-ups.
`list_entities.items` carries each entity's `id` and `parent` category;
`get_facets.facets[dimension].values` carries `value`, `count`, and a `filter` object
ready to merge into the next call's filters. For discovery cursor replay, send only
`cursor` plus optional `limit` and matching `snapshot_id`.

Use exact `names` or `stores` arrays after discovering the recorded literal. These
filters are case-sensitive, with OR within an array and AND across fields. Use `tags`
with `tags_mode: "any"` or `"all"` only for supported tag representations. Unknown
encoding counts are not missing tags; an unsupported-tag filter error is not an empty
result. Declared tag usage is always unknown (`usage_count: null`); requesting only used
declared tags raises `UNVERIFIED_TAG_LINKS`. Use observed tag facets for frequencies. Do
not split opaque strings or infer record linkage from the tag catalog.

Use response provenance, coverage, and exclusions directly. The semantics profile is
caller-defined; moze-rs validates its structure, not its financial correctness. Keep
currencies separate, retain unknown semantic classifications, and distinguish source
totals from expenses classified by the caller-supplied policy. Pin subsequent queries to
the returned snapshot. A completed scan is not proof that the ledger covers every day or
every real-world expense.

For a trend question, compare relevant periods and inspect material contributors with
`compare_spending` and returned drilldown tokens via `search_transactions`. Distinguish
transaction frequency from average recorded transaction amount; neither establishes item
quantity or unit price. Use `get_transaction` for necessary detail only.

Answer with the date range, backup cutoff, matching scope, total, and the requested
monthly or transaction breakdown. Explain missing periods and material exclusions. Avoid
diagnosing a behavior change solely from a few irregular bills or one large item.
