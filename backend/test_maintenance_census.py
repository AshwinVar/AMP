"""The CMMS card's Breakdown KPI could only ever read zero.

TWO DEFECTS IN ONE ROW
----------------------
`MaintenanceSection.tsx:16` renders eight KPIs off `/analytics/maintenance`:

    Tasks | Open | In Progress | Completed | Overdue | PM | Breakdown | Avg Repair

**1. The type split matches a word no backend writer emits.**

    preventive = type_counts.get("Preventive", 0)
    breakdown  = type_counts.get("Breakdown", 0)

Every writer of `MaintenanceTask.task_type` in backend/, excluding tests:

    factory_simulator.py:483  Preventive, Corrective, Predictive,
                              Lubrication, Calibration
    reset_factory.py:235,240  Corrective, Preventive
    ai/agents.py:26,27,39     "Predictive (auto)", "Quality (auto)",
                              "Yield (auto)"

Not one writes `"Breakdown"`. Only the manual create form
(`MaintenanceSection.tsx:21`) offers it. So on every simulator- or
reset_factory-seeded tenant the **Breakdown KPI is pinned at 0** while the
Corrective tasks — the actual firefighting — sit in the table three rows below
it. A plant manager reading "Breakdown 0" concludes there is none.

And `PM` had the mirror problem: exact-matching `"Preventive"` counts neither
`Lubrication` nor `Calibration` nor `Predictive`, all of which are planned work.

**The rule already exists, one directory away.** `ai/maintenance.py:144`:

    def is_reactive(task_type) -> bool:
        \"\"\"True when the task type says the work was triggered by a failure
        rather than scheduled ahead of it.\"\"\"
        t = (task_type or "").lower()
        return any(h in t for h in REACTIVE_HINTS)

matched as substrings and lowercased, so `"Predictive (auto)"` reads planned
while the agent's defect-driven `"Quality (auto)"` / `"Yield (auto)"` read
reactive. `build_maintenance_summary` — rendered from the same table — has used
it all along.

**2. The status split drops the two states the agents write.**
`total_tasks` is published beside `open` / `in_progress` / `completed`, and
`ai/agents.py:129` writes `"Proposed"` on every agent proposal and `"Cancelled"`
when a human rejects one. Neither has a bucket, so both are counted in Tasks
and shown nowhere — #568 and #569 again.

THE FIX
-------
Both rows partition. Status gains `proposed`, `cancelled` and an explicit
`other`; the type split becomes planned-vs-reactive through `is_reactive`, so
`PM + Breakdown == Tasks`.

That widens `preventive` from "typed exactly Preventive" to "planned work",
which is a deliberate change to a published number and is stated here so it is
not mistaken for a bug: a Lubrication task IS preventive maintenance, and the
KPI beside a total should account for it.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_maintenance_census.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import analytics_routes
import models
import tenancy
from ai import maintenance
from database import Base

T = "MAINTCEN"
USER = {"tenant": T, "username": "tester", "role": "Admin"}
failures = []

# (task_no, task_type, status) — one row per word any writer in this repo
# emits, plus the two the manual form offers and one nothing writes.
ROWS = [
    # factory_simulator.py:483
    ("MT-PREV", "Preventive", "Open"),
    ("MT-CORR", "Corrective", "Open"),
    ("MT-PRED", "Predictive", "In Progress"),
    ("MT-LUBE", "Lubrication", "Completed"),
    ("MT-CAL", "Calibration", "Completed"),
    # ai/agents.py:26,27,39 — what the agents propose
    ("MT-AUTOP", "Predictive (auto)", "Proposed"),
    ("MT-AUTOQ", "Quality (auto)", "Proposed"),
    ("MT-AUTOY", "Yield (auto)", "Cancelled"),
    # MaintenanceSection.tsx:21 — the manual create form's own options
    ("MT-BRK", "Breakdown", "Open"),
    ("MT-INSP", "Inspection", "Open"),
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
    # Every task planned in the FUTURE, so `overdue` is 0 throughout and cannot
    # quietly account for a bucket gap.
    due = datetime.utcnow().date() + timedelta(days=30)
    for no, ttype, status in ROWS:
        t = models.MaintenanceTask(tenant_code=T, task_no=no, machine_id=m.id,
                                   task_type=ttype, assigned_to="Tech",
                                   planned_date=due)
        t.status = status
        db.add(t)
    db.commit()
    tenancy.reset_current_tenant(tok)
    return db


def main():
    db = seed()
    tok = tenancy.set_current_tenant(T)
    a = analytics_routes.get_maintenance_analytics(db, USER)

    print("=" * 74)
    print("1. THE STATUS ROW ADDS UP TO THE TOTAL")
    print("=" * 74)
    buckets = ("open", "in_progress", "completed", "proposed", "cancelled", "other")
    parts = sum(a[b] for b in buckets)
    check(f"total_tasks = {a['total_tasks']}", a["total_tasks"] == len(ROWS),
          str(a["total_tasks"]))
    check(f"open+in_progress+completed+proposed+cancelled+other == total_tasks "
          f"({parts} vs {a['total_tasks']})", parts == a["total_tasks"],
          " ".join(f"{b}={a[b]}" for b in buckets))
    check("the agents' two Proposed tasks are reported", a["proposed"] == 2,
          str(a["proposed"]))
    check("...and the rejected one is not lost", a["cancelled"] == 1,
          str(a["cancelled"]))

    print()
    print("=" * 74)
    print("2. THE BREAKDOWN KPI CAN ACTUALLY BE NON-ZERO")
    print("=" * 74)
    # Corrective, Quality (auto), Yield (auto), Breakdown = 4 reactive.
    check(f"reactive work is counted: Breakdown = {a['breakdown']}, not 0",
          a["breakdown"] == 4, str(a["breakdown"]))
    check("...and 'Corrective' — the word the simulator actually writes — is in it",
          maintenance.is_reactive("Corrective"))
    check("...as is the agent's defect-driven 'Quality (auto)'",
          maintenance.is_reactive("Quality (auto)"))
    check("...while 'Predictive (auto)' stays planned — it is scheduled ahead",
          not maintenance.is_reactive("Predictive (auto)"))

    print()
    print("=" * 74)
    print("3. THE TYPE ROW ADDS UP TOO")
    print("=" * 74)
    # PM widens from "typed exactly Preventive" to "planned work": Lubrication
    # and Calibration ARE preventive maintenance, and a KPI beside a total has
    # to account for them.
    check(f"PM = {a['preventive']} (everything planned)", a["preventive"] == 6,
          str(a["preventive"]))
    check(f"PM + Breakdown == total_tasks ({a['preventive']} + {a['breakdown']})",
          a["preventive"] + a["breakdown"] == a["total_tasks"],
          f"{a['preventive'] + a['breakdown']} vs {a['total_tasks']}")

    print()
    print("=" * 74)
    print("4. THE RULE HAS ONE HOME")
    print("=" * 74)
    # NOT a count comparison: build_maintenance_execution's `reactive_count` is
    # over COMPLETED tasks in a rolling window, a different denominator from this
    # endpoint's whole-book count. What must be shared is the CLASSIFIER, so
    # assert the endpoint's number is exactly what is_reactive selects.
    expected = sum(1 for _, ttype, _ in ROWS if maintenance.is_reactive(ttype))
    check(f"Breakdown is exactly what is_reactive() selects ({expected})",
          a["breakdown"] == expected, f"{a['breakdown']} vs {expected}")
    check("...and PM is exactly its complement",
          a["preventive"] == len(ROWS) - expected,
          f"{a['preventive']} vs {len(ROWS) - expected}")
    check("every REACTIVE_HINTS word classifies as reactive",
          all(maintenance.is_reactive(h) for h in maintenance.REACTIVE_HINTS),
          str([h for h in maintenance.REACTIVE_HINTS
               if not maintenance.is_reactive(h)]))
    check("a blank/unknown type reads planned, not firefighting",
          not maintenance.is_reactive(None) and not maintenance.is_reactive(""))

    print()
    print("=" * 74)
    print("5. NOTHING ELSE MOVED")
    print("=" * 74)
    check("overdue is still 0 — everything is planned in the future",
          a["overdue"] == 0, str(a["overdue"]))
    check("open still counts only the four Open tasks", a["open"] == 4,
          str(a["open"]))
    check("in_progress still counts the one", a["in_progress"] == 1,
          str(a["in_progress"]))
    check("completed still counts the two", a["completed"] == 2,
          str(a["completed"]))

    print()
    print("=" * 74)
    print("6. A REAL NULL STATUS IS VISIBLE, NOT DROPPED")
    print("=" * 74)
    # status is Column(String, default="Open") WITHOUT nullable=False, so a
    # NULL is reachable by raw SQL or a migration and must land somewhere.
    db.execute(text("UPDATE maintenance_tasks SET status = NULL "
                    "WHERE task_no = 'MT-INSP'"))
    db.commit()
    assert db.execute(text("SELECT status FROM maintenance_tasks "
                           "WHERE task_no='MT-INSP'")).scalar() is None
    a2 = analytics_routes.get_maintenance_analytics(db, USER)
    check("a NULL-status task lands in 'other'", a2["other"] == 1,
          str(a2["other"]))
    check("...and the row still adds up",
          sum(a2[b] for b in buckets) == a2["total_tasks"],
          " ".join(f"{b}={a2[b]}" for b in buckets))

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


def test_maintenance_census():
    """The pytest entry point — a suite exposing only main() contributes
    nothing to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
