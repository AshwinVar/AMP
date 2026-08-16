# OEM pilot intake

**The form that turns an interested machine manufacturer into an engineering-ready pilot.**

Version 1.0 · against AMP `master` @ `7a1333c` · owner: MARX8 / AMP

---

## How to use this document

One copy per pilot. Fill in Part 1 with the manufacturer; nothing in Part 2 can be
scheduled until Part 1 is complete, because every unanswered box below is a number
AMP would otherwise have to invent, and an invented number in a service platform
sends a van to the wrong site.

Part 2 is the acceptance test. It is filled in **during** the pilot, by whoever is
running it, against the named screen, endpoint or harness. A criterion with no
verification path is not a criterion.

Part 3 is what we are promising and what we are explicitly not promising. Read it
to the manufacturer before signing, not after.

**Nothing in this form is confidential to AMP.** It is designed to be sent to the
manufacturer as-is.

---

# PART 1 — INTAKE

## A. OEM information

| Field | Answer |
|---|---|
| Legal entity name | |
| Trading / brand name (appears in the portal) | |
| `oem_code` to use in AMP (see the rule below) | |
| Head office country / timezone | |
| Primary commercial contact (name, role, email, phone) | |
| Primary **engineering** contact (the person who knows the PLCs) | |
| Service-desk contact (who will actually log in daily) | |
| Portal brand colour (hex) | |
| Portal logo URL (must be publicly reachable) | |
| Support email shown to the customer on the claim screen | |
| Support phone shown to the customer | |
| Who at the OEM will hold the first administrator password | |

> **The `oem_code` rule, because its shape is load-bearing.** It must match
> `^[A-Z0-9][A-Z0-9_-]{1,31}$` — 2 to 32 characters of A–Z, 0–9, underscore or
> dash (`oem_auth.validate_oem_code`; lower case is upper-cased on the way in).
> No spaces, no dots, no colon. The code becomes the suffix of the sentinel tenant
> `OEM:<code>` that every one of this manufacturer's requests binds, which is what
> keeps an OEM session from ever resolving to a factory's data.
>
> AMP creates the organisation with `POST /saas/oems` and provisions **one**
> administrator with a one-time password (`POST /saas/oems/{id}/admin`). That
> route generates the username itself as `<oem_code lowercased>_admin` — it is not
> a field you choose — and refuses to run a second time. Every further OEM account
> is the manufacturer's own job through `POST /oem/users`.
>
> **What the branding actually changes** (`GET /oem/me`, `frontend/app/oem/page.tsx`):
> the portal header shows your name, your logo, and your support email and phone.
> The brand colour is used as the background of an initial-letter tile **when no
> logo URL is supplied**. It is not a full theme — the portal's own palette is
> AMP's.

**Who may do what, so the right people are named above** (`oem_auth.ROLE_CAPABILITIES`).
The four OEM roles are a different vocabulary from the factory's `Admin` /
`Supervisor` / `Operator`, and a token from one never satisfies the other:

| Role | Register machines & issue claims | Commission | Record a service | Manage OEM users |
|---|---|---|---|---|
| `OEM_ADMIN` | yes | yes | yes | yes |
| `OEM_SERVICE_MANAGER` | yes | **no** | yes | no |
| `OEM_SERVICE_ENGINEER` | no | yes | yes | no |
| `OEM_VIEWER` | no | no | no | no |

All four can read the fleet. Note the two gaps that bite during a pilot: a service
**manager** cannot press commission, and a service **engineer** cannot register a
machine or issue a claim code.

## B. Machine family

| Field | Answer |
|---|---|
| Family name as the OEM uses it (e.g. "Screw compressor", "Press brake") | |
| What the machine does, in one sentence a non-specialist understands | |
| Typical installed base size (machines in the field today) | |
| Typical customer profile (SME job shop, tier-1, process plant…) | |
| Is the family already instrumented in the field, or is this new? | |
| Who currently owns the data coming off these machines? | |

## C. Pilot model

One machine model per row. **A model is the unit AMP catalogues** — the telemetry
profile, the service interval and the warranty period all hang off it.

| Field | Model 1 | Model 2 |
|---|---|---|
| `model_code` (short, unique within the OEM) | | |
| Full model name | | |
| Family | | |
| Rated capacity + unit | | |
| Service interval, in **operating hours** | | |
| Warranty period, in **months** | | |
| Documentation URL | | |
| Firmware version(s) in the pilot | | |

> There is **no model-creation route in this release.** `MachineModel` is read by
> `/oem/models`, `/oem/models/{id}/telemetry` and the factory's Connected
> Equipment screen, and written by nothing in the API. The catalogue row and its
> telemetry profile are created by AMP engineering from the answers in sections C
> and H before the pilot starts. This is named as setup, not as workflow — see
> criterion 8 and "What we do not commit to".

## D. Number of pilot machines

| Field | Answer |
|---|---|
| Number of machines in the pilot | |
| Serial numbers (exact strings that will be registered) | |
| Are all serials from one model, or mixed? | |
| Are they already installed, or shipping during the pilot? | |
| Expected date each machine goes live | |

> Serials are unique **per OEM**, not globally. Two manufacturers may both register
> `0001` and they are two different machines. Give us the exact strings — they are
> printed on the claim sticker and typed by a factory administrator.

## E. Pilot factory (the customer)

