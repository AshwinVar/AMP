"""Tenant isolation for AMP-native AI: nothing read, scored or learned for A reaches B (ADR-0020).

THE FIXTURE IS BUILT TO LEAK
----------------------------
Two factories, TA and TB, each with a machine called PRESS-01 and near-identical
data. TB's rows carry unmistakable values (telemetry around 9000, downtime reason
"TB-SECRET") and some of TB's rows are deliberately stamped with TA's MACHINE ID,
so a loader that relied on "the machine belongs to TA" instead of an explicit
tenant filter would pick them up. Every load runs with NO ambient tenant bound,
so the ADR-0002 session hook cannot hide a missing filter.

  1. TELEMETRY     TA's anomaly inputs contain TA's readings and events only.
  2. HISTORIES     TA's failure-risk histories contain TA's machines and records only.
  3. SCORING       predict() is a pure function: the artifact file and the loaded
                   dict are unchanged by scoring, and TB's result does not depend on
                   whether TA was scored first.
  4. BASELINES     a baseline fitted for TA is never used for TB: TB is refused
                   without TB's consent immediately after TA was scored, and with
                   consent TB's result equals scoring TB in a database that never
                   contained TA at all.

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_integration_isolation.py
"""
import copy
import hashlib
import sys
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
from amp_ai import consent as C
from amp_ai.core.contracts import CAPABILITY_TELEMETRY_BASELINE, ConsentRequired
from amp_ai.failure_risk import db_history, predict
from amp_ai.telemetry_anomaly import db_telemetry as DT
from amp_ai.telemetry_anomaly import service
from amp_ai.telemetry_anomaly import synthetic as SY
from database import Base

CAP = CAPABILITY_TELEMETRY_BASELINE
NOW = datetime(2026, 3, 10, 12, 0, 0)
TA_SEED, TB_SEED = 13, 17
failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    return sessionmaker(bind=engine)()


def synthetic_rows(tenant, machine_id, seed, offset=0.0):
    """Four days of one synthetic machine's telemetry ending at NOW, shifted by `offset`."""
    series = next(SY.generate_dataset(seed, "isolation", n_series=1, windows_per_series=1, min_history_days=4,
                                      max_history_days=4, anomaly_fraction=0.0))
    anchor = series.windows[0].end_minute
    first = max(0, anchor - 4 * 24 * 60)

    def ts(minute):
        return NOW - timedelta(minutes=anchor - minute)

    rows = []
    for name, values in series.signals.items():
        for minute in range(first, anchor):
            if values[minute] is not None:
                v = values[minute] + offset
                rows.append({"tenant_code": tenant, "machine_id": machine_id, "signal_name": name,
                             "signal_value": repr(v), "numeric_value": int(v), "source": "MQTT",
                             "created_at": ts(minute)})
    events = []
    for minute, running in SY.state_transitions(series):
        if first <= minute < anchor:
            events.append(models.MachineEvent(tenant_code=tenant, machine_id=machine_id, machine_name="PRESS-01",
                                              old_status=SY.status_of(not running),
                                              new_status=SY.status_of(running), created_at=ts(minute)))
    return rows, events


def seed_tenant(db, tenant, seed, offset, marker):
    m = models.Machine(tenant_code=tenant, site="P1", name="PRESS-01", status="Running", utilization=70)
    db.add(m)
    db.flush()
    rows, events = synthetic_rows(tenant, m.id, seed, offset)
    db.execute(models.IoTTelemetry.__table__.insert(), rows)
    db.add_all(events)
    db.add(models.DowntimeLog(tenant_code=tenant, machine_id=m.id, reason=marker, duration="30 min",
                              created_at=NOW - timedelta(days=2)))
    db.add(models.ProductionRecord(tenant_code=tenant, machine_id=m.id, planned_minutes=480, runtime_minutes=400,
                                   ideal_cycle_time_seconds=30, total_count=500, good_count=480,
                                   rejected_count=20, created_at=NOW - timedelta(days=2)))
    db.commit()
    return m.id, len(rows)


