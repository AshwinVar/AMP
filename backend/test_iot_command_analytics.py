"""IoT command-centre analytics endpoint tests (/analytics/iot-command).

The endpoint reports the live telemetry surface: a headcount of machines and of
signals ingested, how many machines have reported, and the latest value of each
(machine, signal) pair with its machine name resolved. Things it must get right,
pinned here because the route test only covers registration:

  * The "signals" and "live_machines" headline counts are TRUE totals, not the
    length of the last-300 display window. iot_telemetry is a never-pruned
    growing table, so once it passes 300 rows the old `len(telemetry)` froze at
    300 (the .limit() cap leaking into the displayed KPI) and `len(set(...))`
    counted only reporters inside that window — both understated while the
    sibling "machines" stayed a true roster total. They're now bounded SQL
    COUNTs over the full (tenant-scoped) table, reconciling the basis.
  * Machine names are resolved from ONE roster lookup, not a per-signal query
    inside the loop — that inner query was an N+1 on the (bounded but repeatedly
    scanned) latest-signal set, up to 300 point lookups per call, the same
    anti-pattern the maintenance / production-schedule rollups (#273) and the
    purchasing analytics (#276) already dropped.
  * The "latest" of each (machine, signal) pair is the newest by id, and an
    unknown/foreign machine_id still resolves to the honest "Machine {id}" label.

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_iot_command_analytics.py
"""
import inspect

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from analytics_routes import get_iot_command_center

USER = {"tenant": "DEFAULT"}


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _tel(machine_id, name, value, numeric, unit="", source="iot"):
    return models.IoTTelemetry(
        machine_id=machine_id, signal_name=name, signal_value=value,
        numeric_value=numeric, unit=unit, source=source,
    )


def test_latest_per_signal_names_resolved_and_counts():
    db = _fresh_session()
    db.add(models.Machine(id=1, name="CNC-01", status="Running", utilization=70))
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=70))
    # Insert oldest-first so the LAST temperature row (30) is the newest by id;
    # the endpoint dedups to the newest per (machine, signal).
    db.add_all([
        _tel(1, "temperature", "20", 20, "C"),
        _tel(1, "temperature", "30", 30, "C"),            # newer -> wins for (1, temperature)
        _tel(1, "pressure", "5", 5, "bar"),
        _tel(2, "temperature", "40", 40, "C"),
        # a signal pointing at a machine row that doesn't exist -> "Machine {id}" label
        _tel(99, "temperature", "50", 50, "C"),
    ])
    db.commit()

    out = get_iot_command_center(db=db, current_user=USER)

    # headcounts: 2 machines in the roster, 5 telemetry rows ingested, 3 distinct
    # reporting machines (1, 2, 99 — 99 has no Machine row but still reported).
    assert out["machines"] == 2, out            # only two Machine rows exist
    assert out["signals"] == 5, out
    assert out["live_machines"] == 3, out

    # one row per distinct (machine, signal): (1,temperature) (1,pressure)
    # (2,temperature) (99,temperature) = 4
    latest = out["latest_signals"]
    assert len(latest) == 4, latest

    by_key = {(r["machine_id"], r["signal_name"]): r for r in latest}
    # newest temperature on machine 1 is 30, not the earlier 20
    assert by_key[(1, "temperature")]["numeric_value"] == 30, by_key
    assert by_key[(1, "temperature")]["signal_value"] == "30", by_key
    # names resolved from the roster
    assert by_key[(1, "temperature")]["machine_name"] == "CNC-01", by_key
    assert by_key[(1, "pressure")]["machine_name"] == "CNC-01", by_key
    assert by_key[(2, "temperature")]["machine_name"] == "CNC-02", by_key
    # unknown machine_id falls back to the honest label, not a crash
    assert by_key[(99, "temperature")]["machine_name"] == "Machine 99", by_key
    print("PASS iot-command: latest-per-signal, names resolved, counts (2 machines / 5 signals / 3 live)")


