"""Telemetry anomaly: the database path and the service's authorization order.

WHAT IS PINNED HERE
-------------------
Windows
  score = [now - 1 h, now), baseline = [now - 14 d, now - 1 h); both built from
  oee_contract.OeeWindow, both bounded at BOTH ends in SQL, never overlapping. A
  row exactly at now - 1 h is score-window data only (mutation: widening the
  baseline to ``now`` fails a check).
Sources
  iot_telemetry and industrial_signals (quality "Good" only) for this machine,
  named ``iot:<signal>`` and ``plc:<signal>``; values from signal_value, else
  numeric_value; non-finite values dropped. Row caps report ``truncated``, and
  count only rows inside the window, because every query is bounded below in
  SQL (mutation-tested: an unbounded query lets older rows fill the cap).
Tenancy
  Every query filters tenant_code EXPLICITLY, so rows stamped with another
  tenant never arrive even when no ambient tenant is bound (mutation-tested).
State
  The running / not-running timeline comes from MachineEvent inside the window
  plus the last event before it (bounded lookback).
Order in service.score_machine
  machine belongs to the tenant (else MachineNotFound, and the consent gate is
  NOT consulted) -> consent (refusal raises ConsentRequired carrying the reason,
  and no telemetry is read) -> evaluation card verified against its pinned hash
  (else model_unavailable) -> load, bucket, fit, score. Nothing is persisted.
Parity
  A synthetic machine written to the database and read back through this path
  scores exactly as the same machine assembled in memory.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_anomaly_db.py
"""
import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import oee_contract
import tenancy
from amp_ai.core.artifact import payload_hash
from amp_ai.core.contracts import CAPABILITY_TELEMETRY_BASELINE, ConsentDecision, ConsentRequired
from amp_ai.telemetry_anomaly import baseline as B
from amp_ai.telemetry_anomaly import db_telemetry as DT
from amp_ai.telemetry_anomaly import series as SR
from amp_ai.telemetry_anomaly import service
from amp_ai.telemetry_anomaly import synthetic as SY
from database import Base

NOW = datetime(2026, 3, 10, 12, 0, 0)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
US = timedelta(microseconds=1)

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    return sessionmaker(bind=engine)()


def machine(db, tenant, name):
    m = models.Machine(tenant_code=tenant, name=name, status="Running")
    db.add(m)
    db.commit()
    return m


def iot(db, tenant, machine_id, ts, name, value, numeric=0):
    db.add(models.IoTTelemetry(tenant_code=tenant, machine_id=machine_id, signal_name=name,
                               signal_value=str(value), numeric_value=numeric, created_at=ts))


def plc(db, tenant, machine_id, ts, name, value, *, device_id=None, quality="Good", numeric=0):
    db.add(models.IndustrialSignal(tenant_code=tenant, device_id=device_id, machine_id=machine_id, signal_name=name,
                                   signal_value=str(value), numeric_value=numeric, quality=quality, created_at=ts))


def event(db, tenant, machine_id, ts, new, old=None):
    db.add(models.MachineEvent(tenant_code=tenant, machine_id=machine_id, machine_name="m", old_status=old,
                               new_status=new, created_at=ts))


class SpyGate:
    def __init__(self, granted=True, reason="Admin enabled telemetry learning", capability=None):
        self.calls = []
        self.granted = granted
        self.reason = reason
        self.capability = capability

    def check(self, db, tenant, capability):
        self.calls.append((tenant, capability))
        return ConsentDecision(granted=self.granted, capability=self.capability or capability, reason=self.reason,
                               granted_by="admin" if self.granted else None, granted_at=None)


def values_of(readings):
    return sorted(v for _, _, v in readings)


