"""Per-source status spans: the only status history the downtime attribution reads (ADR-0020).

WHAT THIS PINS
--------------
telemetry_coverage.record_message turns every status-bearing message into a
run of one status per (tenant, machine, source):

  * The same status within SPAN_GAP_SECONDS (300) of the span's end EXTENDS the
    span; the end is written only when it moves by SPAN_WRITE_RESOLUTION_SECONDS
    (30) or more, and message_count goes up by one on every message regardless.
  * A different status, or a gap longer than the tolerance, opens a NEW span.
  * Sources never share a span; neither do machines or tenants.
  * The time is the server's RECEIVE time, whole seconds, never a device
    timestamp. A recognised status is stored in its canonical form; an
    unrecognised one raw, cut to 32 characters (the engine treats it as
    DISPUTED); a message with no status writes NO span (no data, never "Idle").
  * The tenant is explicit and must be a factory (never blank, never an OEM
    sentinel); the source must be one the writers use, and never "manual".

And the writers:
  * MQTT (source "mqtt") writes a span from EVERY message, including the ones
    that change nothing about Machine.status.
  * /iot/telemetry ("iot") and /industrial/signals ("industrial_gateway") write
    one for a status signal and nothing for any other signal.
  * The simulator's heartbeat ("simulator") writes one per machine of the bound
    tenant.
  * The manual status PATCH writes NONE.
  * C2, end to end: MQTT Running, a manual PATCH to Breakdown, MQTT Breakdown.
    The second MQTT message writes no MachineEvent (Machine.status already says
    Breakdown) but it does write its span, so the MQTT history is not "Running
    throughout".

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_telemetry_coverage.py
"""
import ast
import json
import os
from datetime import datetime, timedelta, timezone

import contract_route_harness as H
import models
import tenancy

HERE = os.path.dirname(os.path.abspath(__file__))
check, section = H.check, H.section
T0 = datetime(2026, 5, 4, 10, 0, 0)


def tc():
    import telemetry_coverage
    return telemetry_coverage


def spans(tenant=None, machine_id=None, source=None):
    with H.unscoped() as db:
        q = db.query(models.MachineTelemetrySpan)
        if tenant:
            q = q.filter(models.MachineTelemetrySpan.tenant_code == tenant)
        if machine_id:
            q = q.filter(models.MachineTelemetrySpan.machine_id == machine_id)
        if source:
            q = q.filter(models.MachineTelemetrySpan.source == source)
        return [(r.tenant_code, r.machine_id, r.source, r.status, r.span_start, r.span_end,
                 r.message_count)
                for r in q.order_by(models.MachineTelemetrySpan.id.asc()).all()]


def clear_spans():
    with H.unscoped() as db:
        for r in db.query(models.MachineTelemetrySpan).all():
            db.delete(r)
        db.commit()


def record(status, at, machine="A1", source="mqtt", tenant="FACTORY_A"):
    with H.unscoped() as db:
        out = tc().record_message(db, tenant, H.S["machines"][machine], source, status, at)
        db.commit()
        return out


def case_constants():
    section("1. THE CONSTANTS, DEFINED ONCE")
    check("SPAN_GAP_SECONDS is 300", tc().SPAN_GAP_SECONDS == 300)
    check("SPAN_WRITE_RESOLUTION_SECONDS is 30", tc().SPAN_WRITE_RESOLUTION_SECONDS == 30)
    check("SETTLE_SECONDS = gap + write resolution + 60 = 390",
          tc().SETTLE_SECONDS == tc().SPAN_GAP_SECONDS + tc().SPAN_WRITE_RESOLUTION_SECONDS + 60
          == 390)
    import contract_statements
    check("the statement engine reads the same values",
          contract_statements.span_gap_seconds() == 300
          and contract_statements.settle_seconds() == 390)


