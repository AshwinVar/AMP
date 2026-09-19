# ADR-0024: The Factory Command Centre ranks problems by what they cost, and never totals them

**Status:** accepted · **Date:** 2026-09-20 · **Extends** [ADR-0007](0007-read-models-projections.md) (read-models), [ADR-0010](0010-oee-money-story-unit-value.md) (one per-tenant rate), [ADR-0014](0014-canonical-oee-contract.md) (honest OEE), [ADR-0022](0022-copilot-typed-tools-evidence-and-grounding.md) (evidence and provenance)

---

## Context

An owner opening AMP had to assemble their own answer. The scorecard gave four KPIs, the briefing gave alerts ranked by a hand-assigned severity word, the recovery card gave a lever, the schedule card gave plan attainment, and the agent inbox gave pending actions. Five cards, five vocabularies, and no statement of which problem was actually the biggest.

The sprint asks that an owner can answer five questions: what is happening, what is going wrong, why, what it is costing, and what to do next.

Two traps sit in the way:

1. **Severity words are not comparable.** "3 items at reorder level" (medium) and "225 minutes of downtime" (high) were ranked by whoever wrote the alert. An owner wants the biggest *loss* first.
2. **Sizing invites fabrication.** The moment problems are ranked by money, there is pressure to invent a rate for the ones that have none.

## Decision

### 1. One read-model, composed from the ones that exist (`ai/command_centre.py`)

`GET /command-centre` composes OEE, schedule adherence, production, downtime, quality, cost, inventory, delivery, maintenance and the agents' proposed actions. It computes no new figure. Every number it shows carries a `Fact` with its provenance (ADR-0022), so the card and the Copilot say the same things in the same words.

### 2. Ranked by measured impact, in the cost model's own unit

Each problem is converted to **good units not made**, the measure the cost model already uses, and to money only when the company has set a unit value:

- **Downtime causes:** minutes lost × the plant's measured run rate.
- **Missed plans:** the shortfall against the plan that came due.
- **Quality:** the units that failed inspection.

A problem AMP cannot size — an item out of stock, a late order, an overdue task — is still listed, and says **NOT MEASURED**. It is never given a zero, which would sort it last while looking like a measurement.

### 3. Two deliberate exceptions to "biggest first"

- **A machine stopped right now leads**, whatever it has cost so far, because the card's first job is to say what is happening. The first version ranked purely by size, and on a factory with five sized losses a hard-down machine fell off a five-problem card. The test that found it is the one that keeps it fixed.
- **The figures are never totalled.** A missed plan and the downtime on that plan's machine describe the same lost output from two sides. Adding them would double-count. The card ranks them, links them under "why", and says so in `overlap_note`.

Every problem carries `rank_basis`: "stopped now", "measured loss" or "not measured".

### 4. "Why" is labelled, and stops at what the data supports

A missed plan names the downtime measured **on its own machine, in the same window** as a **LIKELY CONTRIBUTOR**, with the minutes as a measured fact. When there is none, it says **INSUFFICIENT EVIDENCE** rather than guessing. **CAUSE CONFIRMED is never used here**: this card correlates two measurements, and a confirmed cause needs the attribution engine that comes with the Root-Cause Explorer.

### 5. Money only where a rate exists

Where a unit value is set, the card shows money. Where it is not, it shows good units, states **NOT CONFIGURED**, and suggests setting a unit value. On a factory with no rate, **no money symbol appears anywhere on the card** — asserted directly over the whole payload, because a single stray currency symbol is the defect ADR-0010 exists to prevent.

### 6. What to do next names who decides

Agent actions awaiting a decision come first, each saying "An Admin or Supervisor approves it" — the approval gate's own rule (ADR-0015), not a promise that AMP will act. Then AMP's suggestion for the biggest problem. Nothing on this card acts.

## Consequences

**Positive.**

- An owner sees the biggest loss first, in a comparable unit, with the evidence behind it.
- Honest states are on the face of the card: partial coverage, no plan, no unit value.
- The Copilot and the card cannot disagree: same read-models, same facts, same words.

**Negative.**

- The card costs about 25 queries. It is loaded per view and refreshed on a minute, not polled every three seconds, and `test_command_centre.py` fails if that budget grows.
- Ranking by units understates a problem whose cost is not units (a late order's penalty, a stock-out's knock-on). Those are listed as NOT MEASURED until the shortage-to-production link is built.

## Honest limits

- **The impacts are estimates from measured inputs**, not an audited P&L. Downtime-to-units uses the plant's own measured run rate.
- **"Why" is a correlation.** Same machine, same window.
- **No forecast.** Everything here is measured history and current state; the risk radar is a separate change.