# --------------------------------------------------------------------------- windows
def section_windows():
    print("\n[windows: OeeWindow, bounded at both ends, never overlapping]")
    score, base = DT.windows(NOW)
    check("both windows are oee_contract.OeeWindow", isinstance(score, oee_contract.OeeWindow)
          and isinstance(base, oee_contract.OeeWindow))
    check("score window is [now - 1 h, now)", score.end == NOW and score.start == NOW - HOUR, repr(score))
    check("baseline window is [now - 14 d, now - 1 h)", base.end == score.start and base.start == NOW - 14 * DAY,
          repr(base))
    check("the windows are exactly the series bucket ranges",
          score.end - score.start == SR.SCORE_BUCKETS * timedelta(seconds=SR.BUCKET_SECONDS)
          and score.end - base.start == SR.TOTAL_BUCKETS * timedelta(seconds=SR.BUCKET_SECONDS))
    original = oee_contract._now
    oee_contract._now = lambda: NOW
    try:
        default_score, _ = DT.windows()
    finally:
        oee_contract._now = original
    check("with no explicit now, the end is the next instant (OeeWindow's rule)", default_score.end == NOW + US)
    try:
        DT.windows(datetime(2026, 3, 10, tzinfo=timezone.utc))
        check("a timezone-aware now is refused (stored timestamps are naive UTC)", False)
    except TypeError:
        check("a timezone-aware now is refused (stored timestamps are naive UTC)", True)


# --------------------------------------------------------------------------- loading
def section_sources_and_bounds():
    print("\n[load: sources, quality, boundaries, values]")
    db = session()
    a = machine(db, "TA", "CNC-01")
    other = machine(db, "TA", "CNC-02")
    good = models.IndustrialDevice(tenant_code="TA", device_code="GW-REAL-1", device_name="gw")
    db.add(good)
    db.commit()
    rows = [
        (NOW, 1.0), (NOW - US, 2.0), (NOW - HOUR, 3.0), (NOW - HOUR - US, 4.0),
        (NOW - 14 * DAY, 5.0), (NOW - 14 * DAY - US, 6.0),
    ]
    for ts, v in rows:
        iot(db, "TA", a.id, ts, "temp", v)
    plc(db, "TA", a.id, NOW - 2 * HOUR, "pressure", 7.0, device_id=good.id)
    plc(db, "TA", a.id, NOW - 2 * HOUR, "pressure", 99.0, device_id=good.id, quality="Bad")
    iot(db, "TA", other.id, NOW - 2 * HOUR, "temp", 55.0)
    iot(db, "TA", a.id, NOW - 3 * HOUR, "status", "Running", numeric=0)
    iot(db, "TA", a.id, NOW - 3 * HOUR, "amps", "12 A", numeric=12)
    iot(db, "TA", a.id, NOW - 3 * HOUR, "bad", "NaN", numeric=5)
    iot(db, "TA", a.id, NOW - 3 * HOUR, "worse", "inf", numeric=5)
    db.commit()

    load = DT.load(db, "TA", a.id, now=NOW)
    score_vals = {n: [] for n in ("iot:temp",)}
    for _, n, v in load.score_readings:
        score_vals.setdefault(n, []).append(v)
    base_by_name = {}
    for _, n, v in load.baseline_readings:
        base_by_name.setdefault(n, []).append(v)
    check("a row AT now is outside the score window", 1.0 not in score_vals["iot:temp"])
    check("now - 1 microsecond is in the score window", 2.0 in score_vals["iot:temp"])
    check("now - 1 h EXACTLY is in the score window", 3.0 in score_vals["iot:temp"])
    check("... and only there, not in the baseline", 3.0 not in base_by_name.get("iot:temp", []), str(base_by_name))
    check("now - 1 h - 1 microsecond is baseline", 4.0 in base_by_name.get("iot:temp", []))
    check("now - 14 d exactly is baseline", 5.0 in base_by_name.get("iot:temp", []))
    check("older than 14 days is outside both", 6.0 not in base_by_name.get("iot:temp", []) and 6.0 not in score_vals["iot:temp"])
    check("industrial_signals are read and named plc:<signal>", base_by_name.get("plc:pressure") == [7.0], str(base_by_name))
    check("a reading whose quality is not Good is excluded", 99.0 not in base_by_name.get("plc:pressure", []))
    check("another machine's rows are not read", 55.0 not in base_by_name.get("iot:temp", []))
    check("a non-numeric signal_value falls back to numeric_value",
          base_by_name.get("iot:amps") == [12.0] and base_by_name.get("iot:status") == [0.0], str(base_by_name))
    check("non-finite values are dropped, not replaced", "iot:bad" not in base_by_name and "iot:worse" not in base_by_name)
    ks_score = {SR.bucket_index(d) for d, _, _ in load.score_readings}
    ks_base = {SR.bucket_index(d) for d, _, _ in load.baseline_readings}
    check("offsets put every score reading in buckets 0-11 and every baseline reading in 12-4031",
          ks_score <= set(range(SR.SCORE_BUCKETS)) and ks_base <= set(range(SR.SCORE_BUCKETS, SR.TOTAL_BUCKETS)),
          f"{sorted(ks_score)} {sorted(ks_base)[:3]}..{sorted(ks_base)[-3:]}")
    check("not truncated", load.truncated is False)
    check("only a real (non-demo) industrial device and iot rows: simulated_source is unknown (None)",
          load.simulated_source is None)
    db.close()


