# ADR-0027: Every health score shows its arithmetic, and the trained model is shown as a model

**Status:** accepted · **Date:** 2026-09-20 · **Extends** [ADR-0006](0006-machine-health-twin.md) (the machine twin), [ADR-0020](0020-amp-native-ai.md) (measure before claiming), [ADR-0022](0022-copilot-typed-tools-evidence-and-grounding.md) (evidence and provenance), [ADR-0026](0026-production-risk-radar.md) (a rule, never a probability)

---

## Context

AMP has shown a machine health score since ADR-0006. It is a good score — eleven
thresholds over recorded data, weighted by hand — and until now a plant could see
only the number and a few words: *"low utilization below 40%"*.

That is the shape of an unarguable number. A maintenance lead cannot tell whether
the score is 62 because of one big thing or four small ones; cannot see which
rules were checked and passed; cannot tell that a score of 0 might be 37 points
worse than another score of 0; and cannot tell, from the card, that the number is
a rule at all rather than a model.

Meanwhile AMP's one adopted model — failure risk — had the opposite problem. Its
card has been on the dashboard since ADR-0020, with its evaluation and its
caveat, but **its actual output had no screen anywhere**. The endpoint existed
(`/ai/native/failure-risk`, Admin and Supervisor). Nothing called it. A model
nobody can see is not honest, it is just absent.

## Decision

### 1. The scorer records every rule it runs

`predictive_engine.calculate_predictive_risk` now appends a component for each
rule it evaluates — the points, whether it fired, the value it read, the
threshold it read against — as the existing if-chain runs. The chain, the order,
the weights and `reasons` are untouched: this is a recording, not a rewrite.

Two derived fields come with it: `points_before_cap`, and `capped`. Two machines
can both sit at health 0 with very different amounts wrong, and the card now says
which.

### 2. `ai/machine_health.py` turns that record into arithmetic

    Health starts at 100.
    Each rule that fired took its points away.
    Each rule that did not fire is listed too, with what it read.

It computes nothing. If this module could produce a point it would be a second
scorer, and the two would drift apart the way the OEE numbers did before
ADR-0014. It reads `components` and nothing else — never the machine.

The explanation travels **with** the score on `/machine-health/{id}`, so no
surface can show one without the other.

### 3. The score is a rule, and says so

Every fact it emits is `RULE-BASED ASSESSMENT`. The card's note reads: *every rule
is a fixed threshold over recorded data, hand-weighted by AMP — not machine
learning, and not a prediction of failure.* The test splits on the phrase and
fails if "machine learning" ever appears unnegated.

A machine the scorer never saw is `NOT MEASURED`, not `Healthy`. The twin reports
100 for such a machine, and 100 there is an absence, not a clean bill — the card
says so in those words.

### 4. The trained model gets a screen, on the model's own terms

`get_failure_risk` (Copilot) and a table under the existing model card both serve
`/ai/native/failure-risk`. The rules they follow:

| Rule | Why |
|---|---|
| Data state is always `MODEL NOT VALIDATED` | the evaluation is synthetic-only; `OK` would claim more |
| Provenance `MODEL ESTIMATE`, the only tool in AMP that uses it | it is neither measured nor derived from a measurement |
| Every estimate carries the artifact's own caveat | *"Evaluated on synthetic machines only; not evidence of accuracy on real plants."* |
| The rule score sits beside every estimate | the two may disagree, and a reader should see that |
| Loaded on request, never coloured as an alarm | a red number says "act on this", which the evaluation cannot support |
| A machine already down is shown as excluded, not blank | the model estimates a **new** breakdown starting |
| An unverifiable artifact produces **no number at all** | see below |

### 5. The first Copilot tool narrower than its route

`get_failure_risk` copies the roles of the route it mirrors: Admin and
Supervisor. Until now every tool inherited a plain authenticated read, so the
role branch in `run_tool` had no real case. It has one now, and
`test_copilot_tools_no_wider_than_routes.py` checks it against the live app. An
Operator is not refused the tool — the tool is never offered to them.

## Consequences

**Positive.**

- A plant can argue with a threshold instead of with a number. That is the only
  kind of disagreement that improves a score.
- The rules that PASSED are visible, so the score reads as a check that ran, not
  a verdict that arrived.
- AMP's one model is now visible in its own right, and every screen that shows it
  repeats what its evaluation does and does not support.

**Negative.**

- The detail payload grows by eleven small objects per machine. It rides on the
  single-machine cockpit, not the fleet poll, which is where the cost would have
  mattered.
- Showing every threshold invites "why 40%?" — a question AMP cannot yet answer
  from its own outcome data. That is the honest position, and it is written on
  the card.

## Honest limits

- **The weights are judgement, not evidence.** 35 points for a breakdown and 20
  for low utilisation are a starting point, not a fit. AMP has not measured which
  rule predicts anything, because it does not yet record what happened next —
  that is the closed-loop change.
- **The model is adopted but not validated on a real plant.** Adoption means it
  beat the baseline on held-out *synthetic* data. Nothing on any screen may read
  as more than that.
- **A capped score loses information by design.** `points_before_cap` is
  reported, but the 0–100 band genuinely cannot separate two very bad machines.
- **The explanation is only as good as the inputs.** A stoppage nobody logged
  costs no points here, and the card cannot say what it never saw.
