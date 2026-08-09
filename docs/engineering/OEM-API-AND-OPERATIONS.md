# OEM API and operations guide

Companion to [OEM-ARCHITECTURE.md](OEM-ARCHITECTURE.md) and
[ADR-0017](../adr/0017-oem-fleet-and-cross-tenant-equipment.md). This is the
working document: the endpoints, and the four procedures somebody actually
performs — onboard a manufacturer, commission a machine, run a service desk,
grant and withdraw sharing.

---

## Part 1 — The API

### Authentication

An OEM session is a JWT carrying `principal: "oem"` and `oem: "<OEM_CODE>"`.
It is **not interchangeable** with a factory session in either direction:

| Token | on `/oem/*` | on factory routes |
|---|---|---|
| OEM | works | **403** — "This is an OEM session; use the /oem portal" |
| factory | **401** — not an OEM session | works |

The principal is re-read from the database on every request, so a suspended or
demoted OEM login stops working on its next call rather than at token expiry.

### `/oem` endpoints

All require `read_fleet`. All are read-only in this release.

| Method | Path | Returns |
|---|---|---|
| GET | `/oem/me` | identity, role, capabilities, white-label branding |
| GET | `/oem/models` | this manufacturer's catalogue |
| GET | `/oem/fleet` | installed fleet, sharing applied per field |
| GET | `/oem/customers` | customers and sites, with what each has granted |
| GET | `/oem/sharing` | the consent position per customer |
| GET | `/oem/service` | the service queue across the whole fleet |
| GET | `/oem/machines/{id}` | one machine, plus `not_shared` |
| GET | `/oem/machines/{id}/service` | service, warranty, commissioning, signals |
| GET | `/oem/models/{id}/telemetry` | one model's telemetry profile |

**`GET /oem/fleet`** — `?customer=`, `?limit=` (max 200), `?offset=`.

`customer` **narrows and can never widen**. The `oem_code` filter is applied
first and unconditionally, so passing a customer belonging to another
manufacturer returns an empty page, not that manufacturer's fleet. (This is
tested. An early version of the test passed for the wrong reason — it filtered to
nothing by coincidence — so the assertion now uses a customer a *competitor*
really has.)

```jsonc
{
  "total": 2, "limit": 100, "offset": 0,
  "machines": [{
    "installation_id": 4,
    "serial_number": "ALPHA-0001",     // the OEM's own record — always visible
    "model_code": "X200", "customer": "FACTORY_A", "site": "Plant 1",
    "lifecycle_status": "Active",
    "operating_hours": 1850.0,          // present ONLY with SHARE_OPERATING_HOURS
    "machine_status": null,             // null here = NOT SHARED, not zero
    "shared": ["SHARE_OPERATING_HOURS"]
  }]
}
```

**A null is ambiguous on the wire and must not be ambiguous on the screen.**
`shared` is what makes it readable, and the frontend's `shareable()` renders
`"not shared"` / `"no data"` / the value as three distinct states. A service desk
that reads a withheld field as `0 h` books an engineer against a number nobody
supplied.

**`GET /oem/machines/{id}`** — **404, never 403**, for another manufacturer's
installation. A 403 confirms the row exists, which turns id probing into fleet
enumeration. Adds `identity` (manufacturer, model, serial, firmware) and
`not_shared` — what the customer has withheld, stated rather than silently
omitted.

### `/connected-equipment` — the factory's side