def plant_leaks(db, into_machine_id):
    """TB-stamped rows that point at TA's machine id: only an explicit tenant filter keeps them out."""
    db.execute(models.IoTTelemetry.__table__.insert(), [
        {"tenant_code": "TB", "machine_id": into_machine_id, "signal_name": "leak", "signal_value": "99999",
         "numeric_value": 99999, "source": "MQTT", "created_at": NOW - timedelta(minutes=m)}
        for m in range(1, 200, 7)])
    db.add(models.IndustrialSignal(tenant_code="TB", machine_id=into_machine_id, signal_name="leakplc",
                                   signal_value="99999", numeric_value=99999, quality="Good",
                                   created_at=NOW - timedelta(minutes=5)))
    db.add(models.MachineEvent(tenant_code="TB", machine_id=into_machine_id, machine_name="PRESS-01",
                               old_status="Running", new_status="Breakdown", created_at=NOW - timedelta(minutes=30)))
    db.add(models.DowntimeLog(tenant_code="TB", machine_id=into_machine_id, reason="TB-SECRET-ON-TA-ID",
                              duration="99 min", created_at=NOW - timedelta(days=1)))
    db.add(models.ProductionRecord(tenant_code="TB", machine_id=into_machine_id, planned_minutes=1,
                                   runtime_minutes=1, ideal_cycle_time_seconds=1, total_count=77777,
                                   good_count=1, rejected_count=77776, created_at=NOW - timedelta(days=1)))
    db.commit()


def build():
    db = session()
    tok = tenancy.set_current_tenant(None)
    # Seeds chosen so each synthetic machine's last hour falls in a state it has
    # enough history for within four days (seeds 11 and 12 gave day-shift machines
    # whose score hour had < 288 fit buckets, so both came back
    # insufficient_history and section 4 could not see a leak either way).
    ta, n_ta = seed_tenant(db, "TA", seed=TA_SEED, offset=0.0, marker="TA-JAM")
    tb, n_tb = seed_tenant(db, "TB", seed=TB_SEED, offset=9000.0, marker="TB-SECRET")
    plant_leaks(db, ta)
    tenancy.reset_current_tenant(tok)
    return db, ta, tb, n_ta, n_tb


def section_telemetry(db, ta, n_ta):
    print("=" * 74)
    print("1. TA'S ANOMALY INPUTS ARE TA'S ALONE (no ambient tenant)")
    print("=" * 74)
    check("CONTROL: no tenant is bound", tenancy.current_tenant() is None)
    load = DT.load(db, "TA", ta, now=NOW)
    values = [v for _, _, v in load.baseline_readings + load.score_readings]
    names = {n for _, n, _ in load.baseline_readings + load.score_readings}
    check("some readings were loaded", len(values) > 1000, str(len(values)))
    check("no TB value (>= 9000) reached TA's inputs", max(values) < 5000, str(max(values)))
    check("no TB signal planted on TA's machine id arrived", not ({"iot:leak", "plc:leakplc"} & names), str(names))
    check("exactly TA's own rows were read", len(values) == n_ta, f"{len(values)} vs {n_ta}")
    own_events = db.query(models.MachineEvent).filter(
        models.MachineEvent.tenant_code == "TA", models.MachineEvent.machine_id == ta).count()
    check("TB's Breakdown event on TA's machine id is not in TA's timeline",
          own_events > 0 and len(load.events) == own_events, f"{len(load.events)} vs {own_events}")


def section_histories(db, ta, tb):
    print()
    print("=" * 74)
    print("2. TA'S FAILURE-RISK HISTORIES ARE TA'S ALONE (no ambient tenant)")
    print("=" * 74)
    histories = db_history.load_histories(db, "TA", NOW)
    check("one history, TA's machine", [h.machine_id for h in histories] == [ta], str([h.machine_id for h in histories]))
    reasons = [r for h in histories for _, r, _ in h.downtime]
    check("TA's downtime is there", "TA-JAM" in reasons, str(reasons))
    check("TB's downtime is not, even the row stamped with TA's machine id",
          not any("TB-SECRET" in str(r) for r in reasons), str(reasons))
    totals = [row[4] for h in histories for row in h.production]
    check("TB's production row on TA's machine id is not there", 77777 not in totals, str(totals))
    new_statuses = [e[2] for h in histories for e in h.events]
    check("TB's Breakdown event on TA's machine id is not there", "Breakdown" not in new_statuses,
          str(new_statuses[-5:]))
    hb = db_history.load_histories(db, "TB", NOW)
    check("CONTROL: TB's own history has TB's machine and TB's reason",
          [h.machine_id for h in hb] == [tb] and "TB-SECRET" in [r for h in hb for _, r, _ in h.downtime])


