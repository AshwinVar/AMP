# PLC pilot readiness

What AMP can connect to a real factory, and — more importantly — what it cannot.

This document exists because "we support OPC UA" is the easiest sentence in
industrial software to write and the hardest to mean. An adapter class is not
support. A passing unit test against a mock is not support. This page says what
has been run against what, and it does not round up.

**Status as of 2026-10-05.** Read the support matrix before promising anything
to a customer.

---

## Support matrix

| Capability | Status | What that actually means |
|---|---|---|
| **OPC UA — read, connect, reconnect, quality, timestamps** | **SIMULATOR VERIFIED** | Driven end to end against a live `asyncua` server over a real socket: session, typed reads, server timestamps, server status codes, a bad node id failing alone, clean disconnect. Never yet run against a physical PLC or a vendor server. |
| **OPC UA — browse** | **SIMULATOR VERIFIED** | Walks the address space and returns readable variables, bounded to 500 nodes / 4 levels. |
| **OPC UA — subscription (data change)** | **SIMULATOR VERIFIED** | A change shorter than the poll interval is delivered. Verified at 50 ms against the test server. |
| **OPC UA — username/password session** | **IMPLEMENTED, NOT VERIFIED** | Wired to the client and to config. Not exercised against a server that enforces it. |
| **OPC UA — certificates, Sign&Encrypt** | **EXPERIMENTAL** | The security string is passed through to `asyncua` untouched. Never run. Do not promise it for a pilot. |
| **Modbus TCP — coils, discrete inputs, input & holding registers** | **SIMULATOR VERIFIED** | Driven end to end against a live `pymodbus` server: all four register types, a 32-bit value across two registers, declared word order, scale/offset, and a device exception response refused rather than read as zero. |
| **Modbus TCP — 4xxxx/3xxxx/1xxxx addressing** | **SIMULATOR VERIFIED** | Documentation-style addresses accepted as written; one that contradicts an explicit `register_type` is refused rather than guessed. |
| **Modbus RTU / serial** | **NOT IMPLEMENTED** | Only TCP. |
| **FANUC FOCAS — session, status record, macro read** | **HARDWARE VERIFIED** | The ONLY row on this page proven against a physical control rather than a simulator. Read from a FANUC Series 0i-MF Plus on a BFW BMV 45+ TC24 (serial PJ-537) on 2026-10-05: system info matched the machine's own plate, and macro #3901 returned 1683 — the number on the operator's PARTS COUNT display at that moment. Via `pyfocas` (MIT), which speaks the wire protocol over an ordinary socket, so **no FANUC `fwlib` licence and no vendor DLL**. |
| **FANUC FOCAS — undefined macro refused** | **HARDWARE VERIFIED** | A macro nobody configured comes back with the control's FF FF sentinel and becomes `no_data`, not zero. Proven on the real control, where the refusal arrives as `DecodingError` — not the `AssertionError` the library's source suggested. |
| **FANUC FOCAS — parameter read (operating hours)** | **NOT IMPLEMENTED** | The HMI's RUN TIME lives in parameters 6751/6752 and `pyfocas` implements four functions, none of them a parameter read. Macro #3002 is a DIFFERENT timer: it read 2128 h against an HMI showing 602H59M. The adapter therefore offers no `operating_hours` at all rather than a confident wrong one; AMP derives runtime from `running` over time. |
| **FANUC FOCAS — browse** | **PARTIAL, BY DESIGN** | The status record is enumerable and is listed. The macro space is 10,000 unnamed numbers with no way to ask which are in use, so it is not — "discovering" it would mean reading all of them against a live CNC. |
| **FANUC FOCAS — writes** | **NEVER** | `pyfocas` can set macro variables, and a macro on a CNC can be a tool offset. Nothing in the adapter calls it. |
| **Tag mapping from configuration** | **VERIFIED** | Addresses, datatypes, scale, offset, boolean maps, enums, units and counter behaviour are all data. No Python is edited to map a pilot's tags. Validation refuses a bad mapping before anything connects. |
| **Canonical signal model** | **VERIFIED** | Both protocols produce identical canonical signals; the layers above the adapters contain no protocol-specific code. |
| **Counter to production arithmetic** | **VERIFIED** | Baseline on first reading, cumulative deltas, declared rollover, declared reset, and a refusal (counting zero) for any backwards step the declared mode does not explain. |
| **Local durable buffer** | **VERIFIED** | SQLite queue survives a restart, is bounded, drops oldest-first while counting the drops, and marks the resumed stream with a gap. |
| **MQTT publish to AMP** | **SIMULATOR VERIFIED** | Run against an in-process MQTT 3.1.1 broker: CONNECT/CONNACK, QoS 1 PUBLISH, PUBACK. A withheld PUBACK correctly leaves the record on disk. |
| **MQTT over TLS to a production broker** | **IMPLEMENTED, NOT VERIFIED** | `tls_set()` is called when `tls: true`. Never run against a real TLS broker. |
| **AMP ingestion onto an existing machine** | **VERIFIED** | Payloads from both protocols were fed to AMP's real `on_message` against a real database and updated the machine the customer had already created. |
| **Machine identity / no duplicates** | **VERIFIED** | A gateway packet adopts a hand-created machine rather than registering a second one; ambiguity is refused and recorded for a human. Mutation-tested (12/12 caught). |
| **Gateway message signing (gateway side)** | **VERIFIED** | HMAC-SHA256 over the whole payload including tenant and site, with a timestamp window and a nonce. Tampering with any field breaks it. |
| **Gateway signature verification (AMP side)** | **VERIFIED** | A credential binds a gateway id to one workspace and one site; AMP verifies the signature to learn who is speaking, then compares the credential against the topic to learn whether they may. A valid gateway re-signing a packet aimed at another customer's topic -- same site name included -- touches nothing. Revocation takes effect on the next packet and does not re-open the workspace. Mutation-tested (ADR-0041, migration 0013). |
| **Production idempotency** | **VERIFIED** | Delivery is at-least-once, so a retried publish used to double a shift's output. `UNIQUE(tenant_code, source_record_id)` makes the retry a no-op; three deliveries of one message write one record. One customer's record id never blocks another's. |
| **Process telemetry on an ordinary machine (temperature, pressure, speed...)** | **NOT IMPLEMENTED** | The gateway maps, scales and publishes it correctly under `readings`, and AMP **drops it**: `mqtt_service` interprets `readings` only for a machine registered as an OEM installation with a telemetry profile (`mqtt_service.py:418`). A pilot mapping a spindle temperature will see nothing and be told nothing. Do not promise a chart of it. |
| **Reconnect after a PLC drop** | **VERIFIED** | The session is dropped rather than reused, a new one is opened, and backoff is bounded and jittered so a cell of gateways does not retry in lockstep against a controller with a session limit. |
| **Reading continues while AMP is unreachable** | **VERIFIED** | The poll loop and the publish loop are genuinely independent: the blocking publish runs off the event loop, so a broker that stops acknowledging cannot stall PLC polling. It could until the assembled Gateway was driven end to end — the separation was documented and not true. |
| **The assembled gateway, as `run` builds it** | **VERIFIED** | Real Gateway, real queue, real publisher, real MQTT broker, scripted PLC: reads, queues, delivers, acks its own queue, reports STREAMING, stops cleanly. Previously every piece was tested and the assembly was not. |
| **Testing the cloud half without streaming** | **VERIFIED** | `check-amp` separates DNS, TCP, MQTT and publish-ACL, and publishes a probe AMP provably writes nothing for. |
| **Clean shutdown keeps the in-flight window** | **VERIFIED** | Stopping flushes the parts counted since the last publish to disk before disconnecting, so a restart mid-shift does not lose them. |
| **Connection health tells you which side is broken** | **VERIFIED** | One verdict, in fixing order: PLC → tags → **values** → cloud → backlog. A connected-but-silent session is not reported as healthy; a gateway that has read nothing is STARTING; and a machine whose values are all REFUSED (wrong datatype, clock ahead) reports `READINGS_REFUSED` rather than STREAMING — it used to report the latter, which is the worst thing it could say. |
| **Clock skew is measured and named** | **VERIFIED** | One wrong clock causes two unrelated-looking failures: readings refused as future-stamped, and signatures AMP will not accept. `preview` reports the skew before streaming; the health verdict names it during. |
| **Issuing and revoking gateway credentials** | **VERIFIED** | Admin-only routes. The key is returned by the POST that creates it and by nothing else — the list response model has no field to put it in. Revocation is a flag, not a delete, so it cannot re-open the workspace. |
| **Commissioning CLI (validate / check-amp / browse / preview / run / diagnose)** | **VERIFIED** | `preview` is exercised against the live OPC UA server, printing raw beside canonical. |
| **Redacted diagnostic export** | **VERIFIED** | Contains the configuration with secrets replaced and the NAMES of relevant environment variables. No values. |
| **PROFINET, EtherNet/IP, Siemens S7, Mitsubishi, Omron, Beckhoff ADS, CAN, BACnet, MTConnect** | **NOT IMPLEMENTED** | Not started. A PLC datasheet listing these does not mean AMP can read them. |

