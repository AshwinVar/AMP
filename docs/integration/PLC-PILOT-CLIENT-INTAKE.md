# AMP Pilot PLC — Client Intake & Site Survey

Use this before scheduling an on-site PLC pilot. Do not promise controller compatibility until the protocol and controller details below are confirmed.

## 1. Pilot identity

- Customer / tenant code:
- Site name / code:
- Machine name in AMP:
- Machine manufacturer:
- Machine model:
- Machine serial number:
- Controls engineer contact:
- Customer IT/OT contact:
- Planned commissioning date:

**Identity rule:** the AMP machine must already be uniquely identified by tenant + site + machine name before live telemetry is enabled. Never solve an ambiguous match by silently creating another machine.

## 2. Controller

- PLC/controller manufacturer:
- PLC/controller model / CPU:
- Firmware version:
- Engineering software and version:
- Existing gateway / IPC / industrial PC:
- Gateway operating system:
- Is controller configuration change permitted? yes / no / unknown

## 3. Protocol

Check what the controller actually exposes.

- OPC UA available: yes / no / unknown
- OPC UA endpoint:
- OPC UA security mode/policy:
- OPC UA authentication method:
- OPC UA certificate requirements:
- Modbus TCP available: yes / no / unknown
- Modbus host:
- Modbus port:
- Modbus unit/device ID:
- Other protocol(s):
- Is read-only pilot access possible? yes / no / unknown

Do not enter passwords, private keys or production secrets in this document.

## 4. Network / Edge host

- PLC IP/subnet:
- Edge host IP/subnet:
- Can Edge reach PLC locally? yes / no / unknown
- DNS available on Edge host: yes / no
- Internet available from Edge host: yes / no
- Outbound MQTT/TLS permitted: yes / no / unknown
- Outbound HTTPS permitted: yes / no / unknown
- Proxy required:
- Firewall change required:
- Customer-approved outbound destinations/ports:
- NTP/time source:
- Customer remote-access method, if any:

Preferred architecture is outbound-only from the factory Edge host to AMP. Do not expose the PLC to the public Internet.

## 5. Required pilot signals

For each signal record the PLC source, type, engineering unit, semantics and reset behaviour.

| AMP signal | Required? | PLC node/register/tag | Data type | Unit | Notes |
|---|---|---|---|---|---|
| running | yes | | | boolean | Define exactly what RUNNING means |
| cycle_active | recommended | | | boolean | |
| part_count | recommended | | | count | Total vs incremental |
| good_count | recommended | | | count | |
| reject_count | recommended | | | count | |
| fault_active | recommended | | | boolean | |
| fault_code | recommended | | | | Provide code dictionary |
| machine_mode | optional | | | | Auto/manual/setup etc. |
| cycle_time | optional | | | seconds | |
| operating_hours | optional | | | hours | |
| temperature | optional | | | | |
| pressure | optional | | | | |
| speed | optional | | | | |
| power | optional | | | | |
| energy | optional | | | | |

## 6. Counter semantics — mandatory when production counts are used

For every production counter confirm:

- Is it cumulative or incremental?
- When does it reset?
- Can an operator reset it manually?
- Does it reset at shift/batch/job/power cycle?
- Maximum value / rollover point:
- Does it ever decrease for reasons other than reset/rollover?
- Is the value retained after PLC power loss?
- Is there a separate good/reject/total counter?
- Expected maximum production rate:
- Required polling/subscription frequency:

Do not derive production by blindly adding successive values from a cumulative PLC counter. Reset, rollover, duplicate and replay behaviour must be defined first.

## 7. OPC UA mapping details

For OPC UA pilots collect:

- Namespace/node IDs or browsable path:
- Data type:
- Engineering unit:
- Sampling interval:
- Deadband requirement:
- Expected OPC UA StatusCode / quality:
- Source timestamp available: yes / no

Bad/uncertain OPC UA quality must not be converted into a valid zero.

## 8. Modbus mapping details

For Modbus TCP pilots collect:

- Register type: coil / discrete input / input register / holding register
- Address as shown in vendor documentation:
- Whether documentation uses zero-based or 1-based / 4xxxx notation:
- Number of registers:
- Signed/unsigned:
- Integer/float:
- 16/32/64-bit:
- Word order:
- Byte order:
- Scale:
- Offset:
- Unit:
- Poll interval:

Never guess Modbus address base, byte order or word order.

## 9. Time and data-quality contract

Confirm:

- PLC clock synchronized: yes / no / unknown
- PLC timezone:
- Edge timezone:
- Source timestamp available:
- Maximum acceptable clock drift:
- Maximum age before a reading is STALE:
- Desired offline timeout:

AMP must preserve these distinctions:

- measured zero
- false
- unknown
- stale
- bad quality
- disconnected
- not configured

A disconnected PLC is not a machine reading zero.

## 10. Pilot safety boundary

For the first pilot, record whether the integration is:

- READ ONLY
- WRITE ENABLED

Default: **READ ONLY**.

No PLC write/control command should be enabled merely to prove telemetry integration. Any future write capability requires a separately reviewed control path, authorization model, machine-safe-state analysis and customer approval.

## 11. Commissioning acceptance

The pilot is accepted only when the agreed path is demonstrated:

1. Existing AMP tenant/site/machine selected.
2. Edge connects to the PLC.
3. Configured tags/registers are readable.
4. Mapping preview shows real values.
5. A deliberate PLC value/state change reaches the correct AMP machine.
6. No duplicate machine is created.
7. Production counters behave correctly across normal changes and configured reset/rollover behaviour.
8. PLC disconnect becomes OFFLINE/UNKNOWN, not zero.
9. PLC reconnect recovers without manual database repair.
10. Internet/cloud loss does not silently lose required buffered records.
11. Buffered records preserve source timestamps and do not masquerade as current readings.
12. Edge restart preserves configuration.
13. Invalid tag/register is diagnosed clearly.
14. Bad datatype/quality is rejected or marked unknown rather than poisoning metrics.
15. A cross-tenant/site/machine identity attempt is rejected.
16. Credentials are absent from ordinary logs and diagnostic exports.
17. Command Centre/machine view reflects the live machine data where the required signals exist.

## 12. Evidence to retain

Retain non-secret commissioning evidence:

- AMP tenant/site/machine identifiers
- Edge version
- adapter/protocol version
- sanitized mapping configuration
- connection-test result
- first successful source timestamp
- first successful AMP receipt timestamp
- disconnect/reconnect result
- buffer/replay result
- counter reset/rollover result if applicable
- diagnostic export with secrets redacted
- customer sign-off / outstanding limitations

## Pilot go/no-go

**GO** only when the actual controller exposes one of the implemented and verified pilot protocols, network access is approved, the signal map is known, machine identity is unambiguous, and the required read-only telemetry can be demonstrated.

Otherwise record the missing prerequisite explicitly. Do not replace missing PLC information with assumptions.
