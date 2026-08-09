# ADR-0013: A bill of materials belongs to a tenant, and lives in the database

**Status:** Accepted
**Date:** 2026-08-09
**Depends on:** ADR-0001 (event bus), ADR-0002 (tenant scoping), ADR-0012 (per-tenant item codes)

## Context

The recipe book was a module-level dict in `bom.py`:

```python
PART_BOM = {
    "SHAFT-001": {"raw": "RM-STEEL-001", "consume_per_unit": 2, "fg": "FG-SHAFT-001"},
    ...
}
```

Six parts, one component each, compiled into the application and shared by every
customer. Two measured consequences.

**One customer's rates applied to another customer's stock.** Two tenants who
had never heard of each other, both making a part they call SHAFT-001:

```
FACTORY_B completes a work order for 10 of THEIR OWN SHAFT-001:
   FACTORY_B  RM-STEEL-001   stock=80      (was 100)
   GMATS      RM-STEEL-001   stock=100     (untouched)
```

Worth being precise: **isolation was never breached.** GMATS's stock was not
read or written. The defect is that FACTORY_B's stock moved at 2 per unit — a
number nobody at FACTORY_B entered and could not change. If their shaft really
consumes 3.5 kg, their inventory is quietly wrong forever, and no screen in the
product lets them correct it.

**A customer's own products moved nothing at all.**

```
FACTORY_B completes 50 of VALVE-77 — a product THEY make:
   B-ALLOY-9      stock=500     (unchanged)
   inventory transactions written: 0
```

`PART_BOM.get(part_number)` missed, the subscriber returned, and nothing was
logged. So the inventory half of the platform only ever worked for the one
company whose recipes had been written into the source — which is the
`build-generic-not-GMATS` directive violated in the most literal way available.

A third, smaller thing surfaced while removing it: `work_orders_routes`'
docstring claimed "create validates the part against the BOM (bom.PART_BOM)".
It does not. The import was dead and the validation did not exist.

## Decision

### 1. Header and lines, not one flat table

`BillOfMaterials` (tenant, part_number, revision, output_item_code,
effective_from, active, audit fields) with `BomComponent` lines
(component_code, quantity_per_unit, unit).

A bill of materials has many components, and the finished good produced is a
property of the **product**, not of each component. Flattening it would let two
lines of one BOM disagree about what they build — a contradiction the schema
should not be able to represent.

### 2. `quantity_per_unit` is a Float

The dict's `consume_per_unit` was an `int`, which cannot express "1.5 kg of bar
per shaft" — the single most ordinary thing a bill of materials says. The API
rejects zero, negative and non-finite values: a zero rate describes no
consumption (delete the line), and a negative one would make a completion
*increase* stock, which is the sign error ADR-0010 exists to keep out of the
data.

### 3. Resolution is explicit, and ordered

`bom.resolve(db, tenant, part_number)` returns the one BOM that applies:
scoped to the tenant, `active` only, `effective_from` past-or-null, and of those
the latest effective date wins (ties on id, so re-issuing on the same day takes
effect).

The tenant filter is stated rather than inherited from the ADR-0002 hook. This
runs from an event subscriber, which a script or background job can drive with
nothing bound — the same shape as the ingest defect in ADR-0011. Mutation
testing confirms the distinction is real: removing `resolve`'s tenant filter is
caught, while removing the equivalent filter from the HTTP handlers is not,
because those are only ever reached with the hook installed.

### 4. A part with no recipe still moves nothing

The same outcome as the old dict's miss — but it is now a state a customer can
fix by entering their recipe, rather than a fact about our source code. The BOM
screen says so explicitly when the list is empty, because an empty table on its
own reads as "nothing to see" rather than "completions move no stock".

### 5. Access stays Admin, deliberately unchanged

Editing a recipe moves inventory for every future completion. That is not a
supervisor-level action, and widening the read at the same time as introducing
writes would be two access decisions hidden in one change.

## Consequences

**`GET /bom` changes shape** — one row per *component* rather than one per part.
`BomViewer.tsx` is updated in the same change: it keyed rows on `part_number`,
which stops being unique the moment a part has two components. React does not
throw on a duplicate key; it warns and then reconciles siblings wrongly, which
surfaces later as rows swapping after a re-render.

**The migration carries the six legacy recipes forward — but not to everyone.**
Seeding them for every tenant would preserve the exact defect. A recipe is
seeded only where it is demonstrably that tenant's already: they stock an
`InventoryItem` whose code it names. A tenant that has never held
`RM-STEEL-001` starts with an empty recipe book, which is the correct state for
a customer whose products we know nothing about. Verified on PostgreSQL: the
incumbent kept its recipes at the built-in rates, a second tenant inherited
nothing, and a re-run neither duplicated rows nor overwrote a customer's edit.

**Numbers can now be fractional.** Any report that assumed integer consumption
should be re-read; the pooled OEE and cost read-models already work in floats.

**Not addressed here:** there is still no validation that a work order's part
has a BOM at all. A work order for a part with no recipe is legitimate (it may
be a service or sub-operation), so refusing it would be wrong — but there is no
warning either, and "why did my completion not move stock" is the support
question that will follow. A visible indicator on the work-order screen is the
right next step and is deliberately out of scope for a change already touching
the schema, the API and the ingest path.

## Verification

`backend/test_per_tenant_bom.py` — three tenants with three genuinely different
rates for the same part number (2.0, 3.5, 0.25 — the last two impossible under
the old integer dict), each consuming its own; a multi-component product the
platform had never heard of; a zero-rate line writing no ledger row; the
revision-resolution order with a control proving a future revision *does* take
over once its date passes; and the API's validation, RBAC and isolation,
including that a rejected create leaves no partial recipe behind.

`backend/mutate_bom.py` — 15 mutations. 13 caught. Two are marked `shadowed`
rather than silently passed: removing the tenant filter from `list_bom` /
`update_bom` changes nothing because the ADR-0002 hook is installed
unconditionally for HTTP routes and still 404s the cross-tenant edit. That is a
different situation from `bom.resolve` and the harness records why, so the
reasoning sits where the next person will look.

`frontend/components/BomViewer.test.tsx` — seven tests against the rendered
table, and four mutations of the component all caught, including reverting the
key to `part_number`.

`backend/verify_pg_bom.py` — migration 0004 against **PostgreSQL 18.3**: an
existing deployment mid-upgrade, the backfill's selectivity in both directions,
a re-run that neither duplicates nor overwrites, and a fresh database.
