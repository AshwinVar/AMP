"""The copilot said "All 12 machines are running" while machines sat idle.

THE DEFECT
----------
`ai/assistant.py:_machines` answers the plant's "what are the machines doing?"
question. It classified two things and asserted a total about everything else:

    down  = [m for m in machines if (m.status or "") in DOWN_STATUSES]
    maint = [m for m in machines if (m.status or "") == "Maintenance"]
    if not down and not maint:
        return f"All {len(machines)} machines are running."

`DOWN_STATUSES` is `("Breakdown", "Down", "Offline")` and the vocabulary is
`VALID_MACHINE_STATUSES = ("Running", "Idle", "Breakdown", "Maintenance",
"Offline")` — so **Idle** is in neither branch. `factory_simulator.tick_machine_status`
writes it at 15% weight:

    random.choices(["Running", "Idle", "Maintenance", "Breakdown"],
                   weights=[70, 15, 10, 5])

So on an ordinary running plant the assistant states, in a full sentence, that
every machine is running while several are stopped waiting for work.

NOT CLAIMED HERE, having been checked: a NULL or unrecognised status. The hunt
that found this also reported those, and they are unreachable — `machines.status`
is NOT NULL (models.py), and every ingest path goes through
`normalize_machine_status`, which leaves the machine untouched rather than
writing an unknown string. Building an "other" bucket for them would be
speculation. Instead the headline is derived from `== "Running"`, so no status —
including a legacy one nobody can write today — can produce a false "all
running".

This is the census defect (#568, #569) in prose rather than in a KPI row, and it
is worse in one specific way: a bucket that reads 0 is a number a manager can
distrust, but "All 12 machines are running" is a **claim**, made by the thing
they asked for the truth.

THE FIX
-------
Assert "all running" only when they are all Running — derived from
`machine_status.RUNNING`, not from the absence of the two states this function
happened to check. Otherwise the headline carries its own denominator ("1 of 4
machines running") and the rest are itemised: down, in maintenance, idle. A
status word nobody itemised can then still not become a false claim about all of
them, which is the property that was missing.

Deliberately unchanged: `DOWN_STATUSES` still excludes Idle and Maintenance.
The comment in machine_status.py is right that a plant reporting scheduled
servicing as a fault is crying wolf, and idle is a scheduling matter rather than
a breakdown. This does not make idle an alarm; it stops the sentence claiming
idle machines are running.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_copilot_machine_answer.py
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
from ai import assistant
from database import Base
from machine_status import DOWN_STATUSES, VALID_MACHINE_STATUSES

T = "COPILOTM"
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def seed(statuses):
    """A plant whose machines hold exactly the given statuses."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    db = sessionmaker(bind=engine)()
    tok = tenancy.set_current_tenant(None)
    db.add(models.TenantConfig(tenant_code=T))
    for i, st in enumerate(statuses):
        m = models.Machine(tenant_code=T, name=f"CNC-{i:02d}", site="P1",
                           utilization=50, downtime="0 min")
        m.status = st
        db.add(m)
    db.commit()
    tenancy.reset_current_tenant(tok)
    return db


def answer(db):
    tok = tenancy.set_current_tenant(T)
    try:
        return assistant._machines(db, T)[0]
    finally:
        tenancy.reset_current_tenant(tok)


def main():
    print("=" * 74)
    print("1. THE SENTENCE THE SIMULATED PLANT ACTUALLY PRODUCES")
    print("=" * 74)
    # tick_machine_status writes Idle at 15% weight, so this is the ordinary
    # state of a running plant, not a corner case.
    db = seed(["Running", "Running", "Idle", "Running"])
    a = answer(db)
    print(f"      -> {a}")
    check("does NOT claim all four are running when one is idle",
          "All 4 machines are running" not in a, a)
    check("...and says how many are idle", "1 idle" in a, a)
    db.close()

    print()
    print("=" * 74)
    print("2. WHEN THEY REALLY ARE ALL RUNNING, IT STILL SAYS SO")
    print("=" * 74)
    # The control. An answer that never claims the good case is useless.
    db = seed(["Running", "Running", "Running"])
    a = answer(db)
    print(f"      -> {a}")
    check("all Running -> 'All 3 machines are running.'",
          a == "All 3 machines are running.", a)
    db.close()

    print()
    print("=" * 74)
    print("3. THE EXISTING BEHAVIOUR IS NOT REGRESSED")
    print("=" * 74)
    db = seed(["Running", "Breakdown", "Maintenance"])
    a = answer(db)
    print(f"      -> {a}")
    check("a broken machine is still named", "CNC-01" in a, a)
    check("...and still called down", "down" in a.lower(), a)
    check("...and maintenance is still reported separately",
          "maintenance" in a.lower(), a)
    check("...and maintenance is NOT called down — planned work is not a fault",
          "1 down" in a, a)
    db.close()

    print()
    print("=" * 74)
    print("4. THE TOTAL IS ACCOUNTED FOR, NOT ASSERTED")
    print("=" * 74)
    # The headline now states how many of how many are running, so the sentence
    # carries its own denominator. That is what stops a forgotten status word
    # turning into a false claim about all of them — the failure mode here, and
    # in #568 and #569 before it.
    db = seed(["Running", "Idle", "Idle", "Breakdown"])
    a = answer(db)
    print(f"      -> {a}")
    check("says 1 of 4 are running", "1 of 4" in a, a)
    check("...and itemises the other three", "2 idle" in a and "1 down" in a, a)
    db.close()

    print()
    print("=" * 74)
    print("5. THE VOCABULARY IS THE SHARED ONE")
    print("=" * 74)
    # Every status the vocabulary allows must reach a branch of the answer, or
    # this defect simply moves to whichever word was forgotten next.
    for st in VALID_MACHINE_STATUSES:
        db = seed([st, "Running"])
        a = answer(db)
        ok = (a == "All 2 machines are running.") if st == "Running" else \
             ("All 2 machines are running" not in a)
        check(f"'{st}' reaches a branch  ->  {a[:58]}", ok, a)
        db.close()
    check("Idle is deliberately NOT a down status — it is a scheduling matter",
          "Idle" not in DOWN_STATUSES)
    check("...nor is Maintenance — planned servicing is not a fault",
          "Maintenance" not in DOWN_STATUSES)

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


def test_copilot_machine_answer():
    """The pytest entry point — a suite exposing only main() contributes
    nothing to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
