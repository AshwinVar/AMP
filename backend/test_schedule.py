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

    # The breakdown rates must use the SAME due-only basis as the headline, or the
    # card contradicts its own drill-down (today's P-4 has actual=0 only because it
    # hasn't run yet, and must not drag the Day shift down).
    # Day due = P-1 (100/100) + P-3 (100/40) -> 140/200 = 70%; Night = 0/200 = 0%.
    assert by_shift["Day"]["attainment_rate"] == 70
    assert by_shift["Night"]["attainment_rate"] == 0
    # ...and the parts must reconcile to the whole.
    assert sum(x["due_planned"] for x in s["by_shift"]) == s["planned_units"]
    assert sum(x["due_actual"] for x in s["by_shift"]) == s["actual_units"]
    assert sum(m["due_planned"] for m in s["by_machine"]) == s["planned_units"]
    assert by_machine["SMT-01"]["attainment_rate"] == 50 and by_machine["IC-01"]["attainment_rate"] == 20

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


def test_shift_drilldown_reads_against_the_plant():
    db = _fresh_session()
    _machine(db, 1, "SMT-01")
    _machine(db, 2, "IC-01")
    db.add_all([
        # Night shift: the shift under the lens — 100 of 300 due-so-far = 33%
        _plan("N-1", 1, 100, 0, -2, shift="Night"),    # missed  (SMT-01)
        _plan("N-2", 1, 100, 40, -1, shift="Night"),   # behind  (SMT-01)
        _plan("N-3", 2, 100, 60, -1, shift="Night"),   # behind  (IC-01)
        _plan("N-4", 2, 100, 10, 0, shift="Night"),    # on_track (today — out of the rate)
        # Day shift: healthy, 200 of 200 = 100%
        _plan("D-1", 1, 100, 100, -1, shift="Day"),
        _plan("D-2", 2, 100, 100, -1, shift="Day"),
    ])
    db.commit()

    s = schedule.build_shift_adherence(db, "DEFAULT", "Night")
    assert s["found"] is True and s["shift"] == "Night"
    assert s["total"] == 4
    assert s["missed"] == 1 and s["behind"] == 2 and s["on_track"] == 1 and s["met"] == 0

    # rate is pooled over this shift's plans due so far (N-1..N-3): 100 of 300
    assert s["planned_units"] == 300 and s["actual_units"] == 100
    assert s["attainment_rate"] == 33 and s["shortfall_units"] == 200
    # plant baseline spans every shift's due plans: 300 of 500 = 60%
    assert s["plant_attainment_rate"] == 60 and s["vs_plant"] == 33 - 60
    # worst-first ranking among the two shifts puts Night (a missed plan) first
    assert s["rank"] == 1 and s["shifts"] == 2

    # machines inside the shift, worst first: SMT-01 carries the missed plan
    assert [m["machine"] for m in s["by_machine"]] == ["SMT-01", "IC-01"]
    assert s["worst_machine"]["machine"] == "SMT-01"
    assert s["by_machine"][0]["shortfall"] == 160   # (100-0) + (100-40)
    # Per-machine attainment inside the shift is on the machine's OWN
    # actual/planned (Night's today plan N-4 on IC-01 is INCLUDED here, unlike
    # the due-only headline): SMT-01 = (0+40)/(100+100) = 20%;
    # IC-01 = (60+10)/(100+100) = 35%.  Pin both rates and IC-01's shortfall so a
    # wrong basis (e.g. due-only, which would read IC-01 as 60/100 = 60%) is caught.
    assert s["by_machine"][0]["attainment_rate"] == 20   # SMT-01 (worst)
    assert s["by_machine"][1]["attainment_rate"] == 35   # IC-01
    assert s["by_machine"][1]["shortfall"] == 130        # 200 - 70

    # chase: missed first, then behind by biggest shortfall; Day's plans excluded
    assert [c["plan_no"] for c in s["chase"]] == ["N-1", "N-2", "N-3"]
    assert s["chase"][0]["state"] == "missed" and s["chase"][0]["shortfall"] == 100

    assert len(s["daily"]) == 7
    # the daily series carries only this shift's units (Day's 200 stay out)
    assert sum(d["planned"] for d in s["daily"]) == 400


def test_shift_drilldown_unknown_shift_is_empty_safe():
    db = _fresh_session()
    _machine(db, 1, "SMT-01")
    db.add(_plan("D-1", 1, 100, 100, -1, shift="Day"))
    db.commit()

    s = schedule.build_shift_adherence(db, "DEFAULT", "Twilight")
    assert s["found"] is False and s["total"] == 0
    assert s["attainment_rate"] == 0 and s["shortfall_units"] == 0
    assert s["rank"] is None and s["by_machine"] == [] and s["worst_machine"] is None
    assert s["chase"] == [] and len(s["daily"]) == 7
    # the plant baseline still reads, so the drawer can show "vs plant" honestly
    assert s["plant_attainment_rate"] == 100


