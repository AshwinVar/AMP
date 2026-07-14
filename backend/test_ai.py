"""AI platform tests (ADR-0003 / PR #6).

Proves the platform skeleton:
  * Prediction wraps the rule engine without changing its result;
  * the ProductionCompleted subscriber turns an event into a stored, per-machine
    recommendation when (and only when) risk is elevated, and is idempotent;
  * register() wires the AI subscriber to the bus.

Run:  python backend/test_ai.py     (exit 0 = pass)
Also collectable by pytest.
"""
import json
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from predictive_engine import calculate_predictive_risk
from events import EventBus, ProductionCompleted, DowntimeStarted, InventoryLow, QualityInspectionFailed
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


def test_downtime_event_recommends_maintenance_when_risky():
    db = _fresh_session()
    db.add(models.Machine(id=1, name="PRESS-01", status="Breakdown", utilization=30))
    db.commit()
    ai_subscribers.recommend_on_downtime_started(
        DowntimeStarted(tenant_code="DEFAULT", machine_id=1, reason="Breakdown", duration="90 min"), db)
    db.commit()
    recs = db.query(models.AIRecommendation).filter_by(recommendation_type="predictive_maintenance").all()
    assert len(recs) == 1 and recs[0].related_machine_id == 1


def test_inventory_low_event_recommends_reorder_idempotently():
    db = _fresh_session()
    low = InventoryLow(tenant_code="DEFAULT", item_id=5, item_code="RM-STEEL-001",
                       item_name="Steel Rod", current_stock=3, reorder_level=10)
    ai_subscribers.recommend_reorder_on_inventory_low(low, db)
    db.commit()
    recs = db.query(models.AIRecommendation).filter_by(recommendation_type="reorder_stock").all()
    assert len(recs) == 1 and "RM-STEEL-001" in recs[0].title
    # a second identical event does not duplicate the open suggestion
    ai_subscribers.recommend_reorder_on_inventory_low(low, db)
    db.commit()
    assert db.query(models.AIRecommendation).filter_by(recommendation_type="reorder_stock").count() == 1


def test_bus_publish_records_event_and_triggers_ai():
    db = _fresh_session()
    db.add(models.Machine(id=1, name="PRESS-01", status="Breakdown", utilization=30))
    db.commit()
    bus = EventBus()
    ai_subscribers.register(bus)
    bus.publish(DowntimeStarted(tenant_code="DEFAULT", machine_id=1, reason="Breakdown", duration="90 min"), db)
    db.commit()
    assert db.query(models.EventLog).filter_by(event_type="DowntimeStarted").count() == 1  # recorded
    assert db.query(models.AIRecommendation).count() == 1                                   # and reacted to


def test_quality_failed_event_recommends_investigation():
    db = _fresh_session()
    ai_subscribers.recommend_on_quality_failed(
        QualityInspectionFailed(tenant_code="DEFAULT", inspection_no="QC-1001",
                                failed_quantity=12, inspected_quantity=100,
                                machine_id=3, defect_category="surface finish"), db)
    db.commit()
    recs = db.query(models.AIRecommendation).filter_by(recommendation_type="quality_defect").all()
    assert len(recs) == 1 and recs[0].related_machine_id == 3
    assert "QC-1001" in recs[0].title


def test_copilot_service_exposes_platform_surface():
    from ai import copilot
    assert copilot.name == "copilot"
    assert isinstance(copilot.is_enabled(), bool)   # returns cleanly whether or not a key is set
    assert callable(copilot.register)


