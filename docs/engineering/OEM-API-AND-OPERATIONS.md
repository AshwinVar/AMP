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
| **POST** | `/oem/machines/{id}/transition` | move one machine along the lifecycle |
| **POST** | `/oem/machines/{id}/commission` | commission it, and record whether the checks passed |
| **POST** | `/oem/machines/{id}/service` | record a completed service |

### Registering and offering a machine (ADR-0019)

| Method | Path | Capability | Purpose |
|---|---|---|---|
| POST | `/oem/machines` | `manage_installations` | register a manufactured machine |
| POST | `/oem/machines/{id}/claim` | `manage_installations` | issue an installation invitation |
| GET | `/oem/claims` | `read_fleet` | invitations and their status |
| POST | `/oem/claims/{id}/revoke` | `manage_installations` | withdraw an unused one |
| GET | `/oem/notifications` | `read_fleet` | what happened to this fleet |

**Registering attaches the machine to nobody.** `factory_tenant_code` stays NULL
and the lifecycle starts at `Manufactured`. There is no new entity — an
unassigned `MachineInstallation` *is* a manufactured machine.

Serials are unique **per OEM**, not globally: a global namespace would let one
manufacturer discover another's serials by probing for collisions. Two
manufacturers may both ship an `SN-001`, and they are different machines.

```jsonc
POST /oem/machines/4/claim   { "expires_in_days": 30 }
→ { "claim_code": "AMP-7K2QM-4XPWD-9TBHF",
    "claim_url": "https://app.marx8.com/claim/AMP-7K2QM-4XPWD-9TBHF",
    "code_hint": "9TBHF"[-4:], "expires_at": "…", "status": "Pending" }
```

**The raw code is in that response and nowhere else, ever again.** AMP stores
only its SHA-256 plus the last four characters — enough to match a row to a
sticker on a support call, useless as a credential. `POST` it to a printer, not
to a log.

The code is 15 characters from a 32-symbol alphabet with `I`, `L`, `O`, `U`, `0`
and `1` removed — about 75 bits, and unambiguous read off a sticker or dictated
over a phone. Typing `amp 7k2qm 4xpwd 9tbhf` works: it is normalised before
hashing.

`intended_customer` is a **hint**. When set it is enforced; when absent the claim
is equally valid. It never assigns anything by itself — acceptance still has to
happen.

### `/connected-equipment` — claiming, the factory's side

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/connected-equipment/claim/{code}` | **Admin** | what is this machine? (a read) |
| POST | `/connected-equipment/claim/{code}` | **Admin** | accept it, and choose sharing |
| POST | `/connected-equipment/{id}/link` | **Admin** | say which machine on the floor this serial is |
| POST | `/connected-equipment/{id}/release` | **Admin** | let it go — the only transfer path |

**The QR opens `APP_BASE_URL/claim/<code>`** — a page that fills the code in and
nothing more. The lookup is still a press and the acceptance is still a separate
press, so a scanned or forwarded link attaches nothing. If nobody is signed in,
the code survives the sign-in (the return path is restricted to a same-origin
path, so it cannot be turned into an open redirect); a manufacturer who scans
their own sticker is told plainly that adding a machine is their customer's
action. AMP supplies the target; the manufacturer's label printer encodes it.

**Linking is the factory's decision too.** The OEM knows the serial it built;
only the factory knows which asset on its floor carries it. Commissioning
requires the link (`linked_to_machine`), and until this release nothing in the
product could write it.

```
POST /connected-equipment/4/link   { "machine_id": 12 }
→ 200 { "machine_id": 12, "machine_name": "COMP-PC40", "site": "Plant 1",
        "message": "Linked. Its manufacturer can now commission it." }

{ "machine_id": null }   detaches, so a mis-link is corrected without releasing
                         the machine back to its maker
