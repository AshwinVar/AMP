"""Shift handover — the end-of-shift summary to hand to the next crew (ADR-0007).

Composes the pillar read-models into one page: what was produced, how the plant
performed, what's still open to carry over (pending approvals, open escalations),
and what needs attention or is worth celebrating — so a shift lead can hand over
in a glance instead of retelling the whole shift. Reads only other read-models
(plus two open-item counts); auto-scoped to the tenant (ADR-0002); no storage.
"""
import models
from ai.briefing import build_briefing
from ai.production import build_production_summary

name = "handover"

# Escalation states still open at handover (anything not resolved/cancelled).
_OPEN_ESCALATION = ("Proposed", "Open", "In Progress")


def build_handover(db, tenant: str) -> dict:
    """A shift-handover digest: output + OEE, the open work to carry over
    (pending agent approvals, open escalations), the attention list, and the
    wins. Composes the briefing and production read-models (ADR-0007)."""
    briefing = build_briefing(db, tenant)
    prod = build_production_summary(db, tenant)
    pending = (db.query(models.AgentAction)
               .filter(models.AgentAction.tenant_code == tenant,
                       models.AgentAction.status == "Proposed").count())
    open_esc = (db.query(models.Escalation)
                .filter(models.Escalation.tenant_code == tenant,
                        models.Escalation.status.in_(_OPEN_ESCALATION)).count())
    return {
        "has_data": briefing["has_data"],
        "oee": briefing["oee"],
        "oee_trend": briefing["oee_trend"],
        "produced": {
            "good": prod["good"], "total": prod["total"],
            "good_rate": prod["good_rate"], "runs": prod["runs"],
        },
        "open_work": {"pending_approvals": pending, "open_escalations": open_esc},
        "attention": briefing["alerts"],
        "wins": briefing["wins"],
    }
