# ADR 0010 — The money story: one per-tenant £/good-unit rate

- **Status:** Accepted (2026-07-22)
- **Deciders:** Ashwin (founder), Principal Architect
- **Related:** [0002 — Tenant-scope the core domain](0002-tenant-scope-core-domain.md), [0007 — Read-models: projections that answer one question](0007-read-models-projections.md)

## Context

The platform measured OEE well but stopped at percentages and unit counts. The question a plant owner actually asks — *"what is this costing me, and what's the prize for fixing it?"* — was unanswered. Two surfaces needed a money figure: the OEE **recovery opportunity** (upside of closing the gap to world-class) and **downtime loss** (output the plant didn't make while machines were down).

The trap is a made-up number. If we hard-code a £/unit, every money figure is fiction dressed as fact — worse than showing nothing, because a prospect can't trust it. And if recovery and downtime each carry their own rate, the two figures silently disagree.

We also standardized plant OEE to a single **pooled** computation (ratio of sums, volume-weighted) so every surface agrees on the percentage before we attach a currency to it — see [[oee-aggregation-methodology]].

## Decision

**One configurable per-tenant rate — the margin per good unit — drives every money figure on the dashboard; unset means units-only, never a fabricated £.**

- The rate is `TenantConfig.unit_value_gbp` (`Float`, nullable; `null` = unset). Admin-settable via `PATCH /tenant-config`, validated non-negative, and editable inline from the dashboard (the shared `UnitRateEditor`, mounted on both the recovery card and the Executive-OEE money panel).
- There is exactly **one lookup**: `tenancy.tenant_unit_value(db, tenant)`. The recovery read-model and the management summary both call it, so recovery upside and downtime loss are always valued off the same number. `ai.recovery._unit_value` is an alias of it (so the read-model's tests can stub the rate without a DB).
- **Honest-units-when-unset:** with no rate set, every surface reports physical units (recoverable good units/yr, good units not made) and leaves the £ fields `null`. £ appears only once the tenant owns the number.
- **Recovery** (`ai/recovery.py`, a read-model per [0007]) projects the gap to the 85% world-class benchmark into recoverable good units (window + annualised) and per-component gaps, then multiplies by the rate when set. **Downtime loss** (`build_management_summary.estimated_loss_value`) converts downtime minutes to lost good units at the observed run-rate, then multiplies by the same rate. With no rate it is units only (see the 2026-09 addendum: it used to fall back to a £8/min proxy).
- **Physical honesty of annualised figures:** because recovery annualises a 7-day window (×52), the window's good count must reflect at most a real week. The read-model caps good output at what the machines could physically produce (`machines × days × 24h`) before annualising. This is a no-op on real data — a machine can't run more than 24h/day — and exists only to keep a long-running **simulator** from annualising physically-impossible volumes into absurd £ (the demo tenant once read ~£24M/yr). The simulator is itself self-limiting at source (a machine gains at most one real day of planned minutes per calendar day).

## Consequences

**Positive**
- **Trustworthy by construction:** no money figure exists without the tenant's own rate; nothing is invented.
- **Internally consistent:** upside and loss can't diverge — they share one rate and one lookup.
- **A real sales lever:** the same OEE the platform already measured now carries a £ the owner set, turning a percentage into a decision.
- **Robust magnitudes:** the physical cap means neither a simulator nor a data glitch can print a number that couldn't physically happen.

**Negative / risks**
- The £ is only as good as the rate the Admin enters; a wrong rate scales every figure. Mitigated by making it visible and one-click editable everywhere it's used.
- Margin-per-good-unit is a simplification (no product mix, no fixed-cost recovery). Deliberate: one honest, legible number beats a false-precision cost model at SME scale.

## Alternatives considered
- **A hard-coded default rate:** rejected — fabricates money and erodes trust the moment a prospect checks it.
- **Per-surface rates (recovery vs downtime):** rejected — the two figures would disagree with no single source of truth.
- **A full activity-based cost model (per-part cost, overhead absorption):** premature; heavy to configure and to defend. Revisit if a tenant needs product-mix costing — it slots behind the same `tenant_unit_value` seam.

## Rollout
1. **Shipped:** `unit_value_gbp` column + migration, `tenant_unit_value` lookup, recovery £ fields, downtime-loss valuation, the `UnitRateEditor`, and the Executive-OEE money panel.
2. **Physical cap** added to the recovery read-model and the simulator so annualised figures stay physically credible.
3. **Later:** richer costing (product mix / fixed-cost recovery) behind the same lookup, only if a tenant needs it — no consumer change.

## Addendum (2026-09): the cost-of-losses family never followed this decision

The decision above covered recovery and the management summary's downtime loss.
The cost-of-losses family, built separately, priced every tenant's losses at a
fixed tariff, **£12 per downtime minute and £25 per scrapped unit** (`ai/cost.py`),
and published it on:

- the Cost of losses cards (`/cost-summary`) and the Trends card (`/cost-trend`);
- the scorecard's "Cost of losses" KPI and its week-on-week change;
- the weekly report, the assistant's cost answer and digest, and the copilot's context;
- the digital twin's cost heat map, which also printed "$".

The management summary itself still fell back to **£8 per downtime minute** when no
rate was set. The dashboard's own money panel tells the customer "one rate drives the
whole dashboard, and stays units-only until you set it". Five surfaces contradicted
it, with figures no customer supplied.

**Now:** one conversion, `backend/loss_value.py`, used by every surface:

- a loss is **good units not made**: one per scrapped unit, and downtime minutes
  times the window's **observed run rate** (good units per run minute over the same
  records; each week of a trend at its own rate);
- **money is units times `unit_value_gbp`, or `None`**. There is no fallback, and a
  rate of 0 is a real £0;
- downtime with **no run time** in the window has no rate to convert it, so its units
  and £ are `None`, not 0;
- every headline equals the sum of its rows (by machine, by line, by day). The rows
  are rounded by largest remainder from the same floats as the headline, so 3 × 1.5
  units reads 5 everywhere, not 5 above rows that add to 6.

The cost trend's noise floor was £300 of the old tariff. It is now the downtime
trend's 30-minute floor, in units at the fortnight's run rate, so the two cards agree
on what counts as a move.

Tests: `test_loss_money_needs_the_tenant_rate.py` (every surface, both modes), the
reworked `test_cost.py`, `test_cost_trend.py`, `test_scorecard.py`,
`test_twin_overlay.py`, plus frontend `CostSnapshot.test.tsx` and
`LossesWithoutARate.test.tsx`. `test_currency_single.py` now also catches a "$" in JSX
text (`${m.cost}` outside a template literal), which is how `CostIntelCard` and
`DigitalTwinSection` printed dollars past the old check.
