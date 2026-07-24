"""Executive scorecard read-model tests (ADR-0007).

Four headline KPIs (OEE, good rate, on-time orders, cost of losses), each with a
tone, composed from the pillar read-models. Run:  python backend/test_scorecard.py
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import scorecard


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_prior_period_loss_cost_shares_the_per_record_downtime_basis():
    # The prior-period KPIs (used for the week-over-week delta) must value downtime
    # the SAME way as the live cost card — per record — or the delta is apples to
    # oranges. A prior week with a job that ran over (100/120) plus a real 50-min
    # stop (100/50) is 50 min of downtime, not max(0, 200-170)=30.
    recs = [SimpleNamespace(planned_minutes=100, runtime_minutes=120, total_count=100,
                            good_count=100, rejected_count=0, ideal_cycle_time_seconds=30),
            SimpleNamespace(planned_minutes=100, runtime_minutes=50, total_count=50,
                            good_count=50, rejected_count=0, ideal_cycle_time_seconds=30)]
    k = scorecard._period_kpis(recs)
    assert k["loss_cost"] == 50 * scorecard.DOWNTIME_COST_PER_MIN                      # 0+50 min -> $600
    assert k["loss_cost"] != max(0, 200 - 170) * scorecard.DOWNTIME_COST_PER_MIN       # not the old $360
    print("PASS scorecard prior-period loss_cost uses the per-record downtime basis (like-for-like WoW)")


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
    # One late order (overdue, undelivered) and one not yet due. Of the orders that
    # have come DUE, 0 of 1 are delivered -> delivery reliability 0%. The not-yet-due
    # order is NOT counted as a success (the old (total-late)/total basis read 50%).
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
    assert kpis["on_time"]["label"] == "Delivery reliability"
    assert kpis["on_time"]["value"] == 0 and kpis["on_time"]["tone"] == "bad"  # 0 of 1 due delivered
    assert kpis["loss_cost"]["unit"] == "$" and kpis["loss_cost"]["value"] > 0
    # deltas vs the prior week: good rate up 7 pts (good); cost down (good); reliability none
    assert kpis["good_rate"]["delta"] == 7 and kpis["good_rate"]["delta_tone"] == "good"
    assert kpis["loss_cost"]["delta"] < 0 and kpis["loss_cost"]["delta_tone"] == "good"
    assert kpis["oee"]["delta"] is not None
    assert kpis["on_time"]["delta"] is None

    # empty plant -> no data
    empty = scorecard.build_scorecard(_fresh_session(), "DEFAULT")
    assert empty["has_data"] is False


def _order(no, customer, qty, dispatched, due_offset_days, status="Pending"):
    return models.CustomerOrder(
        order_no=no, customer_name=customer, product_name="P",
        order_quantity=qty, dispatched_quantity=dispatched, status=status,
        due_date=(datetime.utcnow().date() + timedelta(days=due_offset_days)))


def test_delivery_reliability_kpi_is_honest_and_reconciles():
    from ai import delivery
    db = _fresh_session()
    db.add_all([
        _order("D-1", "Bugatti", 10, 10, 5),    # delivered (dispatched in full)
        _order("D-2", "Bugatti", 10, 0, -1),    # late (overdue, undelivered)
        _order("D-3", "Bugatti", 10, 0, 20),    # on-track (not yet due)
        _order("D-4", "Bugatti", 10, 0, 2),     # at-risk (due soon, not yet due)
    ])
    db.commit()

    s = scorecard.build_scorecard(db, "DEFAULT")
    on_time = {k["key"]: k for k in s["kpis"]}["on_time"]
    # Of the DUE orders (1 delivered + 1 late), 1 delivered -> 50%. The two not-yet-due
    # orders are held out; the old (total - late)/total basis would have read 75%.
    assert on_time["value"] == 50
    assert on_time["value"] != round((4 - 1) / 4 * 100)      # not the inflated 75%
    assert on_time["label"] == "Delivery reliability" and on_time["tone"] == "bad"

    # Reconcile the scorecard KPI with the delivery summary AND its per-customer
    # drill-down — one delivery-reliability definition across all three.
    summ = delivery.build_delivery_summary(db, "DEFAULT")
    detail = delivery.build_customer_detail(db, "DEFAULT", "Bugatti")
    assert summ["resolved"] == 2 and summ["reliability_rate"] == 50
    assert on_time["value"] == summ["reliability_rate"] == detail["reliability_rate"]


def test_delivery_reliability_kpi_is_none_when_nothing_has_come_due():
    db = _fresh_session()
    # Only not-yet-due orders -> no order has come due, so reliability is unknowable
    # (shown as "—"), never a misleading 0% or 100%.
    db.add_all([
        _order("N-1", "Bugatti", 10, 0, 10),    # on-track
        _order("N-2", "Bugatti", 10, 0, 2),     # at-risk
    ])
    db.commit()
    on_time = {k["key"]: k for k in scorecard.build_scorecard(db, "DEFAULT")["kpis"]}["on_time"]
    assert on_time["value"] is None and on_time["tone"] == "none" and on_time["delta"] is None


if __name__ == "__main__":
    test_prior_period_loss_cost_shares_the_per_record_downtime_basis()
    test_scorecard_headlines_one_kpi_per_pillar_with_tone()
    test_delivery_reliability_kpi_is_honest_and_reconciles()
    test_delivery_reliability_kpi_is_none_when_nothing_has_come_due()
    print("SCORECARD OK: one KPI per pillar (OEE / good rate / delivery reliability / cost of "
          "losses) with tone; reliability reconciles with the delivery summary + drill-down and "
          "holds out not-yet-due orders; None when nothing has come due; empty-safe")
