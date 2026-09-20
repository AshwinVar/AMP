# ADR-0030: A shortage is sized from the tenant's own recipe, or not sized at all

**Status:** accepted · **Date:** 2026-09-20 · **Extends** [ADR-0013](0013-bill-of-materials.md) (each tenant's own recipe), [ADR-0007](0007-read-models.md) (read-models compose) · **Supplies the link [ADR-0026](0026-production-risk-radar.md) said was missing**

---

## Context

ADR-0026 shipped the Risk Radar's stock rule with **no size at all**, and said
why in writing:

> A stock-out carries **no** units, because AMP has no measured link from a
> shortage to the units not made. That link is the smart-inventory change, and
> until it exists the radar says nothing about it.

Everything needed for that link already existed and had never been joined up:
bills of materials per tenant (ADR-0013), open work orders with outstanding
quantities, and stock on hand. Until now a buyer saw "Steel bar: 90kg, below
reorder level" and had to work out in their head which orders that stops.

## Decision

### 1. The arithmetic, and only the arithmetic

For every item at or below its reorder level:

1. which **open** work orders still need it — through this tenant's own active
   BOM, never a shared recipe;
2. how much each still needs — outstanding units × quantity per unit;
3. how far the stock on hand goes — allocated **in due-date order, earliest
   first**, which is the rule a plant would use and is stated on the card;
4. what is left unmade — the units at risk.

Partial stock makes **whole** units: 90kg of a component that takes 2kg each
makes 45 units, not 45.5. An order with no due date claims stock **last**: it
cannot take from an order that has a date to miss.

### 2. No recipe, no number

An item in no bill of materials gets **no units figure at all**. It is listed
underneath with the reason, so a buyer still sees it and the list still
reconciles, but AMP does not guess what running out would stop. That is the same
refusal ADR-0026 made; this change narrows where it applies, it does not remove
it.

When some items could be sized and others could not, the result is
`PARTIAL DATA`. When none could, it is `NOT CONFIGURED` — the plant needs
recipes, not a better algorithm.

### 3. The Risk Radar adopts it

`_stock_risks` now carries `impact_units` where a recipe exists, and money where
the company has set a unit value. This supersedes the sentence quoted above, and
only that sentence: an unlinked item's risk is still unsized on the radar,
exactly as before.

### 4. It orders nothing, and writes nothing

`suggested_order_units` is the shortfall — a *demand-based* number a buyer can
act on, shown beside the Reorder agent's existing policy-based draft (refill to
~2× the reorder level), which a human still approves (ADR-0005/0015). This
module performs no writes of any kind, and the test asserts that no purchase
order and no agent action appears after a read.

## Consequences

**Positive.**

- "Which shortage matters most?" has an answer in production units, and in money
  where money is configured — ranked, rather than sorted by how empty a shelf is.
- The demand figure is what the open orders actually need, not a policy multiple
  of a reorder level nobody revisits.
- A second ADR's honest limit turns into a measurement rather than staying a
  permanent caveat.

**Negative.**

- The Risk Radar now composes one more read-model, and the Daily Factory Brief
  composes the radar: the brief's measured cost went from 71 to 79 queries. Both
  are on-demand reads, and both record the number.
- A plant with no bills of materials sees the same honest nothing it saw before,
  with a clearer reason. The feature's value is proportional to how complete the
  recipes are.

## Honest limits

- **It sizes what the OPEN orders need, not the order book.** Demand that has
  not been turned into a work order is invisible here.
- **Lead time is not modelled.** "Short by 340kg" does not say whether the
  supplier can deliver before the earliest due date; AMP has expected delivery
  dates on purchase orders but no measured supplier lead time to judge them by.
- **Inbound stock already on order is not netted off.** A draft or approved PO
  for the same item does not reduce the shortfall, so the suggested order can
  double-count what is already coming. That is the next change in this area.
- **One level of BOM.** A component of a component is not followed.
- **Allocation by due date is a rule, not a plan.** A real planner might protect
  a strategic customer over an earlier date. The rule is stated so it can be
  argued with.
