# AMP Edge → Cloud Wire Contract (Pilot v1)

This is the pilot contract between an on-site AMP Edge gateway and the existing AMP MQTT ingest. It documents the cloud boundary that Edge must target; it does not claim an Edge driver exists merely because this contract exists.

## Topic

Multi-tenant pilot telemetry publishes to:

```
{prefix}/{tenant}/{site}/machines
```

Default prefix: `flowmes`.

For a single-site factory with no site code, use the wire token `-`:

```
flowmes/FACTORY_A/-/machines
```

Identifiers are restricted by the backend to 1–64 characters, beginning with an alphanumeric character and then using only letters, numbers, underscore, dot or hyphen.

The broker must authenticate each gateway and authorize it only for its assigned tenant/site topic. Payload identity is an additional consistency check, not authorization.

## Minimum payload

```json
{
  "tenant": "FACTORY_A",
  "site": "PLANT1",
  "machine": "CNC-01",
  "status": "Running",
  "utilization": 72
}
```

The payload's tenant/site, when present, must agree with the topic. A contradiction is rejected.

## Pilot machine-state vocabulary

Edge should normalize PLC-specific state into the canonical machine status vocabulary already accepted by AMP. Do not publish raw vendor enum names as `status` unless the backend canonicalizer explicitly supports them.

A missing/unusable PLC status must not be represented as `Idle`, `Running`, `0` or `false` merely to fill the field.

## Process/OEM readings

Additional mapped process telemetry can travel under `readings`:

```json
{
  "tenant": "FACTORY_A",
  "site": "PLANT1",
  "machine": "COMP-01",
  "status": "Running",
  "utilization": 81,
  "readings": {
    "operating_hours": 4281.6,
    "discharge_pressure": 7.4,
    "discharge_temperature": 82.1
  }
}
```

The OEM telemetry profile controls which named readings are interpreted for an installed OEM machine. Unknown/out-of-range readings must not be treated as evidence that a configured measurement is valid.

## Existing production-record payload — important pilot constraint

The current cloud ingest can create a `ProductionRecord` when a message contains a coherent snapshot:

```json
{
  "machine": "CNC-01",
  "status": "Running",
  "utilization": 72,
  "planned_minutes": 480,
  "runtime_minutes": 210,
  "ideal_cycle_time_seconds": 45,
  "total_count": 320,
  "good_count": 312,
  "rejected_count": 8
}
```

Cloud validation currently requires:

- all six production numerics to parse as non-negative integers;
- `total_count > 0`;
- `good_count + rejected_count == total_count`.

### Do not blindly publish cumulative PLC counters on every poll

The existing ingest writes a new production record for every valid message. Therefore an Edge implementation must not assume that repeatedly sending an unchanged/cumulative PLC total is automatically deduplicated.

Before production counts are enabled for a pilot, Edge must know whether each source counter is cumulative or incremental, its reset/rollover behaviour and the intended aggregation interval. Until a dedicated idempotency/event contract is implemented and proven, prefer disabling production-record emission rather than risking double-counting.

Machine state and process telemetry can still be piloted independently.

## Timestamp contract

The current MQTT machine-state path records server receipt time for coverage and database-created timestamps. A future Edge message may preserve source timestamps for buffered/replayed telemetry, but adding a field to JSON does not by itself make the existing cloud path use it.

Therefore:

- do not claim historical replay semantics until the cloud consumer explicitly persists/uses source timestamps;
- buffered messages must not masquerade as current machine state after a long outage;
- Edge should retain source timestamp and quality internally even when the current cloud contract cannot yet consume both correctly.

This is a release-blocking consideration for any pilot that requires store-and-forward production history.

## Quality / no-data semantics

Edge must keep these states distinct:

- valid measured zero;
- valid false;
- unknown / no reading;
- bad-quality reading;
- stale reading;
- PLC disconnected;
- signal not configured.

Do not convert any of the last five states into numeric zero or boolean false.

## Identity behaviour

The cloud machine identity is:

```
tenant + site + machine
```

A gateway must be commissioned against the intended existing identity. If the identity is wrong, telemetry can create or resolve a different machine depending on the deployed cloud version and configuration. The commissioning procedure must verify the intended machine ID and verify that no duplicate was created.

## Security boundary

For a pilot:

- PLC-side access defaults to read-only.
- Edge → broker should use authenticated TLS in production.
- Broker ACLs must restrict the gateway's publish topic.
- Do not put PLC passwords, certificates, private keys or broker passwords in telemetry.
- Do not log secrets in diagnostic exports.
- The cloud must not trust `tenant` from the JSON body as authorization.

## Pilot examples

### Machine state only

```json
{
  "tenant": "DEMO_FACTORY",
  "site": "LINE1",
  "machine": "PRESS-01",
  "status": "Breakdown",
  "utilization": 0,
  "downtime": "3 min"
}
```

### No-site wire spelling

Topic:

```
flowmes/DEMO_FACTORY/-/machines
```

Payload:

```json
{
  "tenant": "DEMO_FACTORY",
  "site": "-",
  "machine": "PRESS-01",
  "status": "Running",
  "utilization": 65
}
```

### Rejected identity mismatch

Topic:

```
flowmes/FACTORY_A/PLANT1/machines
```

Payload:

```json
{
  "tenant": "FACTORY_B",
  "site": "PLANT1",
  "machine": "CNC-01"
}
```

The backend must reject this rather than choosing one identity.

## Edge implementation checklist against this contract

- [ ] Build topic from provisioned gateway identity, not operator free text at runtime.
- [ ] Validate machine identifier before streaming.
- [ ] Normalize PLC state before publishing.
- [ ] Preserve source timestamp and source quality internally.
- [ ] Never synthesize zero for disconnected/bad/stale values.
- [ ] Do not emit production snapshots until counter semantics are configured.
- [ ] Use bounded durable buffering.
- [ ] On replay, prevent stale data becoming current machine state.
- [ ] TLS/auth configured for non-local broker.
- [ ] Verify broker ACL rejects another tenant/site.
- [ ] Verify intended existing AMP machine receives the packet.
- [ ] Verify no duplicate machine appears.
