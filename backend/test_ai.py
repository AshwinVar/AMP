"""AI platform tests (ADR-0003 / PR #6).

Proves the platform skeleton:
  * Prediction wraps the rule engine without changing its result;
  * the ProductionCompleted subscriber turns an event into a stored, per-machine
    recommendation when (and only when) risk is elevated, and is idempotent;
  * register() wires the AI subscriber to the bus.

Run:  python backend/test_ai.py     (exit 0 = pass)
Also collectable by pytest.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from predictive_engine import calculate_predictive_risk
from events import EventBus, ProductionCompleted
from ai import prediction, recommendations
import ai.subscribers as ai_subscribers


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_prediction_wraps_engine_without_changing_result():
    machines = [models.Machine(id=1, name="CNC-01", status="Running", utilization=70)]
    assert prediction.assess_risk(machines, [], [], [], []) == \
        calculate_predictive_risk(machines, [], [], [], [])


def test_subscriber_recommends_only_when_risk_is_elevated():
    db = _fresh_session()
    # high risk: breakdown (+35) + utilization < 40 (+20) = 55 -> "High"
    db.add(models.Machine(id=1, name="PRESS-01", status="Breakdown", utilization=30))
    # low risk: a healthy machine
    db.add(models.Machine(id=2, name="CNC-02", status="Running", utilization=65))
    db.commit()

    def completed(machine_id):
        return ProductionCompleted(
            tenant_code="DEFAULT", work_order_id=1, work_order_no="WO-1",
            part_number="P-1", quantity=10, machine_id=machine_id,
        )

    ai_subscribers.recommend_on_production_completed(completed(2), db)  # low risk
    db.commit()
    assert db.query(models.AIRecommendation).count() == 0

    ai_subscribers.recommend_on_production_completed(completed(1), db)  # high risk
    db.commit()
    recs = db.query(models.AIRecommendation).all()
    assert len(recs) == 1
    assert recs[0].related_machine_id == 1
    assert recs[0].recommendation_type == "predictive_maintenance"

    # idempotent: a second identical event doesn't duplicate the open suggestion
    ai_subscribers.recommend_on_production_completed(completed(1), db)
    db.commit()
    assert db.query(models.AIRecommendation).count() == 1


def test_register_wires_ai_subscriber_to_the_bus():
    bus = EventBus()
    ai_subscribers.register(bus)
    assert ai_subscribers.recommend_on_production_completed in bus._subscribers[ProductionCompleted]


if __name__ == "__main__":
    test_prediction_wraps_engine_without_changing_result()
    test_subscriber_recommends_only_when_risk_is_elevated()
    test_register_wires_ai_subscriber_to_the_bus()
    print("AI OK: prediction wraps the engine; ProductionCompleted -> stored recommendation when risk is elevated; subscriber wired")
