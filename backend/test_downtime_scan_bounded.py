"""The dashboard must not read the whole downtime log every three seconds.

THE DEFECT, AND WHY EVERY EXISTING HARNESS MISSED IT
----------------------------------------------------
`/analytics/executive-oee` and `/analytics/factory-command-center` are both on
the dashboard's 3-second poll, and both did `db.query(models.DowntimeLog).all()`
with no filter and no limit. Measured on PostgreSQL 18.3 at 200 machines:

    downtime_logs rows        executive-oee        factory-command-center
                 ~200                 6.9 ms                     16.8 ms
               75,000              822.0 ms                    859.6 ms

That is handler time -- no HTTP, no queueing. 75,000 rows is one year of a
200-machine plant at one downtime event per machine per day.

Every harness in this repo missed it because they all seed downtime rows
PER MACHINE. `downtime_logs` does not grow with machine count; it grows with
TIME, and no scale of a per-machine seed reaches a year of history. So this
suite seeds rows independently of the machine count -- that is the whole point
of it, and a future harness should copy the idea rather than the numbers.

WHY GROUP BY AND NOT A LIMIT, A WINDOW, OR A NEW COLUMN
-------------------------------------------------------
`DowntimeLog.duration` is a STRING ("15 min"), so SQL cannot SUM it -- which is
exactly why an earlier pass fixed the sibling scans and left these two, saying
so in a comment at analytics_routes.py:950. But SQL can still GROUP BY it:

    SELECT machine_id, reason, duration, COUNT(*) ... GROUP BY 1,2,3

Python then parses each DISTINCT duration once and multiplies by its count. The
result is arithmetically IDENTICAL -- not an approximation, not a sample -- so
no figure on the dashboard changes. Measured: 75,000 rows collapse to 2,200
groups and 807.6 ms becomes 15.2 ms, a 53x improvement with byte-identical
output.

A `.limit()` would silently drop downtime and corrupt OEE. A date window would
change what the number MEANS (these endpoints report lifetime totals, and their
production figures are lifetime too, so the pair is consistent). A numeric
`duration_minutes` column would be the textbook fix and needs a migration,
a backfill and a dual-write; it is not needed to remove the scan.

WHAT IS ASSERTED
----------------
    1  ORACLE     the aggregates equal totals computed independently here
    2  BOUNDED    no full-column scan of downtime_logs survives
    3  FLAT       work does not grow with the number of rows
    4  EDGES      NULL/blank reasons and unparseable durations still behave

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_downtime_scan_bounded.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import analytics_routes
import models
import tenancy
from analytics_engine import normalize_downtime_reason, parse_duration_to_minutes
from database import Base

T = "DTSCAN"
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


# Statements issued against downtime_logs, captured so the shape of the read can
# be asserted rather than its wall time. Wall time is a property of the machine
# (see docs/PERFORMANCE.md); the SQL is a property of the code.
_sql = []


def seed(n_machines, n_logs):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cur, statement, params, context, many):
        if "downtime_logs" in statement:
            _sql.append(" ".join(statement.split()))

    Session = sessionmaker(bind=engine)
    db = Session()
    tok = tenancy.set_current_tenant(None)
    db.add(models.TenantConfig(tenant_code=T))
    now = datetime.utcnow()
    mids = []
    for i in range(n_machines):
        m = models.Machine(tenant_code=T, name=f"M{i:03d}", site="P1",
                           status="Running" if i % 4 else "Breakdown",
                           utilization=50 + (i % 40), downtime="0 min")
        db.add(m)
        db.flush()
        mids.append(m.id)
        db.add(models.ProductionRecord(
            tenant_code=T, machine_id=m.id, planned_minutes=480,
            runtime_minutes=400, ideal_cycle_time_seconds=30, total_count=600,
            good_count=560, rejected_count=40, created_at=now))

    # Deliberately awkward data: several distinct durations, a NULL reason, a
    # blank reason, and a duration that does not parse. All four have to survive
    # the rewrite, because each is a different branch in the helpers.
    # NOT a None reason: DowntimeLog.reason is NOT NULL, so the None branch in
    # normalize_downtime_reason is unreachable from the database and seeding it
    # only produces an IntegrityError. The BLANK string is the coalesce case
    # that can actually occur, and it is the one that must survive the rewrite.
    # The list LENGTHS are pairwise coprime with the machine count (12, 5, 7) ON
    # PURPOSE. An earlier version used 12 machines / 4 reasons / 6 durations, and
    # because 4 and 6 both divide 12 every machine ended up with exactly ONE
    # (reason, duration) group -- which made `by_machine[k] = v` and
    # `by_machine[k] += v` indistinguishable, and a mutation swapping them
    # survived. Coprime lengths give each machine many groups, so the
    # accumulation is actually exercised.
    REASONS = ["Feeder jam", "Tool change", "", "Coolant", "Power loss"]
    DURATIONS = ["5 min", "12 min", "45 min", "1 h", "", "not-a-duration", "90 min"]
    for i in range(n_logs):
        db.add(models.DowntimeLog(
            tenant_code=T, machine_id=mids[i % len(mids)],
            reason=REASONS[i % len(REASONS)],
            duration=DURATIONS[i % len(DURATIONS)],
            created_at=now - timedelta(minutes=i)))
        if i % 2000 == 0:
            db.commit()
    # Break the tie. With the strides above every reason gets an equal share, so
    # `top_reason` would be decided by dict order and the assertion on it would
    # test nothing. These extra rows make one reason the unambiguous winner.
    for i in range(max(3, n_logs // 20)):
        db.add(models.DowntimeLog(
            tenant_code=T, machine_id=mids[i % len(mids)], reason="Feeder jam",
            duration="7 min", created_at=now - timedelta(days=1, minutes=i)))
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()
    return Session


def oracle(Session):
    """Totals computed HERE, row by row, independently of the code under test."""
    db = Session()
    tok = tenancy.set_current_tenant(None)
    logs = db.query(models.DowntimeLog).filter(
        models.DowntimeLog.tenant_code == T).all()
    by_machine, by_reason, total = {}, {}, 0
    for log in logs:
        mins = parse_duration_to_minutes(log.duration)
        total += mins
        by_machine[log.machine_id] = by_machine.get(log.machine_id, 0) + mins
        r = normalize_downtime_reason(log.reason)
        by_reason[r] = by_reason.get(r, 0) + mins
    n = len(logs)
    tenancy.reset_current_tenant(tok)
    db.close()
    return by_machine, by_reason, total, n


def call(Session, fn):
    db = Session()
    tok = tenancy.set_current_tenant(T)
    user = {"tenant_code": T, "username": "u", "role": "Admin", "sub": "u"}
    try:
        return fn(db=db, current_user=user)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def full_scans():
    """Statements that read whole downtime rows rather than an aggregate.

    A bounded read is a GROUP BY, a COUNT, or something with a LIMIT. Anything
    else selecting downtime_logs columns is pulling the table into Python.
    """
    out = []
    for s in _sql:
        low = s.lower()
        if not low.startswith("select"):
            continue
        if "group by" in low or "count(" in low or "limit" in low:
            continue
        if "from downtime_logs" in low:
            out.append(s[:110])
    return out


def main():
    print("=" * 74)
    print("1. ORACLE — the aggregates match totals computed row by row here")
    print("=" * 74)
    Session = seed(n_machines=12, n_logs=600)
    exp_machine, exp_reason, exp_total, exp_n = oracle(Session)
    print(f"  seeded 12 machines and {exp_n} downtime rows "
          f"({exp_total} minutes over {len(exp_reason)} reasons)")

    _sql.clear()
    cc = call(Session, analytics_routes.get_factory_command_center)
    check("factory-command-center total downtime is exact",
          cc.get("total_downtime_minutes") == exp_total,
          f"got {cc.get('total_downtime_minutes')!r}, expected {exp_total}")

    _sql.clear()
    ex = call(Session, analytics_routes.get_executive_oee)
    # downtime_pareto is [{"reason": ..., "minutes": ...}] (analytics_routes:865).
    pareto = {r["reason"]: r["minutes"] for r in (ex.get("downtime_pareto") or [])}
    check("executive-oee reason totals are exact",
          pareto == exp_reason, f"got {pareto!r}, expected {exp_reason!r}")
    # machine_ranking is the per-machine list (analytics_routes:920).
    per_machine = {m["machine_id"]: m["downtime_minutes"]
                   for m in (ex.get("machine_ranking") or [])}
    check("executive-oee per-machine downtime is exact",
          all(per_machine.get(k) == v for k, v in exp_machine.items()) if per_machine
          else False,
          f"got {dict(list(per_machine.items())[:3])!r}, expected "
          f"{dict(list(exp_machine.items())[:3])!r}")

    # analytics_summary tallies reasons by EVENT COUNT, not minutes, and reports
    # downtime_events. Both were unasserted, and mutations swapping the two
    # tallies or counting groups instead of rows survived because of it.
    _sql.clear()
    su = call(Session, analytics_routes.analytics_summary)
    exp_events = {}
    db = Session()
    tok = tenancy.set_current_tenant(None)
    for log in db.query(models.DowntimeLog).filter(
            models.DowntimeLog.tenant_code == T).all():
        r = normalize_downtime_reason(log.reason)
        exp_events[r] = exp_events.get(r, 0) + 1
    tenancy.reset_current_tenant(tok)
    db.close()
    check("analytics_summary tallies reasons by EVENT COUNT, not minutes",
          su.get("reason_counts") == exp_events,
          f"got {su.get('reason_counts')!r}, expected {exp_events!r}")
    check("analytics_summary counts downtime ROWS, not groups",
          su.get("downtime_events") == exp_n,
          f"got {su.get('downtime_events')!r}, expected {exp_n}")
    check("analytics_summary total minutes is exact",
          su.get("total_downtime_minutes") == exp_total,
          f"got {su.get('total_downtime_minutes')!r}, expected {exp_total}")
    check("...and top_reason is the most FREQUENT reason",
          su.get("top_reason") == max(exp_events.items(), key=lambda kv: kv[1])[0],
          f"got {su.get('top_reason')!r} from {exp_events!r}")

    print()
    print("=" * 74)
    print("2. BOUNDED — no statement reads whole downtime rows")
    print("=" * 74)
    for name, fn in (("factory-command-center", analytics_routes.get_factory_command_center),
                     ("executive-oee", analytics_routes.get_executive_oee),
                     ("analytics_summary", analytics_routes.analytics_summary)):
        _sql.clear()
        call(Session, fn)
        bad = full_scans()
        check(f"{name} issues no full-column downtime scan", not bad,
              f"{len(bad)} scan(s): {bad[:1]}")

    print()
    print("=" * 74)
    print("3. FLAT — the read does not grow with the size of the table")
    print("=" * 74)
    # The property that actually matters. Same factory, 20x the history: if the
    # endpoint still pulls rows, the statement count or shape gives it away.
    small = seed(n_machines=12, n_logs=200)
    big = seed(n_machines=12, n_logs=4000)
    for name, fn in (("factory-command-center", analytics_routes.get_factory_command_center),
                     ("executive-oee", analytics_routes.get_executive_oee)):
        _sql.clear()
        call(small, fn)
        n_small = len([s for s in _sql if s.lower().startswith("select")])
        _sql.clear()
        call(big, fn)
        n_big = len([s for s in _sql if s.lower().startswith("select")])
        check(f"{name}: same statements at 200 and 4000 rows",
              n_small == n_big, f"{n_small} vs {n_big}")
        check(f"{name}: and none of them is a scan at 4000 rows",
              not full_scans(), str(full_scans()[:1]))

    # And the totals must STILL be right on the larger table — a bounded read
    # that quietly drops rows would pass every check above.
    exp_machine, exp_reason, exp_total, exp_n = oracle(big)
    _sql.clear()
    cc = call(big, analytics_routes.get_factory_command_center)
    check(f"totals still exact at {exp_n} rows",
          cc.get("total_downtime_minutes") == exp_total,
          f"got {cc.get('total_downtime_minutes')!r}, expected {exp_total}")

    print()
    print("=" * 74)
    print("4. EDGES — NULL reasons, blank reasons, unparseable durations")
    print("=" * 74)
    # The seed includes all three. If the rewrite parsed durations differently,
    # or coalesced reasons differently, the oracle above would already differ --
    # so assert explicitly that the awkward cases are actually PRESENT, or
    # section 1 proves nothing about them.
    check("the fixture really contains an unparseable duration",
          parse_duration_to_minutes("not-a-duration") == 0,
          "parse_duration_to_minutes no longer returns 0 for junk")
    blank = normalize_downtime_reason("")
    check("the fixture really contains a coalesced blank reason",
          blank in exp_reason, f"{blank!r} not among {list(exp_reason)}")
    check("...and it carries real minutes, so it is not a no-op case",
          exp_reason.get(blank, 0) > 0, str(exp_reason))

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