### What "SIMULATOR VERIFIED" is not

Every protocol row above marked SIMULATOR VERIFIED was proven against a
**software server on loopback**, not against a physical controller. Note the
asymmetry, which is the opposite of what anyone would guess: the two protocols
that have been in the product longest — OPC UA and Modbus — are the two that
have never met real hardware. FOCAS, the newest, is the only one that has. Software servers are forgiving in ways real
PLCs are not: they do not run out of sessions, they do not have a watchdog that
resets the CPU under load, their clock is the same clock, and their address
spaces are small. The first connection to a real machine will find something
this testing could not.

That is the expected state before a first pilot. It is written here so that
nobody reads a green row as "this has run in a factory".

---

## Before the first pilot: what must still be true

1. ~~**One connection to a real PLC**~~ — **DONE 2026-10-05.** `preview` was run
   against a FANUC Series 0i-MF Plus on a BFW BMV 45+ TC24, on the customer's
   own network, and its PARTS COUNT was checked against the operator's screen.
   What the visit found that no amount of testing had: the undefined-macro
   sentinel raises a different exception than the library's source implies, and
   a FOCAS **range** read is refused by the control outright while single reads
   succeed. Both are now pinned by `edge/test_focas_end_to_end.py`.
   **Still open for OPC UA and Modbus**, which remain simulator-only.
