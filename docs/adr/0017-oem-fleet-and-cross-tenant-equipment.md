# ADR-0017: OEM fleet and cross-tenant equipment relationships

**Status:** Accepted
**Date:** 2026-08-09
**Depends on:** ADR-0002 (tenant scoping), ADR-0007 (read-models), ADR-0011 (machine
identity), ADR-0008 (tenant lifecycle), ADR-0015 (server-side gates)

## Context

AMP is a multi-tenant platform for **factories**. Every operational table carries
`tenant_code`, and one tenant is bound per request in a contextvar; a
`do_orm_execute` hook then rewrites every SELECT of a scoped model with
`tenant_code = :bound` (ADR-0002). The whole product rests on that one binding.

A machine manufacturer — an OEM — has a different shape. ABC Compressor Systems
sells compressors into Factory A, Factory B and Factory C. It needs to see *its
own machines* across all three, and it must **not** see those factories'
production, recipes, orders, inventory, costs, or each other.

That is a genuinely new axis. Every existing AMP question is "what belongs to
this tenant"; the OEM question is "what belongs to this manufacturer, across
tenants, to the extent each of those tenants has agreed".

### The measurement that shapes the whole design

The scoping hook is a no-op when nothing is bound. Measured against the real
hook, two machines seeded in two tenants:

```
bound tenant = 'FACTORY_A'            -> 1 machine   ['FACTORY_A']
bound tenant = '__OEM_NO_FACTORY__'   -> 0 machines  []
bound tenant =  None                  -> 2 machines  ['FACTORY_A','FACTORY_B']
```

An unbound request sees **everything**. So the dangerous design is the obvious
one: let OEM requests run unbound and have each OEM route filter by hand. One
forgotten filter in one handler and an OEM reads a factory's order book. That is
the same shape as the ingest defect in ADR-0011 and the approval defect in
ADR-0015 — a guard that lives in each caller instead of in the structure.

## Decision

### 1. Two scoping dimensions, each applied only when bound

`tenant_code` answers "which factory owns this row". A new `oem_code` answers
"which manufacturer owns this row". They are independent, and a request may bind
either — never both as a widening.

| Request kind | factory binding | OEM binding | Effect on factory tables |
|---|---|---|---|
| Factory user | its own tenant | none | its own rows |
| OEM user | **`__OEM__` sentinel** | its own `oem_code` | **zero rows** |
| System / ingest | none | none | unfiltered (unchanged) |

**An OEM request binds a sentinel factory tenant that matches no factory.** It is
not `None`. Every factory operational table therefore returns nothing for an OEM
principal *by construction* — before any OEM route is written, and whether or not
its author remembers to filter. Deleting the sentinel is a one-line mutation that
must turn the isolation suite red.

This is the security keystone. Everything else is defence in depth behind it.

### 2. Durable machine identity lives beside the factory's, not instead of it

`Machine` is `UNIQUE(tenant_code, site, name)` — a factory-local identity that
dies on rename (ADR-0011). The OEM needs an identity that survives renaming, IP
changes and re-topicking.

`MachineInstallation.serial_number` is that identity, **unique per OEM**, not
globally. Global uniqueness would let one OEM discover another's serials by
collision; per-OEM uniqueness makes a guessed serial belong to nobody.

`MachineInstallation.machine_id` is a *nullable* reference to the factory's
`Machine` row. Nullable because an installation exists from the moment it is
manufactured — before it is sold, shipped, installed or linked to anything live.
ADR-0011's ingest path is **not modified**: MQTT still resolves
`(tenant, site, name)`. The OEM layer reads the link; it does not become the link.

### 3. Dual ownership, resolved explicitly

`MachineInstallation` is the one entity two parties genuinely own: the factory
owns the machine, the OEM owns the equipment relationship. It therefore carries
**both** `tenant_code` and `oem_code`, and is filtered on whichever dimension is
bound:

* a factory request sees its own installations (the Connected Equipment view);
* an OEM request sees its own installations, across factories;
* neither filter can widen the other — both are `AND`ed.

It is deliberately **not** in `SCOPED_MODELS`. If it were, the `__OEM__` sentinel
would filter it to nothing and the OEM could see no fleet at all. It is registered
in `MANUALLY_SCOPED` with that reason, which is what `test_unscoped_model_reads`
demands of any tenant-owned table outside the hook.

### 4. Sharing is explicit, default-deny, and per (OEM, factory)

`OemDataSharingPolicy` is granted by the **factory**, keyed `(oem_code,
tenant_code)`, holding a CSV of grant keys (the `TenantConfig.enabled_modules`
precedent):

```
SHARE_MACHINE_HEALTH  SHARE_OPERATING_HOURS  SHARE_SERVICE_STATUS
SHARE_ALARMS          SHARE_TELEMETRY        SHARE_MAINTENANCE_HISTORY
SHARE_DOWNTIME
```

