# Pilot rehearsal: break it on purpose, before a customer does

Every failure below happens on a real site. Most of them happen in the first
week. This is the list to walk before a commissioning visit, and the evidence
that each one behaves as claimed.

**How to read the Evidence column.** A named test file means the behaviour is
pinned by something that runs in CI. "Not covered" means exactly that — it is
not a claim that the drill fails, it is a claim that nobody has checked. Those
are listed last rather than buried.

Run everything first:

```bash
python -m compileall -q edge && for f in edge/test_*.py; do python "$f" || echo "FAILED $f"; done
```

---

## A. The PLC

| # | Drill | Expected | Evidence |
|---|---|---|---|
| 1 | PLC unreachable at startup | Gateway reports `PLC_UNREACHABLE` with the endpoint; retries with jittered backoff; never exits | `test_edge_health.py` §1, `test_edge_runner.py` §1 |
| 2 | PLC unplugged while streaming | Session dropped, not reused; new session on return; poll loop survives | `test_edge_runner.py` §2 |
| 3 | PLC power-cycled | Reconnects; counters **re-baseline** rather than subtracting across the gap | `test_edge_runner.py` §2, `test_edge_pipeline.py` §1 |
| 4 | One tag address wrong, rest correct | That tag alone refused and named; every other tag keeps reading; state is `DEGRADED`, not disconnected | `test_opcua_end_to_end.py` §2, `test_modbus_end_to_end.py` §4 |
| 5 | Tag returns bad quality (OPC UA `Bad_*`) | No sample. **Not** the value that came with it | `test_opcua_end_to_end.py` §2, `test_edge_pipeline.py` §4 |
| 6 | Tag returns `Uncertain_LastUsableValue` | Refused. A value the server doubts is not a value | `test_edge_pipeline.py` §4 |
| 7 | Modbus register outside the mapped range | Device exception surfaced as a refusal — **never** the zero the wire offers | `test_modbus_end_to_end.py` §4 |
| 8 | Value will not fit its declared datatype | Refused, tag named. No default substituted | `test_edge_pipeline.py` §2, `mapping.py` (`ValueRefused`) |
| 9 | Boolean raw value in neither true- nor false-set | Refused. Not silently "stopped" | `test_edge_pipeline.py` §2 |
| 10b | **Tag reads perfectly, every value unusable** (int declared bool, run bit in neither true- nor false-set, clock ahead) | `READINGS_REFUSED`, naming the signal and the reason. NOT `STREAMING` — the adapter succeeding is not the same as the data being usable | `test_edge_health.py` §9, `test_edge_pipeline.py` §7 |
| 10 | **PLC offline ≠ machine value zero** | No sample at all; AMP keeps the last known state with its age; verdict says `PLC_UNREACHABLE` | `test_edge_pipeline.py` §2, `test_edge_health.py` §1–2 |

## B. Counters and production

| # | Drill | Expected | Evidence |
|---|---|---|---|
| 11 | First reading of a cumulative counter at 48,210 | Baseline. **Zero** parts recorded | `test_edge_pipeline.py` §1 |
| 12 | 16-bit counter wraps 65,530 → 3, `counter_max` declared | 9 parts | `test_edge_pipeline.py` §1 |
| 13 | Counter drops 412 → 0, no `counter_max` | **Zero** parts and a note naming the setting that would explain it | `test_edge_pipeline.py` §1 |
| 14 | Counter drops near zero with `counter_max` declared but far from the ceiling | Still zero — not treated as a rollover | `test_edge_pipeline.py` §1 |
| 15 | Counter declared `resets` and restarts at 0 | The new value is production | `test_edge_pipeline.py` §1 |
| 16 | Counter mapped with no `counter_mode` | Gateway **does not start**; error names the consequence | `test_edge_pipeline.py` §1 |
| 17 | Only `part_count` mapped, no reject counter | **No production record.** AMP does not report 100% quality by default | `test_edge_pipeline.py` §5 |
| 18 | `part_count` ≠ `good` + `reject` | Refused for that window, with a note | `test_edge_pipeline.py` §5 |

## C. Clocks and ordering

| # | Drill | Expected | Evidence |
|---|---|---|---|
| 19 | PLC clock an hour ahead | Reading refused; message names the PLC clock | `test_edge_pipeline.py` §3 |
| 20 | Reading arrives older than the last one for that signal | Refused — a stale value must not overwrite a newer one | `test_edge_pipeline.py` §3 |
| 21 | Same reading delivered twice (replay) | Each record carries a `record_id`, and AMP writes it once | `test_edge_pipeline.py` §6, `test_gateway_ingest_authentication.py` §6 |

## D. The network and the queue

