# OEM platform readiness

**Date:** 2026-08-11 · **Decision record:** [ADR-0017](../adr/0017-oem-fleet-and-cross-tenant-equipment.md)

Can a machine manufacturer sell "connected machines powered by AMP" without any
of its customers' operational data reaching it — or reaching another
manufacturer? This is the evidence, and the five verdicts.

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

Nothing was forked, duplicated, or special-cased. AMP core gained an ownership
dimension; the factory experience is unchanged except for one new screen.

---

## The evidence

| Harness | Scope | Result |
|---|---|---|
| `audit_oem_adversarial.py` | 2 OEMs × 3 factories on **PostgreSQL 18.3** — HTTP, WebSocket, MQTT, CSV exports, read models, and every write verb | **87 checks, NO BREACH** |
| `audit_oem_specialist.py` | attacks the *assumptions* the boundary rests on, not the API | **97 checks, NO FINDING** (after one real fix — below) |
| `mutate_oem_auth.py` | the principal and the sentinel | all caught |
| `mutate_oem_sharing.py` | the consent model and the fleet API | all caught |
| `mutate_oem_service.py` | 19 honesty guards (defaults, clamping, invented confidence) | all caught |
| `mutate_oem_lifecycle.py` | 12 mutations of the write path and event tenancy | all caught |
| `mutate-oem-ui.mjs` | 12 mutations of the portal and consent screen | all caught |
| `oem_perf.py` | 10 → 10,000 machines | query counts **constant** |
| `verify_pg_oem.py` | migrations 0006/0007 on PostgreSQL 18.3, including downgrade | green |

**188 backend suites** and **286 frontend tests** green. CI runs a real
PostgreSQL 18 migration gate on every push (ADR-0018).

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

The 87-check adversarial matrix attacks the boundary from outside. The
specialist audit attacks what the boundary **assumes**, and it found something
the matrix could not.

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

That is the only finding. It was defence-in-depth, not an exploitable path: it
required founder access to create a maliciously-named tenant.

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

## The five verdicts

### 1. Is the existing factory AMP experience still safe?

# YES

The OEM layer is additive. `machines` gained no OEM column; migrations 0006/0007
create new tables and add nullable columns to them only. The factory dashboard
gained exactly one screen. 188 backend suites — including every pre-existing
tenant-isolation guard — are green, and `verify_pg_oem.py` proves on PostgreSQL
that the OEM layer installs against a database that already has factory data and
leaves every factory row untouched.

Two traps were found and closed during the work: offboarding a customer would
have deleted an OEM's fleet history (fixed by naming the column
`factory_tenant_code`, invisible to the purge sweep), and the new foreign key
broke offboarding outright on PostgreSQL while SQLite passed 24/24.

### 2. Is the OEM layer ready for a controlled OEM pilot?

# YES, WITH CONDITIONS

The security spine holds under 87 adversarial checks and 97 specialist checks,
the honesty guards are mutation-proven, and performance is flat to 10,000
machines. What is missing is not safety — it is completeness for unattended
commercial operation. See §Conditions.

### 3. Can two OEMs safely operate across three shared factory customers?

# YES

This is the configuration the adversarial matrix runs in, on PostgreSQL 18.3:
two manufacturers, three factories, one factory holding **both** manufacturers'
equipment. At that shared site each sees only its own machine; a competitor's
serial passed as `?customer=` returns nothing; sweeping every installation id
yields only one's own; a competitor's id is a 404 indistinguishable from a
nonexistent one, on reads and on all three write verbs.

### 4. Does an OEM have any path to unauthorized factory operational data?

# NO

Stated against the evidence rather than against the design:

- **HTTP** — every factory route refuses an OEM token; every `/oem` route takes
  `oem_code` from the principal, never from a parameter (verified by AST scan
  across all 12 handlers).
- **WebSocket** — an OEM-sentinel socket receives no factory broadcast; the
  factory socket does (CONTROL).
- **MQTT** — ingest topics do not resolve under an OEM scope.
- **CSV exports** — inventory and work-order exports yield nothing under an OEM
  scope.
- **Read models** — no factory secret appears in any `/oem` response.
- **Events and notifications** — none land in `DEFAULT` or in another customer's
  tenant; none carry a factory secret.
- **Writes** — cannot reach machine status, utilisation, work orders, or another
  manufacturer's row.

The honest limits of that `NO`: it means **no path I could construct, across the
attack surface I enumerated, on the code as it stands**. It is not a proof of a
negative. The enumeration is written down and re-runnable
(`audit_oem_adversarial.py`, `audit_oem_specialist.py`), and both must be re-run
after any change to `oem_*`, `tenancy.py`, or `auth.py`.

### 5. What remains before an OEM can ship "connected machines powered by AMP" commercially?

**Blockers — cannot pilot without these**

1. **OEM login provisioning.** `OemUser` rows exist and authenticate, but there
   is no route to create one, no password reset, and no invitation flow. Today
   an OEM account can only be created by direct database insert.
2. **Installation assignment.** Nothing can attach a machine to a customer.
   Deliberately unbuilt — an OEM-side assignment is a spoofing vector — so it
   needs a **factory-side** claim or a founder-mediated flow, designed and
   audited.
3. **OEM organisation onboarding.** No route creates an `OemOrganization` or
   sets its branding.

**Blockers for unattended commercial operation**

4. **Real telemetry from OEM equipment.** The profiles interpret readings; the
   edge that produces them (device certificates, provisioning, store-and-forward,
   OTA) does not exist. Fleet health is only as live as MQTT ingest already is.
5. **No OEM-side notifications.** Manufacturers are not told when their own
   fleet needs attention; they must open the portal.
6. **Commercial terms.** Nothing meters, bills, or rate-limits per OEM.

**Should do before a second OEM**

7. **Multi-replica review.** ADR-0018's deploy contract assumes one replica.
8. **Retire the staged compatibility path** (ADR-0018 stages 2–3).
9. **Playwright coverage for the OEM portal.** Unit-tested and mutation-tested;
   not yet driven in a browser.

---

## Conditions attached to verdict 2

| # | Condition |
|---|---|
| 1 | **Pilot with a named OEM whose accounts you provision by hand.** Blockers 1–3 mean onboarding is manual; that is workable for one pilot and not for a product. |
| 2 | **Tell the OEM what "not shared" means.** The portal distinguishes *not shared* / *no data* / a value, and a service desk must be briefed that a blank is a permission, not a fault. |
| 3 | **Tell the factory that Connected Equipment is where consent lives**, and that it is Admin-only and audited. |
| 4 | **Do not describe the service queue as predictive.** It is arithmetic over reported facts and says so; marketing must not outrun it. |
| 5 | **Re-run both audits after any change** to `oem_*`, `tenancy.py`, or `auth.py`. |
| 6 | **Fleet health is only as live as ingest.** With no edge programme, `last_seen_at` reflects whatever MQTT already delivers. |

---

## What I would still like to be able to say, and cannot

- **No production OEM data exists yet.** Every measurement is against seeded
  fixtures on PostgreSQL 18.3. The isolation properties are structural and would
  not change with real data; the *latency* numbers would.
- **No third-party has attempted this boundary.** Both audits are mine, and a
  test author attacking their own design shares its blind spots. An external
  review before a second OEM would be worth more than another hundred of my own
  checks.
- **`ahead` is permitted by the schema guard** (ADR-0018) so rollback works,
  which makes backwards-compatible migrations a review rule rather than a
  machine-checked one.
