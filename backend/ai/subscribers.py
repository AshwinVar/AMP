"""AI reactions to domain events (ADR-0003).

The AI platform doesn't only answer HTTP requests — it *subscribes to the event
stream*. When production completes, Prediction re-scores that machine and, if the
risk is elevated, Recommendations stores a maintenance suggestion for the tenant.
This is the point where a factory event becomes intelligence.
"""
from ai import prediction, recommendations
from events import ProductionCompleted, event_bus

# Only surface a suggestion when the machine's risk reaches "High" or above
# (see predictive_engine.classify_risk) — keeps recommendations signal, not noise.
RISK_THRESHOLD = 55


def recommend_on_production_completed(event: ProductionCompleted, db) -> None:
    if event.machine_id is None:
        return
    risk = prediction.risk_for_machine(db, event.machine_id)
    if not risk or risk["risk_score"] < RISK_THRESHOLD:
        return
    recommendations.persist(db, recommendations.from_risk(risk))


def register(bus=event_bus) -> None:
    """Wire AI subscribers to the bus. Called once at startup."""
    bus.subscribe(ProductionCompleted, recommend_on_production_completed)
