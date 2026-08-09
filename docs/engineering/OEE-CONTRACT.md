# The AMP OEE contract

**One definition. One window. One honesty rule.**
Implemented in `backend/oee_contract.py`; pinned by `backend/test_oee_contract.py`.
Decision record: [ADR-0014](../adr/0014-canonical-oee-contract.md).

---

## 1. The mathematics

For a set of production records **R**, all sums taken over R:

$$
A = \frac{\sum \text{runtime\_minutes}}{\sum \text{planned\_minutes}}
\qquad
P = \frac{\sum (\text{ideal\_cycle\_time\_seconds} \times \text{total\_count})}
         {\sum \text{runtime\_minutes} \times 60}
\qquad
Q = \frac{\sum \text{good\_count}}{\sum \text{total\_count}}
$$

$$
\mathrm{OEE} = A \times P \times Q
$$

**Pooled, not averaged.** Each component is a *ratio of sums*, so a machine is
weighted by the time and volume it actually contributed. Averaging per-record
OEE — a mean of ratios — over-weights small runs.

Worked example (golden 3): a large perfect machine and a tiny terrible one.

| | planned | runtime | ideal | total | good |
|---|---|---|---|---|---|
| M1 | 480 | 480 | 60 s | 480 | 480 |
| M2 | 60 | 30 | 60 s | 10 | 5 |
| **Σ** | **540** | **510** | — | **490** | **485** |

$A = 510/540 = 0.9444$, $P = 29400/(510{\times}60) = 0.9608$,
$Q = 485/490 = 0.9898$, $\mathrm{OEE} = 0.8981 \rightarrow \mathbf{90\%}$.

The naive average of the two machines' individual OEE (100% and 8%) is **54%** —
thirty-six points wrong, because M2 made 10 parts out of 490.

**Clamping.** Every component is clamped to $[0, 1]$, symmetrically. Above 1 is a
figure the data cannot support (runtime beyond planned, good beyond total);
below 0 is the same violation reachable from a legacy row holding a negative
count. Percentages are rounded **once**, at the presentation boundary.

---

## 2. The window

$$
\text{window} = [\,\text{now} - d,\ \text{now}\,)
$$

**Start inclusive, end exclusive.** Half-open so adjacent windows tile without
overlap: $[d{-}14, d{-}7)$ and $[d{-}7, d)$ share no record, so a week-over-week
comparison cannot double-count the boundary.

Default $d = 7$ days — a factory week is the unit operations manage in, and a
shorter window swings on one bad shift.

`days=None` means all time. It is correct only for an explicit "since
commissioning" view and is **never** the default.

---

## 3. Definitions

| Term | Definition |
|---|---|
| **Planned time** | Σ `planned_minutes` on records *inside the window*. A period with no record contributes **no** planned time — we do not know a shift was planned unless something said so. Inventing planned minutes for a silent machine would manufacture downtime. |
| **Unplanned** | `planned_minutes == 0`. No shift was scheduled. Availability is **undefined**, not 0%. There was nothing to be efficient at; reporting 0% invents a loss. |
| **No data** | `has_data` is true only when `planned > 0 OR total > 0` — something was scheduled or something was made. It previously meant "a row exists", so an empty shift rendered as a measured 0%. |
| **Offline** | A machine whose telemetry has stopped. It cannot be measured, so it contributes nothing to numerator or denominator. It must **not** vanish silently — see coverage. |
| **Undefined component** | Returned as `None`, never `0.0`. `as_percentages()` renders it as `None` unless a caller explicitly asks otherwise. A caller cannot accidentally display "not measurable" as "measured zero". |
| **OEE when a component is undefined** | Also `None`. OEE is a product; it needs all three. |

---

## 4. Measurement coverage

Every plant-level result carries how much of the plant it measured:

```json
"coverage": {
  "machines_expected":  3,
  "machines_reporting": 2,
  "coverage_pct":       67,
  "complete":           false
}
```

**Why this exists.** Measured on master before the contract: three machines, the
worst one's gateway drops off the network.

```
plant OEE while the worst machine still reports : 67%
plant OEE after it goes silent                  : 94%   (+27 points)
```

