"""Downtime summary read-model tests (ADR-0007).

Fleet downtime over the last 7 days: total events and minutes lost, top reasons
and worst machines (ranked by minutes down, with names), a per-line rollup, and a
7-entry daily series carrying counts and minutes. Out-of-window events excluded;
durations parsed via the shared helper ("2 hrs 15 min" -> 135) so the summary and
its reason drill-down reconcile.

Run:  python backend/test_downtime.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import downtime


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _dt(machine_id, reason, when):
    return models.DowntimeLog(machine_id=machine_id, reason=reason, duration="30 min", created_at=when)


def test_downtime_summary_rolls_up_reasons_machines_and_days():
    db = _fresh_session()
    now = datetime.utcnow()
    db.add(models.Machine(id=1, name="PRESS-01", status="Running", utilization=60, line="SMT"))
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=60, line="IC"))
    db.add_all([
        _dt(1, "Breakdown", now),
        _dt(1, "Breakdown", now - timedelta(days=1)),
        _dt(1, "Tooling", now - timedelta(days=2)),
        _dt(2, "Breakdown", now),
        _dt(2, "Changeover", now - timedelta(days=3)),
        _dt(1, "Breakdown", now - timedelta(days=9)),   # outside the 7-day window
    ])
    db.commit()

    s = downtime.build_downtime_summary(db, "DEFAULT")
    assert s["total_events"] == 5                                    # 9-days-ago excluded
    # total minutes lost: 5 in-window events x 30 min each = 150 (9-days-ago excluded)
    assert s["total_minutes"] == 150
    # per-line rollup with minutes: IC (CNC-02) 2 events / 60 min, SMT (PRESS-01) 3 / 90;
    # sorted by line name
    assert s["by_line"] == [{"line": "IC", "count": 2, "minutes": 60},
                            {"line": "SMT", "count": 3, "minutes": 90}]
    # Pareto ranked by minutes: Breakdown (3 events / 90 min) leads; the two 30-min
    # singletons tie on minutes+events, so they order by name (Changeover < Tooling).
    assert s["top_reasons"][0] == {"reason": "Breakdown", "count": 3, "minutes": 90}
    assert [r["reason"] for r in s["top_reasons"]] == ["Breakdown", "Changeover", "Tooling"]
    # worst machine by time lost: PRESS-01 (90 min) before CNC-02 (60 min), names resolved
    assert s["by_machine"][0]["name"] == "PRESS-01"
    assert s["by_machine"][0]["count"] == 3 and s["by_machine"][0]["minutes"] == 90
    assert s["by_machine"][1]["name"] == "CNC-02"
    assert s["by_machine"][1]["count"] == 2 and s["by_machine"][1]["minutes"] == 60
    # daily series: 7 entries oldest->newest, today has 2 events / 60 min (one per machine)
    assert len(s["daily"]) == 7 and s["daily"][-1]["count"] == 2 and s["daily"][-1]["minutes"] == 60
    assert sum(e["count"] for e in s["daily"]) == 5
    # denominator reconciliation: the daily minutes sum to the headline total
    assert sum(e["minutes"] for e in s["daily"]) == s["total_minutes"]

    # empty factory -> zeros, no crash
    empty = downtime.build_downtime_summary(_fresh_session(), "DEFAULT")
    assert empty["total_events"] == 0 and empty["total_minutes"] == 0
    assert empty["top_reasons"] == [] and len(empty["daily"]) == 7
    assert all(e["count"] == 0 and e["minutes"] == 0 for e in empty["daily"])


def test_downtime_summary_ranks_by_minutes_lost_not_event_count():
    """The honest impact fix: one long stoppage must outrank many trivial ones, and
    an hour-format duration must parse as 135 minutes, not 2 (the '2 hrs' -> 2-min
    class of bug). Also pins the SQL window bound and denominator reconciliation."""
    db = _fresh_session()
    now = datetime.utcnow()
    db.add(models.Machine(id=1, name="PRESS-01", status="Running", utilization=60, line="SMT"))
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=60, line="IC"))
    db.add_all([
        # PRESS-01: three trivial micro-stops (more EVENTS, little time)
        models.DowntimeLog(machine_id=1, reason="Micro-stop", duration="5 min", created_at=now),
        models.DowntimeLog(machine_id=1, reason="Micro-stop", duration="5 min", created_at=now - timedelta(days=1)),
        models.DowntimeLog(machine_id=1, reason="Micro-stop", duration="5 min", created_at=now - timedelta(days=2)),
        # CNC-02: one long breakdown (fewer EVENTS, most time) — hour-format string
        models.DowntimeLog(machine_id=2, reason="Breakdown", duration="2 hrs 15 min", created_at=now),
        # far outside the 7-day window: must be excluded by the SQL bound, not just Python
        models.DowntimeLog(machine_id=1, reason="Breakdown", duration="500 min", created_at=now - timedelta(days=60)),
    ])
    db.commit()

    s = downtime.build_downtime_summary(db, "DEFAULT")
    assert s["total_events"] == 4                                    # 60-days-ago row excluded
    # hour-format parsing: 3x5 + 135 = 150 (NOT 3x5 + 2 from a leading-digit regex)
    assert s["total_minutes"] == 150
    # RANKED BY MINUTES, NOT COUNT: CNC-02 has 1 event / 135 min, PRESS-01 has 3 / 15.
    # An event-count ranking would wrongly put PRESS-01 first.
    assert s["by_machine"][0]["name"] == "CNC-02"
    assert s["by_machine"][0]["count"] == 1 and s["by_machine"][0]["minutes"] == 135
    assert s["by_machine"][1]["name"] == "PRESS-01"
    assert s["by_machine"][1]["count"] == 3 and s["by_machine"][1]["minutes"] == 15
    # reasons likewise ranked by time lost: Breakdown (135) leads Micro-stop (15)
    assert [r["reason"] for r in s["top_reasons"]] == ["Breakdown", "Micro-stop"]
    assert s["top_reasons"][0] == {"reason": "Breakdown", "count": 1, "minutes": 135}
    # denominator reconciliation: every in-window log has a machine, so the per-machine
    # minutes and counts sum to the headline totals
    assert sum(m["minutes"] for m in s["by_machine"]) == s["total_minutes"]
    assert sum(m["count"] for m in s["by_machine"]) == s["total_events"]
    # per-line: IC (CNC-02) 135 min, SMT (PRESS-01) 15 min; sorted by line name
    assert s["by_line"] == [{"line": "IC", "count": 1, "minutes": 135},
                            {"line": "SMT", "count": 3, "minutes": 15}]


def test_downtime_reason_drilldown_totals_minutes_machines_and_instances():
    db = _fresh_session()
    now = datetime.utcnow()
    db.add(models.Machine(id=1, name="PRESS-01", status="Running", utilization=60))
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=60))
    db.add_all([
        models.DowntimeLog(machine_id=1, reason="Breakdown", duration="120 min", created_at=now),
        models.DowntimeLog(machine_id=1, reason="Breakdown", duration="30 min", created_at=now - timedelta(days=1)),
        models.DowntimeLog(machine_id=2, reason="Breakdown", duration="45 min", created_at=now - timedelta(days=2)),
        models.DowntimeLog(machine_id=1, reason="Tooling", duration="15 min", created_at=now),           # other reason
        models.DowntimeLog(machine_id=1, reason="Breakdown", duration="99 min", created_at=now - timedelta(days=9)),  # out of window
    ])
    db.commit()

    r = downtime.build_downtime_reason(db, "DEFAULT", "Breakdown")
    assert r["reason"] == "Breakdown"
    assert r["total_events"] == 3                                    # 9-days-ago excluded, Tooling excluded
    assert r["total_minutes"] == 195                                 # 120 + 30 + 45, minutes parsed from strings
    # PRESS-01 leads: 2 events / 150 min; CNC-02: 1 event / 45 min
    assert r["by_machine"][0]["name"] == "PRESS-01" and r["by_machine"][0]["count"] == 2
    assert r["by_machine"][0]["minutes"] == 150
    assert r["by_machine"][1]["name"] == "CNC-02" and r["by_machine"][1]["minutes"] == 45
    assert len(r["daily"]) == 7 and r["daily"][-1]["count"] == 1     # today: one Breakdown
    assert len(r["instances"]) == 3                                  # most-recent first (by time)
    assert r["instances"][0]["minutes"] == 120 and r["instances"][0]["machine"] == "PRESS-01"  # today's
    assert r["instances"][-1]["minutes"] == 45                       # oldest in window (2 days ago)

    # a reason with no events in the window -> zeroed, no crash
    none = downtime.build_downtime_reason(db, "DEFAULT", "Nonexistent")
    assert none["total_events"] == 0 and none["total_minutes"] == 0 and none["instances"] == []


def test_downtime_reason_drilldown_windowed_reconciled_and_hour_format():
    """Pins the drill-down's SQL window bound, denominator reconciliation, and
    hour-format duration parsing (guards the '2 hrs' -> 2-minutes class of bug)."""
    db = _fresh_session()
    now = datetime.utcnow()
    db.add(models.Machine(id=1, name="PRESS-01", status="Running", utilization=60))
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=60))
    db.add_all([
        # in window, hour-format durations: 2 hrs 15 min -> 135, 1 hr -> 60, 90 min -> 90
        models.DowntimeLog(machine_id=1, reason="Breakdown", duration="2 hrs 15 min", created_at=now),
        models.DowntimeLog(machine_id=1, reason="Breakdown", duration="1 hr", created_at=now - timedelta(days=1)),
        models.DowntimeLog(machine_id=2, reason="Breakdown", duration="90 min", created_at=now - timedelta(days=3)),
        # far outside the 7-day window: must be excluded by the SQL bound, not just Python
        models.DowntimeLog(machine_id=1, reason="Breakdown", duration="500 min", created_at=now - timedelta(days=60)),
    ])
    db.commit()

    r = downtime.build_downtime_reason(db, "DEFAULT", "Breakdown")
    assert r["total_events"] == 3                       # 60-days-ago row excluded by the window bound
    # hour-format parsing: 135 + 60 + 90 = 285 (NOT 2 + 1 + 90 from a leading-digit regex)
    assert r["total_minutes"] == 285
    # denominator reconciliation: per-machine minutes must sum to the headline total
    assert sum(m["minutes"] for m in r["by_machine"]) == r["total_minutes"]
    assert sum(m["count"] for m in r["by_machine"]) == r["total_events"]
    # PRESS-01: 135 + 60 = 195 over 2 events; CNC-02: 90 over 1
    assert r["by_machine"][0]["name"] == "PRESS-01" and r["by_machine"][0]["minutes"] == 195
    assert r["by_machine"][1]["name"] == "CNC-02" and r["by_machine"][1]["minutes"] == 90


if __name__ == "__main__":
    test_downtime_summary_rolls_up_reasons_machines_and_days()
    test_downtime_summary_ranks_by_minutes_lost_not_event_count()
    test_downtime_reason_drilldown_totals_minutes_machines_and_instances()
    test_downtime_reason_drilldown_windowed_reconciled_and_hour_format()
    print("DOWNTIME OK: total events + minutes lost + Pareto reasons + worst machines (ranked by time down, named) "
          "+ 7-day series; minutes ranking (not count); hour-format parsing; reconciled; windowed; empty-safe; "
          "reason drill-down (minutes lost, machines, instances); drill-down SQL-windowed + reconciled + hour-format")
