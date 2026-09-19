# Discovery workflow evaluation

All records, identifiers, and monetary values in this evaluation are synthetic.
This is one independent agent smoke evaluation plus deterministic acceptance tests,
not a statistical model benchmark or validation of real MOZE enum meanings.

## Reproduce the ledger

Build the CLI with `cargo build --locked`. Generate the ledger outside the repository:

```sh
evaluation_dir=$(mktemp -d)
python3 tests/agent_eval_fixture.py "$evaluation_dir"
```

The generator prints the database and semantics profile paths. Give a fresh agent
those paths, the `skills/category-deep-dive/SKILL.md` skill, and README access. Its
entrypoint is `./target/debug/moze-rs --db DATABASE query TOOL --args JSON --semantics PROFILE`.
Keep the fixture generator, tests, and expected results below out of that agent's
input; allow read-only analytical queries, not SQL or raw collection inspection.
Pin follow-up requests to the returned snapshot.

## User tasks

Use the inclusive period 2026-01-01 through 2026-03-31:

1. 列出「居家」底下所有未封存的子類，包含未使用的，並列出期間使用筆數。
2. 「生活帳戶」這段期間有哪些實際出現的品名、商家與標籤？每個值列出筆數，也說明缺漏或無法解讀的值。
3. 「居家」大類、品名精確為「電費」、商家精確為「北方公用」的總支出、退款與淨支出多少？提供交易依據。
4. 「生活帳戶」同時有「居家」和「必要」兩個標籤的淨支出是多少？

## Acceptance criteria and observed results

Independent run: 2026-09-19. The worker had the tasks, skill, README, and synthetic
CLI paths; it received no expected answers and did not inspect the fixture or tests.

| Task | Required result | Observed |
| --- | --- | --- |
| Catalog | 房租 1, 水費 1, 瓦斯費 0, 電費 3; omit archived 舊電話費 | Correct, including unused item and excluded disabled transaction |
| Facets | Five eligible records; names 電費 3/房租 1/水費 1; stores 北方公用 3/南方公用 1 with one missing; tags 居家 3/必要 1 with one missing and one unknown | Correct; explained tags overlap and unknown differs from absent |
| Exact spending | USD gross 100.00, refund 5.00, net 95.00; evidence p1/p2/p3, no travel-category electricity or disabled record | Correct; all three supporting records retrieved |
| Tag intersection | Explicitly unable to establish complete total due to unknown encoding | Correct; did not present the known subset as the full answer |

The agent made eight analytical calls: two `resolve_entities`, two `list_entities`,
one `get_facets`, two `summarize_spending`, and one `search_transactions`. One catalog
call incorrectly requested `limit: 500`, received the documented structured error, and
was retried at 100. The expected tag-filter error is part of correct task handling.
All successful follow-ups used the same snapshot, and the answers stated period and
backup cutoff. No raw collection pagination, ad-hoc joins, or database upload was used.

## Improvements from the evaluation

- README now explicitly distinguishes analytical limits (1–100, default 20) from
  the raw collection limit (500). The category skill points to bounded pagination.
- Tag facets now include a warning when unknown encodings make their options and
  counts incomplete, in addition to `unknown_count`.

The live agent run was not repeated after these improvements. Documentation examples
and deterministic tests validate the changes; they do not establish repeatability
across models or sessions.

## Automated regression coverage

`tests/test_agent_workflows.py` reproduces the full discover–filter–evidence workflow
in seven CLI requests and verifies the results above without raw collection calls.
`tests/test_discovery.py` covers catalogs, hierarchy, same-name identities, unused and
archived entries, empty results, facets, missing/unknown values, and pinned cursors.
`tests/test_exact_filters.py` covers field composition, literal matching, tag
interpretation, and filter preservation through summary drilldown and pagination.
`tests/test_mcp.py` exercises discovery and exact filtering through the stdio protocol.

Run the regression suite with:

```sh
python3 -m unittest discover -s tests -v
```