Its rows stop existing, so it leaves the denominator. **The metric improves
exactly when visibility is lost.** The figure itself cannot be corrected — the
data is genuinely gone — but presenting a partial measurement as a whole-plant
number is a choice, and this contract refuses to make it.

Every surface showing a plant OEE must be able to state its coverage. The AI
assistant's context does so in prose:

> PLANT OEE (last 7 days, pooled): 95% … — measured from 1 of 3 machines
> (33% coverage); the rest reported nothing in this window

---

## 5. Every surface, one contract

| Surface | Window before | Window now |
|---|---|---|
| `/oee-summary` (dashboard) | last 7 days | last 7 days, **+ coverage** |
| AI copilot LLM context | **last 10 records** | last 7 days, + coverage |
| `plant_oee()` / `machine_oee()` | — | last 7 days |

Measured before, on one factory at one moment:

```
surface                                       window                OEE
----------------------------------------------------------------------
/oee-summary  (the dashboard)                 last 7 days           100%
ai_copilot LLM context                        last 10 records        10%
```

A customer asking the assistant "how is the plant doing?" got an answer **90
points** from the screen in front of them. Both now answer 100%.

---

## 6. Known limitations

These are properties of the schema, not of the contract. They are listed because
a contract that omits what it cannot do is not a contract.

**L1 — Downtime does not reconcile with availability.** Availability comes from
`ProductionRecord` (planned − runtime); downtime comes from `DowntimeLog`. Two
tables, no link. Measured: a shift losing 80 minutes by the production record
carried 120 minutes of downtime logs — 83% availability against 75% implied.
Nothing detects the contradiction. *Fix requires a reconciliation report, or
deriving availability from the downtime ledger.*

**L2 — Overlapping downtime is not representable.** `DowntimeLog` stores a
`duration` and a `created_at`, not a start and an end. Two operators logging the
same hour as a breakdown and a changeover produce 120 minutes of loss from 60
minutes of stoppage, and no query can tell. *Fix requires an interval column,
not a calculation.*

**L3 — Rework is invisible.** A reworked unit is either counted good (hiding the
first-pass failure) or scrap (hiding the recovery). First-pass yield cannot be
derived from `total_count` and `good_count` alone. *Fix requires a rework
count.*

**L4 — Day and shift boundaries are UTC.** `TenantConfig` has no timezone
column. A plant on IST has its 06:00 shift split across two UTC "days" in every
daily rollup, and a night shift crosses midnight mid-shift. Daily trends are
therefore UTC-day trends, correct only for a UTC plant. *Fix requires a tenant
timezone and shift-calendar model.*

All four are recorded as open risks in
[`PRODUCTION-READINESS-FINAL.md`](PRODUCTION-READINESS-FINAL.md).

---

## 7. Golden datasets

`backend/test_oee_contract.py`. Every expected value is derived **by hand** from
the definitions above and written out longhand beside the assertion. None is
copied from a program run — if the implementation and the expectation both came
from the same code, the test would say only that the code equals itself.

| # | Dataset | Expected |
|---|---|---|
| 1 | A perfect shift | A 100, P 100, Q 100, OEE 100 |
| 2 | The textbook example (480/400/30 s/600/570) | A 83, P 75, Q 95, **OEE 59** |
| 3 | Pooling vs averaging (large perfect + tiny terrible) | **OEE 90**, not the naive 54 |
| 4 | Unplanned period | `has_data` false, every component `None` |
| 5 | Scheduled, ran, produced nothing | A 1.0, P 0.0, Q `None` — a *real* zero |
| 6 | Scrap (100 made, 90 good) | Q 90 |
| 7 | Impossible values | clamped to [0, 100] both ways |
| 8 | Window boundary | start included, end excluded, adjacent windows disjoint |
| 9 | A silent machine | coverage 2/3 = 67%, `complete` false |
| 10 | Two tenants, no tenant bound | 100% and 25%, neither sees the other |
| 11 | Dashboard vs AI assistant | identical number, identical window |
| 12 | Partial coverage in the AI context | stated in prose, not hidden |

`backend/mutate_oee_contract.py` — 16 mutations, all caught: each formula term,
each honesty rule, both window bounds, both coverage fields, both tenant
filters, and the AI-context wiring.