def section_simulated_and_truncation():
    print("\n[load: simulated_source and row caps]")
    db = session()
    a = machine(db, "TA", "M")
    demo = models.IndustrialDevice(tenant_code="TA", device_code="PLC-OPCUA-01", device_name="demo")
    real = models.IndustrialDevice(tenant_code="TA", device_code="GW-9", device_name="real")
    db.add_all([demo, real])
    db.commit()
    plc(db, "TA", a.id, NOW - 2 * HOUR, "p", 1.0, device_id=real.id)
    db.commit()
    check("only a customer's real device: simulated_source is False",
          DT.load(db, "TA", a.id, now=NOW).simulated_source is False)
    plc(db, "TA", a.id, NOW - 3 * HOUR, "p", 2.0, device_id=demo.id)
    db.commit()
    check("any reading from AMP's demo PLC fleet: simulated_source is True",
          DT.load(db, "TA", a.id, now=NOW).simulated_source is True)
    check("no telemetry at all: simulated_source is None", DT.load(db, "TA", 999, now=NOW).simulated_source is None)

    for i in range(8):
        iot(db, "TA", a.id, NOW - 2 * HOUR - i * timedelta(minutes=10), "t", float(i))
    for i in range(3):
        iot(db, "TA", a.id, NOW - timedelta(minutes=5 + i), "t", 100.0 + i)
    db.commit()
    capped = DT.load(db, "TA", a.id, now=NOW, row_cap=5)
    iot_base = [v for _, n, v in capped.baseline_readings if n == "iot:t"]
    check("a capped query keeps the NEWEST rows and says truncated", capped.truncated is True
          and sorted(iot_base) == [0.0, 1.0, 2.0, 3.0, 4.0], str(sorted(iot_base)))
    check("the baseline start reports the earliest row actually used",
          capped.baseline_start_used == NOW - 2 * HOUR - 4 * timedelta(minutes=10), str(capped.baseline_start_used))
    check("rows from the other source older than that cut-off are dropped too",
          all(SR.bucket_index(d) <= SR.bucket_index(int((NOW - capped.baseline_start_used) / US))
              for d, _, _ in capped.baseline_readings))
    full = DT.load(db, "TA", a.id, now=NOW)
    check("uncapped: not truncated, baseline start is the window start",
          full.truncated is False and full.baseline_start_used == NOW - 14 * DAY)
    db.close()

    print("\n[load: a row cap counts only rows inside the window (every query is bounded below in SQL)]")
    db = session()
    m = machine(db, "TA", "CAP")
    dev = models.IndustrialDevice(tenant_code="TA", device_code="GW-CAP", device_name="gw")
    db.add(dev)
    db.commit()
    for i in range(4):                                     # inside each window
        iot(db, "TA", m.id, NOW - 2 * DAY - i * HOUR, "t", float(i))
        plc(db, "TA", m.id, NOW - 2 * DAY - i * HOUR, "p", float(i), device_id=dev.id)
        iot(db, "TA", m.id, NOW - timedelta(minutes=10 + i), "t", 50.0 + i)
        plc(db, "TA", m.id, NOW - timedelta(minutes=10 + i), "p", 50.0 + i, device_id=dev.id)
    event(db, "TA", m.id, NOW - 3 * DAY, "Running", "Idle")
    for i in range(12):                                    # older than the baseline window
        iot(db, "TA", m.id, NOW - 15 * DAY - i * HOUR, "t", 900.0 + i)
        plc(db, "TA", m.id, NOW - 15 * DAY - i * HOUR, "p", 900.0 + i, device_id=dev.id)
        event(db, "TA", m.id, NOW - 15 * DAY - i * HOUR, "Idle" if i % 2 else "Running", "Idle")
    for i in range(12):                                    # baseline rows, older than the score window
        iot(db, "TA", m.id, NOW - 90 * 60 * US * 1_000_000 - i * 60 * US * 1_000_000, "t", 700.0 + i)
        plc(db, "TA", m.id, NOW - 90 * 60 * US * 1_000_000 - i * 60 * US * 1_000_000, "p", 700.0 + i,
            device_id=dev.id)
    db.commit()
    capped = DT.load(db, "TA", m.id, now=NOW, row_cap=16)
    check("rows older than a window do not fill its row cap: nothing is truncated",
          capped.truncated is False, f"truncated={capped.truncated}")
    check("... the score window keeps all 8 of its rows, the baseline all 32 of its own",
          len(capped.score_readings) == 8 and len(capped.baseline_readings) == 32,
          f"{len(capped.score_readings)} {len(capped.baseline_readings)}")
    check("... and the one in-window status event is read with the older ones left to the lookback query",
          len(capped.events) == 1, str(capped.events))
    db.close()


