"""Rejecting an agent's maintenance task made the agent escalate it.

THE DEFECT
----------
"Overdue maintenance task" had two definitions:

    analytics_routes.py:1218  planned_date < today AND (status IS NULL OR
    factory_ops_routes.py:456                            status != "Completed")

    ai/maintenance.py:57      status IN ("Proposed", "Open", "In Progress")
                              AND planned_date < today

The first two agree with each other — deliberately, and both say so in a comment
about "a metric spelled out once, rule against re-defining it a second way".
They disagree with the third, in BOTH directions:

    a task with status "Cancelled"   ->  A counts it overdue, B does not
    a task with status NULL          ->  A counts it overdue, B's IN() drops it

The "Cancelled" half is the one that hurts, because of what writes it.
`ai/agents.py:124` sets a proposed maintenance task to "Cancelled" when a human
REJECTS it in the Approvals Inbox:

    item.status = "Open" if approve else "Cancelled"

So: an agent proposes maintenance, a human declines it, and
`POST /maintenance/generate-overdue-escalations` then raises a High/Critical
escalation for that exact task — because `!= "Completed"` is true of a task
nobody is ever going to do. The human's decision is not just ignored, it is
converted into an alert. And the dedup only skips a "Resolved" escalation, so
declining it again does not help.

Both counts are also on screen at once: MaintenanceSection.tsx:16 renders the
`/analytics/maintenance` number while MaintenanceSnapshot.tsx:80 renders the
read-model's, so the same plant shows two different overdue figures.

THE FIX
-------
One predicate, `ai.maintenance.overdue_clause()`, used by all three. Overdue
means **planned in the past and not finished or withdrawn** — TERMINAL_STATUSES
is ("Completed", "Cancelled"), and a NULL status is still overdue, keeping the
NULL convention the analytics comment already documents (#295/#298).

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_overdue_maintenance_one_rule.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import analytics_routes
import factory_ops_routes
import models
import tenancy
from ai import maintenance
from database import Base

T = "OVERDUE"
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def seed():
    """One machine; one past-due task per status that matters."""
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
    past = datetime.utcnow().date() - timedelta(days=3)
    rows = [
        ("T-OPEN", "Open"),          # genuinely outstanding
        ("T-REJECTED", "Cancelled"),  # what a human rejection writes (ai/agents.py:124)
        ("T-DONE", "Completed"),      # finished
        ("T-NULL", None),             # a legacy/raw-SQL row
    ]
    for no, status in rows:
        t = models.MaintenanceTask(tenant_code=T, task_no=no, machine_id=m.id,
                                   task_type="Preventive", assigned_to="Tech",
                                   planned_date=past)
        if status is not None:
            t.status = status
        db.add(t)
    db.commit()
    # The NULL row has to be written in SQL. MaintenanceTask.status is
    # Column(String, default="Open"), and a SQLAlchemy column default is applied
    # on INSERT when the attribute is None — so constructing the object with
    # status=None silently stores "Open" and the NULL case is never exercised.
    # (The same trap that made a guard look effective in #407.)
    from sqlalchemy import text
    db.execute(text("UPDATE maintenance_tasks SET status = NULL WHERE task_no = 'T-NULL'"))
    db.commit()
    assert db.execute(text("SELECT status FROM maintenance_tasks WHERE task_no='T-NULL'")
                      ).scalar() is None, "fixture failed to store a real NULL"
    tenancy.reset_current_tenant(tok)
    return db


def main():
    db = seed()
    tok = tenancy.set_current_tenant(T)

    print("=" * 74)
    print("1. THE THREE SURFACES AGREE ON WHAT 'OVERDUE' MEANS")
    print("=" * 74)
    analytics = analytics_routes.get_maintenance_analytics(db, {"tenant": T})
    summary = maintenance.build_maintenance_summary(db, T)
    check(f"/analytics/maintenance overdue = {analytics['overdue']}",
          analytics["overdue"] == 2, str(analytics["overdue"]))
    check(f"the read-model overdue = {summary['overdue']}",
          summary["overdue"] == 2, str(summary["overdue"]))
    check("...and they are the same number",
          analytics["overdue"] == summary["overdue"],
          f"analytics={analytics['overdue']} read-model={summary['overdue']}")

    print()
    print("=" * 74)
    print("2. A TASK A HUMAN REJECTED IS NOT OUTSTANDING WORK")
    print("=" * 74)
    # The half that matters. "Cancelled" is what ai/agents.py writes on reject.
    result = factory_ops_routes.generate_maintenance_overdue_escalations(
        db, {"tenant": T, "username": "tester", "role": "Admin"})
    raised = [e.title for e in db.query(models.Escalation).all()]
    check("an escalation is raised for the genuinely open task",
          any("T-OPEN" in t for t in raised), str(raised))
    check("NO escalation is raised for the REJECTED task",
          not any("T-REJECTED" in t for t in raised), str(raised))
    check("...nor for the completed one", not any("T-DONE" in t for t in raised),
          str(raised))
    check("...and the NULL-status task IS escalated (unfinished, past due)",
          any("T-NULL" in t for t in raised), str(raised))
    check(f"exactly two escalations, not four (created={result.get('created')})",
          len(raised) == 2, str(raised))

    print()
    print("=" * 74)
    print("3. THE RULE ITSELF")
    print("=" * 74)
    check("'Completed' is terminal", "Completed" in maintenance.TERMINAL_STATUSES)
    check("'Cancelled' is terminal — a withdrawn task is not outstanding",
          "Cancelled" in maintenance.TERMINAL_STATUSES)
    check("'Open' is NOT terminal", "Open" not in maintenance.TERMINAL_STATUSES)
    check("'In Progress' is NOT terminal — work in flight is still outstanding",
          "In Progress" not in maintenance.TERMINAL_STATUSES)
    # The NULL convention the analytics comment documents (#295/#298): SQL's
    # `status != 'X'` is NULL, not TRUE, for a NULL status, so it must be ORed
    # back in or an unfinished past-dated row disappears from the count.
    today = datetime.utcnow().date()
    n = db.query(models.MaintenanceTask).filter(maintenance.overdue_clause(today)).count()
    check("the shared clause selects exactly the two outstanding tasks", n == 2, str(n))

    print()
    print("=" * 74)
    print("4. NOTHING ELSE MOVED")
    print("=" * 74)
    # `open` counts tasks in an OPEN state and is a different question from
    # `overdue`; it must not have been dragged along by this change.
    check("the read-model still reports 1 open task (T-OPEN)",
          summary["open"] == 1, str(summary["open"]))
    check("...and the analytics total still counts every row",
          analytics["total_tasks"] == 4, str(analytics["total_tasks"]))
    check("a second run raises no duplicate escalation",
          factory_ops_routes.generate_maintenance_overdue_escalations(
              db, {"tenant": T, "username": "tester", "role": "Admin"}).get("created") == 0
          and db.query(models.Escalation).count() == 2,
          str(db.query(models.Escalation).count()))

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


def test_overdue_maintenance_one_rule():
    """The pytest entry point — see test_open_escalation_one_rule.py for why.

    Without it this suite runs in CI's `backend` job and contributes nothing to
    the coverage job, so everything it proves about ai/maintenance.py reads as
    untested.
    """
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