| Field | Answer |
|---|---|
| Factory legal name | |
| `tenant_code` to use in AMP (uppercase; **no spaces, no colon**) | |
| Site name(s) the pilot machines stand at | |
| Site token for the MQTT topic (see the constraints below) | |
| Named factory **Admin** who will accept the claim | |
| Is that person's AMP account already provisioned? | |
| Does the factory already use AMP, or is this their first workspace? | |
| Who at the factory decides what is shared with the OEM? | |
| Has the factory agreed to be in a pilot in writing? | |

> **Hard constraint, stated early because it bites late.** The tenant and the site
> are segments of the MQTT topic (`backend/mqtt_identity.py`) and each must match
> `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`. **A site called `Plant 1` cannot be addressed
> at all** — the space is illegal in a topic segment. Pick `PLANT1` or `Plant-1` now.
> A machine with no meaningful site uses the token `-`.
>
> On the tenant code specifically: AMP itself only *refuses* a code containing `:`
> at `/saas/tenants` (`tenancy.assert_tenant_code_available`, reserving the OEM
> sentinel namespace). A code with a space would be **accepted at creation and then
> be permanently unaddressable over MQTT**, with nothing to do about it but recreate
> the workspace. Treat the topic charset as the real rule.
>
> **Second constraint — where a machine's site can come from.** A machine created
> through `POST /machines` has no site: `schemas.MachineBase` carries only `name`,
> `status`, `utilization`, `downtime`, and the column defaults to the empty string.
> The CSV importer (`POST /machines/import-csv`) does not set it either — it reads
> name, line, status and utilization only. **In this release, MQTT ingest is the
> only path in the product that puts a site on a machine**, taken from the topic
> when it auto-creates the machine on first message.
>
> **Third constraint — and it follows directly from the second.** A machine's
> identity is the triple `(tenant, site, name)`. If the factory pre-creates
> `COMP-01` through the UI (site `""`) and the gateway then publishes to
> `flowmes/TENANT/PLANT1/machines`, AMP will not find that row — it will create a
> **second** `COMP-01` at site `PLANT1`. The installation stays linked to the first,
> telemetry lands on the second, and `has_reported` never turns green. For a pilot
> whose machines were created in the UI, publish with the site token `-`. Decide
> which way round you are doing it, here.
>
> **Fourth constraint.** The tenant must be provisioned in AMP *before* the gateway
> is pointed at the broker. `mqtt_service.tenant_is_provisioned` rejects telemetry
> for a tenant that has no config row, no user and no machine, and logs the reason.

## F. Controller

| Field | Model 1 | Model 2 |
|---|---|---|
| PLC / controller make | | |
| Controller model and firmware | | |
| HMI make/model (if it is the data source instead) | | |
| Is there a spare Ethernet port on the machine? | | |
| Is there an existing gateway / edge box on the machine? | | |
| Who may change the controller program — OEM, customer, or neither? | | |
| Is the program password-protected? Who holds it? | | |

## G. Protocol

| Field | Answer |
|---|---|
| How will readings leave the machine? (tick one) | ☐ MQTT from a gateway the OEM supplies ☐ MQTT from a gateway the customer supplies ☐ Something else — describe |
| If "something else": what protocol is available at the machine? (OPC UA / Modbus TCP / Modbus RTU / S7 / EtherNet-IP / ADS / FINS / file drop) | |
| Who builds the bridge from that protocol to MQTT, and by when? | |
| Broker: AMP-hosted, OEM-hosted, or customer-hosted? | |
| Broker credentials and topic ACL — who issues them? | |
| TLS required? | |

> **Read this before answering.** AMP's **only live ingest path is MQTT**
> (`backend/mqtt_service.py`). It subscribes to `{prefix}/+/+/machines` — by default
> `flowmes/{TENANT}/{SITE}/machines`, prefix configurable with `MQTT_TOPIC_PREFIX` —
> and expects a JSON object:
>
> ```json
> {
>   "machine": "COMP-01",
>   "status": "Running",
>   "utilization": 72,
>   "downtime": "0 min",
>   "readings": { "<your tag>": 41.2, "<your tag>": 18422 }
> }
> ```
>
> Optional production counters (`total_count`, `good_count`, `rejected_count`,
> `planned_minutes`, `runtime_minutes`, `ideal_cycle_time_seconds`) are accepted for
> OEE. A production record is written only when `total_count > 0` **and**
> `good_count + rejected_count == total_count`; anything absent, non-numeric or
> negative simply means no production record for that message, and the rest of the
> message is still processed.
>
> **`backend/industrial_adapters.py` is not a second ingest path.** It seeds six
> demo devices labelled OPC UA, Modbus TCP, Siemens S7, Allen-Bradley, Beckhoff ADS
> and Omron FINS, and `get_adapter()` returns `SimulatorAdapter` for every one of
> them — `read()` calls `random.randint` against a per-protocol signal template.
> `backend/industrial_iot_routes.py` is authenticated CRUD over the resulting
> device/signal/mapping tables, plus an `/iot/telemetry` table. There is also a
> `GET /industrial/protocols` route, and it returns a **static list of six protocol
> descriptions** — the port and the Python library an edge agent *would* use. It
> connects to nothing. **No code in this repository opens a connection to a PLC.**
> If your answer above is anything other than MQTT, the bridge is work somebody has
> to do, and this form is where we agree who.

## H. Tag map

**This is the section the pilot actually depends on.** A telemetry profile is a
mapping onto *your* tags; a guessed one produces confident wrong numbers.

One row per signal, per model. `source` is your tag exactly as the gateway will
publish it inside `readings`. `name` is what AMP will call it.