def section_tenant_filter():
    print("\n[load: explicit tenant filter, with no ambient tenant bound]")
    check("no ambient tenant is bound in this test", tenancy.current_tenant() is None)
    db = session()
    a = machine(db, "TA", "CNC-01")
    dev = models.IndustrialDevice(tenant_code="TA", device_code="GW-1", device_name="gw")
    db.add(dev)
    db.commit()
    iot(db, "TA", a.id, NOW - 2 * HOUR, "t", 1.0)
    iot(db, "TB", a.id, NOW - 2 * HOUR, "t", 666.0)
    iot(db, "TB", a.id, NOW - 10 * 60 * US * 1_000_000, "t", 667.0)
    plc(db, "TA", a.id, NOW - 2 * HOUR, "p", 2.0, device_id=dev.id)
    plc(db, "TB", a.id, NOW - 2 * HOUR, "p", 777.0, device_id=dev.id)
    plc(db, "TB", a.id, NOW - 10 * 60 * US * 1_000_000, "p", 778.0, device_id=dev.id)
    event(db, "TA", a.id, NOW - 3 * DAY, "Running", "Idle")
    event(db, "TB", a.id, NOW - 2 * DAY, "Idle", "Running")
    # before the window: had it leaked, "Running" would make TA's initial state True instead of False
    event(db, "TB", a.id, NOW - 20 * DAY, "Running", "Idle")
    db.commit()
    load = DT.load(db, "TA", a.id, now=NOW)
    seen = values_of(load.baseline_readings) + values_of(load.score_readings)
    check("another tenant's telemetry rows never arrive", seen == [1.0, 2.0], str(seen))
    check("another tenant's in-window status events never arrive", len(load.events) == 1, str(load.events))
    check("another tenant's event before the window never sets the initial state (TA's own says Idle)",
          load.initial_running is False, f"initial={load.initial_running}")
    check("load refuses an empty tenant", _raises(ValueError, DT.load, db, "", a.id, now=NOW))
    db.close()


