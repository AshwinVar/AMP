"""Shift handover — the end-of-shift summary to hand to the next crew (ADR-0007).

Composes the pillar read-models into one page: what was produced, how the plant
performed, what's still open to carry over (pending approvals, open escalations),
and what needs attention or is worth celebrating — so a shift lead can hand over
in a glance instead of retelling the whole shift. Reads only other read-models
(plus two open-item counts); auto-scoped to the tenant (ADR-0002); no storage.
"""
from sqlalchemy import func

import models
from ai.briefing import build_briefing
# The single source of truth for which escalation states are TERMINAL — reused so
# handover's carry-over count means exactly what the escalation-queue read-model
# calls "open" (rule-1), rather than a third hand-rolled list that drifts from it.
from ai.escalations import CLOSED_STATUSES
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
    # "Open" = NOT in a terminal/closed state — the SAME set ai/escalations.py
    # (the escalation-queue read-model) calls open, so this carry-over count
    # reconciles with the queue on the very same handover page (rule-3).
    #
    # The old whitelist `.in_(("Proposed", "Open", "In Progress"))` DROPPED a
    # NULL-status escalation: status is Column(String, default="Open") WITHOUT
    # nullable=False, so a raw-SQL / migration / cleared-field row can hold a
    # genuine NULL, and SQL's `status IN (...)` is UNKNOWN (not TRUE) for it. That
    # silently undercounted the open work to hand over — the same NULL-status drop
    # already fixed across the open-escalation readers (analytics /escalations
    # #295, system-notifications #403, the escalation queue #421, saas
    # tenant-activity #408). It also missed any non-terminal status outside those
    # three exact strings. Excluding the closed states instead — NULL-, case- and
    # whitespace-safe via COALESCE+LOWER+TRIM, the exact idiom ai/escalations.py's
    # own prefilter uses — counts a NULL status as open (COALESCE("") is not in
    # CLOSED_STATUSES) and matches _is_closed() row-for-row.
    open_esc = (db.query(models.Escalation)
                .filter(models.Escalation.tenant_code == tenant,
                        func.lower(func.trim(func.coalesce(models.Escalation.status, "")))
                        .notin_(CLOSED_STATUSES)).count())
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
