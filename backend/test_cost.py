"""Cost-of-losses read-model tests (ADR-0007, ADR-0010).

Measures the week's downtime + scrap in good units not made, prices them only with
the tenant's own unit value, and rolls up recorded costs by type. Run:  python backend/test_cost.py     (exit 0 = pass)
"""
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import oee_contract
from database import Base
from datetime import timedelta
from ai import cost


def _fresh_session(rate=None):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    if rate is not None:
        # The tenant's own margin per good unit: the only source of a £ (ADR-0010).
        db.add(models.TenantConfig(tenant_code="DEFAULT", plan="Pro", unit_value_gbp=rate))
        db.commit()
    return db


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
    db = _fresh_session(rate=3)
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=80, line="SMT"))
    # Job A ran 20 min over plan; Job B had a real 50-min stop. Same machine, no scrap.
    # Run rate = good / runtime = 150 / 170 good units a minute.
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=100, runtime_minutes=120,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=100,
                                   rejected_count=0))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=100, runtime_minutes=50,
                                   ideal_cycle_time_seconds=30, total_count=50, good_count=50,
                                   rejected_count=0))
    db.commit()

    s = cost.build_cost_summary(db, "DEFAULT")
    # per-record clamp: 0 (A) + 50 (B) = 50 min -> 50 x 150/170 = 44.1 -> 44 units -> £132
    assert s["downtime_minutes"] == 50
    assert s["downtime_lost_units"] == 44 and s["loss_cost"] == 132, s
    # the OLD aggregate clamp max(0, 200-170) = 30 min -> 26 units -> £78 would have understated it
    assert s["loss_cost"] != 78
    # the headline reconciles with EVERY drill-down it sits above
    assert s["loss_cost"] == sum(m["cost"] for m in s["by_machine"])
    assert s["loss_cost"] == sum(row["cost"] for row in s["by_line"])
    assert s["loss_cost"] == sum(d["cost"] for d in s["daily"])
    print("PASS headline downtime clamps per-record -> reconciles with its by_machine/by_line/daily")


def test_cost_prices_downtime_and_scrap_and_rolls_up_recorded():
    db = _fresh_session(rate=12.5)
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=80, line="SMT"))
    # 40 min downtime (480 planned - 440 runtime); 10 rejected units.
    # Run rate = 90 good / 440 run minutes, so 40 min ≈ 8.2 -> 8 good units.
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=90,
                                   rejected_count=10))
    db.add(models.CostRecord(cost_no="C-1", cost_type="Rework", description="rework", amount=300))
    db.add(models.CostRecord(cost_no="C-2", cost_type="Rework", description="rework2", amount=200))
    db.add(models.CostRecord(cost_no="C-3", cost_type="Energy", description="power", amount=150))
    db.commit()

    s = cost.build_cost_summary(db, "DEFAULT")
    assert s["has_data"] is True and s["priced"] is True and s["unit_value_gbp"] == 12.5
    assert s["downtime_minutes"] == 40 and s["downtime_lost_units"] == 8
    assert s["downtime_cost"] == 100                                    # 8 units x £12.50
    assert s["rejected_units"] == 10 and s["scrap_cost"] == 125         # 10 units x £12.50
    assert s["lost_units"] == 18 and s["loss_cost"] == 225
    assert s["biggest"] == "scrap"                                      # 10 units > 8 units
    # recorded costs grouped by type, worst first (the tenant's own amounts)
    assert s["recorded_total"] == 650
    assert s["by_type"][0] == {"type": "Rework", "amount": 500}
    row = {"downtime_minutes": 40, "rejected_units": 10, "downtime_lost_units": 8, "lost_units": 18,
           "downtime_cost": 100, "scrap_cost": 125, "cost": 225}
    # the loss attributed to the SMT line (the only line here)
    assert s["by_line"] == [{"line": "SMT", **row}]
    # and to the machine that incurred it, biggest first
    assert s["by_machine"] == [{"machine_id": 1, "name": "M1", **row}]
    assert "£12.50/unit" in s["losses"][1]["detail"], s["losses"]
    # The trend spans the calendar dates the rolling window TOUCHES — eight when
    # it opens mid-day, seven exactly at midnight. Pinning a fixed 7 was pinning
    # the defect: the partial eighth date's cost was in the headline and in no
    # bar, so the chart could sum to zero under a five-figure figure (#587).
    # Derived from the contract so it cannot go stale at midnight UTC.
    window = oee_contract.OeeWindow(cost.WINDOW_DAYS)
    touched = len({(window.start + timedelta(hours=h)).date()
                   for h in range(0, cost.WINDOW_DAYS * 24 + 1)})
    assert len(s["daily"]) == touched, (len(s["daily"]), touched)
    assert s["daily"][-1]["cost"] == 225 and s["daily"][-1]["lost_units"] == 18
    # The property that actually matters, and which this suite already had right.
    assert sum(d["cost"] for d in s["daily"]) == 225

    # empty -> no data, no crash; nothing lost, and no £ at all without a rate
    empty = cost.build_cost_summary(_fresh_session(), "DEFAULT")
    assert empty["has_data"] is False and empty["lost_units"] == 0 and empty["loss_cost"] is None
    assert empty["by_type"] == []
    assert cost.build_cost_summary(_fresh_session(rate=4), "DEFAULT")["loss_cost"] == 0


