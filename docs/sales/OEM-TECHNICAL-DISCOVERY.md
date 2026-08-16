# OEM technical discovery

The questionnaire to work through with a machine manufacturer's engineering
contact once they have said "yes, show us how our machines would connect".

**One session, about 45–60 minutes, with their controls engineer and their
service manager in the room.** Sales language does not survive this document —
every question here has an answer that either lands in a specific AMP field or
does not exist yet, and both outcomes are said out loud.

**How to use it**

- Print or copy per manufacturer. Fill the `Answer:` lines during the call.
- Every section ends with **Feeds** — where that answer goes in AMP. If a line
  says *no field in AMP today*, capture the answer anyway (it scopes the build)
  but **do not promise it works**.
- Anything marked **ENGINEERING** is not built. Quote it as work, never as a
  feature.

**Verified against** the AMP repo at `master` @ `7a1333c`. File and symbol
references are real; if code moves, re-verify before reusing this document.

**Who answered**

```
Manufacturer:            ____________________________________
Machine family / model:  ____________________________________
Engineering contact:     ____________________________  Role: ____________
Service contact:         ____________________________  Role: ____________
Date:                    ____________   Ash present:  ____________
```

---

## 1. CONTROLLER

*Why we ask: the controller decides what is even possible. Everything downstream
— which tags exist, which protocol can carry them, whether a gateway can sit on
the panel — is a consequence of the box in the machine.*

- [ ] Which PLC / controller manufacturer and family is in this machine?
      `Answer: ______________________________________________________`
- [ ] Exact controller model / order number (e.g. S7-1200 CPU 1214C, CompactLogix 5380, TwinCAT on IPC)?
      `Answer: ______________________________________________________`
- [ ] Firmware version shipped today, and the oldest firmware still in the field?
      `Answer: ______________________________________________________`
- [ ] Is the controller the same across every unit of this model, or does it vary by build year / option / region?
      `Answer: ______________________________________________________`
- [ ] Is there an HMI or on-board IPC that could host a gateway process, or does a gateway have to be added to the panel?
      `Answer: ______________________________________________________`
- [ ] Is there a spare Ethernet port on the controller or panel switch?
      `Answer: ______________________________________________________`
- [ ] Who is allowed to change the controller program — you, your integrator, or the customer? What does that cost you per unit?
      `Answer: ______________________________________________________`
- [ ] Does the controller expose the machine's serial number on the wire, so a gateway can label packets without hand-configuration?
      `Answer: ______________________________________________________`

**Feeds**

| Answer | AMP artefact |
|---|---|
| Per-unit firmware version | `MachineInstallation.firmware_version` — set at registration (`POST /oem/machines`, `MachineRegistration.firmware_version`); shown to the customer in the claim preview. Nothing computes on it, and **no route updates it afterwards** — a field firmware update cannot be recorded today. |
| Controller make / model / family | **No dedicated field.** Free text in `MachineModel.description` or `MachineInstallation.notes` (both `Text`). |
| "The controller varies by build year" | Decides how many catalogue rows you need: `MachineModel` is `UNIQUE(oem_code, model_code)`, and the telemetry profile hangs off the **model**, so two controller variants with different tag names are two models. |
| Serial available on the wire | Decides whether the gateway can be identical on every unit or needs per-unit commissioning — AMP identifies a machine by `(tenant, site, machine name)` on the topic/payload, not by a serial. |

---

## 2. PROTOCOL

*Why we ask: this is the single question that decides whether the machine can be
connected in a week or needs a driver written. Get it exactly right, and never
guess on the customer's behalf.*

### WHAT AMP CAN INGEST TODAY — read this before answering

