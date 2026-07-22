"""Read-model routes (ADR-0007) — the projection endpoints.

Every handler here is the same shape: a thin GET (or pure-read POST) that
returns one `ai.<module>.build_*` projection scoped to the caller's tenant.
They compose no state and mutate nothing, so they belong together, away from
main.py's transactional surface. Registered from main.py at import time via
`register(app)` — the same pattern as platform_routes / enterprise_inventory.

Adding a new read-model? Build the projection in `ai/`, give it a `test_*.py`,
then add its one-line endpoint here.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import ai
from auth import get_current_user
from database import SessionLocal
from tenancy import request_tenant


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


router = APIRouter(tags=["Read Models"], dependencies=[Depends(get_current_user)])
# Mission Control + twin composites


@router.get("/insights")
def get_insights(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Mission Control read-model (ADR-0003 step 3): open AI recommendations +
    # recent notable events, unified into one tenant-scoped feed.
    return ai.insights.build_feed(db, request_tenant(current_user))


@router.get("/machine-health")
def get_machine_health(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Machine Health twin (ADR-0006): a live per-machine snapshot composing state,
    # a health score from predictive risk, downtime, and open tasks/agent actions.
    return ai.twin.build_twins(db, request_tenant(current_user))


@router.get("/mission-control/pulse")
def get_mission_control_pulse(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Factory Pulse (ADR-0006): the owner's one-glance command header — fleet
    # health from the twins + agent workload from the impact rollup, composed.
    return ai.pulse.build_pulse(db, request_tenant(current_user))

# Pillar summaries


@router.get("/downtime-summary")
def get_downtime_summary(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Downtime summary (ADR-0007): fleet-wide downtime over the last 7 days —
    # total, top reasons (Pareto), worst machines, and a daily series.
    return ai.downtime.build_downtime_summary(db, request_tenant(current_user))


@router.get("/downtime-reason")
def get_downtime_reason(reason: str, db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Downtime reason drill-down (ADR-0007): for one reason over the last 7 days —
    # events, minutes lost, machines hit, a daily trend, and recent instances.
    return ai.downtime.build_downtime_reason(db, request_tenant(current_user), reason)


@router.get("/quality-summary")
def get_quality_summary(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Quality summary (ADR-0007): first-pass yield, fail rate, a defect Pareto,
    # and the worst machines by fail rate — over the tenant's inspections.
    return ai.quality.build_quality_summary(db, request_tenant(current_user))


@router.get("/quality-defect")
def get_quality_defect(category: str, db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Defect drill-down (ADR-0007): for one defect category — units failed
    # (rework/scrap split), the machines producing it, and recent inspections.
    return ai.quality.build_defect_detail(db, request_tenant(current_user), category)


@router.get("/production-summary")
def get_production_summary(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Production summary (ADR-0007): throughput and output quality over the last
    # 7 days — units good/rejected, good rate, top producers, and a daily series.
    return ai.production.build_production_summary(db, request_tenant(current_user))


@router.get("/oee-summary")
def get_oee_summary(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # OEE summary (ADR-0007): the plant's headline metric — one plant-level OEE
    # (Availability x Performance x Quality) over the last 7 days, the component
    # dragging it down, and a worst-first per-machine breakdown.
    return ai.oee.build_oee_summary(db, request_tenant(current_user))


@router.get("/inventory-summary")
def get_inventory_summary(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Inventory summary (ADR-0007): supply risk — items at/below reorder level
    # (worst coverage first), the out-of-stock count, and the Reorder agent's
    # drafted POs still awaiting approval.
    return ai.inventory.build_inventory_summary(db, request_tenant(current_user))


@router.get("/flow-summary")
def get_flow_summary(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # WIP flow (ADR-0007): work orders grouped by material state —
    # RAW -> SMT -> SEMI -> IC -> FIN — for the two-line pipeline view.
    return ai.flow.build_flow_summary(db, request_tenant(current_user))


@router.get("/shift-summary")
def get_shift_summary(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Shift performance (ADR-0007): attainment (actual vs target) per shift over
    # the last 7 days, with the best and worst shift.
    return ai.shift.build_shift_summary(db, request_tenant(current_user))


@router.get("/losses-summary")
def get_losses_summary(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # OEE losses (ADR-0007): the OEE gap attributed to availability / performance
    # / quality (points lost each) with the concrete cost of each.
    return ai.losses.build_losses_summary(db, request_tenant(current_user))


@router.get("/recovery-summary")
def get_recovery_summary(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # OEE recovery opportunity (ADR-0007): the gap to the world-class benchmark
    # and what closing it is worth in recoverable good units (window + annualised),
    # with the per-factor gap so the biggest lever is obvious.
    return ai.recovery.build_recovery_summary(db, request_tenant(current_user))


@router.get("/briefing")
def get_briefing(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Morning briefing (ADR-0007): the "what needs attention right now" digest —
    # headline OEE + trend, ranked alerts across every pillar, and a few wins.
    return ai.briefing.build_briefing(db, request_tenant(current_user))


@router.get("/delivery-summary")
def get_delivery_summary(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Order delivery outlook (ADR-0007): per-customer on-track / at-risk / late
    # order states, unit fulfillment, and the specific orders to chase.
    return ai.delivery.build_delivery_summary(db, request_tenant(current_user))


@router.get("/supply-summary")
def get_supply_summary(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Inbound supply outlook (ADR-0007): per-supplier received / on-track /
    # at-risk / late PO states, unit receipt rate, and the inbound POs to chase.
    return ai.supply.build_supply_summary(db, request_tenant(current_user))


@router.get("/schedule-summary")
def get_schedule_summary(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Schedule adherence (ADR-0007): are we hitting the production plan? Plant-wide
    # met / on-track / behind / missed plan states, a pooled attainment rate over
    # the plans due so far, per-shift and per-machine breakdowns (worst first), a
    # daily planned-vs-actual series, today's scheduled load, and the plans to chase.
    return ai.schedule.build_schedule_adherence(db, request_tenant(current_user))


@router.get("/cost-summary")
def get_cost_summary(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Cost of losses (ADR-0007): downtime + scrap priced at standard rates, and
    # recorded costs for the period rolled up by type.
    return ai.cost.build_cost_summary(db, request_tenant(current_user))


@router.get("/handover")
def get_handover(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Shift handover (ADR-0007): output + OEE, open work to carry over, attention
    # list and wins — the end-of-shift summary composed from the pillar read-models.
    return ai.handover.build_handover(db, request_tenant(current_user))


@router.get("/scorecard")
def get_scorecard(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Executive scorecard (ADR-0007): one headline KPI per pillar (OEE, good rate,
    # on-time orders, cost of losses), each with a tone.
    return ai.scorecard.build_scorecard(db, request_tenant(current_user))


@router.get("/twin-overlay")
def get_twin_overlay(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Digital-twin overlay (ADR-0007): per-machine OEE + cost of losses, keyed by
    # machine, so the floor map can heat by either metric.
    return ai.twin.build_twin_overlay(db, request_tenant(current_user))


@router.get("/maintenance-summary")
def get_maintenance_summary(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Maintenance work summary (ADR-0007): open tasks by priority, overdue +
    # pending-approval counts, and the tasks to do next.
    return ai.maintenance.build_maintenance_summary(db, request_tenant(current_user))


@router.get("/reliability-summary")
def get_reliability_summary(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Machine reliability (ADR-0007): fleet MTBF / MTTR / availability over 30 days
    # from downtime, a least-reliable-first per-machine breakdown, the reliability
    # bottleneck, and the failure-mode Pareto by repair time.
    return ai.reliability.build_reliability_summary(db, request_tenant(current_user))


@router.get("/connectivity-summary")
def get_connectivity_summary(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Edge connectivity (ADR-0007): is the OT edge alive? The headline
    # connectivity score (% of machines reporting fresh telemetry), per-state
    # counts (fresh / stale / dark), device online + signal good-quality rates,
    # instrumentation coverage, and a worst-first chase list of silent machines.
    return ai.connectivity.build_connectivity_summary(db, request_tenant(current_user))


@router.get("/compliance-summary")
def get_compliance_summary(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Compliance document summary (ADR-0007): review load — overdue / due-soon /
    # pending-approval counts, a status breakdown, and the docs to review next.
    return ai.compliance.build_compliance_summary(db, request_tenant(current_user))


@router.get("/search")
def global_search(q: str = "", db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Global entity search: one query across machines, work orders, customer
    # orders, inventory, maintenance, escalations and documents — each hit
    # carrying the dashboard view that opens it.
    return ai.search.build_search(db, request_tenant(current_user), q)


@router.get("/weekly-report")
def get_weekly_report(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Weekly plant report (ADR-0007): a Markdown report composing the scorecard,
    # cost, delivery and briefing read-models, ready to copy or download.
    return ai.report.build_weekly_report(db, request_tenant(current_user))

# Rule-first copilot (ADR-0003) — pure reads over the read-models


@router.post("/copilot/ask")
def copilot_ask(payload: dict, db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Answers a plant question from the read-models, no API key required.
    # Returns the answer text and the view that drills into it.
    return ai.assistant.answer(db, request_tenant(current_user), payload.get("question", ""))


@router.get("/copilot/digest")
def copilot_digest(db: Session = Depends(_get_db), current_user: dict = Depends(get_current_user)):
    # Copilot rundown (ADR-0003): a plain-English one-shot summary of the whole
    # plant, composed from the pillar read-models.
    return ai.assistant.digest(db, request_tenant(current_user))
