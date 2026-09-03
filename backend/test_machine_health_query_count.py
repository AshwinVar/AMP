"""`/machine-health` must not issue one query per machine.

THE DEFECT THIS PINS
--------------------
`ai.twin.build_twins` fetched every machine, then called `_machine_twin` per
machine — and each call issued THREE more queries (recent downtime, open
maintenance tasks, pending agent actions). Measured with dashboard_perf.py:

    10 machines  ->  37 queries
    50 machines  -> 157 queries
   200 machines  -> 607 queries      (3n + 7)

That is a textbook N+1: the cost grows with the size of the customer's factory.
It matters here more than it usually would because this endpoint is not on a
page somebody occasionally opens — `frontend/app/dashboard/page.tsx` polls the
whole dashboard every 3 seconds, so a 200-machine plant was asking its database
for ~600 statements every three seconds, per open browser tab.

WHY THE TEST COUNTS QUERIES RATHER THAN TIMING ANYTHING
-------------------------------------------------------
Wall time is a property of the machine the test runs on; a CI runner having a
good day would hide a regression. The query COUNT is a property of the code, and
it is the thing that actually breaks at a customer's scale. So this asserts the
shape: the count must stay FLAT as machines are added.

The bound is deliberately loose (a small constant, not an exact number) so that
adding one legitimate query does not fail the build — what must never come back
is growth PER MACHINE.
"""
import os
from datetime import datetime

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
from ai import twin
from database import Base

TENANT = "QC_FACTORY"

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def measure(n_machines):
    """Build the twin view for n machines; return (query_count, twins)."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    Session = sessionmaker(bind=engine)

    counter = {"n": 0, "on": False}

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        if counter["on"]:
            counter["n"] += 1

    db = Session()
    tok = tenancy.set_current_tenant(None)
    db.add(models.TenantConfig(tenant_code=TENANT))
    db.commit()
    for i in range(n_machines):
        db.add(models.Machine(tenant_code=TENANT, site="P1", name=f"M-{i:04d}",
                              status="Running", utilization=70, downtime="0 min"))
    db.commit()
    machines = db.query(models.Machine).all()
    # Give every machine history, so a batched query has real grouping to do and
    # cannot pass by finding nothing.
    for m in machines:
        for k in range(4):
            db.add(models.DowntimeLog(tenant_code=TENANT, machine_id=m.id,
                                      reason=f"r{k}", duration="5 min"))
        db.add(models.MaintenanceTask(
            tenant_code=TENANT, machine_id=m.id, task_no=f"MT-{m.id}",
            task_type="Preventive", assigned_to="tech",
            planned_date=datetime.utcnow().date(), status="Open"))
        db.add(models.ProductionRecord(
            tenant_code=TENANT, machine_id=m.id, planned_minutes=480,
            runtime_minutes=400, ideal_cycle_time_seconds=30,
            total_count=100, good_count=95, rejected_count=5))
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()

    db = Session()
    tok = tenancy.set_current_tenant(TENANT)
    counter["n"] = 0
    counter["on"] = True
    try:
        twins = twin.build_twins(db, TENANT)
    finally:
        counter["on"] = False
        tenancy.reset_current_tenant(tok)
        db.close()
    return counter["n"], twins


def main():
    print("=" * 74)
    print("1. THE QUERY COUNT MUST NOT GROW WITH THE FACTORY")
    print("=" * 74)

    q10, t10 = measure(10)
    q60, t60 = measure(60)

    print(f"  10 machines -> {q10} queries")
    print(f"  60 machines -> {q60} queries")

    check("CONTROL: it really did build a twin per machine",
          len(t10) == 10 and len(t60) == 60, f"{len(t10)} / {len(t60)}")

    # 50 more machines must not cost ~150 more queries. A small constant of
    # extra statements is fine; per-machine growth is not.
    growth = q60 - q10
    check("ADDING 50 MACHINES ADDS ALMOST NO QUERIES", growth <= 5,
          f"grew by {growth} ({q10} -> {q60}); before the fix this was ~150")

    check("...and the absolute count stays small at 60 machines", q60 <= 25,
          f"{q60} queries")

    print()
    print("=" * 74)
    print("2. THE DATA IS STILL CORRECT (a fast wrong answer is worse)")
    print("=" * 74)
    # Batching is only a win if it returns what the per-machine queries did.
    sample = t60[0]
    check("each twin still carries recent downtime",
          isinstance(sample.get("recent_downtime"), list)
          and len(sample["recent_downtime"]) == 3,
          f"{len(sample.get('recent_downtime') or [])} entries (expected 3, capped)")
    check("...newest first",
          [d["reason"] for d in sample["recent_downtime"]] == ["r3", "r2", "r1"],
          str([d["reason"] for d in sample["recent_downtime"]]))
    check("...and the open maintenance task count",
          sample.get("open_maintenance_tasks") == 1,
          str(sample.get("open_maintenance_tasks")))
    check("...and a pending agent-action count",
          sample.get("pending_agent_actions") == 0,
          str(sample.get("pending_agent_actions")))
    check("every machine got its own downtime, not one machine's copied",
          all(len(t["recent_downtime"]) == 3 for t in t60),
          "some twins have the wrong number of downtime entries")

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
