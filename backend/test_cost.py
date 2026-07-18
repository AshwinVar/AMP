"""Cost-of-losses read-model tests (ADR-0007).

Prices the week's downtime + scrap at standard rates and rolls up recorded costs
by type. Run:  python backend/test_cost.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import cost


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_cost_prices_downtime_and_scrap_and_rolls_up_recorded():
    db = _fresh_session()
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=80))
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

    # empty -> no data, no crash
    empty = cost.build_cost_summary(_fresh_session(), "DEFAULT")
    assert empty["has_data"] is False and empty["loss_cost"] == 0 and empty["by_type"] == []


if __name__ == "__main__":
    test_cost_prices_downtime_and_scrap_and_rolls_up_recorded()
    print("COST OK: downtime + scrap priced at standard rates (biggest flagged); recorded costs by type; empty-safe")
