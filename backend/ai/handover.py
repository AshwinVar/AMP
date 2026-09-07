"""Shift handover — the end-of-shift summary to hand to the next crew (ADR-0007).

Composes the pillar read-models into one page: what was produced, how the plant
performed, what's still open to carry over (pending approvals, open escalations),
and what needs attention or is worth celebrating — so a shift lead can hand over
in a glance instead of retelling the whole shift. Reads only other read-models
(plus two open-item counts); auto-scoped to the tenant (ADR-0002); no storage.
"""
import models
from ai.briefing import build_briefing
# The single source of truth for what "open" means on an escalation — reused so
# handover's carry-over count means exactly what the escalation-queue read-model
# calls open, rather than a hand-rolled list that drifts from it.
from ai import escalations
from ai.production import build_production_summary

name = "handover"


def build_handover(db, tenant: str) -> dict:
    """A shift-handover digest: output + OEE, the open work to carry over
    (pending agent approvals, open escalations), the attention list, and the
    wins. Composes the briefing and production read-models (ADR-0007)."""
    briefing = build_briefing(db, tenant)
    prod = build_production_summary(db, tenant)
    pending = (db.query(models.AgentAction)
               .filter(models.AgentAction.tenant_code == tenant,
                       models.AgentAction.status == "Proposed").count())
    # "Open" = NOT in a terminal state, via the shared predicate, so this
    # carry-over count reconciles with the escalation queue on the very same
    # handover page. This file used to spell the rule out inline; it is now one
    # definition in ai/escalations.py because three OTHER surfaces spelled it
    # differently and disagreed with this one in both directions (#565).
    open_esc = (db.query(models.Escalation)
                .filter(models.Escalation.tenant_code == tenant,
                        escalations.open_clause()).count())
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