def test_insights_feed_is_unified_and_tenant_scoped():
    from ai import insights
    db = _fresh_session()
    # DEFAULT: an open recommendation, a notable event, and a routine event
    db.add(models.AIRecommendation(tenant_code="DEFAULT", recommendation_type="reorder_stock",
                                   title="Reorder Steel", message="...", status="Open", severity="Medium"))
    db.add(models.EventLog(tenant_code="DEFAULT", event_type="DowntimeStarted", event_version=1,
                           payload=json.dumps({"machine_id": 1, "reason": "Breakdown", "duration": "90 min"})))
    db.add(models.AgentAction(tenant_code="DEFAULT", agent="maintenance", action_type="open_task",
                              summary="Open a Critical maintenance task for PRESS-01",
                              ref_kind="maintenance_task", ref_id=7, severity="Critical", status="Proposed"))
    db.add(models.EventLog(tenant_code="DEFAULT", event_type="ProductionCompleted", event_version=1, payload="{}"))
    # another tenant's data must NOT leak (event_log + agent_actions scoped by tenant)
    db.add(models.AIRecommendation(tenant_code="GMATS", recommendation_type="reorder_stock",
                                   title="GMATS secret rec", message="...", status="Open"))
    db.add(models.AgentAction(tenant_code="GMATS", agent="maintenance", action_type="open_task",
                              summary="GMATS secret action", ref_kind="maintenance_task", ref_id=1, status="Proposed"))
    db.commit()

    feed = insights.build_feed(db, "DEFAULT")
    assert {i["source"] for i in feed} == {"recommendation", "event", "action"}   # advice + context + action
    assert len(feed) == 3                                                          # routine + other-tenant excluded
    assert not any("GMATS" in i["title"] for i in feed)                            # no cross-tenant leak
    assert not any(i["kind"] == "ProductionCompleted" for i in feed)              # routine events omitted
    rec = next(i for i in feed if i["source"] == "recommendation")
    evt = next(i for i in feed if i["source"] == "event")
    act = next(i for i in feed if i["source"] == "action")
    assert rec["ref_id"] is not None and evt["ref_id"] is None                     # recs/actions action-able; events not
    assert act["kind"] == "open_task" and act["severity"] == "Critical" and act["ref_id"] is not None


def test_insights_surfaces_only_proposed_actions():
    from ai import insights
    db = _fresh_session()
    db.add(models.AgentAction(tenant_code="DEFAULT", agent="quality", action_type="open_task",
                              summary="Inspect machine #4 for defects", ref_kind="maintenance_task", ref_id=1, status="Proposed"))
    # an already-decided action (e.g. an auto-approved reorder) must NOT clutter the live feed
    db.add(models.AgentAction(tenant_code="DEFAULT", agent="reorder", action_type="draft_po",
                              summary="Auto-approved reorder", ref_kind="purchase_order", ref_id=2, status="Approved"))
    db.commit()
    actions = [i for i in insights.build_feed(db, "DEFAULT") if i["source"] == "action"]
    assert len(actions) == 1 and actions[0]["kind"] == "open_task"
    assert "machine #4" in actions[0]["title"]   # only the Proposed one surfaces


def test_register_wires_ai_subscriber_to_the_bus():
    bus = EventBus()
    ai_subscribers.register(bus)
    assert ai_subscribers.recommend_on_production_completed in bus._subscribers[ProductionCompleted]
    assert ai_subscribers.recommend_on_downtime_started in bus._subscribers[DowntimeStarted]
    assert ai_subscribers.recommend_reorder_on_inventory_low in bus._subscribers[InventoryLow]
    assert ai_subscribers.recommend_on_quality_failed in bus._subscribers[QualityInspectionFailed]


if __name__ == "__main__":
    test_prediction_wraps_engine_without_changing_result()
    test_subscriber_recommends_only_when_risk_is_elevated()
    test_downtime_event_recommends_maintenance_when_risky()
    test_inventory_low_event_recommends_reorder_idempotently()
    test_quality_failed_event_recommends_investigation()
    test_bus_publish_records_event_and_triggers_ai()
    test_copilot_service_exposes_platform_surface()
    test_insights_feed_is_unified_and_tenant_scoped()
    test_insights_surfaces_only_proposed_actions()
    test_register_wires_ai_subscriber_to_the_bus()
    print("AI OK: prediction + copilot wrapped; production/downtime -> maintenance, inventory-low -> reorder, quality-failed -> defect; insights feed unified + tenant-scoped; wired")