def section_state_timeline():
    print("\n[load: machine state from the event timeline and the event before the window]")
    db = session()
    a = machine(db, "TA", "M")
    event(db, "TA", a.id, NOW - 20 * DAY, "Running", "Idle")               # before the window: the initial state
    event(db, "TA", a.id, NOW - 400 * DAY, "Breakdown", "Running")         # beyond the bounded lookback: ignored
    event(db, "TA", a.id, NOW - 5 * DAY, "Idle", "Running")
    event(db, "TA", a.id, NOW - 30 * 60 * US * 1_000_000, "Running", "Idle")
    db.commit()
    load = DT.load(db, "TA", a.id, now=NOW)
    check("the last event before the baseline start sets the initial state", load.initial_running is True)
    check("in-window events are offsets with running flags, oldest first",
          [r for _, r in load.events] == [False, True], str(load.events))
    states = SR.bucket_states([SR.TOTAL_BUCKETS - 1, 6 * SR.BUCKETS_PER_DAY, 2 * SR.BUCKETS_PER_DAY, 0],
                              load.events, load.initial_running)
    check("buckets before, between and after the events get Running / NotRunning / Running",
          [states[k] for k in (SR.TOTAL_BUCKETS - 1, 6 * SR.BUCKETS_PER_DAY, 2 * SR.BUCKETS_PER_DAY, 0)]
          == [SR.STATE_RUNNING, SR.STATE_RUNNING, SR.STATE_NOT_RUNNING, SR.STATE_RUNNING], str(states))

    db2 = session()
    b = machine(db2, "TA", "N")
    event(db2, "TA", b.id, NOW - 5 * DAY, "Idle", "Running")
    db2.commit()
    check("with no event before the window, the first event's old status is the initial state",
          DT.load(db2, "TA", b.id, now=NOW).initial_running is True)
    p = machine(db2, "TA", "P")
    event(db2, "TA", p.id, NOW - 14 * DAY - DT.STATE_LOOKBACK_DAYS * DAY + HOUR, "Running", "Idle")
    q = machine(db2, "TA", "Q")
    event(db2, "TA", q.id, NOW - 14 * DAY - DT.STATE_LOOKBACK_DAYS * DAY - US, "Running", "Idle")
    db2.commit()
    check("an event inside the bounded state lookback before the window counts",
          DT.load(db2, "TA", p.id, now=NOW).initial_running is True)
    check("an event older than the lookback does not (the query is bounded below too)",
          DT.load(db2, "TA", q.id, now=NOW).initial_running is None)
    c = machine(db2, "TA", "O")
    check("with no events at all the state is unknown, not guessed", DT.load(db2, "TA", c.id, now=NOW).initial_running is None)
    event(db2, "TA", c.id, NOW - 5 * DAY, "Offline", "Down")
    db2.commit()
    check("a legacy status outside the vocabulary is unknown; Offline is not running",
          DT.load(db2, "TA", c.id, now=NOW).initial_running is None
          and [r for _, r in DT.load(db2, "TA", c.id, now=NOW).events] == [False])
    db.close()
    db2.close()


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    return False