```
+---------------------------------------------------------------------------+
| INGESTED TODAY (live, in the product, on master)                          |
|                                                                           |
|   MQTT. That is the whole list.                                           |
|                                                                           |
|   backend/mqtt_service.py subscribes to  {prefix}/+/+/machines            |
|   prefix = env MQTT_TOPIC_PREFIX, default "flowmes", so the real topic is |
|                                                                           |
|        flowmes/{TENANT}/{SITE}/machines                                   |
|                                                                           |
|   (a machine with no site uses the literal token "-"). JSON payload:      |
|                                                                           |
|        {"machine": "CNC-01", "status": "Running", "utilization": 78,      |
|         "downtime": "0 min",                                              |
|         "total_count": .., "good_count": .., "rejected_count": ..,        |
|         "readings": {"<your tag>": <value>, ...}}                         |
|                                                                           |
| ALSO ACCEPTED — authenticated HTTPS, NOT a protocol client                |
|                                                                           |
|   POST /iot/telemetry  and  POST /industrial/signals  accept one signal   |
|   at a time with a FACTORY JWT (Admin/Supervisor). Useful for a gateway   |
|   you write: they can update that machine's status, utilisation and       |
|   downtime on the factory's own row. They do NOT feed the OEM             |
|   installation record, the service clock, or the telemetry profile —      |
|   those are MQTT-only paths.                                              |
|                                                                           |
| REQUIRES ENGINEERING — NOT BUILT, NOT SHIPPED, DO NOT IMPLY               |
|                                                                           |
|   OPC UA . Modbus TCP . Modbus RTU . EtherNet/IP . PROFINET . PROFIBUS    |
|   CAN / CANopen / J1939 . Siemens S7 . Beckhoff ADS . Omron FINS          |
|   BACnet . any proprietary/serial protocol.                               |
|   AMP opens no connection to a PLC in any protocol.                       |
|                                                                           |
| ABOUT THE "SIX PROTOCOLS" YOU MAY SEE IN THE PRODUCT                      |
|                                                                           |
|   backend/industrial_adapters.py seeds six IndustrialDevice demo rows     |
|   labelled OPC UA / Modbus TCP / Siemens S7 / Allen-Bradley /             |
|   Beckhoff ADS / Omron FINS. get_adapter() returns SimulatorAdapter for   |
|   EVERY one of them, and SimulatorAdapter.read() returns random.randint() |
|   values from a per-protocol template, written on the demo simulator      |
|   loop. No socket is opened. None of asyncua / pymodbus / python-snap7 /  |
|   pycomm3 / pyads is imported or present in requirements.txt.             |
|   GET /industrial/protocols returns that same hard-coded table — it is a  |
|   catalogue of what a future edge agent WOULD use, not a driver list.     |
|   Those rows demonstrate the connectivity screen; they are not drivers.   |
|                                                                           |
| THEREFORE: something on site must speak the machine's protocol and        |
| publish MQTT. Today that is the OEM's own gateway, the customer's         |
| existing gateway, or an off-the-shelf one (eWON/HMS, Kepware, Ignition,   |
| Node-RED). AMP has NO edge programme: no device certificates, no          |
| provisioning, no store-and-forward, no OTA.                               |
+---------------------------------------------------------------------------+
```

- [ ] Which of these does the machine speak **natively, today**: OPC UA / Modbus TCP / Modbus RTU / MQTT / EtherNet/IP / PROFINET / CAN / proprietary / none?
      `Answer: ______________________________________________________`
- [ ] Which of those is standard on every unit, and which is a paid option?
      `Answer: ______________________________________________________`
- [ ] If **OPC UA**: server on the controller or a separate box? Endpoint URL, security policy, user/certificate auth? Is a node-id export available?
      `Answer: ______________________________________________________`
- [ ] If **Modbus**: TCP or RTU? Unit/slave id, baud/parity if serial, word and byte order, and is there a published register map?
      `Answer: ______________________________________________________`
- [ ] If **MQTT already**: which broker, TLS or not, credentials model, topic scheme, payload format? Can the topic and payload be changed to AMP's, or is it fixed?
      `Answer: ______________________________________________________`
- [ ] Do you already ship a gateway / IoT box with the machine? Whose? Who owns the SIM or connectivity contract?
      `Answer: ______________________________________________________`
- [ ] Do you already have a cloud portal? Does it expose an outbound feed (MQTT bridge, webhook, API) we could take instead of touching the PLC?
      `Answer: ______________________________________________________`
- [ ] Who owns the bridge to MQTT — you, us, or the customer's integrator? Who pays for it?
      `Answer: ______________________________________________________`