| AMP `name` | Your tag (`source`) | `datatype` | `unit` | `min` | `max` | `aggregation` | `state_signal` | Notes / scaling |
|---|---|---|---|---|---|---|---|---|
| | | number / bool / string | | | | last / sum / max / min / avg | yes / no | |
| | | | | | | | | |
| | | | | | | | | |
| | | | | | | | | |
| | | | | | | | | |

Required for the pilot to mean anything:

| Question | Answer |
|---|---|
| Which tag carries the **hour meter**? (its `name` must be `operating_hours`) | |
| Is the hour meter monotonic, or can it reset? What resets it? | |
| Which tag(s) decide whether the machine is **running**? (`state_signal: yes`) | |
| Raw units and scaling factor for each numeric tag | |
| Plausible min/max for each numeric tag | |
| Which tags are commercially sensitive and must **not** be published? | |

> **How AMP treats this** (`backend/oem_telemetry.py`, `mqtt_service._record_installation_report`):
> * a reading whose `source` is not in the profile is **reported as an unconfigured
>   tag in the log**, never dropped silently and never guessed into a column;
> * a reading outside `min`/`max` is **flagged and logged, never clamped** — a 400 °C
>   discharge temperature stays 400 °C, because clamping turns an overheating machine
>   into a healthy-looking one;
> * `operating_hours` is written to the installation **only** where the profile names
>   a source for it, only where the installation is **linked** to the machine that
>   reported, and only **upward** — a lower reading is logged as going backwards and
>   the higher figure is kept, so a replaced controller cannot silently cancel a due
>   service;
> * a model with no profile keeps `NULL`, and the portal renders that as "no data",
>   never as zero.
>
> Note what these guarantees are, and are not. The range flag and the unconfigured-tag
> report are **log lines**, written through `logging_config`. There is no screen in
> AMP that lists out-of-range readings or unmapped tags, and no alert is raised.

## I. Connectivity

| Field | Answer |
|---|---|
| Agreed reporting interval **R** (seconds/minutes between messages per machine) | |
| Link at the customer site (wired LAN / customer Wi-Fi / 4G / none) | |
| Who owns that network, and who authorises the outbound connection? | |
| Will the customer permit an outbound connection to the AMP broker? | |
| Firewall change required? Who raises it, and how long does it take? | |
| Static outbound IP available for allow-listing? | |
| Expected outage pattern (nightly shutdown, weekend, shift-only power) | |
| What happens to readings while the link is down? | |
| Longest acceptable gap before someone should be told | |

> **There is no store-and-forward.** A message published while the link is down is
> lost, not queued — AMP has no edge programme, no device certificates, no
> provisioning and no OTA (see "What we do not commit to"). If the link at this
> site is unreliable, say so here and we will size the pilot around it rather than
> discover it in week three.
>
> Note the two staleness granularities you will meet: the OEM portal's
> connected / offline / unknown badge (`frontend/lib/oem.ts`, `fleetSummary`) turns
> "offline" only after **48 hours**, and the service queue's `not_reporting`
> recommendation (`backend/oem_service.py`) fires at **2 days**. A tighter gap
> target than that must be measured from `last_seen_at` directly — see criterion 3.

## J. Commissioning

| Field | Answer |
|---|---|
| Your real commissioning procedure — what an engineer checks before sign-off | |
| Who signs a machine off: OEM engineer, customer, or both? | |
| Is there a paper/QMS record today that this must not contradict? | |
| Who will press "commission" in the AMP portal during the pilot? (needs `OEM_ADMIN` or `OEM_SERVICE_ENGINEER`) | |
| Acceptable time from machine powered-on to commissioned | |
| Who prints and attaches the claim code/QR to the machine or crate? | |

> **AMP's four checks** (`backend/oem_service.py`, `COMMISSIONING_CHECKS`) are:
> `assigned_to_customer`, `linked_to_machine`, `telemetry_profile`, `has_reported`.
> They are facts, not ticked boxes. They are also **advice, not a gate** — AMP will
> commission a machine with a check outstanding, record `checks_passed=false` on the
> event, and warn the customer. Tell us if your procedure requires a hard stop; today
> it is not one.
>
> One ordering fact the form has to name: commissioning is reached from the
> `Installed` state, so the OEM posts `POST /oem/machines/{id}/transition` with
> `{"target": "Installed"}` **before** `POST /oem/machines/{id}/commission`.
> Commissioning straight from `Assigned` returns a 400 naming the allowed
> transitions.

## K. Service

| Field | Model 1 | Model 2 |
|---|---|---|
| Service interval basis (hours / calendar / cycles / combination) | | |
| Interval value | | |
| What **resets** the clock? (completed service, part change, both) | | |
| Who records a completed service — OEM engineer or customer? | | |
| Do you want "due soon" warnings, and at what threshold? | | |
| Typical lead time from "due" to an engineer on site | | |

> **AMP's service clock is arithmetic and says so.** `service_state` computes
> `operating_hours − last_service_hours` against the model's
> `service_interval_hours`, and returns `overdue` (≤ 0 remaining), `due` (≤ 5 %),
> `due_soon` (≤ 15 %), `ok`, `unknown` (the machine has never reported hours, **or**
> the counter now reads below the last recorded service — a reset or a replaced
> controller) or `not_configured` (no interval on the model). **It is hours-only.**
> Calendar and cycle-based intervals are not implemented. The "due soon" thresholds
> are fixed percentages in code, not a per-OEM setting. If your answer above is not
> "hours", say so now — it changes what the pilot can claim.
>
> The clock is reset by `POST /oem/machines/{id}/service`, which writes
> `last_service_hours` (defaulting to the machine's last reported hours if the
> service desk does not supply a figure). Nothing else moves it.

