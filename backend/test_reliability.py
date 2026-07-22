"""Machine reliability read-model tests (ADR-0007).

Turns downtime_logs into MTBF / MTTR / availability per machine, ranks the fleet
least-reliable first, names the reliability bottleneck, and Paretos failure modes
by repair time. Run:  python backend/test_reliability.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import reliability


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _machine(db, id_, name, line="SMT"):
    db.add(models.Machine(id=id_, name=name, status="Running", utilization=90, line=line))


def _log(db, machine_id, duration, reason="Breakdown", days_ago=1):
    db.add(models.DowntimeLog(
        machine_id=machine_id, reason=reason, duration=duration,
        created_at=datetime.utcnow() - timedelta(days=days_ago),
    ))


def test_reliability_computes_mtbf_mttr_and_ranks_worst_first():
    db = _fresh_session()
    _machine(db, 1, "SMT-Reflow-01", "SMT")
    _machine(db, 2, "IC-Test-01", "IC")
    _machine(db, 3, "SMT-Printer-01", "SMT")   # never fails -> perfectly reliable
    # Machine 1: three failures totalling 300 min.
    _log(db, 1, "120 min", "Jam", days_ago=2)
    _log(db, 1, "120 min", "Breakdown", days_ago=5)
    _log(db, 1, "60 min", "Jam", days_ago=9)
    # Machine 2: one failure, 30 min.
    _log(db, 2, "30 min", "Calibration", days_ago=1)
    db.commit()

    r = reliability.build_reliability_summary(db, "DEFAULT")
    assert r["days"] == 30
    assert r["machines_tracked"] == 3
    assert r["total_failures"] == 4
    assert r["total_repair_minutes"] == 330
    # fleet MTTR = 330 / 4 = 82.5 min
    assert r["mttr_minutes"] == 82.5

    by = {m["name"]: m for m in r["by_machine"]}
    m1 = by["SMT-Reflow-01"]
    assert m1["failures"] == 3 and m1["repair_minutes"] == 300
    assert m1["mttr_minutes"] == 100.0             # 300 / 3
    # MTBF: operating = 30*24*60 - 300 = 43200 - 300 = 42900 min; /3 /60 = 238.3 h
    assert m1["mtbf_hours"] == 238.3
    # availability = 42900 / 43200 * 100 = 99.3%
    assert m1["availability"] == 99.3

    # least reliable first: machine 1 (3 failures) then machine 2 (1) then machine 3 (0)
    assert [m["name"] for m in r["by_machine"]] == ["SMT-Reflow-01", "IC-Test-01", "SMT-Printer-01"]
    # perfectly reliable machine: no failures -> MTBF undefined, availability 100
    m3 = by["SMT-Printer-01"]
    assert m3["failures"] == 0 and m3["mtbf_hours"] is None and m3["availability"] == 100.0

    # bottleneck is the least-reliable machine that actually failed
    assert r["bottleneck"]["name"] == "SMT-Reflow-01"

    # failure-mode Pareto by repair time: Jam (180) leads Breakdown (120), Calibration (30)
    modes = {m["reason"]: m for m in r["top_modes"]}
    assert modes["Jam"]["minutes"] == 180 and modes["Jam"]["count"] == 2
    assert r["top_modes"][0]["reason"] == "Jam"


def test_reliability_is_empty_safe_and_windows_out_old_failures():
    db = _fresh_session()
    _machine(db, 1, "SMT-Reflow-01")
    # A stoppage 40 days ago is outside the 30-day window and must not count.
    _log(db, 1, "500 min", "Breakdown", days_ago=40)
    db.commit()
    r = reliability.build_reliability_summary(db, "DEFAULT")
    assert r["total_failures"] == 0 and r["bottleneck"] is None
    assert r["by_machine"][0]["mtbf_hours"] is None       # no in-window failures
    assert r["availability"] == 100.0

    # no machines, no logs -> zeros, no divide-by-zero, availability defaults to 100
    empty = reliability.build_reliability_summary(_fresh_session(), "DEFAULT")
    assert empty["total_failures"] == 0 and empty["mttr_minutes"] == 0.0
    assert empty["mtbf_hours"] is None and empty["by_machine"] == []
    assert empty["availability"] == 100.0


if __name__ == "__main__":
    test_reliability_computes_mtbf_mttr_and_ranks_worst_first()
    test_reliability_is_empty_safe_and_windows_out_old_failures()
    print("RELIABILITY OK: MTBF/MTTR/availability per machine; least-reliable-first ranking; "
          "bottleneck named; failure-mode Pareto by repair time; 30-day window; empty-safe")
