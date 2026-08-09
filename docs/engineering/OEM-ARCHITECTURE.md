# The OEM layer: architecture and data ownership

**Decision record:** [ADR-0017](../adr/0017-oem-fleet-and-cross-tenant-equipment.md)

AMP sits between machine manufacturers and their customers' factories. This
document is the map: what exists, why it is shaped this way, and — the part that
matters commercially — what a manufacturer can and cannot see.

---

## 1. The one sentence that governs everything

> **A relationship is not consent.**

Selling a factory a compressor tells you nothing about their order book. Two
**independent** things must both be true before an OEM sees a field:

1. the machine is one the OEM installed — an **installation row**
2. that factory granted that class of data — a **sharing policy**

Neither implies the other. AMP never infers *"the OEM has machines at FACTORY_A,
therefore the OEM may query FACTORY_A"*.

---

## 2. Two ownership dimensions

`tenant_code` answers *which factory owns this row*. `oem_code` answers *which
manufacturer owns this row*. They are independent, and a request binds one.

| Request | factory binding | OEM binding | Factory tables return |
|---|---|---|---|
| Factory user | its own tenant | none | its own rows |
| **OEM user** | **`OEM:<code>` sentinel** | its own `oem_code` | **zero rows** |
| System / ingest | none | none | unfiltered (unchanged) |

### Why a sentinel and not `None`

The ADR-0002 hook filters every scoped model to the bound tenant and is a
**no-op when nothing is bound**. Measured against the real hook, two machines in
two tenants:

```
bound 'FACTORY_A'            -> 1 machine   ['FACTORY_A']
bound 'OEM:OEM_ALPHA'        -> 0 machines  []
bound  None                  -> 2 machines  ['FACTORY_A','FACTORY_B']
```

So the dangerous design is the obvious one — run OEM requests unbound and filter
in each route. One forgotten filter is a cross-customer breach. Binding a tenant
string **no factory can hold** makes factory data invisible *by construction*,
before any OEM route exists.

A tenant code is a plain identifier and cannot contain a colon, so `OEM:` can
never collide with a real tenant.

---

## 3. Components

```
AMP CORE
├── Factory context        machines, orders, inventory, OEE …   (unchanged)
├── OEM context
│    ├── oem_auth.py       principals, roles, capabilities, the sentinel
│    ├── oem_sharing.py    grants + the fleet the OEM may see
│    ├── oem_service.py    lifecycle, warranty, service intelligence
│    ├── oem_telemetry.py  per-model signal profiles
│    ├── oem_routes.py     /oem/*
│    └── connected_equipment_routes.py   the FACTORY's side of the same edge
├── Industrial connectivity  MQTT (ADR-0011) — unchanged
├── Event bus                ADR-0001 — unchanged
├── Read models              ADR-0007, computed on read
└── Identity / authorization auth.py (factory) + oem_auth.py (OEM)
```

---

## 4. Domain model

```
OemOrganization (oem_code)
    ├── OemUser              separate table from User — see §6
    ├── MachineModel         catalogue + telemetry_profile (JSON in Text)
    │      └── MachineInstallation
    │             serial_number        durable identity, UNIQUE PER OEM
    │             oem_code             manufacturer
    │             factory_tenant_code  customer  ← NOT named tenant_code, see §7
    │             machine_id           nullable link to the factory Machine
    │             lifecycle, warranty, operating_hours, last_service_hours
    └── OemDataSharingPolicy (oem_code, tenant_code)  granted BY the factory
```

### Durable identity

`Machine` is `UNIQUE(tenant_code, site, name)` — factory-local, and it dies on
rename (ADR-0011). `serial_number` survives a rename, an IP change or a new MQTT
topic.

**Unique per OEM, not globally.** A global serial namespace would let one
manufacturer enumerate another's fleet by probing for collisions.

`machine_id` is nullable: an installation exists from manufacture, before it is
sold, shipped or linked. The link *references* the factory's machine — MQTT
still resolves `(tenant, site, name)` exactly as ADR-0011 specifies.

---

## 5. The sharing vocabulary

| Grant | What it reveals |
|---|---|
| `SHARE_MACHINE_HEALTH` | health score, connectivity, machine status |
| `SHARE_OPERATING_HOURS` | operating and loaded hours |
| `SHARE_SERVICE_STATUS` | service due / overdue |
| `SHARE_ALARMS` | equipment alarm codes |
| `SHARE_TELEMETRY` | live readings |
| `SHARE_MAINTENANCE_HISTORY` | maintenance work done |
| `SHARE_DOWNTIME` | downtime events |