def case_extend_and_open():
    section("2. SAME STATUS EXTENDS; A CHANGE OR A GAP OPENS A NEW SPAN")
    clear_spans()
    a1 = H.S["machines"]["A1"]
    record("Running", T0)
    check("the first message opens a span of zero length",
          spans() == [("FACTORY_A", a1, "mqtt", "Running", T0, T0, 1)], str(spans()))
    record("Running", T0 + timedelta(seconds=29))
    check("29 s later (under the write resolution) only message_count moves",
          spans() == [("FACTORY_A", a1, "mqtt", "Running", T0, T0, 2)], str(spans()))
    record("Running", T0 + timedelta(seconds=30))
    check("30 s after the written end, the end is written",
          spans() == [("FACTORY_A", a1, "mqtt", "Running", T0, T0 + timedelta(seconds=30), 3)],
          str(spans()))
    end = T0 + timedelta(seconds=30)
    record("Running", end + timedelta(seconds=300))
    check("exactly SPAN_GAP_SECONDS after the end still extends",
          spans() == [("FACTORY_A", a1, "mqtt", "Running", T0, end + timedelta(seconds=300), 4)],
          str(spans()))
    end = end + timedelta(seconds=300)
    record("Running", end + timedelta(seconds=301))
    rows = spans()
    check("one second past the gap opens a new span",
          len(rows) == 2 and rows[1][3:] == ("Running", end + timedelta(seconds=301),
                                             end + timedelta(seconds=301), 1), str(rows))
    t = end + timedelta(seconds=310)
    record("breakdown", t)
    rows = spans()
    check("a status change opens a new span, in canonical form",
          len(rows) == 3 and rows[2][3:] == ("Breakdown", t, t, 1), str(rows))
    record("Running", t + timedelta(seconds=40))
    rows = spans()
    check("changing back opens another span rather than reviving the old Running one",
          len(rows) == 4 and rows[3][3] == "Running" and rows[1][5] == end + timedelta(seconds=301),
          str(rows))
    record("Running", t + timedelta(seconds=45))
    check("... and the next message extends the NEWEST span",
          spans()[3][6] == 2 and spans()[1][6] == 1, str(spans()))


def case_separation():
    section("3. SOURCES, MACHINES AND TENANTS NEVER SHARE A SPAN")
    clear_spans()
    record("Running", T0, source="mqtt")
    record("Running", T0 + timedelta(seconds=60), source="iot")
    record("Running", T0 + timedelta(seconds=60), machine="A2")
    record("Running", T0 + timedelta(seconds=60), machine="AB", tenant="FACTORY_B")
    rows = spans()
    check("four spans: mqtt/A1, iot/A1, mqtt/A2, mqtt/AB at FACTORY_B", len(rows) == 4, str(rows))
    check("the mqtt span of A1 was not extended by the iot message",
          spans(machine_id=H.S["machines"]["A1"], source="mqtt")[0][5:] == (T0, 1),
          str(spans(machine_id=H.S["machines"]["A1"], source="mqtt")))
    check("each span carries its own tenant, explicitly",
          [r[0] for r in rows] == ["FACTORY_A", "FACTORY_A", "FACTORY_A", "FACTORY_B"])
    record("Running", T0 + timedelta(seconds=90), tenant="FACTORY_B")
    check("a message stamped for FACTORY_B never extends FACTORY_A's span of the same machine id",
          len(spans(tenant="FACTORY_A", source="mqtt", machine_id=H.S["machines"]["A1"])) == 1
          and spans(tenant="FACTORY_A", source="mqtt",
                    machine_id=H.S["machines"]["A1"])[0][6] == 1
          and len(spans(tenant="FACTORY_B", machine_id=H.S["machines"]["A1"])) == 1,
          str(spans()))


def case_values():
    section("4. WHAT A SPAN RECORDS, AND WHAT IS REFUSED")
    clear_spans()
    record("  running ", T0)
    check("a recognised status is stored canonically", spans()[0][3] == "Running", str(spans()))
    raw = "Fault-E42 compressor discharge high temperature trip"
    record(raw, T0 + timedelta(seconds=10))
    check("an unrecognised status is kept raw, cut to 32 characters",
          spans()[1][3] == raw[:32], str(spans()))
    for empty in (None, "", "   "):
        count = len(spans())
        out = record(empty, T0 + timedelta(seconds=20))
        check(f"a message with no status ({empty!r}) writes no span", out is None
              and len(spans()) == count)
    clear_spans()
    record("Running", datetime(2026, 5, 4, 10, 0, 0, 900000))
    check("fractional seconds are floored", spans()[0][4] == T0, str(spans()))
    clear_spans()
    record("Running", datetime(2026, 5, 4, 15, 30, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))))
    check("an aware time is converted to naive UTC", spans()[0][4] == T0, str(spans()))
    for label, kwargs in (("a blank tenant", {"tenant": ""}),
                          ("an OEM sentinel tenant", {"tenant": "OEM:OEM_ALPHA"}),
                          ("the manual source", {"source": "manual"}),
                          ("an unknown source", {"source": "carrier-pigeon"})):
        count = len(spans())
        refused = False
        try:
            record("Running", T0 + timedelta(hours=1), **kwargs)
        except ValueError:
            refused = True
        check(f"{label} is refused", refused and len(spans()) == count)
    refused = False
    try:
        record("Running", "2026-05-04T10:00:00Z")
    except (ValueError, TypeError, Exception):
        refused = True
    check("a time that is not a datetime is refused", refused)


class _Msg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = json.dumps(payload).encode()


