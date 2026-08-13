# OEM platform readiness

**Date:** 2026-08-13 · **Decision records:**
[ADR-0017](../adr/0017-oem-fleet-and-cross-tenant-equipment.md) (the fleet),
[ADR-0019](../adr/0019-factory-controlled-machine-claim.md) (the claim)

Can a machine manufacturer sell "connected machines powered by AMP" without any
of its customers' operational data reaching it — or reaching another
manufacturer? And can a real one now be onboarded, register a real machine, and
have a real factory take delivery of it, without a developer? This is the
evidence, and the eight verdicts.

---

## What was built

| PR | What |
|---|---|
| [#509](https://github.com/AshwinVar/AMP/pull/509) | ADR-0017 + the OEM ownership dimension |
| [#510](https://github.com/AshwinVar/AMP/pull/510) | OEM principals, capabilities, the sentinel tenant |
| [#511](https://github.com/AshwinVar/AMP/pull/511) | sharing policy, fleet read models, `/oem` API |
| [#512](https://github.com/AshwinVar/AMP/pull/512) | telemetry profiles, service intelligence, Connected Equipment API |
| [#516](https://github.com/AshwinVar/AMP/pull/516) | the manufacturer portal and the factory's consent screen |
| [#517](https://github.com/AshwinVar/AMP/pull/517) | lifecycle writes, domain events, notifications |
| [#518](https://github.com/AshwinVar/AMP/pull/518) | reserving the tenant namespace the sentinel lives in |
| [#519](https://github.com/AshwinVar/AMP/pull/519) | OEM organisation onboarding, login, and the manufacturer's own user admin |
| [#520](https://github.com/AshwinVar/AMP/pull/520) | **the factory-controlled machine claim** (ADR-0019) + the shop-floor link |

Nothing was forked, duplicated, or special-cased. AMP core gained an ownership
dimension; the factory experience is unchanged except for one new screen.

---

## The evidence

| Harness | Scope | Result |
|---|---|---|
| `audit_oem_adversarial.py` | 2 OEMs × 3 factories on **PostgreSQL 18.3** — HTTP, WebSocket, MQTT, CSV exports, read models, every write verb, and the claim | **140 checks, NO BREACH** |
| `audit_oem_specialist.py` | attacks the *assumptions* the boundary rests on, not the API | **151 checks, NO FINDING** (after two real fixes — below) |
| `audit_oem_pilot_journey.py` | the whole business journey on PostgreSQL 18.3, every step an HTTP request, telemetry through the real MQTT handler | **41 steps, no developer, nothing seeded but the founder and the catalogue** |
| `audit_oem_demo_journey.py` | the ten-minute AERON sales demo, walked end to end; runs in CI on every push | **54 steps** |
| `test_mqtt_installation_reporting.py` | telemetry reaching the OEM's installation record, and the tenant it must not reach | 13 checks; 5 of 6 mutations caught, 1 shadowed and proven so |
| `preflight_backfill_245.py` | the #245 backfill's safety properties, on an adversarial fixture | **24 checks** |
| `mutate_oem_claim.py` | 29 mutations of the claim, the consent union and the link | 22 caught, **7 shadowed with recorded reasons** |
| `mutate_oem_auth.py` | the principal and the sentinel | all caught |
| `mutate_oem_sharing.py` | the consent model and the fleet API | all caught |
| `mutate_oem_service.py` | 19 honesty guards (defaults, clamping, invented confidence) | all caught |
| `mutate_oem_lifecycle.py` | 12 mutations of the write path and event tenancy | all caught |
| `mutate-oem-ui.mjs` | 28 mutations of the portal, the consent screen, the claim UI and the QR deep link | all caught |
| `verify_pg_claim.py` | 25 two-thread races for one claim code on **PostgreSQL 18.3** | **exactly one winner, every round** |
| `oem_perf.py` | 10 → 10,000 machines | query counts **constant** |
| `verify_pg_oem.py` | migrations 0006/0007/**0008** on PostgreSQL 18.3, against a live fleet, including downgrade | green |

**190 backend suites** and **310 frontend tests** green; `tsc`, `next build` and
the lib/ coverage floor clean; eslint at its baseline of exactly 134. CI runs a
real PostgreSQL 18 migration gate on every push (ADR-0018).

### Performance — the number that matters is the query count

| machines | fleet page | queries | service queue | queries | one customer | queries |
|---|---|---|---|---|---|---|
| 10 | 4.9 ms | 9 | 0.8 ms | 1 | 1.1 ms | 2 |
| 100 | 3.8 ms | 9 | 1.2 ms | 1 | 0.7 ms | 2 |
| 1,000 | 11.4 ms | 9 | 10.9 ms | 1 | 2.8 ms | 2 |
| 10,000 | 125.3 ms | **9** | 93.1 ms | **1** | 9.1 ms | **2** |

Constant across a 1000× range. There is no N+1. Measured on one machine against
local PostgreSQL with no network in between — the *latency* would differ in
production; the *query counts* would not.

---

## What the specialist audit found, and what I did about it

The adversarial matrix attacks the boundary from outside. The specialist audit
attacks what the boundary **assumes**, and it found something the matrix could
not.

> **An OEM request binds the sentinel tenant `OEM:<code>`, chosen so no factory
> can hold it. Nothing enforced that.** A tenant created as literally
> `OEM:OEM_ALPHA` would have been visible to that manufacturer's sessions.

Prevented, until now, only by convention — tenant codes are uppercase
identifiers and nobody had used a colon. The audit creates exactly that tenant,
binds the sentinel, and demonstrates the collision.

**Fixed, not merely noted.** `tenancy.assert_tenant_code_available` rejects any
tenant code containing `:`, enforced at `/saas/tenants` — the one place a tenant
code enters the system. The audit now proves the collision *would* work
(a CONTROL) and that creating one is refused.

**The second finding, from the claim campaign.** The audit asserted that only two
code paths write `factory_tenant_code`, and reported a third: `offboard_tenant`.
Reading it showed the check was too coarse rather than the code being wrong —
offboarding *detaches* a departing customer's installations, and detaching to
`NULL` can never place a machine at a factory. The check now separates the two:
**exactly one path assigns** a factory (`oem_claims.accept`), two detach, and a
CONTROL names them so a fourth writer has to be read rather than absorbed.

A third thing the audits produced was not a security finding but a real defect,
found by writing the pilot journey: **nothing in the product could set
`machine_id`.** Commissioning requires the serial to be linked to a machine on
the floor, and that column had a reader and no writer — so the last leg of the
journey was reachable only by editing the database.
`POST /connected-equipment/{id}/link` closes it, on the **factory** side.

---

## The claim, in one page (ADR-0019)

**An OEM may offer a machine. Only a factory may accept one.** There is no route
by which a manufacturer attaches equipment to a customer, and that is the whole
design: an OEM-side assignment would put a stranger's machine on a customer's
Connected Equipment screen with AMP's endorsement.

```
OEM registers a machine       → an installation with NO factory
OEM issues an invitation      → a code; AMP stores only its SHA-256
factory Admin LOOKS IT UP     → a GET. Claims nothing. A URL is not consent.
factory Admin CONFIRMS        → a POST. This, and only this, sets the factory
                                and creates the sharing policy
```

| Property | How |
|---|---|
| the code is a credential | 73.6 bits (30¹⁵), alphabet without I, L, O, U, 0, 1 |
| never stored | SHA-256 only; the last 4 characters are kept for support |
| never logged | asserted statically *and* by a run that greps every audit row |
| one-time | acceptance is `UPDATE … WHERE status='Pending'`; the row count decides |
| expiring | evaluated **when presented**, never by a sweeper that could lag |
| withdrawable | the OEM can revoke a pending invitation |
| no oracle | every failure — mistyped, expired, spent, revoked, meant for somebody else — returns the identical sentence, on the preview *and* the commit |
| race-proof | 25 two-thread races on PostgreSQL 18.3: exactly one winner, every round; the layering is proven by a mutation that removes **both** conditional updates and IS caught |

**Consent is separate from the relationship, and accepting can only widen it.**
The first version of the accept handler *overwrote* the `(oem, tenant)` policy,
which silently revoked sharing on machines that factory had installed earlier.
It now unions, the preview returns `already_granted`, and the UI pre-ticks it so
an existing agreement never reads as something about to be switched off.

**A machine moves only by release.** There is no reassignment route: the factory
lets go, the machine returns to the manufacturer's unassigned stock, and the next
factory accepts a fresh invitation. History stays where it happened (ADR-0002) —
the first factory keeps its commissioning and service record.

---

## The security model, in one page

**Two independent ownership dimensions.** `tenant_code` = which factory.
`oem_code` = which manufacturer. A request binds one.

| Request | factory binding | factory tables return |
|---|---|---|
| factory user | its own tenant | its own rows |
| **OEM user** | **`OEM:<code>` sentinel** | **zero rows** |

Measured against the real ADR-0002 hook: `bound 'FACTORY_A'` → 1 machine,
`bound 'OEM:OEM_ALPHA'` → 0, `bound None` → 2 (all tenants). Binding a tenant no
factory can hold makes factory data invisible *by construction*, before any OEM
route exists.

**A relationship is not consent.** Two independent things must both hold: an
installation row *and* a factory-granted policy. AMP never infers *"the OEM has
machines at FACTORY_A, therefore it may query FACTORY_A"*.

**Consent is read at query time, never cached** — a cached projection would
outlive the permission that allowed it. Withdrawal takes effect on the next
request.

**The factory decides.** `PUT /connected-equipment/sharing` is Admin-only,
audited with before/after, and lives on the factory side. **There is no OEM-side
equivalent** — a manufacturer that could edit its own permissions has
permissions in name only.

**Writes touch only the manufacturer's own columns.** No write can assign a
machine to a customer; an OEM that could set `factory_tenant_code` could plant
equipment at any factory in the system. A test posts it in the body to prove it
is ignored on a *successful* write.

**Events are the customer's history.** Filed under the factory's tenant, never
the OEM's and never `DEFAULT` — an unassigned machine publishes nothing at all,
because the founder's workspace is not a bin for a manufacturer's private
records.

---
## The eight verdicts

Each answers the question asked, against the evidence rather than the design.

### 1. Can a real OEM now be onboarded without database editing?

# YES

Founder-only `POST /saas/oems` creates the organisation; `POST /saas/oems/{id}/admin`
provisions its first administrator with a one-time password and then refuses —
further accounts are the manufacturer's own job, through `GET/POST/PATCH
/oem/users`. `POST /oem/login` and `POST /oem/change-password` are the front
door. The pilot journey does exactly this, in that order, over HTTP.

The tenant code is checked against the reserved namespace on the way in
(`OEM:` cannot be claimed by a factory — #518), so onboarding cannot create the
collision the specialist audit demonstrated.

### 2. Can that OEM register a real machine without source-code changes?

# YES

`POST /oem/machines` takes a serial and a model id from the manufacturer's own
catalogue. The serial is unique **per OEM**, not globally — a global namespace
would let one manufacturer discover another's fleet by probing for collisions,
and the adversarial matrix registers the same serial string for both
manufacturers and proves they are two different machines.

The registered machine belongs to **no factory**. That is the state a
manufactured machine is actually in, and the pilot journey asserts it before
anything else happens.

### 3. Can a factory securely claim that machine without developer intervention?

# YES

The factory Admin enters or scans the code, sees what it is — manufacturer,
model, serial, the warranty the OEM recorded, and exactly what is about to be
shared — and confirms. The lookup is a GET and claims nothing; the commit is a
separate deliberate POST. Both are Admin-only.

The last step of taking delivery — saying which machine on the floor the serial
*is* — is `POST /connected-equipment/{id}/link`, also the factory's. Before this
release that column had a reader and no writer, and commissioning could not be
completed without editing the database. It can now.

### 4. Can the OEM assign itself to an arbitrary factory without factory consent?

# NO

There is no route that lets it. Stated against the evidence:

- **No OEM handler writes `factory_tenant_code`, `machine_id` or `tenant_code`** —
  an AST scan of every handler in `oem_routes.py`, not a reading of them.
- **Exactly one code path in the whole backend assigns a factory tenant**
  (`oem_claims.accept`, reached only from the factory's own POST). Two paths
  detach — the release and the customer offboard — and detaching can never place
  a machine anywhere.
- **Posting it anyway does nothing.** The adversarial audit sends
  `factory_tenant_code` and `tenant` in the body of the OEM's transition and
  commission verbs, then reads the row directly: still unassigned.
- **A manufacturer cannot accept on a factory's behalf** — its own admin, a rival
  and an anonymous caller are all refused at the claim endpoint, while a factory
  Admin succeeds (the CONTROL).
- **The link is the factory's too**, and a factory cannot link across to another
  factory's machine.

### 5. Can a claim token be replayed or double-claimed?

# NO

- **Replay** — a spent code is dead for the factory that used it *and* for any
  other, on the commit and on the preview.
- **Double-claim** — acceptance is `UPDATE machine_claims SET status='Claimed'
  WHERE id=? AND status='Pending'`, followed by `UPDATE machine_installations …
  WHERE factory_tenant_code IS NULL`. The **row counts are the decision**; the
  loser rolls back. 25 two-thread races on PostgreSQL 18.3 produced exactly one
  winner every round, never both, never neither, and never a half-used claim.
- **Guessing** — 73.6 bits, and every refusal is the same sentence, so a guesser
  learns nothing from the difference between "wrong" and "not yours".
- **The database holds the line, not Python.** A mutation that removes *both*
  conditional predicates is caught by the PostgreSQL race harness — which is what
  makes the two guards a layer rather than a duplicate.

### 6. Does claiming a machine give the OEM access to unrelated factory data?

# NO

Claiming creates two things: an installation row and a sharing policy the factory
chose. Neither is a key to anything else.

- The OEM's session still binds the sentinel tenant, so **every factory table
  returns zero rows** before any `/oem` route is consulted.
- **Consent is separate from the relationship.** AMP never infers "it has a
  machine here, therefore it may query here"; the grants are read at query time,
  so withdrawal takes effect on the next request. The pilot journey withdraws one
  and proves the field is gone from the very next response — and shown as *not
  shared*, not as a zero.
- **Accepting can only widen consent, never replace it** — the defect that made
  this true is recorded above.
- 140 adversarial checks across HTTP, WebSocket, MQTT, CSV export and every write
  verb found no factory secret in any `/oem` response.

### 7. Can the full OEM → factory → commission → telemetry → service journey be completed without database editing, manual JWTs or source changes?

# YES, WITH ONE NAMED EXCEPTION

`audit_oem_pilot_journey.py` runs it end to end on PostgreSQL 18.3 — 40 steps,
every one an HTTP request carrying a token AMP itself issued at a login
endpoint: founder onboards the OEM → OEM admin signs in and rotates the
password → registers a machine → issues an invitation → factory Admin previews,
accepts and chooses three sharing categories → links it to a machine on the floor
→ OEM records it installed and commissions it, every check passing → OEM sees
the machine and the hours the customer agreed to share → the service queue raises
it with its evidence → the engineer records the service → the factory sees the
new state → the factory withdraws one permission and the OEM loses that field
immediately.

**Corrected on 2026-08-13, and the correction matters.** This section previously
read "the *telemetry itself* is seeded… hours arrive over MQTT and there is no
HTTP route to post them". The first half was true; the second half concealed a
defect. MQTT ingest wrote `Machine` and **never touched the installation**, so
`operating_hours` and `last_seen_at` — read by the gated fleet row, the
connected/offline split, the service clock and the `has_reported` commissioning
check — were written by nothing in the product. Every OEM would have seen "no
data" forever and no machine would ever have come due for service.

That is now fixed (`mqtt_service._record_installation_report`), and the journey
seeds **no telemetry at all**: it publishes through `on_message`, the same
function the broker calls. What remains seeded is the founder account (how AMP
is installed, not something a user does) and the machine-model catalogue, which
still has no write route. Both are named below as remaining work.

### 8. Is AMP ready for its first controlled machine-manufacturer pilot?

# YES, WITH CONDITIONS

The pilot blocker that stood at the end of the last campaign — installation
assignment — is closed, and closed the way it should have been: the factory
decides, not the manufacturer. What remains is not safety. It is the things a
manufacturer must tell us about its own machines, and the operational
completeness a *product* needs that a *pilot* does not. See the conditions and
the input list below.

---

## What is left, honestly

**Nothing is blocking a controlled pilot.** These block *unattended commercial
operation*:

1. **Real telemetry from OEM equipment.** AMP now ingests it end to end — an
   MQTT report from a linked machine updates the installation's `last_seen_at`,
   and its operating hours where the model's profile names a source. What does
   not exist is the **edge that produces the report**: device certificates,
   provisioning, store-and-forward, OTA. A machine still has to be pointed at
   the broker by somebody.
1a. **A machine's site cannot be set through the API**, and an MQTT topic
   segment may not contain a space (`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`). So a
   machine imported with the site "Plant 1" is unaddressable by MQTT until that
   is changed. Machines created at `POST /machines` have no site and are
   addressed with the `-` token, which works — but this will be met on the
   first real site that uses named plants.
2. **No push notifications.** A manufacturer is told inside the portal when its
   fleet needs attention; nothing emails or pages it.
3. **Commercial terms.** Nothing meters, bills, or rate-limits per OEM.

**Should do before a second OEM**

4. **Multi-replica review.** ADR-0018's deploy contract assumes one replica.
5. **Retire the staged compatibility path** (ADR-0018 stages 2–3).
6. **Playwright coverage for the OEM portal and the claim flow.** Unit-tested and
   mutation-tested; not yet driven in a browser.
7. **A machine's site cannot be set through the API.** `schemas.MachineBase`
   carries no `site`, so a machine created at `POST /machines` has none and the
   link has nothing to copy. CSV onboarding sets it. Not a claim problem, but a
   pilot will meet it.

---

## Conditions attached to verdict 8

| # | Condition |
|---|---|
| 1 | **Pilot with a named OEM.** Onboarding is self-service now, but nothing meters or bills, so who is on the platform must stay a decision rather than a signup. |
| 2 | **Brief the manufacturer's service desk on what "not shared" means.** The portal distinguishes *not shared* / *no data* / a value. A blank is a permission, not a fault. |
| 3 | **Tell the factory that Connected Equipment is where consent lives**, that it is Admin-only and audited, and that adding a machine never switches anything off. |
| 4 | **Print the claim code on the machine, not in an email.** Possession of the code is the credential; the QR on the crate is the intended channel. Codes should be treated as one-per-machine and reissued rather than forwarded. |
| 5 | **Do not describe the service queue as predictive.** It is arithmetic over reported facts and says so; marketing must not outrun it. |
| 6 | **Re-run both audits, the claim mutation harness and the PostgreSQL race after any change** to `oem_*`, `tenancy.py`, or `auth.py`. |
| 7 | **Fleet health is only as live as ingest.** With no edge programme, `last_seen_at` reflects whatever MQTT already delivers. |

---

## REAL OEM INPUT REQUIRED

Everything below is a fact about somebody's actual machines. None of it can be
invented here, and building further OEM features before it arrives would be
guessing with extra steps.

| # | What is needed | Why AMP cannot supply it |
|---|---|---|
| 1 | **PLC / controller make and model** on the machines to be connected | Determines whether AMP can read anything at all, and by which driver |
| 2 | **Protocol available at the machine** — OPC UA, Modbus TCP/RTU, MQTT, a vendor gateway, or a file drop | The adapter is chosen by this, not by preference |
| 3 | **The tag / register map**: which addresses carry running state, hours, alarms, counts, and their units and scaling | A telemetry profile is a mapping onto *their* tags; a made-up one produces confident wrong numbers |
| 4 | **Reporting cadence and connectivity constraints** — how often, over what link, and what happens when it drops | Decides store-and-forward, and what `last_seen_at` staleness actually means on their sites |
| 5 | **Their commissioning procedure** — what an engineer really checks before a machine is signed off | AMP's four checks are a reasonable guess; theirs is the one that matters |
| 6 | **Service intervals and what resets them** — hours, calendar, cycles, or a combination, and per model | The service clock currently assumes hours since last service against a per-model interval |
| 7 | **Alarm definitions** — codes, severities, and which ones mean "stop" versus "note it" | Severity that AMP invents will be ignored, and then a real one will be too |
| 8 | **Warranty rules** — start event (ship, install, commission), duration, and what voids it | AMP records start/end dates; whose event starts the clock is a commercial decision |
| 9 | **The installation environment** — is there a network at the customer's site, who owns it, and will the customer permit an outbound connection | Determines whether the claim flow is even reachable from the shop floor |

---

## The freeze

**This is the end of the OEM campaign.** The controlled-pilot feature set is
frozen: no further OEM features, no additional dashboards, no more AI, and no
AMP Edge without real hardware requirements from the list above.

The next thing that should happen to this code is a real manufacturer using it.

---

## What I would still like to be able to say, and cannot

- **No production OEM data exists yet.** Every measurement is against seeded
  fixtures on PostgreSQL 18.3. The isolation properties are structural and would
  not change with real data; the *latency* numbers would.
- **No third-party has attempted this boundary.** Both audits are mine, and a
  test author attacking their own design shares its blind spots. An external
  review before a second OEM would be worth more than another hundred of my own
  checks.
- **Seven claim mutations survive**, each because a second guard already covers
  it. Each has a written reason in `mutate_oem_claim.py`, and the two that matter
  most are proven to be layers rather than gaps by a mutation that removes both
  guards and IS caught. A reader should still check those reasons rather than
  take the count.
- **`ahead` is permitted by the schema guard** (ADR-0018) so rollback works,
  which makes backwards-compatible migrations a review rule rather than a
  machine-checked one.