| # | Drill | Expected | Evidence |
|---|---|---|---|
| 22 | Internet drops mid-shift | Reading **continues**; queue grows; verdict says `AMP_UNREACHABLE` and *"nothing is being lost"* | `test_edge_runner.py` §3, `test_edge_health.py` §4 |
| 23 | Broker rejects the credentials | Reported as a credentials problem, not as "AMP is offline" | `test_publisher_against_broker.py` §3 |
| 24 | Broker accepts the publish but never sends PUBACK | Publish reports failure; the record **stays on disk** | `test_publisher_against_broker.py` §2 |
| 25 | Connection returns after an outage | Buffered records sent with their **original** timestamps, flagged `buffered` so they become history, not a false "now" | `test_edge_pipeline.py` §6 |
| 26 | Gateway PC restarted mid-shift | Queue survives; the in-flight window is flushed to disk before exit | `test_edge_pipeline.py` §6, `test_edge_runner.py` §4 |
| 27 | Disk fills / queue reaches its bound | Oldest dropped, **counted**, and the next record carries a gap marker so AMP knows the stream is not continuous | `test_edge_pipeline.py` §6 |
| 28 | Publishing fails part-way through a batch | Stops at the first failure; the rest stay queued **in order** | `test_edge_runner.py` §5 |

## E. Identity and tenancy

| # | Drill | Expected | Evidence |
|---|---|---|---|
| 29 | Gateway publishes for a machine the customer created by hand | **Adopted** — same row, same id, same history. No duplicate | `test_opcua_end_to_end.py` §5, `test_machine_identity_adoption.py` §1 |
| 30 | Two machines of one name, one sited and one not | Refused; conflict recorded once for a human; **no** row created | `test_machine_identity_adoption.py` §3–4 |
| 31 | Gateway reconnects repeatedly | Still exactly one machine | `test_machine_identity_adoption.py` §1, §7 |
| 32 | A packet's payload claims a different tenant than its topic | Rejected | `mqtt_identity.check_payload_agrees`, `test_mqtt_tenant_identity.py` |
| 33 | Gateway config edited to another customer's tenant | Refused: the signature proves which gateway it is, and the credential proves which workspace that gateway may speak for | `test_edge_security.py` §1–2, `test_gateway_ingest_authentication.py` §3 |
| 33b | The same attack against the same SITE NAME in another workspace | Refused — and this is the case that isolates the workspace check from the site check | `test_gateway_ingest_authentication.py` §3 |
| 33c | Revoking the last gateway | The workspace stays CLOSED. Revocation must never hand it back to unsigned traffic | `test_gateway_ingest_authentication.py` §2 |
| 34 | Adoption attempted across workspaces | Never crosses; each workspace resolves its own | `test_machine_identity_adoption.py` §5 |

## F. Not covered — the honest half

The pilot's real risk list. Rows struck through were open earlier in this
document's life and are kept, closed, rather than deleted — so that anyone who
read the earlier version can see what changed rather than wonder whether they
misremembered.

| # | Drill | Why it matters | Status |
|---|---|---|---|
| 35 | ~~A gateway publishes into another tenant by editing its topic~~ | **CLOSED** (ADR-0041). Covered end to end by `test_gateway_ingest_authentication.py` §3, including the same-site case. The residual exposure is the key being a shared secret at rest in AMP's database. | **CLOSED** |
| 36 | ~~The same production record delivered twice~~ | **CLOSED** (migration 0013). Three deliveries write one record; `test_gateway_ingest_authentication.py` §6. | **CLOSED** |
| 35b | A PLC or gateway whose clock is wrong | `preview` reports the skew before you stream; a PLC more than 60s ahead is called out as fatal with the reason. While streaming, the health verdict says `READINGS_REFUSED` and NAMES the clock — including that the same clock breaks signing, which presents as a different fault. | `test_edge_pipeline.py` §8, `test_edge_health.py` §9, `test_opcua_end_to_end.py` §4b |
| 37 | Process telemetry (temperature, pressure) on an ordinary machine | Published correctly and **dropped by AMP**: `readings` is interpreted only for an OEM installation with a telemetry profile. | **OPEN — do not promise a chart** |
| 38 | Anything at all against a **physical PLC** | Every protocol row in the support matrix says SIMULATOR VERIFIED. Software servers do not run out of sessions, do not have a CPU watchdog, and share our clock. | **OPEN by design — this is what the first visit is for** |
| 39 | MQTT over TLS to a production broker | `tls_set()` is called; never exercised against a real TLS endpoint. | **OPEN** |
| 40 | OPC UA with certificates / `Sign&Encrypt` | Passed through to `asyncua` untouched. Never run. | **EXPERIMENTAL — do not offer it** |

---

## On the day

1. Run the drills in section A against the customer's controller once connected
   — 1, 2 and 4 take two minutes and catch most of what goes wrong later.
2. Do **drill 11 deliberately**: note the counter's value at the start, make one
   part, and confirm AMP records one part and not 48,211.
3. Do **drill 22 deliberately**: unplug the gateway's network cable for two
   minutes and watch the readings arrive afterwards. It is the single most
   reassuring thing a customer sees, and it costs two minutes.
