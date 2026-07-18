"""Executive scorecard read-model tests (ADR-0007).

Four headline KPIs (OEE, good rate, on-time orders, cost of losses), each with a
tone, composed from the pillar read-models. Run:  python backend/test_scorecard.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import scorecard


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_scorecard_headlines_one_kpi_per_pillar_with_tone():
    db = _fresh_session()
    now = datetime.utcnow()
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=90, line="SMT"))
    # current week: good rate 97, loss cost 40min*12 + 3*25 = 555
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=97,
                                   rejected_count=3, created_at=now))
    # prior week (8 days ago): good rate 90, loss cost 60min*12 + 10*25 = 970
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=420,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=90,
                                   rejected_count=10, created_at=now - timedelta(days=8)))
    # one late order, one on time -> on-time = 1/2 = 50%
    db.add(models.CustomerOrder(order_no="O-1", customer_name="Bugatti", product_name="P",
                                order_quantity=10, dispatched_quantity=0, status="Pending",
                                due_date=(now.date() - timedelta(days=1))))
    db.add(models.CustomerOrder(order_no="O-2", customer_name="Mercedes", product_name="P",
                                order_quantity=10, dispatched_quantity=0, status="Pending",
                                due_date=(now.date() + timedelta(days=20))))
    db.commit()

    s = scorecard.build_scorecard(db, "DEFAULT")
    assert s["has_data"] is True
    kpis = {k["key"]: k for k in s["kpis"]}
    assert set(kpis) == {"oee", "good_rate", "on_time", "loss_cost"}
    assert all(k["tone"] in {"good", "warn", "bad", "none"} for k in s["kpis"])
    assert kpis["good_rate"]["value"] == 97 and kpis["good_rate"]["unit"] == "%"
    assert kpis["on_time"]["value"] == 50                      # 1 of 2 orders not late
    assert kpis["loss_cost"]["unit"] == "$" and kpis["loss_cost"]["value"] > 0
    # deltas vs the prior week: good rate up 7 pts (good); cost down (good); on-time has none
    assert kpis["good_rate"]["delta"] == 7 and kpis["good_rate"]["delta_tone"] == "good"
    assert kpis["loss_cost"]["delta"] < 0 and kpis["loss_cost"]["delta_tone"] == "good"
    assert kpis["oee"]["delta"] is not None
    assert kpis["on_time"]["delta"] is None

    # empty plant -> no data
    empty = scorecard.build_scorecard(_fresh_session(), "DEFAULT")
    assert empty["has_data"] is False


if __name__ == "__main__":
    test_scorecard_headlines_one_kpi_per_pillar_with_tone()
    print("SCORECARD OK: one KPI per pillar (OEE / good rate / on-time orders / cost of losses) with tone; empty-safe")
