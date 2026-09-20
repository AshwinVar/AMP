# ADR-0032: The anomaly check runs over the whole fleet, and says why for every machine it could not score

**Status:** accepted · **Date:** 2026-09-20 · **Extends** [ADR-0020](0020-amp-native-ai.md) (AMP-native AI, consent, honest evaluation), [ADR-0027](0027-machine-health-explained.md) (a model shown as a model) · **Deliberately does NOT extend** [ADR-0031](0031-proactive-restraint.md)

---

## Context

AMP has had a telemetry anomaly check since ADR-0020. It fits a baseline from a
machine's own previous 14 days and scores its last hour against it, gated on the
company's learning consent. It has been correct and it has been almost unusable:
the only way to reach it was to open a model card, pick **one** machine from a
dropdown and press a button.

A plant with forty machines has forty dropdown choices and no way to know it has
looked at all of them. Worse, a machine that was never picked looks exactly like
a machine that was picked and came back quiet.

The model is also **not adopted**. ADR-0020's own evaluation says it did not beat
the rules AMP already uses on held-out synthetic data. So the question is not
only "how do we run it over everything" but "how do we show the output of a
model that did not earn its place".

## Decision

### 1. One call, every machine, one row each

`GET /ai/native/anomaly/sweep` (Admin/Supervisor) runs the **same** scorer the
single-machine route runs — passed in as a function, so this module cannot drift
into a second implementation — and returns one row per machine.

Scored rows come first, highest score first. Unscored rows are **never dropped**:
a machine missing from the list reads as a machine that was fine, and that is a
claim AMP has not made.

### 2. An unscored machine says why, in the honest-data vocabulary

| Row state | Means |
|---|---|
| `INSUFFICIENT HISTORY` | not enough history yet — with the shortfall, e.g. *have 2 of 7 distinct days* |
| `NOT MEASURED` | nothing to score in this window |
| `NOT CONFIGURED` | the model artifact did not verify |

Never a zero. A missing number and a number of zero are different claims
(ADR-0014), and here the difference is *"we have not looked at that one"* against
*"we looked and it is quiet"*.

### 3. Consent is the whole fleet's answer, asked once

The check learns from the company's own telemetry, so it runs only with that
company's consent. The sweep asks **once** — the consent is per company, so a
second machine would refuse for the same reason — and a refusal returns no
scores with the reason, rather than an empty list. An empty list reads as
"nothing unusual".

A founder preview never runs it, for the same reason a preview cannot grant the
consent.

### 4. An experimental model is never an alarm

- the sweep's state is `MODEL NOT VALIDATED`, always;
- every score is `MODEL ESTIMATE` provenance, with the evaluation's caveat in
  the fact's own detail;
- the payload may not contain "alarm", "alert" or "fault" except in the phrase
  that denies it — the test counts occurrences and fails otherwise;
- nothing is colour-coded as a fault.

### 5. It does **not** feed the proactive bar

ADR-0031 lets three things interrupt a person, and an experimental model is not
one of them. A model that has not earned adoption has not earned the right to
interrupt anyone, however interesting its numbers look. This is a deliberate
non-integration, and it is the reason this ADR names ADR-0031 as something it
does *not* extend.

## Consequences

**Positive.**

- "Is anything behaving oddly?" is one call instead of forty, and the answer
  distinguishes *quiet* from *not looked at*.
- The same check, the same consent gate, the same caveats — no second scorer.
- The Copilot can answer it, through a tool restricted to the same roles as the
  route (the second such tool, after `get_failure_risk`).

**Negative.**

- Scoring a fleet costs one baseline fit per machine, so the sweep is bounded at
  60 machines and is a button, not a poll.
- A plant that has not granted learning consent sees the same honest nothing it
  saw before, with a clearer reason.

## Honest limits

- **The model is still experimental.** This ADR makes it *reachable*, not
  *validated*. Nothing here is evidence that its scores predict anything, and
  ADR-0020's evaluation still stands: it did not beat the existing rules.
- **A high score is not a fault.** It means this hour looked unlike this
  machine's own recent history — which a shift change, a product changeover or a
  new operator will also do.
- **Bounded at 60 machines.** A larger plant sees the first 60 by id, and the
  count is on the card. Paging it is the next change if a plant needs it.
- **No trend.** Each sweep is a snapshot; AMP does not record the scores, so
  "has this machine been drifting for a week" has no answer here.