def test_names_resolved_without_a_per_row_query():
    # The per-signal Machine query inside the loop was an N+1. Prove it's gone two
    # ways: (a) the loop source carries no Machine query, and (b) at runtime the
    # endpoint issues exactly ONE SELECT against the machines table regardless of
    # how many distinct signals it renders.
    src = inspect.getsource(get_iot_command_center)
    loop_body = src.split("for row in latest.values():", 1)[1]
    assert "db.query(models.Machine)" not in loop_body, \
        "machine names must come from one lookup, not a per-signal query in the loop"
    assert "machine_names" in src, "expected a single machine-name lookup dict"

    db = _fresh_session()
    db.add(models.Machine(id=1, name="CNC-01", status="Running", utilization=70))
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=70))
    # 6 distinct (machine, signal) pairs -> the old code fired 6 Machine queries.
    db.add_all([
        _tel(1, "temperature", "30", 30),
        _tel(1, "pressure", "5", 5),
        _tel(1, "spindle", "1200", 1200),
        _tel(2, "temperature", "40", 40),
        _tel(2, "pressure", "6", 6),
        _tel(2, "vibration", "3", 3),
    ])
    db.commit()

    machine_selects = []

    @event.listens_for(db.get_bind(), "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        low = statement.lower()
        if low.startswith("select") and "from machines" in low:
            machine_selects.append(statement)

    out = get_iot_command_center(db=db, current_user=USER)
    assert len(out["latest_signals"]) == 6, out
    # exactly one machines SELECT (the roster load), not one-per-signal
    assert len(machine_selects) == 1, \
        f"expected 1 machines SELECT, got {len(machine_selects)} (N+1 in the loop)"
    print("PASS iot-command: 6 signals rendered with a single machines SELECT (no N+1)")


def test_headline_counts_are_true_totals_past_the_display_window():
    # The regression the fix targets: iot_telemetry grows unbounded, and the
    # endpoint only hydrates the last 300 rows for the latest-signal display. The
    # "signals" and "live_machines" KPIs must be the TRUE totals, not the length
    # of that 300-row window (which used to freeze at 300 forever).
    db = _fresh_session()
    # 5 machines, so distinct reporters is an independently-known 5.
    for mid in range(1, 6):
        db.add(models.Machine(id=mid, name=f"CNC-{mid:02d}", status="Running", utilization=70))
    # 350 telemetry rows (> the 300 window) spread across all 5 machines: machine
    # (i % 5) + 1 gets a row for each i, so every machine reports and the total is
    # a hand-counted 350.
    db.add_all([_tel((i % 5) + 1, "temperature", str(i), i) for i in range(350)])
    db.commit()

    out = get_iot_command_center(db=db, current_user=USER)

    # true total ingested — NOT the 300-row display cap
    assert out["signals"] == 350, out
    # every one of the 5 machines has reported at least once
    assert out["live_machines"] == 5, out
    # the display list is still capped: at most 300 rows were scanned, so at most
    # the 5 distinct (machine, "temperature") pairs it could dedup to.
    assert len(out["latest_signals"]) == 5, out
    # machines is the roster total, independent of the telemetry window
    assert out["machines"] == 5, out
    print("PASS iot-command: signals/live_machines are true totals past the 300-row window (350 signals / 5 live)")


def test_empty_factory_is_all_zeros_no_crash():
    out = get_iot_command_center(db=_fresh_session(), current_user=USER)
    assert out["machines"] == 0, out
    assert out["signals"] == 0, out
    assert out["live_machines"] == 0, out
    assert out["latest_signals"] == [], out
    print("PASS empty factory -> zeros, empty signal list, no crash")


if __name__ == "__main__":
    test_latest_per_signal_names_resolved_and_counts()
    test_names_resolved_without_a_per_row_query()
    test_headline_counts_are_true_totals_past_the_display_window()
    test_empty_factory_is_all_zeros_no_crash()
    print("ALL IOT-COMMAND ANALYTICS TESTS PASSED")
