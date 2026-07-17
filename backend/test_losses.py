"""OEE losses read-model tests (ADR-0007).

The OEE gap attributed to availability / performance / quality (OEE points lost
each, cascading so they sum to ~100 - OEE) with the concrete cost of each.

Run:  python backend/test_losses.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import losses


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_losses_attribute_the_oee_gap():
    db = _fresh_session()
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=90))
    # availability 440/480 = 92%; performance is low (ideal 30s x 100 = 3000 machine-s
    # vs 440 min runtime); quality 90/100 = 90%.
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=90, rejected_count=10))
    db.commit()

    s = losses.build_losses_summary(db, "DEFAULT")
    assert s["has_data"] is True
    by = {l["key"]: l for l in s["losses"]}
    assert set(by) == {"availability", "performance", "quality"}
    # the points sum (near-exactly) to the total OEE loss (100 - OEE)
    assert s["total_loss"] == 100 - s["oee"]
    assert abs(sum(l["points"] for l in s["losses"]) - s["total_loss"]) <= 1
    # here slow cycles dominate the loss
    assert s["biggest"] == "performance"
    assert by["availability"]["points"] == 8                  # 100*(1 - 0.92)
    assert "40 min" in by["availability"]["detail"]           # 480 - 440 downtime
    assert "10 units rejected" in by["quality"]["detail"]

    # empty -> no data, no crash
    empty = losses.build_losses_summary(_fresh_session(), "DEFAULT")
    assert empty["has_data"] is False and empty["losses"] == [] and empty["biggest"] is None


if __name__ == "__main__":
    test_losses_attribute_the_oee_gap()
    print("LOSSES OK: OEE gap attributed to availability/performance/quality (cascading, sums to 100-OEE); "
          "concrete costs; empty-safe")