```

Both ends are filtered to the caller's own tenant *in the query*, so another
factory's machine is indistinguishable from one that does not exist — 404, not
403. One machine holds at most one installation (409 names the other serial,
since both are the factory's own). Re-linking the same serial to the same machine
is not a collision.

**The claim endpoints are the only place in AMP that sets `factory_tenant_code`.** An OEM can
offer; it cannot attach. If it could, a row would appear on a customer's screen —
presented by AMP as their equipment, from a supplier they have never dealt with,
beside controls inviting them to grant it access.

**Opening the link claims nothing.** The GET previews. A URL is not consent, and
a link forwarded to the wrong person must not attach equipment to a workspace.
Both routes are Admin-gated; an unauthenticated preview would be an oracle for
testing codes at leisure.

**Every failure returns the same 404 and the same sentence** — mistyped, expired,
revoked, already used, meant for another factory, machine already installed.
Distinguishing them would tell a prober which guesses were close.

```jsonc
POST /connected-equipment/claim/AMP-7K2QM-4XPWD-9TBHF
{ "grants": ["SHARE_OPERATING_HOURS", "SHARE_SERVICE_STATUS"] }
```

**Accepting can only widen consent, never narrow it.** The policy is keyed
`(oem, tenant)` — a *relationship*-level agreement, not per-machine — so the
requested grants are **unioned** with what is already agreed. Adding a second
machine with the boxes unticked changes nothing. (An earlier version overwrote,
which silently revoked sharing on the machines already installed; a test caught
it.) Withdrawal stays on the deliberate, audited control.

**`grants: []` is a complete answer.** The machine is added and nothing is
shared.

### Concurrency: the database decides

Two administrators pressing *Accept* in the same second is not a hypothetical.
`oem_claims.accept` issues two conditional UPDATEs whose **row count is the
security decision**:

```sql
UPDATE machine_claims        SET status='Claimed'         WHERE id=? AND status='Pending'
UPDATE machine_installations SET factory_tenant_code=?    WHERE id=? AND factory_tenant_code IS NULL
```

Read-modify-write would let both succeed: both see `Pending`, both write, and one
factory ends up with a confirmation for a machine it does not have. `verify_pg_claim.py`
runs 25 real two-thread races on PostgreSQL 18.3 — exactly one winner, every time.

### Transfer: a machine moves only by being let go

```
Factory A: POST /connected-equipment/{id}/release
           → factory_tenant_code := NULL, machine link cleared, status := Sold
OEM:       POST /oem/machines/{id}/claim        (a fresh code)
Factory B: POST /connected-equipment/claim/{code}   (a fresh, explicit consent)
```

There is **no reassignment route**. The only path from A to B goes through A's own
decision.

**History survives.** The installation row carries *current* state; what happened
is in `event_log` and `audit_log`, filed under whichever tenant it happened to.
Factory A keeps its record of the commissioning and every service performed on
site; Factory B's log starts at its own acceptance.

### The writes, and what they deliberately cannot do

These three are the only OEM writes, and each is confined to the
manufacturer's **own columns on the installation row**.

| Endpoint | Capability | Refusals |
|---|---|---|
| `transition` | `manage_installations` | **400** with the state machine's own message (`"cannot go from Manufactured to Active; allowed: Sold, Decommissioned"`) |
| `commission` | `commission` | **400** if the lifecycle forbids it |
| `service` | `manage_service` | **400** on a negative reading |

All three return **404** for another manufacturer's machine — never 403.

**No write can assign a machine to a customer.** An OEM that could set
`factory_tenant_code` could plant an installation at any factory in the system,
which would put a row on that factory's Connected Equipment screen and invite
them to grant sharing to a supplier they never bought from. Assignment is a
separate, deliberately unbuilt operation rather than a parameter somebody forgets
to guard — and a test posts `factory_tenant_code` in the body to prove it is
ignored.

**No write touches anything the factory owns.** No machine status, no
utilisation, no production. Asserted, not assumed.

### Recording a service

```jsonc
POST /oem/machines/4/service   { "service_hours": 2100 }
```

`service_hours` is optional and falls back to the hours the machine last
reported. This is the number that makes `overdue` reachable at all — see §4.

### Commissioning is advice, not a gate

`POST /oem/machines/{id}/commission` runs the report, then commissions the
machine **even if checks failed**. Somebody may have a good reason to put a
machine into service with an item outstanding, and AMP is not in a position to
overrule them. What it does instead is refuse to let the record imply it was
clean:

- the response carries the full report, naming which checks failed;
- the `MachineCommissioned` event carries `checks_passed: false`;
- the customer's notification is raised as a **Warning**, telling them to ask
  their supplier which check failed.

---

## Part 2a — Events, and whose history they become

Three lifecycle events reach the domain bus (ADR-0001): `MachineInstalled`,
`MachineCommissioned`, `ServiceCompleted`.

**They are stamped with the FACTORY's tenant, not the OEM's.** The event records
something that happened on the customer's shop floor to the customer's machine;
they own that history (ADR-0002), and it is theirs to read, export and retain.
The manufacturer's code travels on the event as a *field*, not as its owner.

**An installation with no customer publishes nothing.** `events.EventBus` stamps
every event with `getattr(event, "tenant_code", "DEFAULT")`, and that default is
a trap here: a machine still in the manufacturer's stock belongs to nobody's
factory, and "DEFAULT" is the *founder's* workspace. `oem_events.publish` refuses
rather than filing a manufacturer's private record where an unrelated party
reads it. The write itself still succeeds — moving unsold stock through the
lifecycle is a normal thing to do.

Each event raises a notification **in the customer's workspace**. Nothing flows
the other way: there is no OEM-side notification store, and putting manufacturer
rows on the factory's `notifications` table would be worse than the gap. That is
left undone deliberately rather than approximated.

The notification describes the OEM's *action* — serial, model, site, what
happened. It can never become a back-channel for data the sharing policy
withholds, and a test greps every notification for work orders, part numbers,
utilisation and OEE to keep it that way.

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

The whole sequence, and who performs each step:

```
FOUNDER   POST /saas/oems                 register the manufacturer
FOUNDER   POST /saas/oems/{id}/admin      its FIRST admin -> temp password
   ↓      (hand the credentials over, out of band, once)