def test_worst_first_tiebreak_orders_by_attainment():
    db = _fresh_session()
    # 1:1 shift<->machine so the by_shift and by_machine rankings mirror each
    # other. Every plan is past-due, so the due-basis rate == the full rate.
    # Insertion order is [Z, M, A]; the correct tertiary key must reorder A
    # ahead of M (a dropped tertiary would leave stable-sort order [Z, M, A]).
    _machine(db, 1, "MC-Z")   # shift Z: 2 behind, HIGH rate (95%)
    _machine(db, 3, "MC-M")   # shift M: 1 behind, mid rate (50%)
    _machine(db, 2, "MC-A")   # shift A: 1 behind, LOW rate (10%)
    db.add_all([
        _plan("TZ-1", 1, 100, 95, -1, shift="Z"),   # behind (95/100)
        _plan("TZ-2", 1, 100, 95, -1, shift="Z"),   # behind -> Z due 190/200 = 95%
        _plan("TM-1", 3, 100, 50, -1, shift="M"),   # behind -> M due 50/100 = 50%
        _plan("TA-1", 2, 100, 10, -1, shift="A"),   # behind -> A due 10/100 = 10%
    ])
    db.commit()

    s = schedule.build_schedule_adherence(db, "DEFAULT")
    assert s["total"] == 4 and s["behind"] == 4 and s["missed"] == 0

    # Headline pooled over all-due: planned 100*4=400, actual 95+95+50+10=250.
    # round(250/400*100) = round(62.5) = 62 (Python banker's rounding).
    assert s["planned_units"] == 400 and s["actual_units"] == 250
    assert s["attainment_rate"] == 62

    # Worst-first ranking. Primary = missed (all 0). Secondary = behind count:
    # Z has 2 behind so it leads DESPITE the highest attainment (95%) -- proves
    # behind outranks attainment. Tertiary = lowest attainment first, so among
    # the two 1-behind shifts A (10%) precedes M (50%).
    assert [x["shift"] for x in s["by_shift"]] == ["Z", "A", "M"]
    by_shift = {x["shift"]: x for x in s["by_shift"]}
    assert by_shift["Z"]["attainment_rate"] == 95 and by_shift["Z"]["behind"] == 2
    assert by_shift["A"]["attainment_rate"] == 10 and by_shift["A"]["behind"] == 1
    assert by_shift["M"]["attainment_rate"] == 50 and by_shift["M"]["behind"] == 1

    # Same ranking and rates roll up per machine (1:1 with the shifts).
    assert [m["machine"] for m in s["by_machine"]] == ["MC-Z", "MC-A", "MC-M"]
    by_machine = {m["machine"]: m for m in s["by_machine"]}
    assert by_machine["MC-Z"]["attainment_rate"] == 95
    assert by_machine["MC-A"]["attainment_rate"] == 10
    assert by_machine["MC-M"]["attainment_rate"] == 50
    print("PASS worst-first ties break by attainment (behind > attainment > insertion order)")


def test_shift_drilldown_machine_tiebreak_by_attainment():
    db = _fresh_session()
    _machine(db, 1, "SMT-01")
    _machine(db, 2, "IC-01")
    # Both machines inside the shift: 1 behind, 0 missed -> tie on the first two
    # sort keys, so the per-machine attainment rate (lowest first) must decide.
    # Insert IC-01 first so a dropped tertiary key would leave it wrongly ahead
    # in stable-sort insertion order.
    db.add_all([
        _plan("TN-hi", 2, 100, 70, -1, shift="Night"),   # IC-01 behind, 70%
        _plan("TN-lo", 1, 100, 30, -1, shift="Night"),   # SMT-01 behind, 30%
    ])
    db.commit()

    s = schedule.build_shift_adherence(db, "DEFAULT", "Night")
    assert s["found"] is True and s["total"] == 2
    assert s["behind"] == 2 and s["missed"] == 0
    # Shift pooled over due: planned 200, actual 100 -> 50%.
    assert s["planned_units"] == 200 and s["actual_units"] == 100
    assert s["attainment_rate"] == 50

    # Worst-first among the tied machines: lowest attainment leads -> SMT-01 (30%).
    assert [m["machine"] for m in s["by_machine"]] == ["SMT-01", "IC-01"]
    assert s["by_machine"][0]["attainment_rate"] == 30 and s["by_machine"][0]["shortfall"] == 70
    assert s["by_machine"][1]["attainment_rate"] == 70 and s["by_machine"][1]["shortfall"] == 30
    assert s["worst_machine"]["machine"] == "SMT-01"
    print("PASS shift drill-down breaks machine ties by attainment")


if __name__ == "__main__":
    test_schedule_classifies_plans_and_rolls_up()
    test_schedule_shortfall_ordering_and_empty_safe()
    test_shift_drilldown_reads_against_the_plant()
    test_shift_drilldown_unknown_shift_is_empty_safe()
    test_worst_first_tiebreak_orders_by_attainment()
    test_shift_drilldown_machine_tiebreak_by_attainment()
    print("SCHEDULE OK: plans classified met/on-track/behind/missed; pooled attainment "
          "over plans due so far; per-shift + per-machine rollup (worst first); chase "
          "list (missed then behind, biggest shortfall); today's load; shift drill-down "
          "(vs plant, rank, machines inside the shift, chase); empty-safe")
