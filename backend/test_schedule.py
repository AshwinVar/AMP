"""Schedule adherence read-model tests (ADR-0007).

Classifies production plans into met / on-track / behind / missed from actual vs
planned quantity, computes a pooled attainment rate over the plans due so far,
rolls up per shift and per machine (worst first), and lists the plans to chase.
Run:  python backend/test_schedule.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import schedule


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _machine(db, id_, name):
    db.add(models.Machine(id=id_, name=name, status="Running"))


def _plan(no, machine_id, planned, actual, day_offset, shift="Day", status="Planned", wo=None):
    return models.ProductionPlan(
        plan_no=no, machine_id=machine_id, work_order_id=wo,
        planned_quantity=planned, actual_quantity=actual, shift_name=shift, status=status,
        plan_date=(datetime.utcnow().date() + timedelta(days=day_offset)),
    )


def test_schedule_classifies_plans_and_rolls_up():
    db = _fresh_session()
    _machine(db, 1, "SMT-01")
    _machine(db, 2, "IC-01")
    db.add_all([
        # SMT-01: one met (full), one missed (past, nothing made)
        _plan("P-1", 1, 100, 100, -2, shift="Day"),        # met (actual >= planned)
        _plan("P-2", 1, 100, 0, -1, shift="Night"),        # missed (past due, 0 made)
        # IC-01: one behind (past, partial), one on-track (today, unmet), one met by status
        _plan("P-3", 2, 100, 40, -1, shift="Day"),         # behind (past due, 40/100)
        _plan("P-4", 2, 100, 10, 0, shift="Day"),          # on_track (due today, can catch up)
        _plan("P-5", 2, 100, 0, -3, shift="Night", status="Completed"),  # met by status though 0 made
    ])
    db.commit()

    s = schedule.build_schedule_adherence(db, "DEFAULT")
    assert s["total"] == 5
    assert s["met"] == 2 and s["missed"] == 1 and s["behind"] == 1 and s["on_track"] == 1

    # attainment is pooled over plans due so far (plan_date <= today): the 4 past
    # plans (P-1,P-2,P-3,P-5) contribute 400 planned; today's P-4 is excluded.
    # actual due-so-far = 100+0+40+0 = 140 of 400 = 35%
    assert s["planned_units"] == 400 and s["actual_units"] == 140
    assert s["attainment_rate"] == 35

    by_machine = {m["machine"]: m for m in s["by_machine"]}
    assert by_machine["SMT-01"]["plans"] == 2 and by_machine["SMT-01"]["missed"] == 1
    assert by_machine["IC-01"]["behind"] == 1
    # worst-first: SMT-01 (has a missed plan) sorts before IC-01
    assert s["by_machine"][0]["machine"] == "SMT-01"

    by_shift = {x["shift"]: x for x in s["by_shift"]}
    assert by_shift["Day"]["plans"] == 3 and by_shift["Night"]["plans"] == 2

    # chase list: missed first (P-2), then behind (P-3); met/on-track excluded
    chase = s["chase"]
    assert [c["plan_no"] for c in chase] == ["P-2", "P-3"]
    assert chase[0]["state"] == "missed" and chase[0]["shortfall"] == 100
    assert chase[1]["state"] == "behind" and chase[1]["shortfall"] == 60

    # today's scheduled load: only P-4 (10 of 100 = 10%)
    assert s["today"]["plans"] == 1 and s["today"]["planned"] == 100 and s["today"]["attainment_rate"] == 10

    # daily series spans the 7-day window and carries the due plans' totals
    assert len(s["daily"]) == 7


def test_schedule_shortfall_ordering_and_empty_safe():
    db = _fresh_session()
    _machine(db, 1, "SMT-01")
    db.add_all([
        _plan("P-A", 1, 100, 10, -1),   # behind, shortfall 90
        _plan("P-B", 1, 100, 80, -1),   # behind, shortfall 20
        _plan("P-C", 1, 100, 0, -1),    # missed, shortfall 100
    ])
    db.commit()
    s = schedule.build_schedule_adherence(db, "DEFAULT")
    # missed first, then behind by biggest shortfall
    assert [c["plan_no"] for c in s["chase"]] == ["P-C", "P-A", "P-B"]

    # empty plan book -> zeros, no divide-by-zero
    empty = schedule.build_schedule_adherence(_fresh_session(), "DEFAULT")
    assert empty["total"] == 0 and empty["attainment_rate"] == 0 and empty["chase"] == []
    assert empty["today"]["attainment_rate"] == 0


if __name__ == "__main__":
    test_schedule_classifies_plans_and_rolls_up()
    test_schedule_shortfall_ordering_and_empty_safe()
    print("SCHEDULE OK: plans classified met/on-track/behind/missed; pooled attainment "
          "over plans due so far; per-shift + per-machine rollup (worst first); chase "
          "list (missed then behind, biggest shortfall); today's load; empty-safe")