def _publish(payload):
    import mqtt_service
    mqtt_service.SessionLocal = H.SessionLocal
    mqtt_service.on_message(None, None, _Msg("flowmes/FACTORY_A/Plant/machines", payload))


def _machine_events(machine_id):
    with H.unscoped() as db:
        return [(e.old_status, e.new_status, e.source) for e in db.query(models.MachineEvent)
                .filter(models.MachineEvent.machine_id == machine_id)
                .order_by(models.MachineEvent.id.asc()).all()]


class _Clock:
    """Pins telemetry_coverage.received_at, the writers' only clock."""

    def __init__(self, at):
        self.at = at

    def __enter__(self):
        self.original = tc().received_at
        tc().received_at = lambda: self.at
        return self

    def __exit__(self, *exc):
        tc().received_at = self.original


def case_mqtt_and_manual_patch():
    section("5. MQTT WRITES A SPAN FROM EVERY MESSAGE; THE MANUAL PATCH WRITES NONE (C2)")
    clear_spans()
    a1 = H.S["machines"]["A1"]
    before_real = tc().received_at()
    _publish({"machine": "A1-SECRETNAME", "status": "Running", "utilization": 70,
              "timestamp": "2020-01-01T00:00:00Z"})
    after_real = tc().received_at()
    rows = spans(machine_id=a1)
    check("an MQTT message writes an mqtt span", len(rows) == 1 and rows[0][2:4] == ("mqtt", "Running"),
          str(rows))
    check("... at the server's receive time, not the payload's timestamp",
          rows and before_real <= rows[0][4] <= after_real, str(rows))

    clear_spans()
    events_before = len(_machine_events(a1))
    with _Clock(T0):
        _publish({"machine": "A1-SECRETNAME", "status": "Running", "utilization": 70})
    import asyncio
    with _Clock(T0 + timedelta(seconds=60)):
        r = asyncio.run(H._call("PATCH", f"/machines/{a1}/status?status=Breakdown", H.TOKENS["fa"]))
    check("CONTROL: the manual PATCH to Breakdown succeeded", r.status == 200, r)
    check("the manual PATCH wrote no span", len(spans(machine_id=a1)) == 1, str(spans(machine_id=a1)))
    with _Clock(T0 + timedelta(seconds=120)):
        _publish({"machine": "A1-SECRETNAME", "status": "Breakdown", "utilization": 0})
    events = _machine_events(a1)[events_before:]
    check("CONTROL (the C2 loss): the PATCH wrote a manual event and the second MQTT "
          "message wrote none",
          any(e == ("Running", "Breakdown", "manual") for e in events)
          and not any(e[2] == "mqtt" and e[1] == "Breakdown" for e in events), str(events))
    rows = spans(machine_id=a1, source="mqtt")
    check("... but it wrote its span: the MQTT history is Running, then Breakdown",
          [(r[3], r[4]) for r in rows] == [("Running", T0),
                                           ("Breakdown", T0 + timedelta(seconds=120))], str(rows))

    clear_spans()
    with _Clock(T0):
        _publish({"machine": "A1-SECRETNAME", "utilization": 55})
    check("an MQTT message with no status writes no span (never a default 'Idle')",
          spans(machine_id=a1) == [], str(spans(machine_id=a1)))
    with _Clock(T0):
        _publish({"machine": "A1-SECRETNAME", "status": "Tripped-E7", "utilization": 0})
    check("an unrecognised MQTT status is kept raw for the engine to dispute",
          [r[3] for r in spans(machine_id=a1)] == ["Tripped-E7"], str(spans(machine_id=a1)))