# --------------------------------------------------------------------------- service order
def section_service_order():
    print("\n[service: tenant check before consent, consent before data]")
    db = session()
    a = machine(db, "TA", "CNC-01")
    b = machine(db, "TB", "CNC-01")
    loads = []
    original_load = DT.load

    def spy_load(*args, **kwargs):
        loads.append(args)
        return original_load(*args, **kwargs)

    DT.load = spy_load
    try:
        gate = SpyGate()
        try:
            service.score_machine(db, "TA", b.id, gate=gate, now=NOW)
            check("another tenant's machine raises MachineNotFound", False)
        except service.MachineNotFound:
            check("another tenant's machine raises MachineNotFound", True)
        check("... and the consent gate was NOT consulted", gate.calls == [], str(gate.calls))
        try:
            service.score_machine(db, "TA", 424242, gate=gate, now=NOW)
            check("a machine that does not exist raises MachineNotFound", False)
        except service.MachineNotFound:
            check("a machine that does not exist raises MachineNotFound", True)

        refusing = SpyGate(granted=False, reason="Learning from telemetry is off; an Admin can enable it")
        try:
            service.score_machine(db, "TA", a.id, gate=refusing, now=NOW)
            check("a refused gate raises ConsentRequired", False)
        except ConsentRequired as exc:
            check("a refused gate raises ConsentRequired carrying the gate's reason",
                  exc.decision.reason == "Learning from telemetry is off; an Admin can enable it"
                  and exc.decision.capability == CAPABILITY_TELEMETRY_BASELINE)
        check("the gate was asked about THIS tenant and the telemetry_baseline capability",
              refusing.calls == [("TA", CAPABILITY_TELEMETRY_BASELINE)], str(refusing.calls))
        check("no telemetry was read without consent", loads == [], str(loads))

        wrong = SpyGate(granted=True, capability="something_else")
        try:
            service.score_machine(db, "TA", a.id, gate=wrong, now=NOW)
            check("a grant for a different capability is a refusal", False)
        except ConsentRequired:
            check("a grant for a different capability is a refusal", True)
        check("still no telemetry read", loads == [])

        class NotAGate:
            pass

        check("an object that is not a ConsentGate is refused before anything runs",
              _raises(TypeError, service.score_machine, db, "TA", a.id, gate=NotAGate(), now=NOW))

        class BadGate:
            def check(self, db, tenant, capability):
                return True

        check("a gate that does not return a ConsentDecision is refused",
              _raises(TypeError, service.score_machine, db, "TA", a.id, gate=BadGate(), now=NOW))
        check("still no telemetry read", loads == [])

        granted = SpyGate()
        result = service.score_machine(db, "TA", a.id, gate=granted, now=NOW)
        check("with consent and no telemetry the answer is insufficient_history, score null",
              result["status"] == "insufficient_history" and result["score"] is None and result["have"]["scorable_signals"] == 0,
              str(result))
        check("telemetry is read only after consent", len(loads) == 1)
        check("the result names the machine, the windows and the evaluation status",
              result["machine_id"] == a.id and result["score_window"]["end"] == NOW.isoformat()
              and result["baseline_window"]["requested_start"] == (NOW - 14 * DAY).isoformat()
              and isinstance(result["evaluation"]["adopted"], bool) and "synthetic" in result["evaluation"]["caveat"],
              str(result))
    finally:
        DT.load = original_load
    db.close()


