# ADR-0033: An aggregate can disclose what a field could not, so every cross-customer figure has a floor

**Status:** accepted · **Date:** 2026-09-20 · **Extends** [ADR-0017](0017-oem-fleet-and-cross-tenant-equipment.md) (an OEM relationship is not access to a factory), [ADR-0014](0014-canonical-oee-contract.md) (null is not zero)

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

### 7. A floor on each cell is not a floor on the table

§3 closes slicing. It does not close the **complement**, and an adversarial
pass found that out: with the fleet figure published and every slice but one,

```
withheld_total = fleet.value × fleet.machines − Σ(published slice totals)
withheld_mean  = withheld_total ÷ that slice's own machine count
```

and for a slice with a single customer, that **is** that customer's reading.
Measured against this module's own fixture, a withheld `900.0` came back as
`900.1`. Suppressing the cell while publishing the margin withheld nothing —
the arithmetic was the disclosure, not the row.

So the table must leave **either no slice withheld, or at least two**. One
withheld slice is the only case that solves. When exactly one is withheld:

- the **smallest publishable slice is suppressed with it**, smallest because it
  is the least the manufacturer loses, and it says on screen that the
  arithmetic is what is being withheld rather than the row itself;
- when there is nothing publishable to suppress alongside it, the **fleet
  figure goes instead**, because a margin over a single unknown is that
  unknown.

A withheld slice whose customers share **nothing** is not in the fleet total at
all, so it needs no complement — its protection is the consent, and suppressing
a good row to shield it would cost the manufacturer a figure for no gain.

This is ordinary complementary cell suppression, borrowed from official
statistics, and it is a bound rather than a guarantee for the same reason the
floor is (see *Honest limits*).

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
- **Complementary suppression is a bound too.** Two withheld cells still
  publish their *combined* total through the margin, so an OEM that learns one
  of them by other means recovers the other. It removes the case that solves
  outright; it does not make the table safe. Proper protection is the same
  answer as above: a larger floor, or noise.
- **The rule protects the table AMP publishes, not every table.** The fleet
  figure is capped at `MAX_MODELS` rows, so contributing machines can sit
  outside the shown slices; the suppression reasons about the rows it emits.
  Another margin added later — service intervals, alarm rates — must run the
  same check, and nothing structural forces it to beyond this ADR and the test.
- **It bounds disclosure through THIS endpoint.** Another aggregate added later
  must use the same floor; nothing structural forces it to, beyond this ADR and
  the test.
- **Only operating hours and utilisation are pooled today.** Service and alarm
  aggregates are not built, and when they are they inherit the same rule.
- **No trend.** Every figure is a snapshot of what is shared right now; a
  customer that withdraws a grant simply stops contributing on the next request,
  which is the ADR-0017 behaviour and is deliberate.
