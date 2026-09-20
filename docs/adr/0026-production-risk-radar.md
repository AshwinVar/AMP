# ADR-0026: The Risk Radar states a rule and a measurement, never a probability

**Status:** accepted · **Date:** 2026-09-20 · **Extends** [ADR-0020](0020-amp-native-ai.md) (measure before claiming), [ADR-0022](0022-copilot-typed-tools-evidence-and-grounding.md) (evidence and provenance), [ADR-0024](0024-factory-command-centre.md) (the owner's view)

---

## Context

The Command Centre and the Root-Cause Explorer both look backwards: what happened, and why. The owner's other question is forward: **what is about to go wrong?**

Everything in this area invites a dishonest answer. A "risk score" with no stated rule is a number that cannot be argued with. A percentage implies a calibrated forecast. And the word "predictive" is the easiest claim in manufacturing software to make and the hardest to earn: AMP has exactly one adopted model (failure risk), evaluated on synthetic machines only, and its own model card says so (ADR-0020).

## Decision

### 1. Every risk is a rule over measured data, and says so

`ai/risk_radar.py` produces risks that each carry:

- **the rule**, in a sentence, with its threshold in it;
- **the measurement** it was computed from, as `Fact`s with provenance;
- **a likelihood word**, never a number.

The rules shipped here:

| Risk | Rule |
|---|---|
| Order misses its date | units still to ship ÷ days left, against the plant's own measured good-units-a-day over the window. Above it: LIKELY. Within 30%: POSSIBLE. Already past its date: LIKELY, and the rule says it is a fact, not a forecast |
| Item runs out | days of cover from the item's own measured burn: ≤3 days LIKELY, ≤7 POSSIBLE. Nothing on hand: LIKELY, "there is no stock on hand" |
| Machine stops | the hand-weighted rule score (`predictive_engine`): ≥75 LIKELY, ≥55 POSSIBLE |
| Maintenance backlog | a task already overdue is LIKELY; three or more due this week is a WATCH |
| Quality drifting | the week-on-week fail-rate move against the drift threshold the quality trend already defines; a thin sample is a WATCH, and says so |

### 2. Likelihood is three words, in the shared vocabulary

`LIKELY`, `POSSIBLE`, `WATCH` live in `ai/evidence.py` beside the provenance, data-state and root-cause vocabularies, and are mirrored in `frontend/lib/evidence.ts` with the same drift test. A word is only ever rendered next to its rule; the card's test walks every row and fails if a likelihood appears without one.

### 3. No prediction is claimed, and the tests enforce it

The payload may not contain "probability", "predicted", "forecasted", "% chance" or an unqualified "machine learning". The machine-risk fact says, in the fact itself, "hand-weighted rule points, not machine learning", and the card repeats it: *every risk here is a rule over measured data… none of it is a prediction from a trained model.*

The adopted failure-risk model is **not** used here. It is evaluated on synthetic machines only; surfacing it as a plant-floor forecast would be exactly the unsupported claim the sprint forbids. Where it belongs is its own model card, with its caveats.

### 4. A rate is measured, not assumed

The order rule needs a production rate. It uses the plant's own good units a day over the window. With no production recorded, the radar returns `INSUFFICIENT HISTORY` and says it cannot judge whether a date is reachable — rather than assuming a rate.

### 5. Sized only where there is a size

An order carries the units still to ship, and money only where the company set a unit value. A stock-out carries **no** units: AMP has no measured link from a shortage to the units not made. That link is the smart-inventory change, and until it exists the radar says nothing about it.

## Consequences

**Positive.**

- An owner sees what is coming with the arithmetic in the open, and can disagree with a rule rather than with a black box.
- The vocabulary and the evidence are the same ones the Copilot and the Command Centre use.
- "Predictive maintenance" claims cannot creep in: the tests fail on the words.

**Negative.**

- Rules are blunter than a fitted model. A plant with unusual patterns will see risks that a trained model might rank differently — which is why the thresholds are constants, stated on the card, and changeable in one place.
- The radar cannot see a risk nobody records. An unlogged stoppage pattern, a supplier's verbal warning, a machine that sounds wrong: none of it is in the data.

## Honest limits

- **The thresholds are judgement, not evidence.** 75 rule points, 3 days of cover, 30% of the measured rate: each is a starting point chosen to be visible and adjustable, not a number derived from AMP's own outcome data. AMP has not yet measured how often a LIKELY risk became a problem, because it does not yet record the outcome — that is the closed-loop change.
- **The order rule uses a plant-wide rate**, not the line or machine that will actually make those units.
- **No probability is offered, because none is calibrated.**
