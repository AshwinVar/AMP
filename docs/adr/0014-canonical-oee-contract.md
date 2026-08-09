# ADR-0014: One canonical OEE contract, and an honesty rule

**Status:** Accepted
**Date:** 2026-08-09
**Depends on:** ADR-0010 (never invent a number)

## Context

The OEE *maths* was already right. `analytics_engine.pooled_oee` pools correctly,
clamps symmetrically, and tolerates NULL columns. What was wrong is that each
surface chose its own **record set**, and "no data" was indistinguishable from
"measured zero".

Measured on master — one factory, one moment, asked four ways:

```
surface                                       window                OEE
----------------------------------------------------------------------
/oee-summary  (the dashboard)                 last 7 days           100%
ai_copilot LLM context                        last 10 records        10%
pooled over everything in the table           all time               17%
pooled over the 7-day window                  last 7 days           100%
```

A customer asking the assistant "how is the plant doing?" got an answer **90
points** from the screen in front of them. The comment above the offending line
claimed it used "the ONE definition every dashboard uses" — and it did use the
same *function*. It did not use the same *records*: `.order_by(id.desc()).limit(10)`
is a window counted in rows.

Separately, and worse:

```
plant OEE while the worst machine still reports : 67%
plant OEE after its gateway goes silent         : 94%   (+27 points)
```

A machine that stops reporting leaves the denominator. **The metric improves
exactly when visibility is lost** — the one direction a metric must never move.

And three honesty defects:

* `has_data` meant "a row exists", so a shift that produced nothing rendered as a
  measured 0%.
* `planned_minutes == 0` (an unscheduled weekend) reported 0% OEE, inventing a
  loss where nothing was scheduled.
* The per-record view had no `has_data` at all, so a drill-down row could not
  distinguish "0% quality" from "no units were made".

## Decision

`backend/oee_contract.py` is THE contract. Full mathematics, window rule,
definitions and limitations: [`docs/engineering/OEE-CONTRACT.md`](../engineering/OEE-CONTRACT.md).

The decisions that were previously implicit, now stated once:

1. **Window is `[now − d, now)`** — start inclusive, end exclusive, so adjacent
   windows tile without sharing a record. Default 7 days. All-time is never the
   default.

2. **Undefined is `None`, not `0.0`.** Availability over zero planned minutes is
   not zero; it is undefined. OEE is a product, so a missing component makes the
   product missing too. `as_percentages()` preserves that distinction rather
   than flattening it to a number a UI will render as measured.

3. **`has_data` means measurable** — `planned > 0 OR total > 0`.

4. **Coverage travels with the number.** Every plant result carries
   `machines_expected`, `machines_reporting`, `coverage_pct`, `complete`. The
   figure cannot be corrected for a silent machine — that data is gone — but
   presenting a partial measurement as a whole-plant number is a choice, and
   this contract declines it. The AI context states coverage in prose so the
   model cannot assert a whole-plant fact from a partial figure.

5. **Tenant filtering is explicit**, not inherited from the ADR-0002 hook,
   because exports, scripts and the AI context builder all call this with
   nothing bound — the shape of the ingest defect in ADR-0011.

## Consequences

**The AI assistant's OEE changed** — it now matches the dashboard. Any prompt or
evaluation that captured the old number will differ.

**`/oee-summary` gains a `coverage` block.** Additive; no field removed.

**Undefined components render as `null`.** A consumer that does arithmetic on
the response without checking `has_data` will now see `null` where it previously
saw `0`. That is the point — the `0` was a fabrication — but it is a contract
change for any consumer written against the old shape. `as_percentages(...,
missing=0)` exists for a legacy response shape that cannot carry null, and
`has_data` must be shown beside it.

**Four limitations are documented, not fixed** (L1–L4 in the contract doc):
downtime does not reconcile with availability; overlapping downtime is not
representable; rework is invisible; day and shift boundaries are UTC. Each is a
schema property — an interval column, a rework count, a tenant timezone — not
something a calculation can repair. They are recorded as open risks rather than
worked around, because a contract that omits what it cannot do is not a
contract.

**A dead helper was deleted.** `OeeWindow.contains()` duplicated the boundary
rule in Python while the SQL filter did the real work. Mutation testing found it
by surviving: breaking it failed nothing, because nothing called it. Two
implementations of one rule is precisely the defect this ADR removes.

## Verification

`backend/test_oee_contract.py` — 12 golden datasets, every expected value
derived by hand from the definitions and written out longhand beside the
assertion. Including the textbook worked example (59%), pooling versus averaging
(90% against the naive 54%), unplanned-versus-real-zero as *distinguishable*
states, the half-open boundary with adjacent windows proven disjoint, coverage
exposing a silent machine, tenant isolation with nothing bound, and the
dashboard and AI assistant agreeing on both number and window.

`backend/mutate_oee_contract.py` — 16 mutations, all caught: each formula term,
each honesty rule, both window bounds, both coverage fields, both tenant
filters, and the AI-context wiring that closed the 90-point gap.
