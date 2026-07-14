"""Recommendations — turn AI findings into stored, per-tenant suggestions (ADR-0003).

Maps the platform's ``Recommendation`` value object onto the ``ai_recommendations``
table, with a light dedupe so the same open suggestion isn't stored twice. The
tenant is stamped automatically on insert (ADR-0002).
"""
import models
from ai.base import Recommendation

name = "recommendations"


def from_risk(risk_row) -> Recommendation:
    """Build a maintenance recommendation from a Prediction risk row."""
    return Recommendation(
        recommendation_type="predictive_maintenance",
        title=f"{risk_row['machine_name']}: {risk_row['risk_level'].lower()} failure risk",
        message=risk_row["recommendation"] + " (" + ", ".join(risk_row["reasons"]) + ")",
        severity=risk_row["risk_level"],
        confidence=risk_row["risk_score"],
        related_machine_id=risk_row["machine_id"],
    )


def from_low_stock(event) -> Recommendation:
    """Build a reorder recommendation from an InventoryLow event."""
    return Recommendation(
        recommendation_type="reorder_stock",
        title=f"Reorder {event.item_name} ({event.item_code})",
        message=(
            f"Stock has fallen to {event.current_stock}, at or below the reorder "
            f"level of {event.reorder_level}. Raise a purchase order to replenish."
        ),
        severity="High" if event.current_stock <= 0 else "Medium",
        confidence=90,
    )


def persist(db, rec: Recommendation) -> bool:
    """Store a recommendation unless an identical *open* one already exists.

    Dedupe is keyed on (type, title): the title already identifies the subject —
    a machine for maintenance, an item for reorder — so this is generic across
    recommendation kinds. ``tenant_code`` is auto-stamped by the scoping layer.
    Returns ``True`` if a new row was written.
    """
    exists = (
        db.query(models.AIRecommendation)
        .filter(
            models.AIRecommendation.recommendation_type == rec.recommendation_type,
            models.AIRecommendation.title == rec.title,
            models.AIRecommendation.status == "Open",
        )
        .first()
    )
    if exists:
        return False
    db.add(models.AIRecommendation(
        recommendation_type=rec.recommendation_type,
        severity=rec.severity,
        title=rec.title,
        message=rec.message,
        related_machine_id=rec.related_machine_id,
        confidence=rec.confidence,
        status="Open",
    ))
    return True
