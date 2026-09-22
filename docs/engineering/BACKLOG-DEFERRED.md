# Deferred backlog — recorded, not being worked

**Opened:** 2026-09-22, when the founder changed engineering strategy to
BUILD-FIRST: *"Lower-severity metric cleanup should go into backlog… Do NOT
spend the next several days making every historical read-model architecturally
perfect. We can harden those later."*

Everything here is **real and verified** — each line was measured, not guessed —
and each is deliberately **not** being fixed now. Nothing here can cause a wrong
HIGH/CRITICAL recommendation, a cross-tenant or cross-OEM leak, an incorrect
financial value, a production outage, data corruption, or an auth failure; those
categories are still fixed immediately, whatever else is in flight.

---

## Windows: read-models that cut their own cutoff

Ten read-models still cut a private cutoff instead of `oee_contract.OeeWindow`,
and most have **no upper bound**, so a row dated in the future counts toward
"the last 7 days".

| Module | Shape | Why it is deferred |
|---|---|---|
| `ai/production` | `datetime.combine(...) + window_set` over seven calendar dates | The figure is a count or a sum; the disagreement with the canonical window is at the boundary instant only |
| `ai/losses` | same | as above |
| `ai/workforce` | same | as above |
| `ai/roster` | same | as above |
| `ai/trends` | same | as above |
| `ai/coverage` | private cutoff, no upper bound | as above |
| `ai/stock_health` | private cutoff, no upper bound | as above |
| `ai/stock_accuracy` | private cutoff, no upper bound | as above |
| `ai/supplier_performance` | private cutoff, no upper bound | as above |
| `recommendations_routes` | windowed to 30 days, **no upper bound** | See the note below — checked, and it is *not* a wrong-HIGH defect |

**`recommendations_routes` was checked first, and cleared.** It was recorded as
possibly driving a wrong "High" recommendation. It does not. The quality rows
are already bounded (`QualityInspection.created_at >= cutoff`,
`recommendations_routes.py:95`), and the `0` it publishes on an empty
denominator cannot fire a recommendation because the threshold is `fail_rate >= 10`
(`:171`). What remains is the missing upper bound, which is metric hygiene.

## Zeros that should be "not measured"

| Site | What it publishes |
|---|---|
| `ai/trace._pct` | `0` for an empty denominator |
| `recommendations_routes` | `0` for an empty denominator (harmless today — see above) |

The rule these break is the one established across #697–#700: a rate or an
average over an empty denominator is `None` with a `*_measured` flag, never `0`,
because `0` is a real reading on those scales. A SUM or a COUNT over an empty
set really is zero and stays a number.

## Test and tooling debt

| Item | Detail |
|---|---|
| `test_boot_migrations.py` isolation | Fails to prove anything when any `test_t*.py` suite runs before it in the same pytest process: its reproduction drops a column and the User query then *succeeds*, so the fix it exists to prove is never exercised. Reproduced on master at `f3c6b55` with `pytest -q test_t*.py test_boot_migrations.py`. Passes standalone, and CI's coverage job is green on its own ordering — so it fails no build while proving nothing. |
| `lib/dashboard-write-errors.test.ts` | Guards only `app/dashboard/page.tsx`, not the component tree beside it |
| GMATS CSV import | No per-row savepoint. Latent only: no unique index exists on that table, so no row can fail at flush today. |

## Product gaps recorded on 2026-09-22 (not defects)

Found by three read-only assessments of the differentiator slices. These are
**absences**, not incorrect behaviour, and they are the material for the next
slices rather than cleanup.

| Gap | Where |
|---|---|
| The Root-Cause Explorer cannot say *when* — a 7-day aggregate with no timeline | `ai/root_cause.py:280-303` |
| `AIRecommendation` is a dead-end queue: no task, no approval gate, no outcome | `recommendations_routes.py`, `AIInsightsSection.tsx` |
| The Daily Brief has no "what went well" section and no money section (the collapse defect is fixed) | `ai/brief.py:268-276` |
| Outcome tracking covers 3 of 5 agents (`maintenance_task`, `escalation`, `purchase_order`) | `ai/outcomes.py:63-67` |
| No "connect your data" screen: nothing prints the broker host or the topic | `mqtt_service`, `mqtt_identity.topic_filters` |
| `Machine.site` is unreachable — not in `MachineCreate`, no route, no form | `schemas.py:84-92` |
| `reset_machines.py` mutates **every** machine in the database at import time, with no `__main__` guard and no tenant binding | `reset_machines.py:24-56` |

**Closed 2026-09-22** (machine health on the home screen): `twin.fleet_health` is the one definition, shared by the Command Centre and the pulse header.

**Closed 2026-09-22** (the owner-intelligence slice): work orders now reach both the Command Centre and the Risk Radar, and the demo plants a late one that explains its late customer order.

**Closed 2026-09-22** (the risk-actions slice): every Risk Radar risk now ends in an `action`, and a machine likely to stop carries a proposable ADR-0039 draft. The frontend write lives in one shared `ProposeActionButton.tsx`.

**Closed 2026-09-22** (the onboarding slice): `POST /production-records` finally has a screen (`ProductionEntryForm.tsx`), so a factory with no gateway can give AMP the rows OEE is measured from; and `SetupChecklist.tsx` is the first-run guidance the app never had. Still open below: the "connect your data" screen and `Machine.site`.

**Closed 2026-09-22** (the demo-factory slice): the seed is now deterministic (`reset_factory.DEMO_SEED`), all seven Command Centre problem kinds are planted and discovered, and `unit_value_gbp` is configured so the demo reads in money. `docs/sales/FACTORY-DEMO-RUNBOOK.md`.