def case_http_ingest():
    section("6. /iot/telemetry AND /industrial/signals WRITE A SPAN FOR A STATUS SIGNAL ONLY")
    clear_spans()
    a2 = H.S["machines"]["A2"]
    with _Clock(T0):
        r = H.POST("/iot/telemetry", H.TOKENS["fa"],
                   {"machine_id": a2, "signal_name": "temperature", "signal_value": "71",
                    "numeric_value": 71})
    check("CONTROL: a non-status telemetry reading is accepted", r.status == 200, r)
    check("... and writes no span", spans(machine_id=a2) == [], str(spans(machine_id=a2)))
    with _Clock(T0 + timedelta(seconds=5)):
        r = H.POST("/iot/telemetry", H.TOKENS["fa"],
                   {"machine_id": a2, "signal_name": "Status", "signal_value": "running"})
    check("a status signal is accepted", r.status == 200, r)
    check("... and writes an 'iot' span in FACTORY_A",
          spans(machine_id=a2) == [("FACTORY_A", a2, "iot", "Running", T0 + timedelta(seconds=5),
                                    T0 + timedelta(seconds=5), 1)], str(spans(machine_id=a2)))

    with H.unscoped() as db:
        device = models.IndustrialDevice(tenant_code="FACTORY_A", device_code="PLC-T1",
                                         device_name="PLC", linked_machine_id=a2)
        db.add(device)
        db.commit()
        device_id = device.id
    with _Clock(T0 + timedelta(seconds=10)):
        r = H.POST("/industrial/signals", H.TOKENS["fa"],
                   {"device_id": device_id, "machine_id": a2, "signal_name": "load",
                    "signal_value": "40", "numeric_value": 40})
    check("CONTROL: a non-status PLC signal is accepted", r.status == 200, r)
    check("... and writes no gateway span", spans(machine_id=a2, source="industrial_gateway") == [])
    with _Clock(T0 + timedelta(seconds=15)):
        r = H.POST("/industrial/signals", H.TOKENS["fa"],
                   {"device_id": device_id, "machine_id": a2, "signal_name": "state",
                    "signal_value": "Maintenance"})
    check("a PLC state signal is accepted", r.status == 200, r)
    check("... and writes an 'industrial_gateway' span",
          [(r[2], r[3]) for r in spans(machine_id=a2, source="industrial_gateway")]
          == [("industrial_gateway", "Maintenance")], str(spans(machine_id=a2)))
    with _Clock(T0 + timedelta(seconds=20)):
        r = H.POST("/iot/telemetry", H.TOKENS["fb"],
                   {"machine_id": a2, "signal_name": "status", "signal_value": "Breakdown"})
    check("another factory cannot write a span onto FACTORY_A's machine",
          r.status == 404 and len(spans(machine_id=a2)) == 2, f"{r} {spans(machine_id=a2)}")


def case_simulator_heartbeat():
    section("7. THE SIMULATOR'S HEARTBEAT WRITES ONE 'simulator' SPAN PER MACHINE OF THE TENANT")
    import factory_simulator
    clear_spans()
    token = tenancy.set_current_tenant("FACTORY_A")
    try:
        with _Clock(T0), H.SessionLocal() as db:
            factory_simulator.tick_status_heartbeat(db)
    finally:
        tenancy.reset_current_tenant(token)
    rows = spans()
    with H.unscoped() as db:
        expected = sorted((m.id, m.status) for m in db.query(models.Machine)
                          .filter(models.Machine.tenant_code == "FACTORY_A").all())
    check("every FACTORY_A machine, and only those, got a simulator span with its status",
          sorted((r[1], r[3]) for r in rows) == expected
          and all(r[0] == "FACTORY_A" and r[2] == "simulator" for r in rows), str(rows))
    unbound = False
    try:
        with H.SessionLocal() as db:
            factory_simulator.tick_status_heartbeat(db)
    except ValueError:
        unbound = True
    check("with no tenant bound the heartbeat refuses rather than writing every tenant's",
          unbound and len(spans()) == len(rows))


def _source_calls(path):
    with open(os.path.join(HERE, path), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "record_message"):
            src = node.args[3] if len(node.args) > 3 else None
            if isinstance(src, ast.Constant):
                found.append(src.value)
            elif (isinstance(src, ast.Attribute) and isinstance(src.value, ast.Name)
                  and src.value.id == "telemetry_coverage" and hasattr(tc(), src.attr)):
                found.append(getattr(tc(), src.attr))
            else:
                found.append(ast.dump(src) if src is not None else None)
    return found


def case_structural_writers():
    section("8. STRUCTURAL: WHO WRITES SPANS, WITH WHICH SOURCE")
    expected = {"mqtt_service.py": ["mqtt"],
                "industrial_iot_routes.py": ["iot", "industrial_gateway"],
                "factory_simulator.py": ["simulator"]}
    for path, sources in expected.items():
        got = _source_calls(path)
        check(f"{path} calls record_message with {sources}", sorted(got) == sorted(sources), str(got))
    check("machines_routes.py (the manual PATCH) never calls record_message",
          _source_calls("machines_routes.py") == [])
    check("the writers' sources are exactly telemetry_coverage.SOURCES",
          sorted(s for v in expected.values() for s in v) == sorted(tc().SOURCES),
          str(tc().SOURCES))


def run_all():
    H.boot()
    H.seed()
    case_constants()
    case_extend_and_open()
    case_separation()
    case_values()
    case_mqtt_and_manual_patch()
    case_http_ingest()
    case_simulator_heartbeat()
    case_structural_writers()


def test_telemetry_coverage():
    H.failures.clear()
    run_all()
    assert not H.failures, H.failures


if __name__ == "__main__":
    run_all()
    H.finish("EVERY STATUS MESSAGE IS A SPAN OF ITS OWN SOURCE; THE MANUAL PATCH IS NOT TELEMETRY")