def test_machine_cost_is_the_full_uncapped_map_not_just_top_n():
    """`by_machine` is a biggest-first DISPLAY page capped at TOP_N (5); the twin
    floor-map overlay instead reads `machine_cost` / `machine_lost_units`, the FULL
    per-machine maps. With more loss-making machines than the cap, every one must
    appear with its real figure — a machine ranked outside the top 5 must NOT be
    dropped (it would paint as nothing on the map). Derived by hand."""
    db = _fresh_session(rate=10)
    # 7 machines, each with a DISTINCT, decreasing downtime so their ranks are
    # unambiguous: machine i has (8 - i) * 10 minutes of downtime, no scrap, and
    # makes exactly half a good unit per run minute, so the pooled run rate is 0.5:
    #   M1 -> 70 min -> 35 units -> £350 ... M7 -> 10 min -> 5 units -> £50
    expected = {}
    for i in range(1, 8):
        db.add(models.Machine(id=i, name=f"M{i}", status="Running", utilization=80, line="SMT"))
        down_min = (8 - i) * 10
        runtime = 480 - down_min
        db.add(models.ProductionRecord(machine_id=i, planned_minutes=480,
                                       runtime_minutes=runtime, ideal_cycle_time_seconds=30,
                                       total_count=runtime // 2, good_count=runtime // 2, rejected_count=0))
        expected[i] = down_min // 2 * 10
    db.commit()

    s = cost.build_cost_summary(db, "DEFAULT")

    # by_machine is still the capped display list — exactly TOP_N rows, biggest first.
    assert len(s["by_machine"]) == cost.TOP_N == 5
    assert [m["machine_id"] for m in s["by_machine"]] == [1, 2, 3, 4, 5]
    # machine_cost is the FULL map: all 7 machines, each with its independently-derived cost.
    assert s["machine_cost"] == expected, s["machine_cost"]
    assert s["machine_lost_units"] == {i: v // 10 for i, v in expected.items()}
    # The machines dropped from the display page (ranks 6-7) are present with real,
    # non-zero cost in the full map — the exact rows the overlay would otherwise lose.
    assert s["machine_cost"][6] == 100 and s["machine_cost"][7] == 50
    assert 6 not in {m["machine_id"] for m in s["by_machine"]}
    # The full map reconciles to the headline loss cost (parts sum to the whole, rule 3).
    assert sum(s["machine_cost"].values()) == s["loss_cost"]
    # The two views agree on the machines they share (one breakdown, rule 1).
    for m in s["by_machine"]:
        assert s["machine_cost"][m["machine_id"]] == m["cost"]

    # empty -> an empty map, not a crash.
    assert cost.build_cost_summary(_fresh_session(), "DEFAULT")["machine_cost"] == {}
    print("PASS machine_cost is the full uncapped per-machine map (ranks past TOP_N keep real cost)")


def test_summary_exposes_the_card_contract():
    # The Costing view's intel card (CostIntelCard.tsx) and CostSnapshot render
    # exactly these keys; pin them so a read-model edit can't silently break them.
    db = _fresh_session()
    s = cost.build_cost_summary(db, "DEFAULT")
    for k in ("has_data", "days", "priced", "unit_value_gbp", "loss_cost", "downtime_cost", "scrap_cost",
              "downtime_minutes", "rejected_units", "downtime_lost_units", "lost_units", "biggest",
              "by_line", "by_machine", "daily"):
        assert k in s, f"cost-summary missing card key {k!r}"
    print("PASS cost-summary exposes the keys the intel card renders")


if __name__ == "__main__":
    test_downtime_minutes_floors_per_record_not_on_the_net()
    test_summary_exposes_the_card_contract()
    test_headline_downtime_reconciles_with_breakdown_when_a_job_runs_over()
    test_cost_prices_downtime_and_scrap_and_rolls_up_recorded()
    test_machine_cost_is_the_full_uncapped_map_not_just_top_n()
    print("COST OK: losses in good units, priced only at the tenant's rate; recorded costs by type; empty-safe")