OEM       POST /oem/login                 sign in
OEM       POST /oem/change-password       rotate the temporary password
OEM       POST /oem/users                 add its own people
```

Step 5 is deliberately the manufacturer's own job. A platform operator who
provisions every one of a supplier's engineers becomes a help desk, and holds
passwords it has no reason to hold — so `/saas/oems/{id}/admin` **refuses once an
administrator exists**.

### Founder-side routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/saas/oems` | every manufacturer, with user and installation counts |
| POST | `/saas/oems` | register one |
| PATCH | `/saas/oems/{id}` | branding, contact details, **suspension** |
| POST | `/saas/oems/{id}/admin` | provision the first administrator |

All four demand the **founder workspace**, checked against the token's own tenant
claim so the company switcher cannot be used to become the founder. A customer
Admin and a manufacturer's own admin are both refused — the second is the point:
an organisation that could create, rename or reactivate itself is not one the
platform controls.

`PATCH … {"is_active": false}` takes effect **immediately**, not at token expiry,
because `oem_auth.resolve` re-reads the organisation on every request. A
suspended manufacturer is refused at the door *and* on its existing sessions.

`oem_code` is absent from the update model on purpose: it is the identity every
installation, sharing policy and audit row already points at.

### The code's shape is load-bearing

`oem_code` must match `^[A-Z0-9][A-Z0-9_-]{1,31}$` — lower case is folded up,
anything else is a **400**. It becomes the suffix of the sentinel tenant
`OEM:<code>` that every one of this manufacturer's requests binds, and the whole
isolation rests on that string being unmatchable by a factory. A code carrying a
colon would produce a sentinel nobody reasoned about.

### Passwords

Both provisioning routes **generate** the password, return it **once**, and store
only a bcrypt hash. Nobody types a password into AMP on somebody else's behalf.
`/oem/login` and `/oem/change-password` are rate-limited like the factory's front
door.

### Signing in

```jsonc
POST /oem/login   { "username": "oem_alpha_admin", "password": "…" }
→ { "access_token": "…", "role": "OEM_ADMIN", "oem": "OEM_ALPHA",
    "branding": { "name": "Alpha Connect", "color": "#0f766e", "logo_url": null } }
```

The token carries `principal: "oem"` and `oem`, and **no `tenant` claim at all** —
`tenancy.effective_tenant` binds the sentinel regardless, so a tenant claim would
be inert, and an inert claim that looks authoritative is how somebody later
"fixes" the branch to honour it.

Refusals are deliberately uneven. Bad credentials give **one indistinguishable
answer**, because telling an attacker which half was wrong hands them a username
oracle. Everything *after* a correct password names the real reason — a suspended
engineer told "invalid credentials" spends the afternoon resetting a password
that was never the problem.

### Manufacturer-side user management

| Method | Path | Capability |
|---|---|---|
| GET | `/oem/users` | `manage_users` |
| POST | `/oem/users` | `manage_users` |
| PATCH | `/oem/users/{id}` | `manage_users` |
| POST | `/oem/change-password` | any OEM role (own account only) |

`oem_code` appears in **no** request body. There is no input through which an
administrator at one manufacturer could create or edit an account inside another;
a competitor's user id is a **404**, indistinguishable from one that does not
exist, so probing ids cannot enumerate a rival's staff.

A username already taken by **either** an OEM or a factory login is refused. The
two tables are never queried together, so this is not an isolation hole — it is a
human one: two people, two companies, one name in every audit line.

**One lockout rule:** an organisation may never be left without an active
administrator. A sole administrator therefore cannot demote or disable
themselves; with a successor in place, a handover is allowed. (An earlier version
had a separate "you cannot demote yourself" rule as well — writing the test
showed the organisation-level rule could then never fire, so it was deleted
rather than kept as decoration.)

### Roles

Give each account the narrowest role that works:

   | Role | Give it to |
   |---|---|
   | `OEM_ADMIN` | one or two people; it can manage users and branding |
   | `OEM_SERVICE_MANAGER` | whoever runs the service desk |
   | `OEM_SERVICE_ENGINEER` | field engineers — read plus commission |
   | `OEM_VIEWER` | sales, management, anybody who only looks |

### The catalogue

Per model: `model_code`, `family`, `name`,
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
