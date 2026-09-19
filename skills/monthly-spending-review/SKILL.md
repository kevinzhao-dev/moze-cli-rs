---
name: monthly-spending-review
description: Produce a monthly MOZE spending report with category breakdowns, historical comparisons, and transaction evidence using moze-rs analytical tools.
---

# Monthly spending review

Use the local moze-rs MCP tools, or the equivalent `moze-rs query TOOL --args JSON`. In
this repository the binary is `./target/release/moze-rs`; for setup and parameters read
[README.md](../../README.md#agent-interface).

Resolve the month in the user's timezone. Start with `summarize_spending`, an explicit
period, and category grouping. Reuse its snapshot for follow-ups. Its metadata replaces
separate raw-schema and status discovery calls.

Read the backup cutoff, coverage, exclusions, and semantic warnings. A semantics profile
is caller-defined; moze-rs validates its structure, not its financial correctness. Show
currencies separately. Present classified gross expense, refunds, and net expense
distinctly; if enum mappings are unverified, describe source totals and unknown records
rather than asserting a complete expense report. Do not invent an enum mapping to
complete an analysis.

If the report needs a category inventory, use `list_entities`; to discover which names,
stores, or tags occur in this month, use `get_facets`. Distinguish declared but unused
categories from observed values. Preserve exact returned literals for `names` and
`stores` filters. Facet counts include eligible source records, not just expenses.
Unknown tag encoding is reported separately from missing tags; do not silently omit it
from a claim about all tagged spending.

Compare with a relevant prior month using `compare_spending`. For an incomplete month,
compare the same elapsed calendar window and label both periods; avoid comparing a
partial total with an entire month as evidence of improvement. A missing month's data is
not proof of zero spending. Mention unusually long or short comparison periods.

Default result groups are dimension-ordered, not the largest categories. For leading
categories or drivers, set `filters.currency_codes` to one currency and use summary
`sort_by: "net_expense"` or comparison `sort_by: "net_expense_difference"` before
applying limits. Monetary sorts reject mixed currencies. Unknown semantics permit
source-amount analysis only, clearly labeled as such.

Investigate the largest material changes using returned drilldown tokens and
`search_transactions` before attributing causes. Separate one-off transactions from
possible persistent changes; a note is evidence of what was recorded, not independent
confirmation of the cause. Use `get_transaction` only when details are needed.

Deliver the period and cutoff, currency-specific totals, major categories, and supported
changes. Include material unknown/excluded amounts or counts. Add practical follow-ups
only when supported by the user's goals. This report describes the imported ledger;
account balances and MOZE saved-budget calculations require separate validation.
