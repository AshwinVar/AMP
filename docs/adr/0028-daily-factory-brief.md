# ADR-0028: The Daily Factory Brief, and the section that says what AMP could not see

**Status:** accepted · **Date:** 2026-09-20 · **Extends** [ADR-0007](0007-read-models.md) (read-models compose), [ADR-0014](0014-oee-contract.md) (one window, coverage stated), [ADR-0024](0024-factory-command-centre.md), [ADR-0025](0025-root-cause-explorer.md), [ADR-0026](0026-production-risk-radar.md)

---

## Context

AMP now answers the owner's questions in four places: the Command Centre ranks
what is wrong by what it cost, the Root-Cause Explorer says why, the Risk Radar
says what is likely next, and the approval queue says who decides. Each is a
card. Together they ask someone to open four cards and join them up, every
morning, in their head.

A brief does the joining. But prose is the easiest place in a product to lie,
because sentences hide their sources: "the plant had a strong week" reads as an
assessment and is accountable to nothing.

There is a second problem a brief makes visible. Every card AMP has shows what
AMP knows. None of them shows what AMP **does not** know — which machines never
reported, that no unit value is set so nothing can be costed, that 1,495 units
of lost capacity carry no recorded reason. A reader is left to assume the rest
is fine.

## Decision

### 1. `ai/brief.py` composes; it computes nothing

Seven sections, each quoting one engine: where we are, what changed, what is
wrong, why, what is likely, how the shifts did, what to do next. Every figure in
every line comes from a payload an engine already produced.

`test_daily_brief.py` enforces this literally: it flattens every number out of
the Command Centre, Root-Cause and Risk Radar payloads for that factory, then
scans every line of every section and fails on any figure that is not in that
set. A number the brief invented, rounded differently, or carried over from
another factory fails the build.

### 2. The window is stated; no shift is claimed

AMP records shift **output** ("Shift A - 17 Jul") and no shift **times**. It
therefore cannot know which shift is running, and the brief never says "this
shift", "since 6am" or "overnight" — the test fails on those phrases. It states
the rolling window it actually covers, in the header and in the copied text, and
reports per-shift attainment from the shift log as the measurement it is.

### 3. The last section is what AMP could not see

| Blind spot | Raised when | State |
|---|---|---|
| Machines that did not report | OEE coverage is incomplete | `PARTIAL DATA` |
| Nothing costed in money | no unit value is configured | `NOT CONFIGURED` |
| No target to measure against | no plan came due | `NOT CONFIGURED` |
| Lost time with no reason | the Root-Cause Explorer has unattributed units | `NOT MEASURED` |
| No rate to judge a date by | the Radar has no measured rate | `INSUFFICIENT HISTORY` |

Each is read out of a payload above, never guessed. When nothing is missing the
section still appears and says so — *"AMP still only sees what is recorded in
it."* It is never behind a toggle, and it survives into the copied text, so the
version pasted into an email cannot be rosier than the version on screen.

### 4. A blind spot does not relabel the brief

The brief's data state is the worst of its **sections**. Folding the blind spots
in would have marked a whole factory `PARTIAL DATA` — whose UI text reads *"part
of the plant did not report"* — because a stoppage lacked a reason. That is a
different claim, and it is the same mistake ADR-0025 had to correct on the
Root-Cause card. Unlogged reasons are `NOT MEASURED`: the time is measured; the
reason is what AMP has no reading for.

### 5. The headline may not read "all clear" when AMP could not see

Whenever a blind spot exists, the headline says how many — checked in the tests,
for all three factories.

## Consequences

**Positive.**

- One page answers the owner's questions in order, in sentences, and can be
  pasted into an email or a WhatsApp message intact.
- The brief inherits every honesty rule already built: provenance on facts, the
  coverage phrase glued to OEE, money only where a unit value exists, likelihood
  words with their rules attached, `overlap_note` on the ranked problems.
- AMP now states its own blind spots. Nothing else in the product did.

**Negative.**

- It composes five read-models in one request, which is the most expensive read
  AMP serves. It loads on demand rather than polling, and the test records the
  query count so a regression is visible.
- Seven sections is a lot of text on a dashboard. The bodies collapse; the
  headline and the blind spots do not.

## Honest limits

- **"What changed" is week-on-week**, because that is the comparison AMP already
  measures. It is not "since yesterday", and the lines say which window they
  compare against.
- **The brief is only as good as its engines.** It adds no check of its own; if
  the Command Centre ranks something badly, the brief reads it out badly.
- **The blind-spot list is the list AMP can derive.** There will be things
  missing from a plant's data that none of these five rules can notice — an
  unlogged shift, a machine nobody registered. The closing sentence says exactly
  that rather than implying the list is complete.