**No row = nothing shared**, beyond what the OEM already knows from having sold
the machine (its serial, its model, which customer site it shipped to — the
OEM's own records). An *empty* policy row is a considered "no" and behaves
identically.

**Nothing in this vocabulary can reach** orders, recipes, BOMs, inventory,
costs, customers, production quantities, quality detail or operators. That is
asserted as a test, not a promise.

**Read at query time, never cached.** A cached projection of shared data would
survive the revocation of the policy that permitted it.

---

## 6. Principals

`OemUser` is a **separate table** from `User`. A factory administrator's user
surface operates on `User`, so it cannot create, promote or disable an OEM
login; an OEM administrator cannot mint a factory user. They cannot impersonate
each other because they are not the same kind of row.

| Role | Capabilities |
|---|---|
| `OEM_ADMIN` | read_fleet, manage_models, manage_installations, manage_users, manage_branding, manage_service, commission |
| `OEM_SERVICE_MANAGER` | read_fleet, manage_service, manage_installations |
| `OEM_SERVICE_ENGINEER` | read_fleet, manage_service, commission |
| `OEM_VIEWER` | read_fleet |

Routes ask for **capabilities**, not role names, so adding a role later cannot
silently widen an existing route.

`oem_auth.resolve` re-reads the principal **from the database on every request** —
affordable here (an OEM login is rare and long-lived) and necessary (a stale
claim is a cross-*company* breach, not a cross-page one). A demotion or
suspension therefore takes effect on the next request.

---

## 7. Two traps this design walked into, and how

**Offboarding a customer would have deleted the OEM's fleet history.**
`offboard_tenant.purge_tenant_data` hard-deletes rows from every model carrying a
`tenant_code` **attribute**. An installation carries the *customer's* tenant but
belongs to the *OEM*. The column is therefore named `factory_tenant_code`, which
the sweep cannot see, and offboarding **unlinks** instead
(`_unlink_oem_installations`).

**The new foreign key broke offboarding outright.**
`machine_installations.machine_id → machines.id` blocked the machine delete
(`purge blocked by constraints on: machines`). SQLite does not enforce foreign
keys by default and showed nothing; PostgreSQL failed immediately. The unlink now
runs *before* the sweep.

---

## 8. Service intelligence, not predictive maintenance

Everything is an arithmetic rule over reported facts. Every recommendation
carries `reason`, `evidence`, `action`, `machine` (by serial) and a timestamp.

`confidence` is present **only** on the hours-trend projection, where it is
derived from the sample size and span — and capped at 0.85, because it is a
straight line through an hours counter. Arithmetic recommendations carry
`confidence: null` rather than a decorative number.

The module refuses to guess:

| Situation | What it says |
|---|---|
| model has no service interval | `not_configured` |
| machine never reported hours | `unknown` |
| no warranty end recorded | `unknown` — not "expired" |
| fewer than 3 samples, or under a day | **no projection at all** |
| hours counter below the last service | `unknown` — "probably reset" |
| machine silent | "off, disconnected, or faulty — cannot be told apart from here" |

### The bug this section exists because of

Service position was `operating_hours % interval`, which silently assumes every
service happened on schedule — making `overdue` **unreachable**. A machine at
2,100 h against a 2,000 h interval that was never serviced reported *"1,900 h
remaining"* instead of *100 h overdue*. `last_service_hours` (migration 0007)
records the real last service; NULL means never serviced.

---

## 9. Telemetry profiles

A profile is **data on the machine model**, not code. AMP core knows the *shape*
of a profile and never the names inside it — asserted by a test that greps the
interpreter for `pressure`, `spindle`, `compressor`, `cnc`.

Out-of-range readings are **flagged, never clamped**: a discharge temperature of
400 °C is either an emergency or a broken sensor, and clamping would turn an
overheating machine into a healthy-looking one. Unknown source tags are
**reported**, because a gateway sending a tag nobody configured is a
commissioning defect that silence would hide.

---

## 10. Performance

Measured on PostgreSQL 18.3, one machine, real statement counting:

| machines | fleet page | queries | service queue | queries | one customer | queries |
|---|---|---|---|---|---|---|
| 10 | 4.9 ms | 9 | 0.8 ms | 1 | 1.1 ms | 2 |
| 100 | 3.8 ms | 9 | 1.2 ms | 1 | 0.7 ms | 2 |
| 1,000 | 11.4 ms | 9 | 10.9 ms | 1 | 2.8 ms | 2 |
| 10,000 | 136.4 ms | **9** | 94.5 ms | **1** | 8.8 ms | **2** |

**Query counts are constant across a 1000× range — there is no N+1.** Grants are
cached per *customer*, so a 10,000-machine fleet across 8 sites costs 8 policy
reads, not 10,000.

---

## 11. Future: AMP Edge

Not built. Documented so the identity model does not have to change later.

```
PLC → AMP Edge → MQTT/TLS → AMP Cloud
```

`MachineInstallation.serial_number` is the durable identity a device certificate
would be issued against. **What does not exist today:** device certificates,
secure provisioning, store-and-forward buffering, OTA configuration, connector
deployment. Ingest is MQTT as per ADR-0011, and the OEM layer reads what ingest
has already written.

---

## 12. Where to look

| Question | File |
|---|---|
| Can an OEM reach factory data? | `test_oem_authorization.py`, `audit_oem_adversarial.py` |
| What does a grant reveal? | `oem_sharing.py`, `test_oem_sharing.py` |
| Is the API boundary right? | `oem_routes.py`, `test_oem_routes.py` |
| Does the factory control consent? | `connected_equipment_routes.py`, `test_connected_equipment.py` |
| Is the service maths honest? | `oem_service.py`, `test_oem_service.py` |
| Does it scale? | `oem_perf.py` |
| Would removing a guard be caught? | `mutate_oem_auth.py`, `mutate_oem_sharing.py` |
