# Real OEM input required

**Date:** 2026-08-13 · **Verified against:** `master` @ `7a1333c` · **Related:**
[ADR-0011](../adr/0011-machine-identity-and-tenant-aware-ingest.md),
[ADR-0017](../adr/0017-oem-fleet-and-cross-tenant-equipment.md),
[ADR-0019](../adr/0019-factory-controlled-machine-claim.md),
[OEM platform readiness](../engineering/OEM-PLATFORM-READINESS.md)

This document separates three things that are constantly confused in a sales
conversation: what AMP **does**, what only a real machine manufacturer **can
tell us**, and what **might have to be built** once they have told us.

Its value is entirely in its honesty. An overstated "already supports" entry is
the worst possible error this document can contain — it survives the meeting,
gets repeated in a proposal, and is discovered by an engineer on a shop floor.
So every entry in Part 1 names the file or endpoint that proves it, anything
seeded or simulated is labelled as such in the same sentence, and Part 3 states
a **trigger** rather than a commitment.

**Nothing in Part 2 has been guessed at.** Where a fact is unknown, it is listed
as unknown.

---

## Part 1 — AMP already supports

Every row below was read in the code at the commit above. Where a capability is
narrower than its name suggests, the narrowing is stated in the same entry
rather than in a footnote.

### 1.1 Manufacturer identity, onboarding and access

| # | Capability | Proof |
|---|---|---|
| 1 | **A manufacturer organisation can be onboarded without touching the database.** Founder-workspace only. | `oem_admin_routes.py` — `POST /saas/oems`, `PATCH /saas/oems/{oem_id}`; founder check at `:52` `_require_founder` |
| 2 | **Its first administrator is provisioned once**, with a generated password returned exactly once and stored only as a bcrypt hash (`security.hash_password`). The route **refuses** if an admin already exists — further accounts are the manufacturer's own job. | `oem_admin_routes.py:189` — `POST /saas/oems/{oem_id}/admin` |
| 3 | **The manufacturer signs in and manages its own people.** | `oem_routes.py:705` `POST /oem/login`, `:755` `POST /oem/change-password`, `:778` `GET /oem/users`, `:789` `POST /oem/users`, `:832` `PATCH /oem/users/{user_id}` |
| 4 | **Four OEM roles with a capability map**, a vocabulary deliberately disjoint from the factory roles. | `oem_auth.py:52` `ROLE_CAPABILITIES` — `OEM_ADMIN`, `OEM_SERVICE_MANAGER`, `OEM_SERVICE_ENGINEER`, `OEM_VIEWER` |
| 5 | **An OEM session cannot resolve to any factory's data by construction.** Every OEM request binds a sentinel tenant `OEM:<code>`; the ADR-0002 scoping hook then returns zero rows from every scoped factory table. | `oem_auth.py:134` `sentinel_tenant`, `:209` `require_oem`; bound for every request at `tenancy.py:262` |
| 6 | **The sentinel namespace is reserved**, so no factory can be created with a colliding tenant code. | `tenancy.py:59` `assert_tenant_code_available` |
| 7 | **Per-OEM white-label branding** (name, colour, logo URL, support email and phone) is returned to the portal. | `oem_routes.py:60` `GET /oem/me`; fields on `models.OemOrganization` |

