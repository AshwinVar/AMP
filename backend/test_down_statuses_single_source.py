"""Which machines count as "down" must have one answer, not one per module.

THE DEFECT
----------
`DOWN_STATUSES` was defined TWICE, independently and identically:

    ai/assistant.py:28   DOWN_STATUSES = ("Breakdown", "Down", "Offline")
    ai/briefing.py:27    DOWN_STATUSES = ("Breakdown", "Down", "Offline")

with three consumers across them — the copilot's "which machines are down?",
the briefing's "what needs attention", and `ai/agents.py`, which imports the
briefing's copy. Nothing kept the two equal. Editing either would have made two
AMP surfaces disagree about the same plant, which is precisely the split-brain
that #549 and #552 were about: one rule, two implementations, and only one of
them maintained.

Found by scanning for module-level vocabularies whose members are enumerated
elsewhere as literals. It is a small defect and it had not bitten yet; it is
here because it is the same shape as two that had.

WHAT THIS PINS, AND WHY EACH HALF MATTERS
------------------------------------------
1. One object. Not "equal tuples" — the SAME tuple, so drift is not expressible.
2. `Maintenance` is NOT down. Planned servicing reported as a fault is a plant
   crying wolf, and the comment that said so was the only thing protecting it.
3. `Offline` IS down. That is the machine whose gateway dropped (#549).
4. `"Down"` is in the set and NOT in VALID_MACHINE_STATUSES. That looks like a
   bug and is a deliberate hedge: nothing can write it today, but Machine.status
   has no database constraint and a row predating normalisation could hold it.
   Both halves are asserted so a future tidy-up has to be a decision — deleting
   it, or promoting it into the vocabulary, both fail here.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_down_statuses_single_source.py
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import machine_status
import models
import tenancy
from ai import assistant, briefing
from database import Base

T = "DOWNSET"
failures = []


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
    for i, status in enumerate(machine_status.VALID_MACHINE_STATUSES):
        m = models.Machine(tenant_code=T, name=f"LINE-{i:02d}", site="P1",
                           status=status, utilization=80, downtime="0 min")
        db.add(m)
        db.flush()
        # Production is needed for the BRIEFING half of section 4, not for the
        # copilot half. build_briefing returns early with alerts=[] when plant
        # OEE has no data, so a plant whose machines are down before anything has
        # been produced raises no machines_down alert at all. That is the
        # briefing's own call — "No production data yet" is at least honest — but
        # it is worth knowing, and without a record here this test would be
        # asserting against that early return rather than against DOWN_STATUSES.
        db.add(models.ProductionRecord(
            tenant_code=T, machine_id=m.id, planned_minutes=480,
            runtime_minutes=400, ideal_cycle_time_seconds=30, total_count=600,
            good_count=560, rejected_count=40))
    db.commit()
    tenancy.reset_current_tenant(tok)
    return db


def main():
    DOWN = machine_status.DOWN_STATUSES
    VALID = machine_status.VALID_MACHINE_STATUSES

    print("=" * 74)
    print("1. ONE OBJECT, SO THE TWO COPIES CANNOT DRIFT APART")
    print("=" * 74)
    # `is`, not `==`. Two modules that happen to hold equal tuples today is
    # exactly the state this file exists to end.
    check("assistant and briefing share the definition",
          assistant.DOWN_STATUSES is briefing.DOWN_STATUSES,
          f"{assistant.DOWN_STATUSES!r} vs {briefing.DOWN_STATUSES!r}")
    check("...and it is the one in machine_status",
          assistant.DOWN_STATUSES is DOWN, repr(assistant.DOWN_STATUSES))
    # agents.py imports it from briefing; that re-export has to keep working.
    from ai import agents  # noqa: F401  - import is the assertion
    from ai.briefing import DOWN_STATUSES as via_briefing
    check("...and importing it from briefing still yields the same object",
          via_briefing is DOWN, repr(via_briefing))

    print()
    print("=" * 74)
    print("2. WHAT IS AND IS NOT 'DOWN'")
    print("=" * 74)
    check("Breakdown is down", "Breakdown" in DOWN, str(DOWN))
    check("Offline is down — the machine whose gateway dropped",
          "Offline" in DOWN, str(DOWN))
    # The judgement worth protecting. It was carried only by a code comment.
    check("Maintenance is NOT down — planned servicing is not a fault",
          "Maintenance" not in DOWN, str(DOWN))
    check("Running is not down", "Running" not in DOWN, str(DOWN))
    check("Idle is not down — waiting for work is a scheduling matter",
          "Idle" not in DOWN, str(DOWN))

    print()
    print("=" * 74)
    print("3. THE 'Down' ODDITY IS DELIBERATE, IN BOTH DIRECTIONS")
    print("=" * 74)
    check("'Down' is in the down-set", "Down" in DOWN, str(DOWN))
    check("...and is NOT a status the product can write",
          "Down" not in VALID, str(VALID))
    check("...because normalisation rejects it at the door",
          machine_status.normalize_machine_status("Down") is None,
          repr(machine_status.normalize_machine_status("Down")))
    # Everything else in the set must be real, or the set is drifting.
    stray = [s for s in DOWN if s not in VALID and s != "Down"]
    check("no OTHER unwritable status has crept in", not stray, str(stray))

    print()
    print("=" * 74)
    print("4. THE TWO SURFACES AGREE ON A REAL PLANT")
    print("=" * 74)
    # The behaviour the single definition buys. One machine per valid status, so
    # exactly Breakdown and Offline are down.
    db = seed()
    tok = tenancy.set_current_tenant(T)
    machines = db.query(models.Machine).all()
    expected = sorted(m.name for m in machines if m.status in DOWN)
    check("the fixture has exactly two hard-down machines",
          len(expected) == 2, str(expected))

    answer = assistant.answer(db, T, "which machines are down?")["answer"]
    named = sorted(n for n in (m.name for m in machines) if n in answer)
    check(f"the copilot names exactly {expected}", named == expected,
          f"named {named} in: {answer}")
    check("...and does not name the machine in planned maintenance",
          all(m.name not in answer for m in machines if m.status == "Maintenance"),
          answer)

    # The briefing surfaces it as an ALERT keyed "machines_down", whose detail
    # is the machine names — so this compares the two surfaces on the actual
    # names, not on a count that could match for the wrong reason.
    b = briefing.build_briefing(db, T)
    alert = next((a for a in b.get("alerts", []) if a.get("key") == "machines_down"), None)
    check("the briefing raises a machines_down alert", alert is not None,
          str([a.get("key") for a in b.get("alerts", [])]))
    if alert is not None:
        check(f"...naming exactly the same machines as the copilot: {expected}",
              sorted(alert["detail"].split(", ")) == expected,
              f"briefing={alert['detail']!r} copilot={named}")
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


if __name__ == "__main__":
    raise SystemExit(main())
