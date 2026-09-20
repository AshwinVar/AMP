# ADR-0029: Did it help? — measured, and never claimed as a cause

**Status:** accepted · **Date:** 2026-09-20 · **Extends** [ADR-0005](0005-autonomous-agents.md) (agents propose, humans decide), [ADR-0014](0014-oee-contract.md) (null is not zero), [ADR-0022](0022-copilot-typed-tools-evidence-and-grounding.md) (evidence and provenance) · **Closes a gap named in** [ADR-0026](0026-production-risk-radar.md) and [ADR-0027](0027-machine-health-explained.md)

---

## Context

AMP has proposed, ranked, explained and approved work since ADR-0005, and has
never once looked back to see whether any of it helped. That gap is not a
suspicion — it is written into two accepted ADRs as an honest limit:

> **ADR-0026:** *AMP has not yet measured how often a LIKELY risk became a
> problem, because it does not yet record the outcome.*

> **ADR-0027:** *The weights are judgement, not evidence… AMP has not measured
> which rule predicts anything, because it does not yet record what happened
> next.*

Both sentences were waiting for this change. Without it, every threshold in the
product is a guess that can never be checked, and the sprint's loop —
OBSERVE → UNDERSTAND → EXPLAIN → PRIORITIZE → RECOMMEND → APPROVE → ACT →
**MEASURE RESULT** — stops one step short of its last word.

It is also the easiest place in this product to lie. A before-and-after with
nothing held still is not evidence of causation, and a card that implies it is
would be far more damaging than no card at all: it would make AMP's own
recommendations look validated by AMP's own data.

## Decision

### 1. One row per approved action, written at the moment of approval

`action_outcomes` (migration `0011`) records the metric the action was meant to
move and the reading it had when a human approved. That reading cannot be
recovered later — it is the whole reason a row exists rather than a query.

A **rejected** action records nothing: it changed nothing, so there is nothing
to follow up.

### 2. Nothing is judged before its window has elapsed

Inside the window there is no verdict at all, not a provisional one. The card
shows how many days are left. Once the window has passed, the same metric is
measured over the same length of window after the decision, frozen once, and
never recomputed — the source rows are pruned by retention, and a number that
silently changes on every read is not a record of anything.

### 3. The verdict is about the metric, never the action

`BETTER` means the number improved after the approval — not because of it. A
factory is not a laboratory: downtime falls when the order book empties, stock
rises when production stops.

So every change carries `CORRELATION` provenance, the caveat rides in the
change fact's own detail as well as the card's, and the card states it above
the numbers rather than under them:

> AMP measured what changed after the decision. It cannot show the action caused
> the change: nothing else in the plant was held still.

There is **no confidence score and no p-value**, because an uncontrolled before
and after cannot produce one, and the schema has nowhere to put one.

### 4. Null is not zero, in the column type itself

`baseline_value` and `measured_value` are nullable with no default. A `NOT NULL`
there would force `0.0` into the slot meant for "AMP had no reading", and
manufacture an improvement out of an absence. A NULL on either side gives
`NOT MEASURABLE`. PostgreSQL proves the nullability by insertion
(`verify_pg_action_outcomes.py`), because SQLite would not.

### 5. Only metrics the action has a claim on

| Action | Metric | Good direction |
|---|---|---|
| Maintenance task | minutes of logged stoppage on that machine | lower |
| Escalation | minutes of logged stoppage on that machine | lower |
| Purchase order | units of that item on hand | higher |

An action kind with no metric is not followed up at all. And the test asserts
the reverse too: every kind a human can approve (`approvals.PENDING`) must have
a metric here, so a fourth kind cannot be added and then silently never looked
at again.

### 6. The noise floor is stated, not buried

A change is `NO CHANGE` unless it exceeds **both** 10% of the baseline and a
per-metric absolute floor (5 minutes; 1 unit). Both are judgement. They are
constants in one place, tested at their edges, and named on the card.

## Consequences

**Positive.**

- AMP can, for the first time, be asked "are your recommendations working?" and
  answer from its own records instead of a shrug.
- The two honest limits quoted above become *measurable* rather than permanent.
  They are not yet resolved — that needs data over time — but the instrument now
  exists.
- The Copilot's `get_action_outcomes` gives the same answer in the same words,
  grounded in the same facts.

**Negative.**

- **A read writes.** `build_outcome_summary` freezes readings that have come
  due. It is additive, idempotent, and only ever fills a row that was empty, but
  it is still a `GET` with a side effect. The alternative — recomputing on every
  read — was measurably worse: the test caught the same outcome reading 20
  minutes and then 520 after more stoppages were logged. AMP runs no per-tenant
  scheduler for real tenants; when it does, this moves there.
- One more table on the offboarding path, and one more thing to keep in the
  tenancy lockstep lists.

## Honest limits

- **This is not evidence that any action worked.** It is two measurements with a
  decision between them. Nothing here is a controlled comparison, and the card
  says so every time it is rendered.
- **The window is fixed at 7 days** for every metric. A bearing change and a
  purchase order do not pay off on the same schedule.
- **Downtime with no logs reads as zero minutes.** AMP cannot tell "nothing
  broke" from "nobody logged it" out of downtime rows alone, which means a plant
  that stops logging looks like a plant that stopped breaking.
- **`stock_on_hand` is read at the end of the window, not summed over it.** A
  delivery that arrived and was consumed inside the window leaves no trace.
- **The thresholds are judgement**, exactly as in ADR-0026 and ADR-0027 — and,
  unlike those, this table is what could eventually replace that judgement with
  measurement.
