"""A task a technician is working on was invisible to two more surfaces.

TWO CONSUMERS THAT KEPT THEIR OWN COPY OF A SHARED RULE
--------------------------------------------------------
`ai/maintenance.py` owns two rules about maintenance tasks, and the difference
between them is written down at line 106, in `build_maintenance_summary`:

    # Counted with THE shared clause, not from `tasks` above: that list is
    # filtered to OPEN_STATUSES, which answers "in an open state" and drops a
    # NULL-status row that is still unfinished and past due.

    OPEN_STATUSES = ("Proposed", "Open", "In Progress")   # "in an open state"
    overdue_clause(today)                                 # "unfinished and past due"

Two other places did not use them.

**1. The digital twin cannot see work in progress.** `ai/twin.py:141`:

    .filter(models.MaintenanceTask.status.in_(("Proposed", "Open")))

A hardcoded pair, missing `"In Progress"` — which is exactly what the Approvals
Inbox writes when a human accepts an agent's proposal (`ai/agents.py:129`) and
what `EscalationSection`/`MaintenanceSection` write when a technician picks the
job up. So `open_maintenance_tasks` reads **0** for a machine somebody is
working on right now, on the Machine Health cockpit whose whole job is to say
what is happening to that machine. Same defect as #565's agent dedup, in a
different consumer.

**2. The execution view drops a NULL-status past-due task** —
the exact row the comment above warns about. `build_maintenance_execution`
prefetches with `status.in_(OPEN_STATUSES)`, so a NULL-status row never enters
`all_tasks` at all, and then derives the overdue backlog from that list:

    open_tasks = [t for t in all_tasks if t.status in OPEN_STATUSES]
    overdue = [t for t in open_tasks if t.planned_date and t.planned_date < today]

`None in ("Proposed", "Open", "In Progress")` is False, so the task vanishes
from the backlog, the aging buckets and the per-machine chase list — while
`/analytics/maintenance`, which uses `overdue_clause`, counts it. Two numbers
for one backlog, from the same table, on the same screen.

`status` is `Column(String, default="Open")` WITHOUT `nullable=False`, so a
NULL is reachable by raw SQL or a migration — the same reachable-NULL class as
#295/#298/#562.

THE FIX
-------
The twin uses `OPEN_STATUSES`. The execution view uses the overdue rule: its
prefetch widens to non-terminal (so the row is fetched at all), and the backlog
is selected by `is_overdue`, a Python twin of `overdue_clause` that this file
pins to agree with the SQL row-for-row — so there is one rule with two
representations rather than two rules.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_maintenance_live_tasks.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
from ai import maintenance, twin
from database import Base

T = "MLIVE"
failures = []

# (task_no, status, days_from_today) — negative is past due.
ROWS = [
    ("MT-PROPOSED", "Proposed", +5),
    ("MT-OPEN", "Open", +5),
    ("MT-WORKING", "In Progress", +5),    # a technician has it right now
    ("MT-DONE", "Completed", -3),
    ("MT-REJECTED", "Cancelled", -3),     # what a human rejection writes
    ("MT-LATE", "Open", -3),              # genuinely overdue
    ("MT-LATE-WORKING", "In Progress", -3),
    ("MT-NULL", None, -3),                # unfinished, past due, no status
]


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def seed():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    db = sessionmaker(bind=engine)()
    tok = tenancy.set_current_tenant(None)
    db.add(models.TenantConfig(tenant_code=T))
    m = models.Machine(tenant_code=T, name="PRESS-01", site="P1", status="Running",
                       utilization=80, downtime="0 min")
    db.add(m)
    db.flush()
    today = datetime.utcnow().date()
    for no, status, offset in ROWS:
        t = models.MaintenanceTask(tenant_code=T, task_no=no, machine_id=m.id,
                                   task_type="Preventive", assigned_to="Tech",
                                   planned_date=today + timedelta(days=offset))
        if status is not None:
            t.status = status
        if status == "Completed":
            t.completed_date = today + timedelta(days=offset)
        db.add(t)
    db.commit()
    # status is Column(String, default="Open"): a SQLAlchemy column default is
    # applied on INSERT when the attribute is None, so constructing with
    # status=None silently stores "Open" and the NULL case is never exercised
    # (the trap from #407/#562).
    db.execute(text("UPDATE maintenance_tasks SET status = NULL "
                    "WHERE task_no = 'MT-NULL'"))
    db.commit()
    assert db.execute(text("SELECT status FROM maintenance_tasks "
                           "WHERE task_no='MT-NULL'")).scalar() is None, \
        "fixture failed to store a real NULL"
    tenancy.reset_current_tenant(tok)
    return db, m.id


def main():
    db, machine_id = seed()
    tok = tenancy.set_current_tenant(T)
    today = datetime.utcnow().date()

    print("=" * 74)
    print("1. THE TWIN SEES WORK A TECHNICIAN IS DOING")
    print("=" * 74)
    counts = twin._open_task_counts(db)
    n = counts.get(machine_id, 0)
    # Proposed + Open x2 + In Progress x2 = 5 tasks in an open state.
    # (The hardcoded number here was wrong first time round; the derived check
    # below is what caught it, which is the argument for having both.)
    check(f"open_maintenance_tasks counts the In Progress ones ({n})",
          n == 5, str(n))
    check("...which is exactly the shared OPEN_STATUSES set",
          n == sum(1 for _, s, _ in ROWS if s in maintenance.OPEN_STATUSES),
          str(n))
    check("'In Progress' IS an open state", "In Progress" in maintenance.OPEN_STATUSES)
    check("...and a rejected task is not", "Cancelled" not in maintenance.OPEN_STATUSES)

    print()
    print("=" * 74)
    print("2. THE EXECUTION BACKLOG DOES NOT DROP A NULL-STATUS TASK")
    print("=" * 74)
    ex = maintenance.build_maintenance_execution(db, T)
    # Overdue = past due and not finished or withdrawn: MT-LATE,
    # MT-LATE-WORKING, MT-NULL. NOT MT-DONE (completed) or MT-REJECTED.
    check(f"overdue backlog is 3, including the NULL-status task "
          f"({ex['backlog']['overdue']})",
          ex["backlog"]["overdue"] == 3, str(ex["backlog"]["overdue"]))
    chased = {t["task_no"] for t in ex.get("chase", [])}
    check("the NULL-status task is on the chase list", "MT-NULL" in chased,
          str(sorted(chased)))
    check("...and so is the one being worked", "MT-LATE-WORKING" in chased,
          str(sorted(chased)))
    check("a withdrawn task is NOT chased", "MT-REJECTED" not in chased,
          str(sorted(chased)))
    check("...nor a completed one", "MT-DONE" not in chased, str(sorted(chased)))

    print()
    print("=" * 74)
    print("3. THE TWO SURFACES AGREE WITH /analytics/maintenance")
    print("=" * 74)
    # The endpoint uses overdue_clause. The execution view now uses the same
    # rule, so one backlog cannot read two numbers on one screen.
    import analytics_routes
    a = analytics_routes.get_maintenance_analytics(db, {"tenant": T})
    check(f"endpoint overdue ({a['overdue']}) == execution backlog "
          f"({ex['backlog']['overdue']})",
          a["overdue"] == ex["backlog"]["overdue"],
          f"{a['overdue']} vs {ex['backlog']['overdue']}")
    summary = maintenance.build_maintenance_summary(db, T)
    check(f"...and the read-model's overdue ({summary['overdue']}) too",
          summary["overdue"] == ex["backlog"]["overdue"],
          f"{summary['overdue']} vs {ex['backlog']['overdue']}")

    print()
    print("=" * 74)
    print("4. ONE RULE, TWO REPRESENTATIONS, PINNED TO AGREE")
    print("=" * 74)
    # is_overdue() is the Python twin of overdue_clause(). If they can disagree
    # this fix has created a fourth spelling instead of removing one.
    sql_ids = {t.id for t in db.query(models.MaintenanceTask)
               .filter(maintenance.overdue_clause(today)).all()}
    py_ids = {t.id for t in db.query(models.MaintenanceTask).all()
              if maintenance.is_overdue(t, today)}
    check(f"is_overdue selects exactly what overdue_clause does "
          f"({len(sql_ids)} rows)", sql_ids == py_ids,
          f"sql={sorted(sql_ids)} python={sorted(py_ids)}")
    # And on the awkward values individually.
    for status, offset, expected in (("Open", -3, True), ("Open", +5, False),
                                     ("Completed", -3, False),
                                     ("Cancelled", -3, False),
                                     ("In Progress", -3, True),
                                     (None, -3, True), (None, +5, False)):
        t = models.MaintenanceTask(task_no="x", task_type="Preventive",
                                   assigned_to="T",
                                   planned_date=today + timedelta(days=offset))
        t.status = status
        got = maintenance.is_overdue(t, today)
        check(f"status={status!r} planned {offset:+d}d -> overdue={got}",
              got == expected, f"expected {expected}")
    # A task with no planned date has no promise to keep.
    t = models.MaintenanceTask(task_no="x", task_type="Preventive",
                               assigned_to="T", planned_date=None)
    t.status = "Open"
    check("no planned date -> not overdue, matching the SQL",
          maintenance.is_overdue(t, today) is False)

    tenancy.reset_current_tenant(tok)
    db.close()

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


def test_maintenance_live_tasks():
    """The pytest entry point — a suite exposing only main() contributes
    nothing to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
