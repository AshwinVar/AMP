# ADR-0033: An aggregate can disclose what a field could not, so every cross-customer figure has a floor

**Status:** accepted · **Date:** 2026-09-20 · **Extends** [ADR-0017](0017-oem-boundary.md) (an OEM relationship is not access to a factory), [ADR-0014](0014-oee-contract.md) (null is not zero)

---

## Context

A manufacturer can already see its fleet machine by machine, each field gated on
what that customer granted. What it has never had is the view a manufacturer
actually wants: *across my installed base, which model runs hardest, where is my
exposure, what is coming up for service?*

Building that is easy. Building it **without quietly undoing the consent** is
the work, because an aggregate can disclose what a field could not:

> *"Average operating hours across the ACX-75 fleet: 4,120 h"*

If exactly one customer shares operating hours, that average **is** that
customer's reading, relabelled. ADR-0017's gate said no, and the arithmetic said
yes.

This is not hypothetical for AMP's market. An SME manufacturer often has a
handful of customers, and several models installed at exactly one of them.

## Decision

### 1. Every cross-customer figure has a floor, and the floor counts customers

A pooled figure is published only when at least `MIN_CUSTOMERS` (2) **distinct
customers** contributed. Ten machines at one site is one customer: pooling them
would still publish that one customer's operation.

Below the floor the figure is **withheld with the reason** — never rounded,
never noised, never silently dropped.

### 2. A withheld figure is UNKNOWN, not absent and not zero

A missing figure reads as a fleet with nothing running in it. So the fact is
emitted with `UNKNOWN` provenance and the reason in its detail, and the screen
renders *"not shown"* with the explanation rather than a blank cell.

This is ADR-0014's rule applied to a disclosure question instead of a
measurement one.

### 3. The floor applies to every slice, not just the total

A per-model average over one customer is the same disclosure in a narrower
slice. So the per-model rows obey the same floor — and a model installed at a
single customer stays withheld **even when every customer in the fleet shares**,
because the slice is what identifies them.

Slicing is the obvious way round a floor, and it is closed here explicitly.

### 4. Counts are always visible

How many machines the OEM shipped, of which model, to how many customers: those
come from the **manufacturer's own records**, not from any factory's operations.
They need no grant, exactly as ADR-0017 already says for the per-machine view.
So a model row always shows its machine and customer counts, and withholds only
the operational figure beside them.

### 5. No customer is named beside an operational figure

The per-customer breakdown names only counts the OEM already knows. Operational
figures are pooled across customers or not shown at all. The test asserts that
no customer code appears anywhere in the aggregate payload.

### 6. Coverage is stated, not implied

*"2 of 3 customers share anything at all; operating hours come from 3 of 6
machines"* is a different claim from an unqualified average, and only one of
them is true.

## Consequences

**Positive.**

- A manufacturer gets the fleet view it wants without the consent becoming
  decorative.
- The rule is one function (`_pooled`) and one constant, so it cannot be applied
  inconsistently across sections.
- The reason is always on screen, so a manufacturer who wants the number knows
  exactly what would have to change: another customer sharing, not a setting.

**Negative.**

- A small manufacturer with one sharing customer sees almost no operational
  figures. That is the correct answer and it will still be frustrating.
- The floor is a blunt instrument: two customers of very different sizes still
  pool into one number, and the larger dominates it.

## Honest limits

- **A floor of 2 is a floor, not a privacy guarantee.** With two customers, one
  of them can subtract its own machines from a pooled average and infer the
  other's. Proper protection needs a larger floor or added noise, and AMP has
  neither — this ADR says so rather than implying the number is safe.
- **It bounds disclosure through THIS endpoint.** Another aggregate added later
  must use the same floor; nothing structural forces it to, beyond this ADR and
  the test.
- **Only operating hours and utilisation are pooled today.** Service and alarm
  aggregates are not built, and when they are they inherit the same rule.
- **No trend.** Every figure is a snapshot of what is shared right now; a
  customer that withdraws a grant simply stops contributing on the next request,
  which is the ADR-0017 behaviour and is deliberate.