## L. Alarms

| Field | Answer |
|---|---|
| Alarm code list (attach) — code, text, severity | |
| Which codes mean **stop the machine** vs **note it**? | |
| How does an alarm leave the machine — a tag, a bitfield, a string? | |
| Who is supposed to act on an alarm today: OEM or customer? | |
| Do you expect AMP to raise anything to the OEM on alarm during the pilot? | |

> **Answer honestly and expect nothing back in this pilot.** There is a sharing
> grant called `SHARE_ALARMS` ("Equipment alarm codes raised by this machine"), and
> the vocabulary exists — but **no alarm entity, ingest path or read model exists in
> the codebase**. An alarm tag in the profile is interpreted and range-checked like
> any other reading; it is not stored as an alarm, not surfaced in the portal, and
> not escalated. Collecting the list now is how we specify the next release, not how
> we deliver this one.

## M. Warranty

| Field | Answer |
|---|---|
| What event starts the warranty — ship, install, or commission? | |
| Duration, in months, per model | |
| What voids it? | |
| Who is entitled to see remaining cover: OEM, customer, or both? | |
| Do you want the customer to see the warranty on their own screen? | |

> AMP records `warranty_start` and `warranty_end` **dates** on the installation and
> derives `active` / `expired` / `not_started` / `unknown`. It never guesses a period:
> no end date means `unknown`, not `expired`. Which *event* starts the clock is a
> commercial decision AMP will not make for you — the dates are entered when the
> machine is registered (`POST /oem/machines`), and nothing derives them from the
> model's `warranty_months`.
>
> The warranty dates are visible to the factory with no sharing grant at all: they
> come from the OEM's own record, and they appear on the claim preview before the
> factory decides.

## N. Data-sharing expectations

The grant vocabulary is a **closed set of seven** (`backend/oem_sharing.py`,
`ALL_GRANTS`). The factory grants these and nothing else. Ask for what you want;
the factory decides.

| Grant key | Label shown to the factory | OEM wants it? | Factory agrees? |
|---|---|---|---|
| `SHARE_MACHINE_HEALTH` | Machine health score and connectivity state | ☐ | ☐ |
| `SHARE_OPERATING_HOURS` | Operating and loaded hours | ☐ | ☐ |
| `SHARE_SERVICE_STATUS` | Service due / overdue status | ☐ | ☐ |
| `SHARE_ALARMS` | Equipment alarm codes raised by this machine | ☐ | ☐ |
| `SHARE_TELEMETRY` | Live telemetry readings from this machine | ☐ | ☐ |
| `SHARE_MAINTENANCE_HISTORY` | Maintenance work carried out on this machine | ☐ | ☐ |
| `SHARE_DOWNTIME` | Downtime events recorded against this machine | ☐ | ☐ |

| Field | Answer |
|---|---|
| Which grants are **essential** to the OEM's pilot case? | |
| Which would the OEM like but can live without? | |
| Has the factory seen this list before signing? | |
| Any data the factory has stated it will **never** share? | |
| Which named person at the factory holds the consent switch? | |
| Is there a DPA / NDA that must be signed first? Who owns it? | |

> **What is visible with no policy at all** (`ALWAYS_VISIBLE`): the serial, the model,
> the installation's lifecycle status, the customer code, the site, the warranty
> dates, `commissioned_at` and `installed_at`. All of it comes from the OEM's own
> records. It is not derived from factory operations and it is not withheld.
>
> **What each grant currently *does*** — this is the honest part. In the shipped
> fleet read model (`oem_sharing.fleet_row`):
> * `SHARE_OPERATING_HOURS` releases `operating_hours`;
> * `SHARE_MACHINE_HEALTH` releases `last_seen_at`, and — through the one gated
>   read of a factory table, `visible_machine` — the machine's `status` and its
>   `utilization`;
> * `SHARE_SERVICE_STATUS` releases the service **verdict** — whether the machine
>   is ok, approaching, at or past its interval — in the service queue
>   (`GET /oem/service`) and the per-machine service view. It also decides
>   whether a manufacturer may record a service against an hours reading it
>   supplies itself;
> * `SHARE_ALARMS`, `SHARE_TELEMETRY`, `SHARE_MAINTENANCE_HISTORY` and
>   `SHARE_DOWNTIME` are **valid, storable, auditable consent with no data class
>   behind them yet.** Granting them today changes nothing an OEM can see. They
>   are in the vocabulary so that consent is recorded before the capability
>   ships, not after — and a factory should read a tick against them as a
>   decision taken in advance, not as data flowing now.
>
> **What the verdict necessarily tells a manufacturer.** A verdict is a coarse
> function of the hour meter: "due" means the machine sits between 95% and 100%
> of an interval the manufacturer already knows, because the interval is on its
> own model. A factory granting `SHARE_SERVICE_STATUS` is agreeing to that band,
> and a verdict that did not depend on the meter would not be a verdict. What it
> is *not* agreeing to is arithmetic that narrows the band further — the hours
> run, the hours since service and the hours remaining are all withheld unless
> `SHARE_OPERATING_HOURS` is also granted, including inside the free-text
> evidence a recommendation carries.
>
> **This changed on 2026-08-16, and the previous version of this page was wrong.**
> It said the service views "need no grant" and that granting
> `SHARE_SERVICE_STATUS` "adds nothing". That described the code accurately at the
> time, and the code was the thing that was wrong: the service queue printed the
> hour meter in prose for a manufacturer whose customer had switched
> `SHARE_OPERATING_HOURS` off, and kept printing it after "Withdraw everything".
> Both are now gated, with the tests in
> `backend/test_oem_service_consent.py` pinning it. If an earlier copy of this
> pack was handed to you, this paragraph is the correction.