**Narrowing, stated plainly:** branding is editable **only by the platform
operator** (`PATCH /saas/oems/{oem_id}`). The `manage_branding` capability
exists in `ROLE_CAPABILITIES` but **no route consumes it** — verified by
grepping every `require_oem(...)` call site. The same is true of
`manage_models` (see 1.2 #1).

### 1.2 Equipment records and the claim

| # | Capability | Proof |
|---|---|---|
| 1 | **A machine-model catalogue exists and is readable per manufacturer.** | `oem_routes.py:87` `GET /oem/models`, `:258` `GET /oem/models/{model_id}/telemetry`; `models.py:1033` `MachineModel` |
| 2 | **A manufactured machine can be registered** with a serial unique *per OEM*, starting in state `Manufactured` and belonging to no factory. | `oem_routes.py:484` `POST /oem/machines`; `models.py:1066` `MachineInstallation` (`UNIQUE(oem_code, serial_number)`) |
| 3 | **The OEM offers a machine with a one-time claim code**: 15 random characters from a 30-symbol transcription-safe alphabet (printed as `AMP-XXXXX-XXXXX-XXXXX`), **only the SHA-256 stored**, last four characters kept as a support hint, expiring (30 days by default, 365 maximum), revocable. | `oem_claims.py:57` `generate_code`, `:76` `hash_code`, `:98` `create`; `oem_routes.py:536` `POST /oem/machines/{id}/claim`, `:582` `GET /oem/claims`, `:615` `POST /oem/claims/{id}/revoke` |
| 4 | **Only a factory can accept.** Look-up is a GET that claims nothing; acceptance is a separate Admin-only POST, and it is the **only** code path in AMP that sets `MachineInstallation.factory_tenant_code`. Every failure returns one identical sentence. | `connected_equipment_routes.py:221` `GET /connected-equipment/claim/{code}`, `:247` `POST /connected-equipment/claim/{code}`; `oem_claims.py:46` `REFUSAL`, `:171` `accept` |
| 5 | **The factory links the serial to a machine on its own floor**, and can unlink or release it back to the manufacturer. | `connected_equipment_routes.py:361` `POST /connected-equipment/{id}/link`, `:441` `POST /connected-equipment/{id}/release` |
| 6 | **An explicit lifecycle state machine.** A transition not listed does not exist. | `oem_service.py:40` `LIFECYCLE`; `oem_routes.py:310` `POST /oem/machines/{id}/transition` |
| 7 | **Four commissioning checks, each a fact rather than a checkbox** — customer assigned, linked to a machine, model defines a telemetry profile, machine has reported at least once. They are **advice, not a gate**: AMP records `checks_passed` and raises a warning notification in the customer's workspace rather than refusing. | `oem_service.py:53` `COMMISSIONING_CHECKS`, `:93` `commissioning_report`; `oem_routes.py:350` `POST /oem/machines/{id}/commission`; `oem_subscribers.py:65` |
| 8 | **Warranty state from recorded dates**, with `unknown` — never `expired` — when the OEM recorded no end date. | `oem_service.py:128` `warranty_state` |
| 9 | **A service clock**: hours since the last recorded service against the model's `service_interval_hours`, with `not_configured` / `unknown` as first-class answers. A completed service can be recorded. | `oem_service.py:151` `service_state`; `oem_routes.py:396` `POST /oem/machines/{id}/service` |
| 10 | **A service queue with every recommendation carrying its reason and evidence**, explicitly *not* predictive — no model is trained, nothing is inferred, and `confidence` is `None` on every recommendation the queue can actually produce. | `oem_service.py:200` `recommendations`, `:328` `fleet_recommendations`; `oem_routes.py:205` `GET /oem/service` |
| 11 | **Fleet and per-machine read models**, and a customer roll-up. | `oem_routes.py:105` `GET /oem/fleet`, `:135` `GET /oem/customers`, `:157` `GET /oem/machines/{id}`, `:229` `GET /oem/machines/{id}/service` |
| 12 | **A manufacturer portal and a factory Connected Equipment screen exist as shipped UI.** | `frontend/app/oem/page.tsx`, `frontend/components/OemMachineRegistry.tsx`, `frontend/components/ConnectedEquipment.tsx`, `frontend/components/AddConnectedEquipment.tsx`, `frontend/app/claim/[code]/page.tsx` |

**Narrowings, stated plainly:**

- **There is no API to create or edit a machine model, and none to write a
  telemetry profile.** `GET /oem/models` and `GET /oem/models/{id}/telemetry`
  are reads; a repo-wide search for `models.MachineModel(` finds constructions
  only in tests, audit/verification/performance harnesses and the untracked
  sales-demo script. A real catalogue must today be loaded by a **direct
  database write**. The `manage_models` capability is granted to `OEM_ADMIN`
  and gates nothing.
- **`warranty_months` on a model is stored and displayed but never used to
  compute anything.** Warranty dates come from the registration payload
  (`oem_routes.py:449` `MachineRegistration`); AMP does not derive an end date.
- **AMP does not render a QR image.** It returns `claim_url`
  (`oem_routes.py:576`) and the portal prints it as text —
  `OemMachineRegistry.tsx:188` (`QR target: {issued.url}`). There is no QR
  library in `frontend/package.json`.
- **Notifications are in-portal only.** `oem_subscribers.py` writes
  `Notification` rows (the factory's own tenant, or the OEM sentinel) and
  `GET /oem/notifications` (`oem_routes.py:645`) reads them. There is no email
  or push — a search for `smtplib` / `smtp` / `sendgrid` / `send_email` across
  the backend returns nothing.
- **The service *projection* (`project_service_date`) is unreachable through
  any endpoint.** It needs ≥3 `(date, operating_hours)` samples passed as
  `history`, and every caller — `recommendations` at `oem_routes.py:254` and
  `fleet_recommendations` at `oem_service.py:338` — passes nothing. There is no
  operating-hours history table. Today the queue answers "how many hours
  remain", never "on which day". (The projection is the only code in the module
  that would carry a `confidence`, and it is derived from the sample size, not
  from a model.)

### 1.3 Consent — what a factory shares with its supplier

| # | Capability | Proof |
|---|---|---|
| 1 | **A closed grant vocabulary of exactly seven keys.** Unknown tokens are dropped on read and **refused** on write. | `oem_sharing.py:45` `ALL_GRANTS`, `:52` `GRANT_LABELS`, `:68` `parse_grants`; refusals at `connected_equipment_routes.py:132-136`, `:260-264` |
| 2 | **Default deny is the absence of a row.** No policy means nothing beyond the OEM's own records (serial, model, customer, site, warranty, lifecycle dates). | `oem_sharing.py:78` `grants_for`, `:64` `ALWAYS_VISIBLE` |
| 3 | **Consent is read at query time, never cached.** Withdrawal takes effect on the next request. | `oem_sharing.py:26-31`, `:78` |
| 4 | **The factory decides, and only the factory.** Admin-only, audited before/after; there is no OEM-side equivalent. | `connected_equipment_routes.py:113` `PUT /connected-equipment/sharing` |
| 5 | **Accepting a machine can only widen consent, never narrow it** — grants are unioned at claim time. | `connected_equipment_routes.py:307-316` |
| 6 | **A relationship is not consent.** Two independent things must both hold: an installation row *and* a granted policy. | `oem_sharing.py:10-17`, `:95` `installations_for` |
| 7 | **"Not shared" is distinguishable from "no data"** in the API and the portal. | `oem_routes.py:183` `not_shared`; `oem_sharing.py:171` `fleet_row` |

The exact seven keys and their labels, as a factory sees them:

| Key | Label shown to the factory |
|---|---|
| `SHARE_MACHINE_HEALTH` | Machine health score and connectivity state |
| `SHARE_OPERATING_HOURS` | Operating and loaded hours |
| `SHARE_SERVICE_STATUS` | Service due / overdue status |
| `SHARE_ALARMS` | Equipment alarm codes raised by this machine |
| `SHARE_TELEMETRY` | Live telemetry readings from this machine |
| `SHARE_MAINTENANCE_HISTORY` | Maintenance work carried out on this machine |
| `SHARE_DOWNTIME` | Downtime events recorded against this machine |

**The most important narrowing in this document.** Only **two** of the seven
grants gate any data today. Verified by searching every use of each constant
outside tests and audit harnesses:

- `SHARE_MACHINE_HEALTH` → gates `last_seen_at`, `machine_status`,
  `utilization` (`oem_sharing.py:202-207`, `:128` `visible_machine`)
- `SHARE_OPERATING_HOURS` → gates `operating_hours` (`oem_sharing.py:200`)
- `SHARE_ALARMS`, `SHARE_TELEMETRY`, `SHARE_MAINTENANCE_HISTORY`,
  `SHARE_DOWNTIME` → **appear nowhere outside the vocabulary itself.** They can
  be granted, stored, displayed and audited, and **no endpoint serves the data
  they describe.** AMP holds no equipment-alarm records at all. The two reading
  tables it does have — `IoTTelemetry` and `IndustrialSignal` — are factory
  tables keyed to a `Machine`, never to a `MachineInstallation`, and have no
  OEM-facing projection; the same is true of `DowntimeLog` and
  `MaintenanceTask`.
- `SHARE_SERVICE_STATUS` → also consulted nowhere. Service position is served
  ungated at `GET /oem/service` and `GET /oem/machines/{id}/service` by
  deliberate design: it is computed from the OEM's own records (its serial, its
  model's interval, the hours the machine reported), so `oem_routes.py:205`
  states it needs no grant. **A factory ticking that box today changes
  nothing.**

This is a consent *framework* that is fully built and a set of *payloads* that
mostly are not. It is honest to demonstrate the consent screen. It is not
honest to imply that granting `SHARE_ALARMS` will show a manufacturer alarms.

### 1.4 Telemetry — the single most dangerous area for overstatement

| # | Capability | Proof |
|---|---|---|
| 1 | **MQTT is the only ingest path a device can use unattended**, subscribing to `{prefix}/+/+/machines` (prefix default `flowmes`, `MQTT_TOPIC_PREFIX`). | `mqtt_service.py:39`, `:182` `on_connect`; `mqtt_identity.py:74` `topic_filters` |
| 2 | **The tenant comes from the topic, never from the body**, because the topic is the only part a broker can enforce. A payload that restates tenant/site must **agree** or the message is dropped. | `mqtt_identity.py:95` `parse_topic`, `:132` `check_payload_agrees` |
| 3 | **Fail-closed routing.** An unroutable topic, an unprovisioned tenant, or a contradicting payload is dropped **with a logged reason** — never defaulted to a tenant. | `mqtt_service.py:284-297`; `mqtt_identity.py:53` `RouteError` |
| 4 | **Machine identity is `(tenant, site, name)`**, enforced by a database unique constraint. | `models.py:30` `Machine.__table_args__`; `mqtt_service.py:103` `get_or_create_machine` |
| 5 | **Ingest is defensive**: status canonicalised, utilization clamped, production counts rejected if negative/non-finite, one `DowntimeLog` per breakdown *transition* rather than per message. | `mqtt_service.py:44` `_non_negative_int`, `:320-328`, `:394` |
| 6 | **Telemetry profiles are per-model configuration, not code.** AMP knows the *shape* of a profile; the signal names are the OEM's. Out-of-range values are **flagged, never clamped**. | `oem_telemetry.py:49` `parse`, `:109` `interpret`; `models.py:1059` `MachineModel.telemetry_profile` |
| 7 | **MQTT ingest writes `last_seen_at` and `operating_hours` on a LINKED installation**, taking hours only where the model's profile names a source, and only monotonically upward. | `mqtt_service.py:193` `_record_installation_report`, called at `:330` |
| 8 | **An authenticated live WebSocket** broadcasts machine updates, filtered to the connection's own tenant. | `mqtt_service.py:407-436`; `live_ws.py` |

**Narrowings, stated plainly — read these before any customer conversation
about connectivity:**

- **Topic segments must match `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`**
  (`mqtt_identity.py:43`). **Spaces are illegal.** A machine whose site is
  `Plant 1` cannot be addressed at all. `-` is the wire spelling of "no site".
- **`Machine.site` cannot be set through any HTTP API.** `schemas.MachineBase`
  has no `site` field (`schemas.py:65`), and the word `site` does not occur
  anywhere in `schemas.py`. The machine CSV import
  (`machines_routes.py:244`) reads only name / line / status / utilization. The
  only writers of `Machine.site` reachable from the running product are MQTT
  auto-create (`mqtt_service.py:126`, from the topic) and offboarding, which
  clears it (`offboard_tenant.py:107`); everything else that sets it is a
  direct-database seed or disaster-recovery script.
  *(The engineering readiness note states that CSV onboarding sets `site`; that
  is incorrect, and this document is the accurate one.)*
- **Readings other than operating hours are interpreted and then discarded.**
  `_record_installation_report` calls `oem_telemetry.interpret`, uses
  `operating_hours`, and keeps nothing else. Unknown tags and out-of-range
  values are written to the **server log only** (`mqtt_service.py:241-247`) —
  no endpoint surfaces them, so a commissioning engineer cannot see a mapping
  error without log access.
- **The two HTTP ingest endpoints are not a device integration.**
  `POST /iot/telemetry` (`industrial_iot_routes.py:36`) updates a machine's
  status and utilization; `POST /industrial/signals` (`:110`) updates status,
  utilization and the downtime string. Both are real — and both require a
  **factory Admin or Supervisor JWT**, and neither touches
  `MachineInstallation.last_seen_at` or `operating_hours`. A machine fed this
  way would never advance an OEM's service clock or pass the `has_reported`
  commissioning check.
- **The AMP-side MQTT client sets no credentials and no TLS.**
  `mqtt_service.start_mqtt_service` calls `mqtt.Client()` then `connect(host,
  port, 60)`. There is no `username_pw_set` and no `tls_set` anywhere in
  `mqtt_*.py`. Broker authentication and per-tenant ACLs are assumed to exist
  *on the broker*; AMP ships neither.

### 1.5 What is seeded or simulated — say so out loud

| # | What it looks like | What it actually is |
|---|---|---|
| 1 | Six industrial devices labelled **OPC UA, Modbus TCP, Siemens S7, Allen-Bradley, Beckhoff ADS, Omron FINS** | **Demo rows.** `industrial_adapters.py:160` `_DEMO_DEVICES` seeds six `IndustrialDevice` rows with invented IPs (`192.168.10.21` …) at startup (`main.py:634`), once, only if the table is empty. |
| 2 | A protocol adapter framework | **The framework is real; the drivers are not.** `get_adapter` (`industrial_adapters.py:152`) **always** returns `SimulatorAdapter` (`:141`), whose `read()` returns `random.randint` values from a per-protocol template table (`:48` `_SIGNAL_TEMPLATES`). `ProtocolAdapter.read` raises `NotImplementedError`. **Nothing in AMP opens a socket to a PLC.** |
| 3 | A live connectivity dashboard | `tick_industrial` (`industrial_adapters.py:189`) is called from the simulation loop (`main.py:480`) and appends random `IndustrialSignal` rows, trimmed back to 1,000 whenever the table exceeds 1,200. |
| 4 | `GET /industrial/protocols` | A **static table** of protocol names, ports and the Python library an edge agent *would* use (`industrial_adapters.py:32`, `:216`). It is a statement of intent, not a capability. |
| 5 | `/industrial/devices`, `/industrial/signals`, `/industrial/mappings` | **Plain CRUD** over three tables (`industrial_iot_routes.py`). A "PLC signal mapping" row is a record; nothing consumes it to read a PLC. |
| 6 | The AERON compressor demo | `backend/demo_aeron.py` is an **untracked working-tree script** (not committed), a self-contained sales demo that seeds a fictional manufacturer and a `DEMO_`-prefixed tenant by direct database write. |

**The honest one-line summary:** AMP's protocol story today is *"send us MQTT
JSON on a topic we can attribute"* — plus two HTTP endpoints that need a
factory user's login and are therefore not a device credential. Everything
labelled OPC UA / Modbus / S7 / Allen-Bradley / Beckhoff / Omron in the product
is a simulator and a table of names.

### 1.6 Platform plumbing that a pilot depends on

| # | Capability | Proof |
|---|---|---|
| 1 | **PostgreSQL migrations** for the OEM schema, including downgrades. | `alembic/versions/0006_oem_foundation.py`, `0007_oem_service_clock.py`, `0008_machine_claim.py` |
| 2 | **A schema-readiness deploy gate** — the app does not serve traffic against a schema it has not verified. | `schema_guard.py`; startup verdict at `main.py:546`, the 503 middleware at `main.py:684` |
| 3 | **Rate limiting on the OEM front door.** | `http_security.py:117` `RATE_LIMITS` — `/oem/login`, `/oem/change-password` |
| 4 | **Audit trail on every consent and claim event**, with before/after. | `connected_equipment_routes.py:160`, `:322-327`; `oem_routes.py:527`, `:566`, `:639` |

**Narrowing:** rate limits are **per path and per caller IP**. There is no
per-OEM metering, quota, or billing anywhere in the codebase.

---

## Part 2 — The OEM must provide

Facts about somebody's actual machines. None can be invented here. For each:
what it is, why AMP cannot supply it, and what breaks or stays empty without it.

### 2.1 Connectivity

| # | What is needed | Why AMP cannot invent it | What breaks / stays empty without it |
|---|---|---|---|
| 1 | **Controller make and model** on the machines to be connected (per model in the catalogue) | AMP has no way to know what silicon is in somebody else's machine | Nobody can say whether AMP can read anything at all. Every downstream decision — protocol, driver, edge box, cost — is blocked |
| 2 | **The protocol actually available at the machine**: OPC UA, Modbus TCP/RTU, S7, EtherNet/IP, ADS, FINS, a vendor cloud API, a file drop, or MQTT | The adapter is chosen by what the machine speaks, not by preference | If it is not MQTT and there is no MQTT-capable gateway, **there is no unattended ingest path at all** (Part 1.4 #1). Everything else in the OEM portal still works, and every telemetry-derived field stays `NULL` |
| 3 | **Whether an MQTT-capable gateway exists or can be fitted**, and who owns it | This is a hardware and commercial fact | Determines whether the pilot needs an edge programme AMP does not have (Part 3 #1) |
| 4 | **Broker decision**: whose broker, what authentication, TLS or not, and who writes the topic ACLs | AMP's security model *assumes* the broker authenticates the gateway and restricts its topic filters (`mqtt_identity.py:7-19`). AMP cannot make that true from its side | Without broker ACLs the tenant-in-the-topic guarantee is unenforced — any gateway could publish to any customer's topic. This is the load-bearing assumption of the whole ingest design |
| 5 | **Reporting cadence, link type, and behaviour when the link drops** | Only they know their sites | Decides what "last reported 2 days ago" means. `not_reporting` fires at ≥2 days (`oem_service.py:256`) — a number chosen without knowing their reality |
| 6 | **Customer-site network reality**: is there a network, who owns it, will the customer's IT permit an outbound connection | A commercial and IT fact about a third party | Determines whether the claim flow and the gateway are even reachable from the shop floor |

### 2.2 Signal semantics

| # | What is needed | Why AMP cannot invent it | What breaks / stays empty without it |
|---|---|---|---|
| 7 | **The tag / register map**: which address or node name carries running state, hours, counts, alarms — with **units and scaling** | A telemetry profile is a mapping onto *their* tags. `oem_telemetry.EXAMPLE_COMPRESSOR` and `EXAMPLE_CNC` are explicitly labelled examples to copy, not defaults | Without it no profile can be written. `MachineModel.telemetry_profile` stays `NULL`, the `telemetry_profile` commissioning check fails, and **no machine can be commissioned cleanly** (`oem_service.py:111`) |
| 8 | **Which tag is the hour meter**, its unit, whether it is monotonic, and what resets it (controller swap, service, power cycle) | AMP takes hours only where the profile names a source, and refuses a value going backwards | Without it `operating_hours` stays `NULL` — a different fact from zero. The **service clock never starts**: `service_state` returns `unknown` forever (`oem_service.py:165`) |
| 9 | **Plausible min/max per signal** | Out-of-range is flagged, never clamped — but AMP cannot know the band | No range flags. A disconnected analog input reading infinity is accepted as a reading |
| 10 | **Which signal decides "running"** (`state_signal`) | A profile with none is not broken; AMP simply cannot infer state and must say so (`oem_telemetry.py:150`) | Machine state cannot be derived from readings for that model |
| 11 | **Alarm definitions**: codes, severities, and which mean *stop* versus *note it* | A severity AMP invents will be ignored, and then a real one will be too | There is nowhere for alarms to go today (Part 1.3). Without their definitions there is nothing to design against either |

### 2.3 The catalogue and the commercial rules

| # | What is needed | Why AMP cannot invent it | What breaks / stays empty without it |
|---|---|---|---|
| 12 | **The model catalogue itself**: family, model code, name, rated capacity + unit, documentation URL — per model to be sold connected | It is their product line | `MachineInstallation.model_id` is `NOT NULL`, so **no machine can be registered at all** until at least one model row exists. Today those rows require a direct database write (Part 1.2) |
| 13 | **Service interval, per model, and its basis** — hours, calendar, cycles, or a combination — and **what resets it** | AMP models one basis: `service_interval_hours` since `last_service_hours` | A model with no interval reports `not_configured` (`oem_service.py:161`) and appears in no service queue. A calendar- or cycle-based interval **cannot be expressed at all** |
| 14 | **Warranty rules**: which event starts the clock (ship, install, commission), duration, and what voids it | AMP records `warranty_start` / `warranty_end` supplied at registration; whose event starts the clock is a commercial decision | With no end date every machine reports `unknown` and raises a `warranty_unknown` recommendation (`oem_service.py:239`). `warranty_months` on the model is stored but computes nothing |
| 15 | **Serial-number scheme**, and at what point in manufacture a serial is known | Uniqueness is enforced per OEM; the scheme is theirs | Determines whether registration happens at build, at despatch, or at install — and therefore where the claim code is applied |
| 16 | **Their real commissioning procedure** — what an engineer actually checks before signing a machine off | AMP's four checks are a reasonable guess; theirs is the one that matters | AMP's checks may be simultaneously too weak (missing a real sign-off step) and too strict (blocking on a link they do not care about) |

### 2.4 The people and the process

| # | What is needed | Why AMP cannot invent it | What breaks / stays empty without it |
|---|---|---|---|
| 17 | **Which of the seven data categories they will actually ask customers for**, and what they will do with each | Consent language is theirs to justify to their customers | Only two categories carry data today. Knowing which they *need* is what decides whether Part 3 #5–#8 are ever built |
| 18 | **Who is on their side**: how many people, in which of the four roles, and what a service engineer must be able to do | Roles are configuration, but the mapping to their org is theirs | Wrong role assignment either blocks engineers or hands write access to viewers |
| 19 | **Support contact and brand assets** (name, colour, logo URL, support email/phone) | It is their brand | The portal falls back to the raw OEM code; a factory sees a code rather than a supplier |
| 20 | **How a claim code physically reaches the customer** — printed label, crate sticker, despatch note, handover sheet — and who applies it | Possession of the code is the credential; the channel is a factory-floor process | Codes end up forwarded by email, which is the one channel the design is trying to avoid |
| 21 | **Machine and site naming per customer** | Site becomes a topic segment | A site name containing a space or `/` **cannot be addressed** (Part 1.4). Discovering this at commissioning is expensive |
| 22 | **Fleet size and shape** for the pilot: how many machines, at how many customers, at how many sites | Scale changes nothing structurally but changes everything operationally | The fleet-page query count was measured constant to 10,000 synthetic machines on a local PostgreSQL (`oem_perf.py`); the service queue deliberately walks the whole fleet, so its wall-clock grows with fleet size, and latency at their scale on a real network is unknown |

---

## Part 3 — May require engineering after discovery

None of this is committed. Each item states the **trigger** — the discovery
answer that would create the work. If the trigger does not fire, the work does
not happen.

### 3.1 Ingest and the edge

| # | Work | Trigger |
|---|---|---|
| 1 | **A real protocol client** (OPC UA / Modbus / S7 / EtherNet/IP / ADS / FINS) running on an edge agent | The machines speak a fieldbus protocol **and** there is no MQTT-capable gateway between the PLC and the network. Today `get_adapter` always returns the simulator |
| 2 | **MQTT broker authentication and TLS on the AMP side** | Any deployment outside a trusted network. The AMP client currently sets neither |
| 3 | **Store-and-forward / offline buffering** | The link at customer sites is intermittent, **or** the OEM states that gaps in `last_seen_at` are commercially unacceptable. Nothing of the kind exists today |
| 4 | **An edge device programme**: device certificates, provisioning, OTA update | The OEM wants AMP-supplied hardware or an agent it does not maintain itself. None of this exists in any form — there is no certificate handling, no device provisioning and no update mechanism anywhere in the codebase |
| 5 | **A non-MQTT ingest path** — vendor cloud API poll, SFTP/file drop, or an authenticated device-token HTTP endpoint | The machines report only to a vendor cloud, or only produce files. The existing HTTP ingest endpoints require a factory user's JWT and are not a device credential |
| 6 | **Surfacing unconfigured tags and out-of-range readings in the product** | Commissioning engineers need to see mapping errors. Today they exist only as `log.warning` lines |

### 3.2 The data the consent vocabulary promises

| # | Work | Trigger |
|---|---|---|
| 7 | **Storing telemetry readings per installation, plus a `SHARE_TELEMETRY` read model** | The OEM asks to see any reading other than operating hours. Today `interpret` returns them and `_record_installation_report` throws them away |
| 8 | **Alarm capture, storage, severity mapping, and a `SHARE_ALARMS` read model** | The OEM supplies alarm codes (Part 2 #11) and wants them visible. There is no equipment-alarm store for a machine anywhere in AMP |
| 9 | **`SHARE_DOWNTIME` and `SHARE_MAINTENANCE_HISTORY` read models** | The OEM asks for either — or a factory grants one and expects something to appear. `DowntimeLog` and `MaintenanceTask` exist as factory tables with no OEM-facing projection |
| 10 | **Gating service status on `SHARE_SERVICE_STATUS`** | A factory or its counsel objects that service position reaches the manufacturer without a grant. The current design treats it as the OEM's own arithmetic and consults the grant nowhere |
| 11 | **An operating-hours history table and the service-date projection** | The OEM wants "due on approximately this date" rather than "this many hours remain". The projection code exists and is unreachable — nothing supplies `history` |

### 3.3 Rules AMP cannot currently express

| # | Work | Trigger |
|---|---|---|
| 12 | **Calendar- or cycle-based service intervals**, or a combination with hours | The OEM's interval is not hours-only (Part 2 #13) |
| 13 | **A warranty rule engine**: start event, derived duration from `warranty_months`, extension, voiding conditions | Warranty starts at an event AMP does not record, or duration must be derived rather than typed, or voiding matters commercially |
| 14 | **Configurable commissioning checks** | Their sign-off procedure differs from AMP's four checks (Part 2 #16) |
| 15 | **Per-model or per-customer alarm severity policy** | Their severities are model-specific rather than global |

### 3.4 Product gaps a pilot will meet

| # | Work | Trigger |
|---|---|---|
| 16 | **A machine-model and telemetry-profile management API and UI** | The OEM has more models than a developer will hand-load once, or wants to change a profile without a deploy. Today both require a direct database write |
| 17 | **Setting `Machine.site` through the API** (and on CSV import) | The pilot has any customer with more than one plant, or machines created any way other than MQTT auto-create. No HTTP path can set it today |
| 18 | **Bulk machine registration** (CSV or batch endpoint) | Machines are registered in production batches rather than one at a time |
| 19 | **QR image generation and label artwork** | Claim codes are applied as printed labels at the factory. AMP supplies the URL as text and nothing else |
| 20 | **Email or push notification to the OEM** | The manufacturer's service desk will not sit watching the portal. Every notification today is an in-app row |
| 21 | **Per-OEM metering, quotas or billing** | Commercial terms are per machine, per connection, or usage-based. Nothing meters or bills per OEM |
| 22 | **Multi-replica review of the deploy contract** | The pilot runs on more than one replica. The deploy contract (ADR-0018) has never been reviewed for that case, and each replica starts its own MQTT listener and its own simulation loop |
| 23 | **Browser-level (Playwright) coverage of the OEM portal and claim flow** | Regressions in the portal must be caught before a customer sees them. A Playwright suite exists (`frontend/e2e`), but no spec in it touches the OEM portal or the claim flow — those are unit- and mutation-tested only |
| 24 | **An external review of the isolation boundary** | A second OEM joins the platform. Both existing audits were written by the same author as the design |

---

## The freeze

**AMP's OEM feature set is deliberately frozen.**

It is frozen because the platform has reached the point where the next honest
increment cannot be chosen from inside the codebase. Every remaining question —
which protocol, which tags, which interval, which alarms, which of the seven
consent categories anybody actually wants — is a question about somebody else's
machines, and answering it by inference produces confident, wrong software that
is more expensive to unwind than to never build.

**The next development work must originate from one of exactly two sources:**

1. **A verified production defect** — something in Part 1 that does not do what
   this document says it does, reproduced against a running deployment.
2. **A real OEM requirement** — an answer to a question in Part 2, from a named
   manufacturer, that fires a trigger in Part 3.

Speculation is not a third source. No further OEM features, no additional
dashboards, no additional AI, and no edge programme without real hardware
requirements from Part 2.

The right next event for this code is a real manufacturer using it.

---

## Correction, 2026-08-16 — four checkboxes that control nothing

Part 1 of this document lists what AMP already supports. One line in it needs
sharpening, because a prospective OEM will be shown the factory's consent screen
and will reasonably assume every switch on it does something.

Of the seven sharing permissions a factory can grant, **three are enforced and
four are not yet connected to any data**:

| permission | today |
|---|---|
| `SHARE_MACHINE_HEALTH` | **enforced** — releases the machine's status, utilisation and last-report time |
| `SHARE_OPERATING_HOURS` | **enforced** — releases the hour meter and every figure derived from it |
| `SHARE_SERVICE_STATUS` | **enforced** — releases the service verdict, and permits recording a service at a supplied hours reading |
| `SHARE_ALARMS` | no read path exists — nothing to release |
| `SHARE_TELEMETRY` | no read path exists |
| `SHARE_MAINTENANCE_HISTORY` | no read path exists |
| `SHARE_DOWNTIME` | no read path exists |

The four with no read path are **not a hole** — nothing escapes through them,
because there is nothing behind them. They are consent recorded ahead of a
capability, which is the right order. But they are a checkbox a customer can tick
believing data starts flowing, and the honest thing to say in a discovery call is:

> "Four of those seven are the vocabulary for capabilities we have not built. If
> alarms or downtime matter to your service business, that is a build, and it is
> exactly the kind of thing this pilot is for scoping."

**What we need from a real OEM:** which of the four actually matters commercially,
and what the data would have to look like to be worth anything. Alarm codes in
particular are model-specific and mean nothing without the manufacturer's own
fault dictionary — which AMP does not have and cannot invent.

## When does cover start? — `warranty_months` is declared and unused

A model carries `warranty_months` (the ACX-75 demo model says 24). AMP stores it,
returns it on `GET /oem/models`, and **applies it to nothing**. Warranty dates are
recorded per machine, by hand, at registration — since 2026-08-16 there are two
optional date fields on the registration form for exactly that; before then there
was no way to record a warranty through any interface at all, which is why every
machine read *"no warranty end date recorded"*.

Deriving `warranty_end` from `warranty_months` would need a start date, and that
is the question AMP cannot answer for a manufacturer:

> Does your cover run from despatch, from delivery, from installation, or from
> commissioning? And does it pause while a machine is out of service?

`oem_service.warranty_state` deliberately refuses to guess a period, on the
grounds that inventing one decides a commercial question on the customer's
behalf. Wiring `warranty_months` without settling the above would do precisely
that, one layer earlier.

**What we need from a real OEM:** the rule, in their words. If several
manufacturers give the same answer it is worth building; if they give four
different answers, the two date fields are the right design and
`warranty_months` should become a default the form offers rather than a value the
platform applies.
