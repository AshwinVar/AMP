"""AI reactions to domain events (ADR-0003).

The AI platform doesn't only answer HTTP requests — it *subscribes to the event
stream*. As more domain events flow (production completed, downtime started,
inventory low), the platform reacts: re-scoring machines and storing per-tenant
maintenance or reorder suggestions. This is where factory events become
intelligence.
"""
from ai import prediction, recommendations
from events import ProductionCompleted, DowntimeStarted, InventoryLow, event_bus

# Only surface a maintenance suggestion when the machine's risk reaches "High" or
# above (see predictive_engine.classify_risk) — keeps recommendations signal, not
# noise.
RISK_THRESHOLD = 55


def _recommend_maintenance_if_risky(db, machine_id) -> None:
    """Re-score one machine via Prediction and store a maintenance suggestion if
    its risk is elevated. Shared by the completion and downtime reactions."""
    if machine_id is None:
        return
    risk = prediction.risk_for_machine(db, machine_id)
    if risk and risk["risk_score"] >= RISK_THRESHOLD:
        recommendations.persist(db, recommendations.from_risk(risk))


def recommend_on_production_completed(event: ProductionCompleted, db) -> None:
    _recommend_maintenance_if_risky(db, event.machine_id)


def recommend_on_downtime_started(event: DowntimeStarted, db) -> None:
    _recommend_maintenance_if_risky(db, event.machine_id)


def recommend_reorder_on_inventory_low(event: InventoryLow, db) -> None:
    recommendations.persist(db, recommendations.from_low_stock(event))


def register(bus=event_bus) -> None:
    """Wire AI subscribers to the bus. Called once at startup."""
    bus.subscribe(ProductionCompleted, recommend_on_production_completed)
    bus.subscribe(DowntimeStarted, recommend_on_downtime_started)
    bus.subscribe(InventoryLow, recommend_reorder_on_inventory_low)