## O. Pilot success criteria — the OEM's own

Part 2 is AMP's engineering acceptance test. This box is for the manufacturer's
commercial one, in their words.

| Field | Answer |
|---|---|
| What must be true at the end for the OEM to call this a success? | |
| What would make the OEM walk away? | |
| Pilot start date | |
| Pilot end date | |
| Observation window for the telemetry criterion (see criterion 3) | |
| Who signs the pilot off, on each side? | |
| What happens to the data at the end of the pilot? | |

---

# PART 2 — PILOT SUCCESS CRITERIA

Every criterion below is measurable, and every one names the screen, endpoint or
file that decides it. Fill the last column with **PASS** or **FAIL** and a date.
A criterion with a blank verification column is a criterion nobody checked.

Fill these in first — the pass conditions reference them:

| Symbol | Meaning | Value for this pilot |
|---|---|---|
| **R** | agreed reporting interval, per machine (section I) | |
| **W** | continuous observation window for criteria 3 and 8 | |
| **N** | number of pilot machines (section D) | |

## The checklist

| # | Criterion | Measurable pass condition | Verified by | PASS / FAIL |
|---|---|---|---|---|
| 1 | **Machine claimed by the factory** | All **N** installations move `Manufactured → Assigned` through a factory Admin's own POST. Zero claimed by the OEM. Each claim code is single-use: a second attempt with the same code returns 404. | Factory screen: **Connected Equipment** (`frontend/components/ConnectedEquipment.tsx`) / claim page `frontend/app/claim/[code]/page.tsx`. API: `GET /connected-equipment/claim/{code}` then `POST /connected-equipment/claim/{code}`; confirm with `GET /connected-equipment`. Regression evidence: `backend/audit_oem_pilot_journey.py` §3, `backend/verify_pg_claim.py`. | |
| 2 | **Commissioning completed with every check passing** | For all **N**: the factory has linked the serial to a shop-floor machine, the OEM has moved the installation to `Installed`, and `POST /oem/machines/{id}/commission` then returns `commissioning.ready == true` with all four checks `passed: true` (`assigned_to_customer`, `linked_to_machine`, `telemetry_profile`, `has_reported`), and `to == "Active"`. | OEM portal machine drawer (`frontend/app/oem/page.tsx`, "Commissioning"/"Telemetry" panels). API: `POST /connected-equipment/{id}/link` (factory) → `POST /oem/machines/{id}/transition` `{"target":"Installed"}` → `POST /oem/machines/{id}/commission`; re-read `GET /oem/machines/{id}/service`. Logic: `backend/oem_service.py::commissioning_report`. Regression evidence: `backend/audit_oem_pilot_journey.py` §4. | |
| 3 | **Telemetry received reliably** | Over the window **W**, for every pilot machine: (a) no gap between consecutive accepted messages exceeds **3 × R**, excluding pre-agreed shutdown windows listed in section I; (b) ≥ 98 % of expected messages accepted, where expected = W ÷ R; (c) **zero** messages rejected as unroutable after commissioning; (d) `last_seen_at` sampled at any time is never more than **3 × R** behind, outside those windows. | (a)+(b)+(c) from the ingest log — `backend/mqtt_service.py` logs every accepted message and every rejection with its reason (`MQTT message REJECTED …`) through `logging_config`. (d) by polling `GET /oem/fleet` (needs `SHARE_MACHINE_HEALTH`) or, factory-side, `GET /connected-equipment`. That MQTT actually writes `last_seen_at`/`operating_hours` for a **linked** installation is pinned by `backend/test_mqtt_installation_reporting.py`; topic routing by `backend/test_mqtt_tenant_identity.py` and `backend/mutate_mqtt_identity.py`. **AMP has no gap report — this is measured from logs and samples by hand.** | |
| 4 | **OEM sees permitted information** | Every field the factory granted is populated and correct: with `SHARE_OPERATING_HOURS`, `operating_hours` on `GET /oem/fleet` equals the hour meter the gateway published (± one reporting interval). With `SHARE_MACHINE_HEALTH`, `machine_status`/`utilization` match the shop floor. `shared` on each fleet row lists exactly the granted keys. | OEM portal fleet table and machine drawer (`frontend/app/oem/page.tsx`). API: `GET /oem/fleet`, `GET /oem/machines/{id}`, `GET /oem/sharing`. Cross-check against the factory's own machine screen. Regression evidence: `backend/audit_oem_pilot_journey.py` §4, `backend/audit_oem_adversarial.py` §D. | |
| 5 | **OEM cannot see prohibited information** | Every un-granted field on `GET /oem/machines/{id}` is `null`, and its grant key appears in the `not_shared` array — never rendered as `0`. No `/oem` response contains any work order, order, part, price, cost, recipe, employee, or any of the **factory's own** customers. (The `customer` field on a fleet row is the factory's own tenant code — the OEM's record of who it shipped to — and is visible by design.) An OEM token is refused (403) at every factory route, including `/connected-equipment` and `/connected-equipment/sharing`. Another manufacturer's installation id returns **404, not 403**. | API: `GET /oem/machines/{id}` (`not_shared` array), `GET /oem/fleet`. Refusal checks against `/connected-equipment`, `/machines`, `/work-orders` with the OEM token. UI wording: `frontend/lib/oem.ts::shareable` renders "not shared" vs "no data" vs a value. Regression evidence: `backend/audit_oem_adversarial.py` §A–H (2 OEMs × 3 factories, HTTP + WebSocket + MQTT + CSV export) and `backend/audit_oem_specialist.py`. | |
| 6 | **Service workflow demonstrated end to end** | A real service event completes without AMP support: the machine appears on `GET /oem/service` as `kind: "service_due"` with non-empty `evidence` and `confidence: null`; the engineer records it via `POST /oem/machines/{id}/service`; `service.state` returns to `ok`; and the **factory** sees the new state on its own screen within one page load. | OEM portal **Service queue** (`frontend/app/oem/page.tsx`, `<h2>Service queue</h2>`). API: `GET /oem/service` → `POST /oem/machines/{id}/service` → `GET /connected-equipment` (factory side, `service.state`). Logic: `backend/oem_service.py::service_state`, `recommendations`. Regression evidence: `backend/audit_oem_pilot_journey.py` §5, `backend/mutate_oem_service.py` (19 honesty mutations, all caught). | |
| 7 | **Factory can revoke consent and the OEM loses the field immediately** | Factory Admin removes a grant in Connected Equipment. On the OEM's **very next request** the field is `null` and the key is absent from `shared` on `GET /oem/fleet` (and present in `not_shared` on `GET /oem/machines/{id}`, which is the only response carrying that array). The machine itself stays visible. No restart, no cache flush, no AMP involvement. Timed: revoke, then re-request within 10 s. | Factory screen: **Connected Equipment** sharing controls. API: `PUT /connected-equipment/sharing` (Admin-only, audited before/after via `log_audit`), then immediately `GET /oem/fleet`. Logic: `oem_sharing.grants_for` is read at query time and never cached. Regression evidence: `backend/audit_oem_pilot_journey.py` §6, `backend/audit_oem_adversarial.py` §G, `backend/mutate_oem_sharing.py`. | |
| 8 | **No developer or database intervention during the normal workflow** | Across the whole window **W**, the count of (a) direct database writes, (b) hand-minted tokens, (c) source or config changes, and (d) AMP-staff actions taken on behalf of either party, is **zero** — for every step from claim through commissioning, telemetry, service and revocation. Setup actions listed below are excluded and must be logged separately, with a count. | Kept as a running log by whoever runs the pilot, reconciled at the end against `audit_log` (`log_audit` records the actor for claim, sharing change, link, release, registration, login and user administration) and against the deployment's commit history for **W**. Structural evidence that the journey needs no developer: `backend/audit_oem_pilot_journey.py` — 40 steps, every one an HTTP request carrying a token AMP issued, run on PostgreSQL. | |

