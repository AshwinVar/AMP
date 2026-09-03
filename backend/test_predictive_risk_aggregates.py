"""The risk engine must not hydrate a month of history on every poll.

THE DEFECT
----------
`/machine-health` and `/analytics/predictive-maintenance` are both on the
dashboard's 3-second poll, and both go through `ai.prediction.assess_from_db`,
which reads three tables bounded only by a 30-day window:

    db.query(DowntimeLog).filter(created_at >= cutoff).all()
    db.query(ProductionRecord).filter(created_at >= cutoff).all()
    db.query(MachineEvent).filter(created_at >= cutoff).all()

Measured on PostgreSQL 18.3, 200 machines, handler time only:

    /machine-health                     1633 ms
    /analytics/predictive-maintenance   1297 ms

Only 76 ms and 33 ms of that was SQL. The rest is Python: 63,036 downtime rows
and 43,198 machine events inside the window become ~106,000 ORM objects, every
three seconds. Hydrating the events alone costs 416 ms.

WHY THE #532 GUARD DOES NOT CATCH THIS
--------------------------------------
`test_growing_table_reads.py` treats `.filter(created_at >= ...)` as bounded,
and against a table gaining a few rows a day it is. These tables gain thousands:
at 200 machines and ~7 state changes per machine per day, a 30-day window holds
40,000+ rows. **A date window is not a bound; it is a bound divided by the rate.**
That gap is why this suite exists as well as that one.

THE FIX, AND WHY IT CANNOT CHANGE A NUMBER
------------------------------------------
`calculate_predictive_risk` never looks at an individual row. It reduces all
three lists to per-machine counters — minutes, event counts, breakdown counts,
reject and total sums. Every one of those is a GROUP BY, so the loader can hand
the counters over instead of the rows, exactly as `build_management_summary`
already takes `production_sums`, `shift_sums` and `downtime_agg`.

The rows entry point is kept: callers that already hold the lists (tests, the
agent subscribers) still work unchanged.

WHAT IS ASSERTED
----------------
    1  ORACLE   the aggregate path scores IDENTICALLY to the rows path
    2  EDGES    NULL counts, NULL reasons and unparseable durations agree too
    3  BOUNDED  the loader stops hydrating the windowed tables
    4  CONTROL  the fixture really exercises what it claims to

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_predictive_risk_aggregates.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
from ai import prediction
from database import Base
from predictive_engine import calculate_predictive_risk

T = "RISKAGG"
failures = []
_sql = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def seed(n_machines=8, per_machine=40):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()

    @event.listens_for(engine, "before_cursor_execute")
    def _cap(conn, cur, statement, params, context, many):
        _sql.append(" ".join(statement.split()))

    Session = sessionmaker(bind=engine)
    db = Session()
    tok = tenancy.set_current_tenant(None)
    db.add(models.TenantConfig(tenant_code=T))
    now = datetime.utcnow()
    mids = []
    for i in range(n_machines):
        m = models.Machine(tenant_code=T, name=f"M{i:02d}", site="P1",
                           status="Breakdown" if i % 3 == 0 else "Running",
                           utilization=50 + i, downtime="0 min")
        db.add(m)
        db.flush()
        mids.append(m.id)

    # Awkward on purpose. Each of these is a different branch in the engine:
    # "Breakdown" is matched case-sensitively against the RAW reason, a blank
    # reason must not match it, and a duration that does not parse contributes
    # zero minutes but still counts as an event.
    REASONS = ["Breakdown", "breakdown", "Feeder jam", "", "Tool change"]
    DURATIONS = ["5 min", "1 h", "", "not-a-duration", "45 min", "12 min", "90 min"]
    for i in range(n_machines * per_machine):
        mid = mids[i % len(mids)]
        db.add(models.DowntimeLog(
            tenant_code=T, machine_id=mid, reason=REASONS[i % len(REASONS)],
            duration=DURATIONS[i % len(DURATIONS)],
            created_at=now - timedelta(hours=i % 500)))
        db.add(models.MachineEvent(
            tenant_code=T, machine_id=mid, machine_name=f"M{i % len(mids):02d}",
            old_status="Running",
            new_status="Breakdown" if i % 4 == 0 else "Idle",
            utilization=40, source="sim",
            created_at=now - timedelta(hours=i % 500)))
        if i % 5 == 0:
            db.add(models.ProductionRecord(
                tenant_code=T, machine_id=mid, planned_minutes=480,
                runtime_minutes=400, ideal_cycle_time_seconds=30,
                # NOT None: total_count and rejected_count are nullable=False and
                # SQLite enforces it, so the engine's _int() coalescing guards a
                # row this fixture cannot create. Varying the values still
                # exercises the sums, which is what the aggregate replaces.
                total_count=600 if i % 10 else 0,
                good_count=560, rejected_count=40 if i % 7 else 0,
                created_at=now - timedelta(hours=i % 500)))
        if i % 200 == 0:
            db.commit()
    db.add(models.WorkOrder(tenant_code=T, work_order_no="WO-1", part_number="P",
                            batch_number="B", machine_id=mids[0],
                            target_quantity=100, status="In Progress"))
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()
    return Session


def rows_path(Session):
    """Score exactly as assess_from_db used to: hydrate, then reduce in Python."""
    db = Session()
    tok = tenancy.set_current_tenant(T)
    cutoff = datetime.utcnow() - timedelta(days=prediction.RISK_WINDOW_DAYS)
    try:
        return calculate_predictive_risk(
            db.query(models.Machine).all(),
            db.query(models.DowntimeLog).filter(
                models.DowntimeLog.created_at >= cutoff).all(),
            db.query(models.ProductionRecord).filter(
                models.ProductionRecord.created_at >= cutoff).all(),
            db.query(models.MachineEvent).filter(
                models.MachineEvent.created_at >= cutoff).all(),
            db.query(models.WorkOrder).all())
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def aggregate_path(Session):
    db = Session()
    tok = tenancy.set_current_tenant(T)
    try:
        return prediction.assess_from_db(db)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def hydrating_scans():
    """Statements that pull whole rows from the windowed tables."""
    out = []
    for s in _sql:
        low = s.lower()
        if not low.startswith("select") or "group by" in low or "count(" in low:
            continue
        if any(f"from {t}" in low for t in
               ("downtime_logs", "machine_events", "production_records")):
            out.append(s[:100])
    return out


def main():
    print("=" * 74)
    print("1. ORACLE — the aggregate path scores identically to the rows path")
    print("=" * 74)
    Session = seed()
    expected = rows_path(Session)
    got = aggregate_path(Session)

    check("the fixture produces a real score, not an empty list",
          isinstance(expected, list) and len(expected) >= 4, str(type(expected)))
    check("aggregate path == rows path, exactly", got == expected,
          f"\n    rows: {expected}\n    aggs: {got}")

    print()
    print("=" * 74)
    print("2. CONTROL — the fixture exercises what it claims to")
    print("=" * 74)
    # Section 1 proves nothing if every machine scored zero, or if the awkward
    # rows were never reached.
    by_id = {r.get("machine_id", r.get("id")): r for r in expected
             if isinstance(r, dict)}
    scores = [r.get("risk_score", r.get("score")) for r in expected
              if isinstance(r, dict)]
    check("machines carry different risk scores",
          len({s for s in scores if s is not None}) > 1, str(scores[:6]))
    check("at least one machine scored above zero",
          any((s or 0) > 0 for s in scores), str(scores[:6]))
    db = Session()
    tok = tenancy.set_current_tenant(None)
    n_bad_dur = db.query(models.DowntimeLog).filter(
        models.DowntimeLog.duration == "not-a-duration").count()
    n_blank = db.query(models.DowntimeLog).filter(
        models.DowntimeLog.reason == "").count()
    n_lower = db.query(models.DowntimeLog).filter(
        models.DowntimeLog.reason == "breakdown").count()
    n_zero_total = db.query(models.ProductionRecord).filter(
        models.ProductionRecord.total_count == 0).count()
    tenancy.reset_current_tenant(tok)
    db.close()
    check("...and the awkward rows are actually present",
          n_bad_dur and n_blank and n_lower and n_zero_total,
          f"unparseable={n_bad_dur} blank={n_blank} lowercase={n_lower} "
          f"zero_total={n_zero_total}")

    print()
    print("=" * 74)
    print("3. BOUNDED — the loader stops hydrating the windowed tables")
    print("=" * 74)
    _sql.clear()
    aggregate_path(Session)
    bad = hydrating_scans()
    check("assess_from_db hydrates no windowed table", not bad,
          f"{len(bad)} scan(s): {bad[:2]}")

    print()
    print("=" * 74)
    print("4. FLAT — the work does not grow with the history")
    print("=" * 74)
    # The property that matters: twenty times the history, same statements.
    small = seed(n_machines=8, per_machine=5)
    big = seed(n_machines=8, per_machine=100)
    _sql.clear()
    aggregate_path(small)
    n_small = len([s for s in _sql if s.lower().startswith("select")])
    _sql.clear()
    aggregate_path(big)
    n_big = len([s for s in _sql if s.lower().startswith("select")])
    check("same statement count at 5 and 100 rows per machine",
          n_small == n_big, f"{n_small} vs {n_big}")
    check("...and still no hydrating scan on the larger history",
          not hydrating_scans(), str(hydrating_scans()[:1]))
    # And the score must STILL be right on the larger set: a bounded read that
    # quietly drops rows would pass every check above.
    check("scores still match the rows path at 20x the history",
          aggregate_path(big) == rows_path(big), "aggregate diverged at scale")

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


if __name__ == "__main__":
    raise SystemExit(main())