def section_scoring_is_pure(db):
    print()
    print("=" * 74)
    print("3. SCORING LEARNS NOTHING AND REMEMBERS NOTHING")
    print("=" * 74)
    with open(predict.ARTIFACT_PATH, "rb") as fh:
        before_bytes = hashlib.sha256(fh.read()).hexdigest()
    artifact, _model = predict.load_model()
    snapshot = copy.deepcopy(artifact)
    ha = db_history.load_histories(db, "TA", NOW)
    hb = db_history.load_histories(db, "TB", NOW)
    b_alone = predict.predict(copy.deepcopy(hb), NOW)
    predict.predict(ha, NOW)
    b_after_a = predict.predict(copy.deepcopy(hb), NOW)
    check("TB's scores are identical whether or not TA was scored first", b_alone == b_after_a,
          f"{b_alone.get('machines')} vs {b_after_a.get('machines')}")
    check("CONTROL: TB was actually scored", b_alone.get("status") == "ok" and b_alone.get("machines"),
          str(b_alone)[:200])
    with open(predict.ARTIFACT_PATH, "rb") as fh:
        check("the artifact file is byte-identical after scoring both tenants",
              hashlib.sha256(fh.read()).hexdigest() == before_bytes)
    artifact_after, _ = predict.load_model()
    check("the loaded artifact is unchanged", artifact_after == snapshot)
    check("every scored machine says its data was not learned from",
          all(m["data_basis"]["learned_from"] is False for m in b_alone["machines"]))


def section_baselines(db, ta, tb):
    print()
    print("=" * 74)
    print("4. A BASELINE FITTED FOR TA IS NEVER USED FOR TB")
    print("=" * 74)
    gate = C.DbConsentGate()
    C.set_consent(db, "TA", CAP, True, "ta-admin")
    a = service.score_machine(db, "TA", ta, gate=gate, now=NOW)
    check("CONTROL: TA is scored from its own history", a.get("status") == "ok", str(a.get("have")))
    refused = None
    try:
        service.score_machine(db, "TB", tb, gate=gate, now=NOW)
    except ConsentRequired as e:
        refused = e
    check("TB, immediately after TA, without TB's consent -> ConsentRequired", refused is not None)
    raised = None
    try:
        service.score_machine(db, "TB", ta, gate=gate, now=NOW)
    except service.MachineNotFound as e:
        raised = e
    check("TB asking for TA's machine id -> MachineNotFound", raised is not None)

    C.set_consent(db, "TB", CAP, True, "tb-admin")
    b_shared = service.score_machine(db, "TB", tb, gate=gate, now=NOW)

    solo = session()
    tok = tenancy.set_current_tenant(None)
    try:
        m = models.Machine(id=tb, tenant_code="TB", site="P1", name="PRESS-01", status="Running", utilization=70)
        solo.add(m)
        solo.flush()
        rows, events = synthetic_rows("TB", tb, seed=TB_SEED, offset=9000.0)
        solo.execute(models.IoTTelemetry.__table__.insert(), rows)
        solo.add_all(events)
        solo.commit()
        C.set_consent(solo, "TB", CAP, True, "tb-admin")
        b_solo = service.score_machine(solo, "TB", tb, gate=C.DbConsentGate(), now=NOW)
    finally:
        tenancy.reset_current_tenant(tok)
        solo.close()
    keys = ("status", "score", "score_resolution", "state", "method", "signals_used", "deviating", "have",
            "baseline_window", "score_window", "truncated")
    check("CONTROL: TB is scored", b_shared.get("status") == "ok", str(b_shared.get("have")))
    check("TB's result in the shared database == TB's result in a database that never held TA",
          all(b_shared.get(k) == b_solo.get(k) for k in keys),
          str({k: (b_shared.get(k), b_solo.get(k)) for k in keys if b_shared.get(k) != b_solo.get(k)})[:500])
    check("TA's and TB's results differ (the fixture can tell them apart)",
          a.get("score") != b_shared.get("score") or a.get("signals_used") != b_shared.get("signals_used")
          or a.get("deviating") != b_shared.get("deviating"), "identical results; the test cannot see a leak")


def main_():
    db, ta, tb, n_ta, n_tb = build()
    try:
        section_telemetry(db, ta, n_ta)
        section_histories(db, ta, tb)
        section_scoring_is_pure(db)
        section_baselines(db, ta, tb)
    finally:
        db.close()
    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def test_amp_ai_integration_isolation():
    assert main_() == 0, failures


if __name__ == "__main__":
    sys.exit(main_())