## Verification detail and honest caveats, criterion by criterion

**1 — Claim.** The code is a credential: 73.6 bits (15 characters from a 30-symbol
alphabet), stored only as SHA-256, shown exactly once, with the last four characters
kept as a support hint. Every refusal — mistyped, expired, spent, revoked, meant for
another factory, or for a machine already installed somewhere — returns the identical
sentence, so a failed claim during the pilot tells you nothing about *why* by design.
Use the code hint on `GET /oem/claims` to reconcile. AMP supplies `claim_url`
(`{APP_BASE_URL}/claim/{code}`); **AMP does not render the QR image** — the portal
displays the URL as text and printing it is the OEM's job (section J).

**2 — Commissioning.** `has_reported` is satisfied by `last_seen_at` being non-null,
which in turn requires the installation to be **linked** to a shop-floor machine
(`POST /connected-equipment/{id}/link`, factory-side) *before* telemetry arrives.
Order matters: telemetry that arrives before the link is not attributed to the
installation and the check stays red. `linked_to_machine` will also stay red if the
gateway's site token and the machine's stored site disagree — see section E's third
constraint. Also note the report is advice — a commission call succeeds with a
failing check and records `checks_passed: false`. For this criterion, `ready == true`
is required, not merely a 200.

**3 — Telemetry.** Define R and W in section I/O before the pilot starts; "reliably"
without a number is not a criterion. The three-interval tolerance is deliberate: one
missed publish is a hiccup, three consecutive is a fault. Measurement is manual —
there is no ingest-gap read model in AMP, and the portal's own connected/offline
badge only flips at 48 hours (`frontend/lib/oem.ts`), far coarser than most useful R.
**Rejections** to watch for in the log: a payload that is not a JSON object, an
unroutable topic, a payload tenant or site contradicting the topic, and an
unprovisioned tenant. Distinct from those, and easy to confuse with them: a message
that IS accepted but logs `reported unconfigured tags` or `reported out-of-range`.
Those are warnings on an accepted message, not rejections — the unconfigured-tag one
is a commissioning defect in the tag map, section H, and should be driven to zero
even though it costs you no data.

**4 / 5 — Permitted and prohibited.** These are one test run twice: once for what
should be there and once for what should not. Criterion 5 is the one that protects
the *factory*, and it is the one to run first, before the factory has granted
anything. Structurally, an OEM session binds a sentinel tenant `OEM:<code>` that no
factory can hold, so factory tables return zero rows before any `/oem` route is
consulted. Note the wording distinction the service desk must be briefed on: **"not
shared" is a permission, "no data" is a fault** — they look similar and lead to very
different actions.

**6 — Service.** The queue is arithmetic over reported facts and labels itself as
such: `confidence` is `null` on every rule-derived recommendation. A confidence
figure appears **only** on `service_projection`, is derived from the sample (≥ 3
points spanning ≥ 1 day) and is capped at 0.85 — and note that the fleet-wide
`GET /oem/service` passes no history, so in this release the projection does not
appear there at all. Do not describe any of it as predictive maintenance in pilot
materials.