These are **factory** routes and reject OEM tokens.

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/connected-equipment` | any factory user | OEM machines here, and what each OEM can see |
| PUT | `/connected-equipment/sharing` | **Admin** | grant or withdraw |

```jsonc
// PUT /connected-equipment/sharing
{ "oem_code": "OEM_ALPHA", "grants": ["SHARE_OPERATING_HOURS", "SHARE_ALARMS"] }
```

The list is **absolute, not additive** — it replaces what was granted, so
withdrawal is `grants: []` and needs no separate verb.

Refusals, and why each one is a refusal rather than a shrug:

| Condition | Status | Why |
|---|---|---|
| unknown grant key | **400** | dropping it silently lets an admin believe they shared something they did not — or, after a rename, something they did |
| OEM has no equipment here | **404** | granting to a manufacturer never on the shop floor is a typo or a third-party hand-off |
| blank `oem_code` | **400** | — |
| non-Admin | **403** | consent is an administrative act |

Every change writes an audit row with **before and after**, because "who agreed
to share our operating hours, and when" is asked after something goes wrong.

There is **no OEM-side equivalent of this endpoint**. A manufacturer that can
edit its own permissions has permissions in name only.

---

## Part 2 — Onboarding a manufacturer

1. **Create the organisation.** `oem_code` is durable, appears in audit rows and
   cannot be renamed casually. Uppercase, no colon (`OEM:` is the reserved
   sentinel prefix — see the architecture doc §2).
2. **Brand it.** `brand_name`, `brand_color`, `brand_logo_url`, `support_email`,
   `support_phone`. These drive `/oem/me`, which drives the portal's appearance.
   **One build, configuration per OEM — never a fork.** A fork means a security
   fix has to land N times, and the Nth is the one that gets forgotten.
3. **Create users** with the narrowest role that works:

   | Role | Give it to |
   |---|---|
   | `OEM_ADMIN` | one or two people; it can manage users and branding |
   | `OEM_SERVICE_MANAGER` | whoever runs the service desk |
   | `OEM_SERVICE_ENGINEER` | field engineers — read plus commission |
   | `OEM_VIEWER` | sales, management, anybody who only looks |

4. **Load the catalogue.** Per model: `model_code`, `family`, `name`,
   `service_interval_hours`, `warranty_months`, and a telemetry profile.

### The telemetry profile

Data on the model, not code. AMP core never learns the names inside it.

```json
{"signals": [
  {"name": "discharge_pressure", "source": "PRESS_01", "unit": "bar",
   "kind": "gauge", "min": 0, "max": 16, "normal_min": 6, "normal_max": 10},
  {"name": "running", "source": "RUN", "kind": "state"}
]}
```

`name` is yours. `source` is the tag the gateway publishes. `min`/`max` are the
instrument's range; `normal_min`/`normal_max` are the healthy band.

Two behaviours worth knowing before you write one:

- **Out-of-range values are flagged, never clamped.** A discharge temperature of
  400 °C is either an emergency or a dead sensor. Clamping it to the maximum
  turns an overheating machine into a healthy-looking one.
- **Unknown source tags are reported.** A gateway publishing a tag nobody
  configured is a commissioning defect, and silence would hide it.

---

## Part 3 — Commissioning

Lifecycle: `Manufactured → Shipped → Installed → Commissioned → Active`, then
`Decommissioned` or `Retired`. Transitions are checked, and skipping a step is
refused — a machine cannot be `Active` before anybody said it was installed.

`GET /oem/machines/{id}/service` returns a **commissioning report**: a list of
checks, each with `key`, `description`, `passed` and a `detail` that says what is
actually missing.

| Check | Passes when |
|---|---|
| `customer_assigned` | the installation names a factory tenant |
| `site_recorded` | a site is recorded |
| `machine_linked` | linked to the factory's AMP machine |
| `telemetry_profile` | the model has a valid profile |
| `data_received` | the machine has reported at least once |
| `warranty_recorded` | warranty start and end are recorded |

`ready` is true only when every check passes. **It is advice, not a gate** — AMP
does not refuse to run a machine somebody commissioned differently; it tells you
what was skipped.

A failing `data_received` is the common one, and it means exactly what it says:
no reading has arrived. It does not distinguish a machine that is switched off
from a gateway pointed at the wrong broker — the report says so rather than
guessing.

---

## Part 4 — Running the service desk

`GET /oem/service` is the whole-fleet queue. Each recommendation carries
`kind`, `severity`, `machine` (by serial), `reason`, `evidence`, `action`,
`confidence` and `at`.

**Why this needs no grant from the factory:** service position is computed from
the manufacturer's *own* records — the serial it shipped, the hours the machine
reported to it, the interval on its own model. What a grant controls is the
*factory's* data: machine status, utilisation, downtime. A recommendation
therefore never quotes them.

### What it will not claim

`confidence` appears **only** on the hours-trend projection, derived from sample
size and span, capped at 0.85 — it is a straight line through an hours counter.
Arithmetic recommendations carry `confidence: null` rather than a decorative
number.

| Situation | The answer |
|---|---|
| model has no service interval | `not_configured` |
| machine never reported hours | `unknown` |
| no warranty end recorded | `unknown` — **not** "expired" |
| fewer than 3 samples, or under a day | no projection at all |
| hours below the last service | `unknown` — "counter was probably reset" |
| machine silent | "off, disconnected, or faulty — cannot be told apart from here" |

### Recording a service

Set `last_service_hours` (and `last_service_at`) when work is done. Until then
the machine is treated as **never serviced**, which is the safe direction.

This exists because of a real bug: service position used
`operating_hours % interval`, which quietly assumes every service happened on
schedule and made `overdue` **unreachable**. A machine at 2,100 h against a
2,000 h interval, never serviced, reported *"1,900 h remaining"* instead of
*100 h overdue*.

---

## Part 5 — Sharing, from the factory's chair

Open **Connected Equipment**. It answers three questions: which machines here
came from an OEM, what that OEM can see, and how to change it.

Grant the least that makes the manufacturer useful. A reasonable start for a
compressor supplier is `SHARE_OPERATING_HOURS` + `SHARE_SERVICE_STATUS` +
`SHARE_ALARMS` — enough to service the machine, nothing about what you make on
it.

**Withdrawal is immediate.** Grants are read at query time and never cached, so
the next request the manufacturer makes returns less. There is no projection
built earlier that outlives the permission that allowed it.

**What no grant can ever reveal:** work orders, production quantities, recipes,
BOMs, inventory, purchase orders, suppliers, customers, costs, operator names,
quality detail, factory-wide analytics, or any machine the manufacturer did not
supply. That is asserted by 60 adversarial checks on PostgreSQL, not by policy.

---

## Part 6 — For developers

### Adding an `/oem` route

1. Depend on `oem_auth.require_oem("<capability>")`. Ask for a **capability**,
   never a role name.
2. Take `oem_code` from `principal["oem"]` — **never** from a path, query or
   body parameter. A route that accepts one as input is a tenancy bug regardless
   of what it checks afterwards.
3. Fetch installations through `oem_sharing.installations_for` /
   `get_installation`. A second copy of a security filter is a second chance to
   get it subtly wrong.
4. Apply grants per field via `oem_sharing.fleet_row`, or read them with
   `grants_for` at query time. **Do not cache a projection of shared data.**
5. Return **404** for another manufacturer's row.
6. Write the adversarial test — the one where the *other* OEM asks — before the
   happy path.

### Things that will bite you

- **Never name a column `tenant_code` on an OEM-owned table.** Three separate
  mechanisms key off that literal attribute name: the ORM auto-scope and stamp,
  `offboard_tenant.purge_tenant_data`'s hard-delete sweep, and
  `test_unscoped_model_reads`'s membership guard. `MachineInstallation` carries
  the *customer's* tenant but belongs to the *OEM*, so the column is
  `factory_tenant_code`.
- **SQLite hides foreign keys.** The `machine_id → machines.id` FK broke
  offboarding outright; SQLite passed 24/24 and PostgreSQL failed immediately.
  Run `verify_pg_oem.py` before believing a migration.
- **A bulk write is not auto-scoped.** The hook rewrites SELECTs only. New bulk
  writes fail `test_bulk_write_scoping.py` until justified.
- **When a mutation does not fail the tests, suspect your fixture.** Of the nine
  survivors across PRs 2 and 3, **seven were my tests being wrong** and one was
  genuinely dead code. Only one was a real gap.

### Running the checks

```bash
cd backend && DATABASE_URL="sqlite:///./ci.db" python test_oem_authorization.py
```

```bash
cd backend && python audit_oem_adversarial.py 5432
```

```bash
cd backend && python oem_perf.py 5432
```

```bash
cd frontend && npm test
```