- [ ] Can the gateway be configured **per customer** at build or commissioning time? (The customer code and site are segments of the MQTT topic, so each unit's gateway must be told where it is shipping.)
      `Answer: ______________________________________________________`

**Feeds**

| Answer | AMP artefact |
|---|---|
| Native protocol | **No field, and no driver.** It determines the size of the bridge work. (`IndustrialDevice.protocol` is factory-side free text feeding the simulated connectivity screen — never quote it as support.) |
| Broker host / port | Deployment env, not data: `MQTT_BROKER`, `MQTT_PORT` in `backend/mqtt_service.py`. |
| Topic scheme | `MQTT_TOPIC_PREFIX` (default `flowmes`) + `{tenant}/{site}/machines`. Tenant and site come **from the topic, never the payload** (`mqtt_identity.parse_topic`) because the broker's ACL is the only thing that can enforce them. (A legacy single-tenant topic `{prefix}/machines` is still accepted, but only when `MQTT_LEGACY_TENANT` is set — otherwise those messages are dropped rather than given an owner.) |
| Payload format | Must match `mqtt_service.on_message`. If the payload restates `tenant`/`site`, it must **agree** with the topic or the message is dropped (`mqtt_identity.check_payload_agrees`). The production record also reads `planned_minutes`, `runtime_minutes` and `ideal_cycle_time_seconds`; if your gateway omits them AMP applies its own defaults (480 / 0 / 60), so send them if the OEE figure matters. |
| Customer / site naming | Topic segments must match `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`, and the `machine` name in the payload is held to the same charset. **Spaces are illegal** — a site called `Plant 1` cannot be addressed at all; agree `PLANT1` or `Plant-1` on this call. |
| Go-live order | The customer tenant must exist in AMP **before** the gateway is pointed at the broker — `mqtt_service.tenant_is_provisioned` drops messages for an unknown tenant with a logged reason rather than inventing a workspace. |

---

## 3. TELEMETRY

*Why we ask: this is the actual product. A tag list with units and plausible
ranges is what turns "connected" into a service clock, a fleet view, and a
reason to travel.*

- [ ] Can you send a tag list export (name, address/node-id/register, datatype, unit, scaling)?
      `Answer: ______________________________________________________`
- [ ] For each tag, what is the engineering unit **as it arrives on the wire**? (AMP reports units, it never converts them.)
      `Answer: ______________________________________________________`
- [ ] For each numeric tag, what is the plausible min/max band a healthy machine stays inside?
      `Answer: ______________________________________________________`
- [ ] **Which single tag is the operating-hour counter?** Unit (h / min / s), resolution, and does it ever reset (controller swap, board replacement)?
      `Answer: ______________________________________________________`
- [ ] Do you distinguish running hours from loaded/working hours? Which one governs service?
      `Answer: ______________________________________________________`
- [ ] Which tag(s) decide "the machine is running"? What are the machine's own state names, and what do they map to?
      `Answer: ______________________________________________________`
- [ ] Where do alarms come from — a code register, a bit array, a string, an alarm buffer? (See §7.)
      `Answer: ______________________________________________________`
- [ ] Update frequency: per tag, on-change or on-interval? What interval? Any burst behaviour at start/stop?
      `Answer: ______________________________________________________`
- [ ] Counters: cumulative for life, or per shift/batch? Do they roll over, and at what value?
      `Answer: ______________________________________________________`
- [ ] Does the machine report production counts (total / good / rejected) and a cycle time?
      `Answer: ______________________________________________________`

**Feeds**

| Answer | AMP artefact |
|---|---|
| One tag | One entry in `MachineModel.telemetry_profile` (JSON list on the model row) — `{name, source, datatype, unit, min, max, aggregation, state_signal, description}`. `source` is **your** tag as it arrives; `name` is AMP's canonical name. See `backend/oem_telemetry.py`. |
| Datatype | `signal.datatype` ∈ `number` \| `bool` \| `string`. Anything else makes the **whole profile** unusable (`ProfileError`) — deliberately, so a broken config never looks like a silent machine. |
| Engineering unit | `signal.unit`. Reported, **never converted**. |
| Min / max | `signal.min` / `signal.max`. Out-of-range is **flagged and logged, never clamped** (an overheating machine must not be rendered healthy). |
| Aggregation | `signal.aggregation` ∈ `last\|sum\|max\|min\|avg`. Parsed and stored; **nothing in the product collapses a period with it yet** — capture it, don't demo it. |
| Operating-hour source | The signal whose canonical `name` is exactly `operating_hours` → written to `MachineInstallation.operating_hours` by `mqtt_service._record_installation_report`. **Monotonic**: a lower reading is logged and rejected, so a controller swap cannot silently cancel a due service. Written **only** when the installation is linked to that factory's machine. |
| Machine states | Payload `status` → normalised by `machine_status.normalize_machine_status` to one of **Running, Idle, Breakdown, Maintenance, Offline** (case-insensitive). An unrecognised state leaves the previous value untouched. A change writes a `MachineEvent`; a transition **into** Breakdown writes one `DowntimeLog`. |
| `state_signal: true` in the profile | Stored and returned by `oem_telemetry.state_signals`, but **no code derives machine state from it today** — state still comes from payload `status`. Map your states to the five above on this call. |
| Production counts | A `ProductionRecord` is written only when `total_count > 0` **and** `good + rejected == total`; negative / non-finite values are dropped rather than recorded. |
| Update frequency | **No field.** It determines how fresh `MachineInstallation.last_seen_at` is; the "not reporting" recommendation fires after **≥ 2 days** of silence (`oem_service.recommendations`) — and only for a machine that has reported at least once. One that has **never** reported raises no connectivity recommendation at all. |
| Everything else in `readings` | **Interpreted and then discarded.** Only `operating_hours` is persisted. Unknown tags and out-of-range values are written to the log as warnings, not to a table. There is no per-signal OEM telemetry history — **ENGINEERING** if the manufacturer needs trends. |
| Who writes the profile | **AMP does, directly in the database.** There is no API at all — OEM-facing or factory-facing — to create a `MachineModel` or edit `telemetry_profile`; today it is a seed/migration write. (The `manage_models` capability exists in `oem_auth.ROLE_CAPABILITIES` with no route behind it.) The OEM reads it back at `GET /oem/models/{id}/telemetry`. |

---

## 4. CONNECTIVITY

*Why we ask: most integrations die on the customer's firewall, not on the PLC. We
need to know the path out of the plant before we quote a date.*

- [ ] What does the machine have on board: Ethernet, Wi-Fi, cellular modem, none?
      `Answer: ______________________________________________________`
- [ ] Does the machine sit on the customer's LAN, or in an isolated machine cell behind your own switch/router?
      `Answer: ______________________________________________________`
- [ ] Does the customer normally provide internet to the machine? Who pays — you, them, or a SIM you ship?
      `Answer: ______________________________________________________`
- [ ] Is **outbound** TCP 1883 / 8883 typically permitted? To an arbitrary host, or only to an allow-listed one?
      `Answer: ______________________________________________________`
- [ ] Is there a proxy? Does it break non-HTTP traffic?
      `Answer: ______________________________________________________`
- [ ] Static or DHCP addressing on site? Who assigns it? Is there a VLAN for OT?
      `Answer: ______________________________________________________`
- [ ] What is the customer's firewall-change process, and who signs it? How long does it typically take?
      `Answer: ______________________________________________________`
- [ ] Is any inbound access ever expected (your remote support tooling)? What do you use today?
      `Answer: ______________________________________________________`

**Feeds**

| Answer | AMP artefact |
|---|---|
| Outbound-only requirement | Non-negotiable: telemetry reaches AMP by the gateway **publishing outbound** to the broker. AMP never dials in to the machine — no inbound path, no remote access, no OTA. |
| Broker endpoint | `MQTT_BROKER` / `MQTT_PORT`. Note honestly: AMP's own client calls `client.connect(host, port, 60)` with **no TLS and no username/password configured in code** — transport security and client authentication are the broker's job in the current design, and **per-tenant topic ACLs on the broker are what make the tenant segment trustworthy** (`mqtt_identity` module docstring). Agree the broker's security posture explicitly with the customer's IT. |
| Site / plant naming | Becomes the `{SITE}` topic segment, charset-restricted as in §2 (no spaces). `Machine.site` is set from the **topic** the first time a machine reports (`mqtt_service.get_or_create_machine`); `POST /machines` has no `site` field and the CSV import does not set one, so the topic is effectively how a site gets onto the record. |
| Cellular / no customer LAN | Simplifies the firewall conversation and is usually the fastest pilot path. Still MQTT out; still no store-and-forward if the link drops — **gaps are gaps**. |

---

## 5. COMMISSIONING

*Why we ask: whatever you already do on site is where the AMP steps have to fit.
We are adding four checks to an existing procedure, not inventing one.*

- [ ] What is your commissioning procedure today? Is there a document/checklist we can see?
      `Answer: ______________________________________________________`
- [ ] Which measurements are taken at commissioning (pressures, temperatures, currents, alignment, run-in)?
      `Answer: ______________________________________________________`
- [ ] What are the acceptance criteria, and who signs off — your engineer, a dealer, or the customer?
      `Answer: ______________________________________________________`
- [ ] How do you prove connectivity today, before you leave site?
      `Answer: ______________________________________________________`
- [ ] How long is a commissioning visit, and who is physically present?
      `Answer: ______________________________________________________`
- [ ] Should the customer be notified in AMP when you commission a machine?
      `Answer: ______________________________________________________`
- [ ] Which of your people need AMP logins, and what should each be allowed to do?
      `Answer: ______________________________________________________`

**Feeds**

| Answer | AMP artefact |
|---|---|
| The four checks AMP runs | `oem_service.COMMISSIONING_CHECKS`: **assigned_to_customer** (installation names a customer/site), **linked_to_machine** (serial linked to a machine in that factory), **telemetry_profile** (the model defines what it reports), **has_reported** (`MachineInstallation.last_seen_at` is set). |
| "The machine has reported" | `last_seen_at`, written by MQTT ingest **only for a linked installation** — so linking must happen before this check can pass. |
| Linking | Factory Admin action: `POST /connected-equipment/{installation_id}/link`. **The factory does this, never the OEM** — only the customer knows which asset on their floor carries your serial. The site is copied from the machine. One machine can carry only one installation (a second attempt is a 409). |
| Assignment to a customer | The OEM issues a one-time claim code (`POST /oem/machines/{id}/claim` → `claim_url = APP_BASE_URL/claim/{code}`, default base `https://app.marx8.com`); the **factory accepts** it (`POST /connected-equipment/claim/{code}`, Admin only). That acceptance is the only thing in AMP that sets `MachineInstallation.factory_tenant_code`. Only a SHA-256 of the code is stored — it is shown once. Codes expire (default 30 days, max 365) and only one may be live per machine. **AMP supplies the URL; it does not render a QR image** — your label printer does. |
| Getting from accepted to commissioned | Acceptance leaves the installation at **Assigned**. Somebody with `manage_installations` must then transition it **Assigned → Installed** (`POST /oem/machines/{id}/transition`) before commissioning is legal — the lifecycle refuses to jump. Worth knowing when you decide who gets which login: a service *engineer* can commission but cannot make that transition. |
| Commissioning itself | `POST /oem/machines/{installation_id}/commission` — moves the row into `Commissioning` if it is not already there, then to `Active`. The report is **advice, not a gate**: a machine can go Active with a check outstanding, and the `MachineCommissioned` event carries `checks_passed` so the history cannot later imply it was clean. |
| Notifying the customer | An **in-app notification row** is written into the customer's own workspace when a machine is claimed, installed, commissioned or serviced — raised as a **Warning** when commissioning checks failed. That is the whole notification story: AMP sends **no email, no SMS and no push**, and the OEM portal is pull-only (`GET /oem/notifications`). |
| Your measurements / acceptance criteria | **No structured field.** Free text in `MachineInstallation.notes`. A commissioning sheet with typed measurements is **ENGINEERING**. |
| Responsible engineer | `OemUser.role` → `oem_auth.ROLE_CAPABILITIES` (the role strings are literally uppercase): `OEM_ADMIN` (everything), `OEM_SERVICE_MANAGER` (`read_fleet`, `manage_service`, `manage_installations` — **cannot commission**), `OEM_SERVICE_ENGINEER` (`read_fleet`, `manage_service`, `commission` — **cannot register machines or issue claim codes**), `OEM_VIEWER` (read only). |
| Who did what | Uneven, and worth saying plainly. Registering a machine, issuing or revoking a claim, creating or changing a user, and signing in are written to the **audit log with the acting username**. Commissioning, lifecycle transitions and recorded services are written to the **append-only event log** with the OEM code, serial and site — but **not** the individual who did it. Factory-side actions (accepting a claim, linking, releasing, changing sharing) are audited with the acting username. If per-engineer attribution on a commissioning matters to them, that is **ENGINEERING**. |

---

## 6. SERVICE

*Why we ask: the service clock is the first thing that pays for itself. It only
works if the interval, the counter, and the reset all come from you.*

- [ ] What are the scheduled service intervals — by operating hours? Give the numbers.
      `Answer: ______________________________________________________`
- [ ] Are there **calendar** intervals as well (every 6 months regardless of hours)? Which governs when they disagree?
      `Answer: ______________________________________________________`
- [ ] Are there count-based intervals (cycles, parts, batches, litres)?
      `Answer: ______________________________________________________`
- [ ] Does the interval change with duty cycle, environment, or product?
      `Answer: ______________________________________________________`
- [ ] Are there service **levels** (A/B/C, minor/major)? What resets which interval?
      `Answer: ______________________________________________________`
- [ ] What exactly resets an interval in your process — the visit, the parts change, or a counter reset in the controller?
      `Answer: ______________________________________________________`
- [ ] Which parts are normally replaced at each interval? Part numbers, typical price, lead time?
      `Answer: ______________________________________________________`
- [ ] Do you sell service contracts today? What do they promise (response time, uptime, included parts)?
      `Answer: ______________________________________________________`

**Feeds**

| Answer | AMP artefact |
|---|---|
| Hours-based interval | `MachineModel.service_interval_hours` (integer, per model). **NULL is honest**: `oem_service.service_state` returns `not_configured` rather than inventing a schedule. |
| Hours reading | `MachineInstallation.operating_hours` (from §3 telemetry). NULL → state `unknown` ("never reported operating hours"), which is not the same as zero. A counter reading **below** the last recorded service also returns `unknown`, with "the counter was probably reset or the controller replaced" as the stated reason — it never reports a negative interval as if it were fresh. |
| What resets an interval | `POST /oem/machines/{id}/service` writes `last_service_hours` + `last_service_at` (`manage_service` — admin, service manager or service engineer). **This is the only reset**, and it is what makes `overdue` reachable at all — position is hours *since the last recorded service*, never `hours % interval`. |
| Due thresholds | Derived, **not configurable**: `overdue` at ≤ 0 h remaining, `due` within 5% of the interval, `due_soon` within 15%, else `ok`. |
| Service levels (A/B/C) | **Not modelled.** One interval per model, one reset. **ENGINEERING.** |
| Calendar intervals | **Not modelled. ENGINEERING.** There is no months/days service field anywhere; only *warranty* uses dates. |
| Count/cycle intervals | **Not modelled. ENGINEERING.** |
| Loaded vs running hours | Only **one** hours figure is consumed (`operating_hours`). A `loaded_hours` signal can be declared in the profile, but nothing reads it — note that the sharing grant is *labelled* "Operating and loaded hours", which is vocabulary, not a second figure. |
| Parts normally replaced | **No parts list on a model, and no link from an installation to inventory.** The factory-side inventory module exists but is not connected to OEM service. **ENGINEERING.** |
| The service queue | `GET /oem/service` — arithmetic over reported facts, and it says so. It is **Service Intelligence, never predictive maintenance**; no model is trained, nothing is learned, and there is no AI or ML anywhere in this path. Each item carries reason, evidence, action, and a `confidence` that is `null` for arithmetic. The one straight-line projection in the code needs ≥ 3 hours samples over ≥ 1 day, **nothing stores that history and the fleet queue passes no history in**, so it cannot fire today — do not demo it. |

---

## 7. ALARMS

*Why we ask: alarms are the fastest route to "we knew before the customer
called". Nothing in AMP stores them yet, so this section is scoping, not setup —
say that plainly.*

- [ ] Is there a published alarm code list (code, text, severity)? Can we have it?
      `Answer: ______________________________________________________`
- [ ] How does an alarm arrive on the wire — a code register, a bit array, a string, a buffered alarm list?
      `Answer: ______________________________________________________`
- [ ] Can several alarms be active at once? Is there a priority order?
      `Answer: ______________________________________________________`
- [ ] Is there an acknowledgement state on the machine, and is it readable?
      `Answer: ______________________________________________________`
- [ ] What resets an alarm — auto-clear on condition, operator acknowledge, power cycle, or a service visit?
      `Answer: ______________________________________________________`
- [ ] Which alarms should reach your service desk, and which are operator-only noise?
      `Answer: ______________________________________________________`
- [ ] Are any alarm codes commercially sensitive (i.e. the customer should not be shown the raw code)?
      `Answer: ______________________________________________________`

**Feeds — be exact about what does not exist**

| Answer | AMP artefact |
|---|---|
| Alarm codes, severity, ack, reset | **There is no alarm storage in AMP.** No alarm table in `models.py`; MQTT ingest reads no alarm field; nothing raises, acknowledges, or clears an equipment alarm. Collect the catalogue now, build later. **ENGINEERING.** |
| `SHARE_ALARMS` grant | The grant key exists in `oem_sharing.ALL_GRANTS` and a factory can tick it ("Equipment alarm codes raised by this machine"), but **no code reads it**. The same is true today of `SHARE_TELEMETRY`, `SHARE_MAINTENANCE_HISTORY` and `SHARE_DOWNTIME` — four of the seven. Never demo a ticked box as a working feed. **`SHARE_SERVICE_STATUS` moved off this list on 2026-08-16**: it now gates the service verdict in `GET /oem/service` and the per-machine service view, and gates whether a manufacturer may record a service against an hours reading it supplies itself. Until then it gated nothing while the service views disclosed the customer's hour meter regardless of consent — see `docs/engineering/OEM-PLATFORM-READINESS.md` §6. Three of seven are enforced: `SHARE_MACHINE_HEALTH`, `SHARE_OPERATING_HOURS`, `SHARE_SERVICE_STATUS`. |
| An alarm tag in the profile | Can be declared (`datatype: "string"`, as in `oem_telemetry.EXAMPLE_COMPRESSOR`'s `alarm_code`). It is parsed on arrival and then **discarded** — not stored, not surfaced. |
| The nearest thing that works today | A machine reporting `status: "Breakdown"` flips the factory's machine row, writes a `MachineEvent` and one `DowntimeLog` per breakdown event. That is factory-side data, visible to the OEM only via `SHARE_MACHINE_HEALTH` (status/utilisation), and it is not an alarm code. |

---

## 8. WARRANTY

*Why we ask: warranty is where telemetry becomes commercially load-bearing —
and where an overstated capability becomes a dispute. We record dates; we do not
adjudicate.*

- [ ] What **starts** warranty — dispatch, delivery, installation, commissioning, or first run?
      `Answer: ______________________________________________________`
- [ ] Standard duration? Extended options you sell?
      `Answer: ______________________________________________________`
- [ ] Is there an operating-hour limit as well (e.g. 24 months **or** 4,000 h, whichever first)?
      `Answer: ______________________________________________________`
- [ ] Which exclusions are telemetry-relevant — over-temperature, over-pressure, over-speed, missed service, wrong consumable?
      `Answer: ______________________________________________________`
- [ ] Would you want a flag when telemetry shows an exclusion condition, and would you show it to the customer?
      `Answer: ______________________________________________________`
- [ ] Who adjudicates a claim today, and what evidence do they ask for?
      `Answer: ______________________________________________________`

**Feeds**

| Answer | AMP artefact |
|---|---|
| Warranty dates | `MachineInstallation.warranty_start` / `warranty_end` (dates), set by the OEM at registration (`POST /oem/machines`). These two dates are the **only** input to `oem_service.warranty_state` → `active` / `not_started` / `expired` / `unknown`. **There is no route to change them afterwards** — a mistyped date has no correction path today except a database write. |
| No end date recorded | State is **`unknown`**, never "expired" and never "active" — AMP refuses to decide a commercial question on the customer's behalf, and raises a `warranty_unknown` recommendation asking you to record the dates. |
| Standard duration | `MachineModel.warranty_months` exists and is returned by `GET /oem/models` — but **nothing computes with it**. `warranty_end` is not derived from it. If you want that, it is **ENGINEERING** (small). |
| Start **event** | **Not modelled.** Nothing sets `warranty_start` on commissioning or first report; it is whatever was typed at registration. |
| Operating-hour warranty limits | **Not modelled. ENGINEERING.** `warranty_state` has no hours term. |
| Telemetry-based exclusions | **Not supported as evidence.** Out-of-range readings are logged as warnings and not stored, so they cannot substantiate a claim today. **ENGINEERING.** |
| Visibility to the customer | Before accepting, the claim preview shows the OEM's own record only: manufacturer and support email, serial, model code/name/family, firmware version, warranty **state**, and when the code expires. After acceptance the customer's Connected Equipment screen adds lifecycle status, site, commissioning date and service position. In the other direction, the fields an OEM sees with **no** sharing grant at all (`oem_sharing` "always visible") are serial, model, customer, site, lifecycle status, installed/commissioned dates and warranty dates — because they come from the OEM's own records. |

---

## 9. SITE

*Why we ask: the customer's IT and legal constraints decide whether any of this
can be switched on, and they are usually discovered too late.*

- [ ] Do your customers generally permit outbound cloud connectivity from machines? Which ones do not?
      `Answer: ______________________________________________________`
- [ ] Are there hosting/region constraints (EU only, in-country data residency)?
      `Answer: ______________________________________________________`
- [ ] Do customers send you a cybersecurity questionnaire? IEC 62443, NIS2, ISO 27001, customer-specific?
      `Answer: ______________________________________________________`
- [ ] Do any customers require a fully offline / air-gapped installation? What share of your base?
      `Answer: ______________________________________________________`
- [ ] If the link drops for a day, must the missed data arrive later, or is a gap acceptable?
      `Answer: ______________________________________________________`
- [ ] How long must this data be retained, and by whom?
      `Answer: ______________________________________________________`
- [ ] In your contract with the customer, who owns the machine data? Is per-customer consent needed in writing?
      `Answer: ______________________________________________________`
- [ ] Do you need your customers to see exactly what they are sharing with you?
      `Answer: ______________________________________________________`

**Feeds**

| Answer | AMP artefact |
|---|---|
| Outbound cloud permitted | **Required.** There is no offline mode and no store-and-forward: if the link is down, the data for that period does not exist. An air-gapped customer needs a self-hosted deployment (`docs/DOCKER.md`, Postgres) — a deployment answer, not a product feature — or **ENGINEERING**. |
| Consent, per customer | `OemDataSharingPolicy`, keyed `(oem_code, tenant_code)`. The **factory** sets it (`PUT /connected-equipment/sharing`, Admin only, audited); there is no OEM-side equivalent, because a manufacturer that could edit its own permissions has permissions in name only. Granting to an OEM with no equipment on site is refused (404). |
| Withdrawal | Grants are read **at query time** (`oem_sharing.grants_for`), never cached — a withdrawal takes effect on the very next request. |
| Default | **Deny.** No policy row means the OEM sees only the always-visible fields: serial, model, lifecycle status, customer, site, warranty dates, installed/commissioned dates — facts from its own records. |
| "Show the customer what they share" | The Connected Equipment screen lists every grant key with a plain-English label (`oem_sharing.GRANT_LABELS`) — including the ones **not** ticked. Caveat from §7: five of the seven keys are vocabulary only today. |
| Retention | `docs/RETENTION.md`: `iot_telemetry` and `industrial_signals` 14 days, `machine_events` 180 days, `notifications` 90 days, `audit_logs` and `event_log` forever; deletion is dry-run by default, NULL timestamps are never pruned, and the job never deletes on a schedule (weekly dry-run report; deleting is a manual dispatch). Note that the OEM figures (`operating_hours`, `last_seen_at`) are **current state on the installation row**, not a history series. |
| Certification | **We hold none.** No IEC 62443, NIS2, ISO 27001 or SOC 2 certification, and no third-party audit to point at. Answer their questionnaire with the posture below and say so. |
| Security posture we can actually claim | JWT auth with role capabilities, per-tenant query scoping (ADR-0002), an OEM sentinel that keeps manufacturers out of factory tables, hashed one-time claim codes, security headers/CSP and per-IP throttling on the login and AI endpoints, an append-only audit log, PostgreSQL migrations with a schema-readiness deploy gate. |
| What we must **not** claim | Device certificates or device identity, per-OEM usage metering, billing or API quotas, email/SMS/push notifications of any kind (all notification is an in-app row; the OEM portal is pull-only, `GET /oem/notifications`), predictive-maintenance or AI/ML models, OTA updates, TLS/credentials on AMP's own MQTT client (broker-side today), any security certification. |

---

## After the call — what these answers decide

| If the answer is… | Then… |
|---|---|
| The machine already speaks MQTT and the topic is configurable | Fastest possible pilot. Map tags → telemetry profile, agree tenant/site tokens, register serials, issue claim codes. |
| The machine speaks OPC UA / Modbus / EtherNet/IP only | A bridge is needed. Decide on this call **who builds and pays for it** — OEM gateway, customer gateway, or off-the-shelf. AMP ships no driver. |
| No hours counter on the wire | The service clock cannot run. `service_state` returns `unknown`, honestly. Either expose a counter or the pilot is fleet-visibility only. |
| Service is calendar-based, not hours-based | Not modelled. Scope it as engineering before promising a service queue. |
| Alarms are the manufacturer's main interest | Nothing is built. Take the alarm catalogue, quote the work, do not demo a ticked `SHARE_ALARMS` box as a feed. |
| Any customer is air-gapped | Out of scope for the cloud product as it stands. |
| Site names contain spaces | Fix now, on this call: the MQTT topic charset forbids them and the machine would be unaddressable. |

**Three sentences to keep saying, unchanged, in every one of these meetings**

1. "Today AMP ingests MQTT. Everything else needs a bridge, and I will quote it."
2. "The customer grants what we see, they can withdraw it, and it takes effect on the next request."
3. "The service queue is arithmetic over what your machine reported — it is not a prediction, and it says so on the screen."