**7 — Revocation.** Withdrawal takes effect on the **next request**, not
retroactively: anything the OEM already exported or wrote down is gone from AMP's
control. Say this to the factory before they grant anything. Note also that accepting
a *new* machine can only widen consent, never narrow it — the policy is keyed
(OEM, factory), and adding a second machine with no boxes ticked does not switch off
sharing on the first. Withdrawal happens in one place only: the Admin-only control
under Connected Equipment.

**8 — No intervention.** Two setup actions are excluded, and both must be counted and
disclosed rather than quietly absorbed:

1. **The founder/platform account**, which is how AMP is installed rather than
   something a user does.
2. **The machine model and its telemetry profile.** There is no model-creation route
   in this release; AMP engineering writes the catalogue row from sections C and H
   before the pilot opens. `backend/audit_oem_pilot_journey.py` seeds exactly this
   and names it as the exception at the top of the file.

Everything after those two — onboarding the OEM, first admin, password rotation,
registering machines, issuing claims, the factory accepting, linking, transitioning,
commissioning, telemetry, service, and revocation — must run with zero intervention
or criterion 8 fails. Note that the pilot-journey harness *itself* seeds the telemetry
rows (there is no HTTP route to post operating hours, and there should not be one);
in a live pilot that step is the real MQTT gateway, which is why criterion 3 is
measured against the broker and not against the harness.

## Sign-off

| | Name | Role | Date | Signature |
|---|---|---|---|---|
| OEM | | | | |
| Factory | | | | |
| AMP | | | | |

**Criteria passed:** ____ / 8    **Pilot outcome:** ☐ Proceed ☐ Extend ☐ Stop

---

# PART 3 — BOUNDARIES

## What we commit to

1. **A manufacturer can be onboarded and run its own portal** — organisation,
   first administrator with a one-time password, then its own user administration
   through `/oem/users`. White-labelled by configuration from section A — name,
   logo, support contacts, and a brand colour used for the fallback tile: one
   build, no per-OEM fork.
2. **A model catalogue with a telemetry profile per model** — AMP's canonical names
   mapped onto *your* tags, with units, plausible ranges, aggregation and which
   signal decides running state. Written by AMP engineering before the pilot, since
   there is no model-creation route (see gap 10 below).
3. **Machine registration and a factory-controlled claim.** The OEM registers what it
   built and *offers* it; a one-time, hashed, expiring, revocable code travels with
   the machine; **only a factory Admin can accept it.** There is no route by which a
   manufacturer attaches equipment to a customer.
4. **Explicit, per-relationship, revocable data-sharing consent** from the closed
   vocabulary in section N, read at query time, withdrawable on the factory's own
   screen, audited before-and-after, with no OEM-side equivalent.
5. **Isolation between manufacturers and between factories**, enforced structurally
   rather than by handler discipline, and evidenced by
   `backend/audit_oem_adversarial.py` (2 OEMs × 3 factories) and
   `backend/audit_oem_specialist.py`.
6. **MQTT telemetry ingest** on `flowmes/{TENANT}/{SITE}/machines` (prefix
   configurable with `MQTT_TOPIC_PREFIX`), routed by topic (never by payload),
   fail-closed on anything ambiguous, writing `last_seen_at` and — where your profile
   names the source — `operating_hours` to the linked installation, monotonically.
7. **Commissioning checks, warranty state and an hours-based service clock**, each
   reporting its own evidence and each saying "unknown" or "not configured" rather
   than guessing.
8. **A factory-side Connected Equipment screen** showing which machines came from
   which manufacturer, exactly what that manufacturer can see, and the controls to
   change or withdraw it — plus link and release, release being the only way a
   machine leaves a site.
9. **The full journey without a developer**, subject to the two named setup
   exceptions in criterion 8.
10. **Honest empty states.** "Not shared", "no data" and a value are three different
    answers and AMP renders them as three different answers.

## What we do not commit to

Everything here is **NOT BUILT**, or built only as far as stated, in the pilot
release. None of it is on the pilot critical path; all of it is on somebody's roadmap
conversation, and none of it should appear in a pitch deck as if it shipped.

1. **No edge device programme.** No device certificates, no provisioning flow, no
   store-and-forward buffering, no OTA updates. A message published while the link is
   down is lost. The gateway is the OEM's or the customer's responsibility.
2. **No PLC drivers.** The six protocol rows in `backend/industrial_adapters.py`
   (OPC UA, Modbus TCP, Siemens S7, Allen-Bradley, Beckhoff ADS, Omron FINS) are demo
   devices served by `SimulatorAdapter`, which generates values with `random`.
   `industrial_iot_routes.py` is CRUD over the resulting signal table, and
   `GET /industrial/protocols` returns a static description of those six protocols.
   **AMP opens no connection to any PLC.** MQTT is the only live ingest path.
3. **Almost no notification to the OEM, and none outside the portal.**
   `GET /oem/notifications` exists and is scoped to the manufacturer — but exactly
   **one** event writes to it today: a factory accepting a claim
   (`installation_accepted`). Installation, commissioning and service events raise a
   notification in the **factory's** workspace only. Nothing emails, SMSs, pages or
   webhooks a manufacturer about anything, ever. If a machine goes down at 02:00,
   nobody is woken up and nothing is queued for the morning either.