2. **A real broker with TLS**, with credentials issued per gateway.
3. **NTP on the gateway host.** A clock more than five minutes out cannot
   authenticate at all — the replay window has to be short to be worth having.
   The intake form asks for the site's time source for this reason.

~~AMP must verify gateway signatures~~ — done (ADR-0041). The remaining exposure
is that a gateway's key is a shared secret **at rest in AMP's database**: anyone
with a dump can publish as any gateway in it. Accepted for a pilot, recorded in
the ADR, and the wire `scheme` field exists so asymmetric signing can replace it
without breaking gateways already in the field.

---

## Client discovery checklist

**Use [PLC-PILOT-CLIENT-INTAKE.md](PLC-PILOT-CLIENT-INTAKE.md).** This page had
its own checklist for about an hour; the intake form covers every item it did
and a dozen more (byte order, deadband, counter retention across power loss,
expected maximum production rate), and two overlapping checklists in one folder
is how a commissioning engineer misses the half that was in the other one.

Two things on that form matter more than the rest, because the gateway's
behaviour depends on them and it will refuse rather than guess:

- **Counter semantics (section 6).** Cumulative, resets, or per-cycle; and the
  rollover point if it wraps. Without a declared mode the mapping does not load
  at all; without a declared maximum a backwards step counts zero.
- **Whether there is a reject counter (section 5).** If there is not, AMP
  records no production at all for that machine unless the config explicitly
  says untracked parts may be counted as good. Agree that with the customer in
  advance, not on the day.

---

## Failure behaviour you can quote to a customer

Walk [PLC-PILOT-REHEARSAL.md](PLC-PILOT-REHEARSAL.md) before a commissioning
visit — 40 drills, each with the evidence that pins it, and a final section
listing the six that nothing pins yet.


| What happens | What AMP does |
|---|---|
| PLC unplugged | The machine stops updating. AMP does **not** show it as stopped or as zero — it shows the last known state with its age, and the gateway reports `PLC_UNREACHABLE`. |
| Internet drops | Readings queue on the plant PC's disk. When the link returns they are sent with their **original timestamps**, marked as buffered, so they become history and not a false "now". |
| Gateway PC restarts | The queue survives. Counters re-baseline, so parts made while it was down are **not** counted — under-reporting rather than inventing. |
| A tag address is wrong | That one tag is refused and named in the health report; every other tag keeps working. The gateway reports `TAGS_FAILING`, which tells the engineer it is the tag list and not the network. |
| A counter goes backwards unexpectedly | Zero parts counted for that interval, with a note naming the setting that would explain it. Never an invented quantity. |
| The broker rejects the credentials | Reported as a credentials problem, not as "AMP is offline". |
| The disk fills | The queue is bounded; oldest records are dropped, counted, and the next message carries a gap marker so AMP knows the stream is not continuous. |

---

## How each claim above was verified

| Test | What it drives |
|---|---|
| `edge/test_opcua_end_to_end.py` | A live `asyncua` server through adapter, mapper, normalizer, payload and AMP's real `on_message`, plus `preview` and a subscription. |
| `edge/test_modbus_end_to_end.py` | A live `pymodbus` server through the same pipeline onto the same kind of machine. |
| `edge/test_focas_end_to_end.py` | A fake FOCAS server speaking the real packet framing, decoded by the adapter's own client: macro reads, the undefined-macro refusal, the status record read once per poll however many status tags, single-macro reads only, and a dead socket raising so the runner reconnects. |
| `edge/mutate_focas.py` | 5 mutations of the FOCAS guards; all caught. |
| `edge/test_edge_pipeline.py` | Counter arithmetic, absence-is-not-zero, clock handling, quality codes, and the durable queue. |
| `edge/test_edge_security.py` | Signing, tampering, replay windows, and the refusal to hold a secret in a config file. |
| `edge/test_publisher_against_broker.py` | A real MQTT 3.1.1 broker, including a withheld PUBACK. |
| `backend/test_machine_identity_adoption.py` | Gateway adoption of a hand-created machine, ambiguity refusal, and the real message handler. |
| `edge/test_edge_health.py` | The commissioning verdict, in fixing order, and that the printed report carries no credential. |
| `edge/test_edge_runner.py` | A PLC that goes away and returns, an AMP outage that does not stop reading, a shutdown that flushes, and a part-way publish failure that preserves order. |
| `backend/test_gateway_ingest_authentication.py` | The real handler refusing a gateway that speaks for another workspace — including one at the same site name, which is what isolates the workspace check. |
| `backend/verify_pg_gateway_credentials.py` | Migration 0013 on PostgreSQL from the frozen baseline, with the idempotency constraint proven against a populated table. |
| `backend/mutate_gateway_auth.py` | 21 mutations of authentication and idempotency. |
| `backend/mutate_machine_identity.py` | 12 mutations of the identity path; all caught. |