def section_evaluation_integrity():
    print("\n[service: the evaluation card is verified against its pinned hash]")
    card = service.evaluation_card()
    check("the committed evaluation verifies against the hash pinned in service.py",
          card["available"] is True, str(card))
    root = tempfile.mkdtemp(prefix="amp_anomaly_eval_")
    try:
        with open(service.EVAL_ARTIFACT_PATH, "rb") as fh:
            data = json.loads(fh.read().decode("utf-8"))
        tampered = dict(data, adoption=dict(data["adoption"], adopted=not data["adoption"]["adopted"]))
        tampered["sha256"] = payload_hash(tampered)
        path = os.path.join(root, "tampered.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(tampered, fh)
        bad = service.evaluation_card(path=path)
        check("a flipped adoption verdict with its embedded hash recomputed is unavailable",
              bad["available"] is False and "pinned" in bad["reason"], str(bad))

        other_method = dict(data, parameters=dict(data["parameters"], min_calibration_buckets=50))
        other_method["sha256"] = payload_hash(other_method)
        other_path = os.path.join(root, "other_method.json")
        with open(other_path, "w", encoding="utf-8") as fh:
            json.dump(other_method, fh)
        stale = service.evaluation_card(path=other_path, expected_sha256=other_method["sha256"])
        check("an evaluation of DIFFERENT method parameters is unavailable even when its hash is pinned",
              stale["available"] is False and "parameters" in stale["reason"], str(stale))
        same = dict(data)
        same_path = os.path.join(root, "same.json")
        with open(same_path, "w", encoding="utf-8") as fh:
            json.dump(same, fh, indent=1)
        check("... while the committed evaluation, re-indented, is available against the pin",
              service.evaluation_card(path=same_path)["available"] is True)

        db = session()
        a = machine(db, "TA", "M")
        original = service.EVAL_ARTIFACT_PATH
        service.EVAL_ARTIFACT_PATH = path
        try:
            result = service.score_machine(db, "TA", a.id, gate=SpyGate(), now=NOW)
        finally:
            service.EVAL_ARTIFACT_PATH = original
        check("score_machine answers model_unavailable instead of guessing", result["status"] == "model_unavailable"
              and result.get("score") is None, str(result))
        db.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- parity
def section_parity():
    print("\n[parity: a synthetic machine through the database scores as it does in memory]")
    series = next(SY.generate_dataset(3, "db-parity", n_series=1, windows_per_series=1, min_history_days=4,
                                      max_history_days=4, anomaly_fraction=0.0))
    anchor_minute = series.windows[0].end_minute
    first_minute = max(0, anchor_minute - SR.TOTAL_BUCKETS * 5)
    now = NOW

    def ts(minute):
        return now - timedelta(minutes=anchor_minute - minute)

    db = session()
    a = machine(db, "TA", "SYN-1")
    rows = []
    for name, values in series.signals.items():
        for minute in range(first_minute, anchor_minute):
            if values[minute] is not None:
                rows.append({"tenant_code": "TA", "machine_id": a.id, "signal_name": name,
                             "signal_value": repr(values[minute]), "numeric_value": int(values[minute]),
                             "source": "MQTT", "created_at": ts(minute)})
    db.execute(models.IoTTelemetry.__table__.insert(), rows)
    transitions = SY.state_transitions(series)
    for minute, running in transitions:
        if minute < anchor_minute:
            event(db, "TA", a.id, ts(minute), SY.status_of(running), None if minute == 0 else SY.status_of(not running))
    db.commit()

    result = service.score_machine(db, "TA", a.id, gate=SpyGate(), now=now)
    us_per_minute = 60 * 1_000_000
    base_readings, score_readings = [], []
    for name, values in series.signals.items():
        for minute in range(first_minute, anchor_minute):
            if values[minute] is None:
                continue
            reading = ((anchor_minute - minute) * us_per_minute, "iot:" + name, values[minute])
            (score_readings if anchor_minute - minute <= 60 else base_readings).append(reading)
    events = [((anchor_minute - m) * us_per_minute, r) for m, r in transitions if m < anchor_minute]
    in_memory = B.assess(
        SR.build_buckets(base_readings, events, None, first_k=SR.SCORE_BUCKETS, last_k=SR.TOTAL_BUCKETS - 1),
        SR.build_buckets(score_readings, events, None, first_k=0, last_k=SR.SCORE_BUCKETS - 1),
        integer_signals=SR.integer_valued_signals(base_readings))
    check("the machine is scored through the database", result["status"] in ("ok", "insufficient_history"), str(result))
    check("database path == in-memory path (score, deviations, signals, method)",
          all(result[key] == in_memory[key] for key in ("status", "score", "score_resolution", "state", "method",
                                                          "signals_used", "deviating", "have")),
          f"db={ {k: result.get(k) for k in ('status', 'score', 'method', 'have')} } "
          f"mem={ {k: in_memory.get(k) for k in ('status', 'score', 'method', 'have')} }")
    check("the synthetic machine has enough history to be scored", result["status"] == "ok", str(result.get("have")))
    check("signals are namespaced iot:<name>", all(s.startswith("iot:") for s in result["signals_used"]))
    check("nothing was written by scoring", db.query(models.IoTTelemetry).count() == len(rows)
          and db.query(models.MachineEvent).count() == sum(1 for m, _ in transitions if m < anchor_minute))
    db.close()


SECTIONS = [section_windows, section_sources_and_bounds, section_simulated_and_truncation, section_tenant_filter,
            section_state_timeline, section_service_order, section_evaluation_integrity, section_parity]


def main():
    print("=" * 74)
    print("Telemetry anomaly: database path and service")
    print("=" * 74)
    for section in SECTIONS:
        section()
    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


def test_amp_ai_anomaly_db():
    """pytest entry point; CI runs this file as a standalone script."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
