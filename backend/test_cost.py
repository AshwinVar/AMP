"""Cost-of-losses read-model tests (ADR-0007).

Prices the week's downtime + scrap at standard rates and rolls up recorded costs
by type. Run:  python backend/test_cost.py     (exit 0 = pass)
"""
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import cost


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_downtime_minutes_floors_per_record_not_on_the_net():
    # A job that runs OVER its planned minutes must contribute 0 downtime, never a
    # negative that offsets a real stop on another job. So the total is the SUM of
    # per-record shortfalls, not max(0, sum(planned) - sum(runtime)).
    recs = [SimpleNamespace(planned_minutes=100, runtime_minutes=120),  # ran 20 over -> 0
            SimpleNamespace(planned_minutes=100, runtime_minutes=50)]   # real 50-min stop
    assert cost.downtime_minutes(recs) == 50            # 0 + 50, NOT max(0, 200-170)=30
    assert cost.downtime_minutes([]) == 0               # empty-safe
    assert cost.downtime_minutes([SimpleNamespace(planned_minutes=None, runtime_minutes=None)]) == 0
    print("PASS downtime_minutes floors each record at 0 (an over-run can't offset a real stop)")


def test_headline_downtime_reconciles_with_breakdown_when_a_job_runs_over():
    # OEE caps availability at 100%, so raw runtime > planned is real, ingestible
    # data. When it happens, the headline must still equal the sum of its own
    # by_machine / by_line / daily drill-down — never silently less.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=80, line="SMT"))
    # Job A ran 20 min over plan; Job B had a real 50-min stop. Same machine, no scrap.
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=100, runtime_minutes=120,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=100,
                                   rejected_count=0))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=100, runtime_minutes=50,
                                   ideal_cycle_time_seconds=30, total_count=50, good_count=50,
                                   rejected_count=0))
    db.commit()

    s = cost.build_cost_summary(db, "DEFAULT")
    # per-record clamp: 0 (A) + 50 (B) = 50 min of real downtime -> $600
    assert s["downtime_minutes"] == 50
    assert s["loss_cost"] == 50 * cost.DOWNTIME_COST_PER_MIN                       # 600
    # the OLD aggregate clamp max(0, 200-170) = 30 min -> $360 would have understated it
    assert s["loss_cost"] != max(0, 200 - 170) * cost.DOWNTIME_COST_PER_MIN
    # the headline reconciles with EVERY drill-down it sits above
    assert s["loss_cost"] == sum(m["cost"] for m in s["by_machine"])
    assert s["loss_cost"] == sum(row["cost"] for row in s["by_line"])
    assert s["loss_cost"] == sum(d["cost"] for d in s["daily"])
    print("PASS headline downtime clamps per-record -> reconciles with its by_machine/by_line/daily")


def test_cost_prices_downtime_and_scrap_and_rolls_up_recorded():
    db = _fresh_session()
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=80, line="SMT"))
    # 40 min downtime (480 planned - 440 runtime); 10 rejected units
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=90,
                                   rejected_count=10))
    db.add(models.CostRecord(cost_no="C-1", cost_type="Rework", description="rework", amount=300))
    db.add(models.CostRecord(cost_no="C-2", cost_type="Rework", description="rework2", amount=200))
    db.add(models.CostRecord(cost_no="C-3", cost_type="Energy", description="power", amount=150))
    db.commit()

    s = cost.build_cost_summary(db, "DEFAULT")
    assert s["has_data"] is True
    assert s["downtime_minutes"] == 40 and s["downtime_cost"] == 40 * cost.DOWNTIME_COST_PER_MIN     # 480
    assert s["rejected_units"] == 10 and s["scrap_cost"] == 10 * cost.SCRAP_COST_PER_UNIT            # 250
    assert s["loss_cost"] == 480 + 250                                                              # 730
    assert s["biggest"] == "downtime"                                                               # 480 > 250
    # recorded costs grouped by type, worst first
    assert s["recorded_total"] == 650
    assert s["by_type"][0] == {"type": "Rework", "amount": 500}
    # the loss cost attributed to the SMT line (the only line here)
    assert s["by_line"] == [{"line": "SMT", "downtime_cost": 480, "scrap_cost": 250, "cost": 730}]
    # and to the machine that incurred it, costliest first
    assert s["by_machine"] == [{"machine_id": 1, "name": "M1", "downtime_cost": 480, "scrap_cost": 250, "cost": 730}]
    # 7-day trend; today's run carries the whole $730
    assert len(s["daily"]) == 7 and s["daily"][-1]["cost"] == 730
    assert sum(d["cost"] for d in s["daily"]) == 730

    # empty -> no data, no crash
    empty = cost.build_cost_summary(_fresh_session(), "DEFAULT")
    assert empty["has_data"] is False and empty["loss_cost"] == 0 and empty["by_type"] == []