4. **No metering, billing or per-manufacturer quota.** Nothing counts machines,
   messages or requests per manufacturer, and nothing invoices. The only throttle
   that touches `/oem` is a per-IP sliding window on `/oem/login` and
   `/oem/change-password` (default 10 per minute, `http_security.RATE_LIMITS`) —
   a brute-force guard on the front door, not a commercial limit. No other `/oem`
   route is rate limited at all. Who is on the platform stays a decision, not a
   signup.
5. **No predictive maintenance.** The service queue is arithmetic over reported facts
   and labels itself so: `confidence` is `null` on every rule, and appears only on a
   straight-line hours projection from ≥ 3 samples, capped at 0.85 — which the
   fleet-wide queue does not currently produce. Do not market it as prediction.
6. **AMP does not render QR images.** It supplies `claim_url` and shows it as text;
   printing and sticking the label is the manufacturer's job.
7. **A machine's site can only be set by MQTT ingest.** `POST /machines` has no
   `site` field (`schemas.MachineBase`), and `POST /machines/import-csv` does not
   read one either. The site is taken from the topic when MQTT auto-creates a
   machine, and nowhere else. Because the site is an MQTT topic segment, **a site
   containing a space cannot be addressed at all** — and because identity is
   `(tenant, site, name)`, a mismatch between a pre-created machine and the topic
   silently creates a second machine instead of updating the first.
8. **Five of the seven sharing grants release no data yet.**
   `SHARE_SERVICE_STATUS`, `SHARE_ALARMS`, `SHARE_TELEMETRY`,
   `SHARE_MAINTENANCE_HISTORY` and `SHARE_DOWNTIME` are real, auditable, revocable
   consent with no field behind them in the shipped fleet read model. Only
   `SHARE_OPERATING_HOURS` and `SHARE_MACHINE_HEALTH` currently gate a value.
9. **No alarm handling.** No alarm entity, no alarm ingest, no alarm view, no
   escalation. An alarm tag in a profile is interpreted as a reading and nothing more.
10. **No model-creation route.** The catalogue row and telemetry profile are written
    by AMP engineering before the pilot. This is the largest remaining gap between
    "pilot" and "product".
11. **Service intervals are hours-only.** Calendar-based and cycle-based intervals are
    not implemented, and the due/due-soon thresholds are fixed in code rather than
    configured per OEM.
12. **No ingest-gap or message-count report, and no telemetry history view.**
    Criterion 3 is measured from the ingest log and by sampling `last_seen_at`, by
    hand. Out-of-range and unmapped-tag findings are log lines, not a screen. The
    portal's own connected/offline badge only flips after 48 hours.
13. **No Playwright coverage of the OEM portal or the claim flow.** The repository's
    browser suite covers the factory app (auth, dashboard, cockpit, role gating,
    accessibility). The OEM portal and claim flow are unit- and mutation-tested
    (`frontend/mutate-oem-ui.mjs`, 28 mutations); neither has been driven in a real
    browser end to end.
14. **Multi-replica deployment is unreviewed.** The deploy contract assumes a single
    replica: the login rate limiter keeps its window in process, so N replicas means
    N times the configured limit, and each replica runs its own MQTT subscriber.
15. **No production OEM data exists yet.** Every performance figure AMP quotes was
    measured against seeded fixtures on local PostgreSQL. The query counts are
    constant from 10 to 10,000 machines and would stay constant; the latency numbers
    would not.
16. **No third party has attacked this boundary.** Both audits are ours, and a test
    author attacking their own design shares its blind spots. An external review
    before a second OEM would be worth more than another hundred of our own checks.

---

## Reference — files that decide the answers above

| Concern | File |
|---|---|
| MQTT ingest, installation reporting | `backend/mqtt_service.py` |
| Topic routing, tenant/site identity | `backend/mqtt_identity.py` |
| Telemetry profiles, range flagging | `backend/oem_telemetry.py` |
| Sharing grants, fleet row, default-deny | `backend/oem_sharing.py` |
| Commissioning, warranty, service clock | `backend/oem_service.py` |
| OEM roles, capabilities, the sentinel tenant | `backend/oem_auth.py` |
| OEM portal API | `backend/oem_routes.py` |
| Founder-side OEM onboarding | `backend/oem_admin_routes.py` |
| Factory Connected Equipment, claim accept, link, release | `backend/connected_equipment_routes.py` |
| Claim codes, one-time acceptance | `backend/oem_claims.py` |
| Who gets notified, and in whose workspace | `backend/oem_subscribers.py` |
| Login throttles | `backend/http_security.py` |
| Protocol adapters (simulated) | `backend/industrial_adapters.py`, `backend/industrial_iot_routes.py` |
| Whole-journey harness | `backend/audit_oem_pilot_journey.py` |
| Cross-tenant / cross-OEM attack matrix | `backend/audit_oem_adversarial.py`, `backend/audit_oem_specialist.py` |
| MQTT → installation write, pinned | `backend/test_mqtt_installation_reporting.py` |
| Claim race under PostgreSQL | `backend/verify_pg_claim.py` |
| Fleet scale / query counts | `backend/oem_perf.py` |
| OEM portal UI | `frontend/app/oem/page.tsx`, `frontend/components/OemMachineRegistry.tsx` |
| Factory consent UI | `frontend/components/ConnectedEquipment.tsx` |
| Claim landing page | `frontend/app/claim/[code]/page.tsx` |
| Decision records | `docs/adr/0017-oem-fleet-and-cross-tenant-equipment.md`, `docs/adr/0019-factory-controlled-machine-claim.md`, `docs/adr/0011-machine-identity-and-tenant-aware-ingest.md` |
| Readiness verdicts | `docs/engineering/OEM-PLATFORM-READINESS.md` |
