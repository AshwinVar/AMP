# ADR-0025: The Root-Cause Explorer separates what AMP measured from what has a reason

**Status:** accepted · **Date:** 2026-09-20 · **Extends** [ADR-0014](0014-canonical-oee-contract.md) (honest OEE), [ADR-0022](0022-copilot-typed-tools-evidence-and-grounding.md) (evidence and provenance), [ADR-0024](0024-factory-command-centre.md) (ranked problems)

---

## Context

"Why are we behind?" is the question the owner acceptance journey turns on, and it is the easiest question in the product to answer dishonestly. The tempting answer takes the largest number on the screen and calls it the cause. AMP already had the ingredients — the OEE loss split, the downtime Pareto, the plan adherence — but nothing put them against the gap and said what was left over.

## Decision

### 1. Three mechanisms, each of them arithmetic (`ai/root_cause.py`)

- **Scrap:** the units recorded as rejected. A rejected unit is a unit not made good.
- **Slow running:** what the recorded runtime could have produced at the machines' own ideal cycle, minus what it produced.
- **Availability:** planned time that was not runtime, in minutes, and what that time could have produced at the same cycle.

Nothing is modelled or estimated from a rate AMP invented. Where no ideal cycle time exists, no units are claimed.

### 2. The split that matters: measured, versus attributed

Availability loss is split into the minutes that **have a logged stoppage reason** and the minutes that do not. The card then reports three figures:

- **lost capacity measured** — everything above;
- **with a reason recorded** — slow running, scrap, and the logged stoppages;
- **with no reason recorded** — the rest.

The first version reported only the second figure and called it "explained". On the evaluation's broken factory that read **"82% of the gap explained"** while the largest single block of lost time — 1,495 units' worth — had no reason logged at all. For an SME plant, that block is not a footnote. It is the finding: *most of your stopped time has no reason recorded*, which is both the reason AMP cannot say more and the cheapest thing to fix.

### 3. The labels, and what earns each one

| Label | What earns it |
|---|---|
| **CAUSE CONFIRMED** | a measured mechanism on this plant in this window: scrap, slow running, the minutes a logged stoppage took |
| **LIKELY CONTRIBUTOR** | a stoppage reason's share of the logged minutes, valued at the ideal cycle — a rule, not a measurement of that reason's cost |
| **CORRELATED EVENT** | true in the same window with no measured link: an item out of stock, an overdue task. **No units are claimed for it** |
| **INSUFFICIENT EVIDENCE** | non-running time with no reason logged, and the part of the gap the measured losses do not account for |

"Confirmed" means the loss is measured, not that removing it would have closed the gap. That distinction is why the remainder is reported separately.

### 4. Two denominators, said out loud

The gap is measured against **the plan**. The losses are measured against the machines' **ideal cycle**. A plan can be missed while everything that ran was at full speed, and capacity can be lost on a day nothing was planned. The card reports both, states the attributed share of the gap as an indication of size, and carries a note saying it is not an accounting identity.

### 5. In the Copilot, and on the Executive page

- A plan question that asks **why** ("Why are we behind?", "Explain what went wrong") routes to the typed tool `explain_production_gap`; one that asks **whether** ("Did we hit the target?") routes to the plan figures. Deliberately narrow: "why is my OEE low?" still reaches the OEE pillar, so `/copilot/ask` keeps answering the pinned questions exactly as the rule copilot does.
- `GET /root-cause` and a card on the Executive view show the same figures, with each contributor's evidence one tap away.

## Consequences

**Positive.**

- The owner journey's question has an answer built from measurements, with the gaps in it visible.
- "Log your stoppages" becomes a measured recommendation rather than advice.
- The Copilot, the Command Centre and this card cannot disagree: same read-models, same facts.

**Negative.**

- On a plant that logs little, most of the loss lands in "no reason recorded". That is accurate, and it will read as AMP knowing less than a tool that guesses.
- The attributed share of the gap can exceed 100% when capacity losses are large and the plan was small. It is clamped for display and the note explains why the two are not the same denominator.

## Honest limits

- **Correlated events stay correlated.** A stock-out is listed with no units attached until the shortage-to-production link is built.
- **A reason's cost is a share, not a measurement.** AMP does not know how much output each stoppage reason cost; it knows the minutes and values them at the ideal cycle.
- **No prediction.** Everything here is the last seven days, measured.