def test_machine_cost_is_the_full_uncapped_map_not_just_top_n():
    """`by_machine` is a costliest-first DISPLAY page capped at TOP_N (5); the twin
    floor-map overlay instead reads `machine_cost`, the FULL per-machine cost map.
    With more cost-incurring machines than the cap, every one must appear in
    machine_cost with its real figure — a machine ranked outside the top 5 must NOT
    be dropped (it would paint as £0 on the map). Costs are derived by hand."""
    db = _fresh_session()
    # 7 machines, each with a DISTINCT, decreasing downtime so their ranks are
    # unambiguous: machine i has (8 - i) * 10 minutes of downtime, no scrap.
    #   M1 -> 70 min -> $840 ... M7 -> 10 min -> $120   (all > 0, all distinct)
    expected = {}
    for i in range(1, 8):
        db.add(models.Machine(id=i, name=f"M{i}", status="Running", utilization=80, line="SMT"))
        down_min = (8 - i) * 10
        db.add(models.ProductionRecord(machine_id=i, planned_minutes=480,
                                       runtime_minutes=480 - down_min, ideal_cycle_time_seconds=30,
                                       total_count=100, good_count=100, rejected_count=0))
        expected[i] = down_min * cost.DOWNTIME_COST_PER_MIN
    db.commit()

    s = cost.build_cost_summary(db, "DEFAULT")

    # by_machine is still the capped display list — exactly TOP_N rows, costliest first.
    assert len(s["by_machine"]) == cost.TOP_N == 5
    assert [m["machine_id"] for m in s["by_machine"]] == [1, 2, 3, 4, 5]
    # machine_cost is the FULL map: all 7 machines, each with its independently-derived cost.
    assert s["machine_cost"] == expected, s["machine_cost"]
    # The machines dropped from the display page (ranks 6-7) are present with real,
    # non-zero cost in the full map — the exact rows the overlay would otherwise lose.
    assert s["machine_cost"][6] == 240 and s["machine_cost"][7] == 120
    assert 6 not in {m["machine_id"] for m in s["by_machine"]}
    # The full map reconciles to the headline loss cost (parts sum to the whole, rule 3).
    assert sum(s["machine_cost"].values()) == s["loss_cost"]
    # The two views agree on the machines they share (one `_row` formula, rule 1).
    for m in s["by_machine"]:
        assert s["machine_cost"][m["machine_id"]] == m["cost"]

    # empty -> an empty map, not a crash.
    assert cost.build_cost_summary(_fresh_session(), "DEFAULT")["machine_cost"] == {}
    print("PASS machine_cost is the full uncapped per-machine map (ranks past TOP_N keep real cost)")


if __name__ == "__main__":
    test_downtime_minutes_floors_per_record_not_on_the_net()
    test_headline_downtime_reconciles_with_breakdown_when_a_job_runs_over()
    test_cost_prices_downtime_and_scrap_and_rolls_up_recorded()
    test_machine_cost_is_the_full_uncapped_map_not_just_top_n()
    print("COST OK: downtime + scrap priced at standard rates (biggest flagged); recorded costs by type; empty-safe")