No row means **nothing is shared** beyond what the OEM already knows from having
sold the machine: its own serial, model, and which customer/site it went to.
Never `"OEM has a relationship with Factory A, therefore OEM may query Factory A"`.

Changing a policy is audited. Revocation takes effect on the **next request** —
the policy is read at query time, not baked into a cached projection — so there is
no window where a revoked OEM keeps reading.

### 5. OEM principals are a separate table, not a flag on `User`

`OemUser` is its own table with its own login path. A factory administrator's
user-management surface (`users_routes`) operates on `User` and therefore cannot
create, modify or promote an OEM principal; an OEM administrator cannot mint a
factory user. The two cannot impersonate each other because they are not the same
kind of row, and the JWT carries a `principal` discriminator that the middleware
uses to choose which dimension to bind.

Roles are OEM-specific and deliberately not the factory strings:
`OEM_ADMIN`, `OEM_SERVICE_MANAGER`, `OEM_SERVICE_ENGINEER`, `OEM_VIEWER`.

### 6. Read-models compute on read (ADR-0007), but do not ride the hook

ADR-0007 read-models "ride the auto-scoping layer". OEM read-models cannot: their
principal has no factory tenant to ride. They therefore filter **explicitly** by
`oem_code`, join to an active installation, and apply the sharing policy per
field. This is a stated divergence from ADR-0007's second clause, not an
oversight, and it is why every OEM read-model takes `oem_code` as a required
argument rather than reading it from a contextvar.

No materialised projections in this ADR. Compute-on-read stays correct as the
underlying data changes and needs no sync job; materialisation is a performance
decision to be made against a measurement, not a guess.

### 7. Events extend the existing vocabulary

OEM events are frozen dataclasses on the existing bus (ADR-0001), carrying
`oem_code` and — where a factory is involved — `tenant_code`. Existing events are
reused where the semantics already match; `MachineInstalled`,
`MachineCommissioned`, `ServiceDue`, `ServiceCompleted`, `WarrantyExpired` are new
because nothing existing means those things.

### 8. White-label is configuration, not a build

`OemOrganization` carries brand name, colour, logo URL and support contact, the
same shape as `TenantConfig`'s branding. One frontend build; the OEM portal reads
its branding from the API. No per-OEM forks, no per-OEM deployments.

## Domain model

```
OemOrganization (oem_code)
    │
    ├── OemUser              (oem_code, role, is_active)
    ├── MachineModel         (oem_code, family, model_code, telemetry profile)
    │        │
    │        └── MachineInstallation
    │                 serial_number   ← durable identity, unique per OEM
    │                 oem_code        ← manufacturer
    │                 tenant_code     ← factory customer
    │                 site            ← plant
    │                 machine_id      ← nullable link to the factory Machine
    │                 lifecycle status, commissioning, warranty
    │
    └── OemDataSharingPolicy (oem_code, tenant_code) ← granted BY the factory
```

## Security boundaries

Each is a property to be tested, not an intention:

1. An OEM request reads zero rows from every factory operational table.
2. OEM A cannot read OEM B's models, installations, users or service records.
3. An OEM sees a factory field only if that factory's policy grants it.
4. Revoking a policy removes access on the next request.
5. A disabled or deleted OEM user cannot authenticate or act.
6. A guessed serial or installation id belonging to another OEM is a 404.
7. A factory token cannot reach `/oem`; an OEM token cannot reach factory routes.
8. Factory ↔ factory isolation is unchanged.

## Alternatives rejected

**Give OEM users a factory `User` row per customer.** An OEM engineer would hold
N logins and every one of them would be a real factory principal with a real
factory tenant bound — precisely the "OEM belongs to Factory A therefore OEM can
query Factory A" model the brief forbids.

**Run OEM requests unbound and filter in each route.** The measurement above shows
unbound means *all tenants*. One missing filter is a cross-customer breach, and
nothing structural would catch it.

**Put `oem_code` on `Machine`.** It would drag the OEM concept through every
factory query, and a machine can be resold or re-badged; the relationship belongs
on the installation, not on the machine.

**Materialise OEM projections now.** A cached copy of shared factory data would
survive revocation of the policy that permitted it. Compute-on-read cannot.

**Globally unique serial numbers.** Lets one OEM enumerate another's fleet by
collision probing. Unique per OEM instead.

## Migration strategy

New tables only — `0006_oem_foundation`, on `0005_approval_gate`. No existing
table is altered, so:

* every existing factory query is byte-identical;
* an AMP database with no OEM rows behaves exactly as it does today;
* `downgrade()` drops the new tables and leaves the factory schema untouched.

## Backwards compatibility

The OEM layer is **additive**. No existing column changes type or nullability, no
existing route changes shape, no existing event changes payload. A factory user
who never meets an OEM sees no difference. `SCOPED_MODELS` is unchanged in
membership, so the ADR-0002 hook behaves identically for every existing model —
the lockstep count in `test_tenancy` is untouched because no new model joins the
factory-scoped set.
