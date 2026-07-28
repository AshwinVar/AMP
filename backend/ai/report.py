"""Weekly plant report — the week on one page, ready to send (ADR-0007).

Composes the pillar read-models into a single Markdown report an owner can copy
into an email or file at the end of the week: the scorecard with its week-on-week
moves, the cost of losses, the delivery position, what needs attention, and the
wins. Reads only other read-models; auto-scoped to the tenant (ADR-0002); no
storage.
"""
from datetime import datetime

from ai.scorecard import build_scorecard
from ai.cost import build_cost_summary
from ai.delivery import build_delivery_summary
from ai.briefing import build_briefing
from ai.compliance import build_compliance_summary
from ai.maintenance import build_maintenance_forecast, build_maintenance_execution
from ai.escalations import build_escalation_summary
from ai.schedule_load import build_schedule_load
from ai.stock_health import build_stock_health
from ai.supplier_performance import build_supplier_performance
from ai.flow import build_wip_aging
from ai.workforce import build_operator_summary

name = "report"


def _kpi_line(k) -> str:
    if k["value"] is None:
        val = "—"
    elif k["unit"] == "$":
        val = f"${k['value']:,}"
    else:
        val = f"{k['value']}{k['unit']}"
    delta = ""
    if k.get("delta") is not None and k["delta"] != 0:
        arrow = "up" if k["delta"] > 0 else "down"
        mag = f"${abs(k['delta']):,}" if k["unit"] == "$" else f"{abs(k['delta'])}{'' if k['unit'] == '%' else k['unit']}"
        delta = f" ({arrow} {mag} vs last week)"
    return f"- **{k['label']}**: {val}{delta}"


def build_weekly_report(db, tenant: str) -> dict:
    """A Markdown weekly report composed from the scorecard, cost, delivery and
    briefing read-models (ADR-0007). Returns the text plus a data echo so a UI can
    render or offer it for copy/download."""
    sc = build_scorecard(db, tenant)
    cost = build_cost_summary(db, tenant)
    delivery = build_delivery_summary(db, tenant)
    briefing = build_briefing(db, tenant)
    today = datetime.utcnow().date().isoformat()

    lines = [f"# Weekly Plant Report — {today}", ""]

    lines.append("## Scorecard")
    lines += [_kpi_line(k) for k in sc["kpis"]]
    lines.append("")

    lines.append("## Cost of losses")
    lines.append(f"- ${cost['loss_cost']:,} lost this week "
                 f"(downtime ${cost['downtime_cost']:,}, scrap ${cost['scrap_cost']:,})")
    if cost["by_machine"]:
        worst = cost["by_machine"][0]
        lines.append(f"- Costliest machine: {worst['name']} (${worst['cost']:,})")
    lines.append("")

    lines.append("## Delivery")
    lines.append(f"- {delivery['total']} orders · {delivery['fulfillment_rate']}% fulfilled · "
                 f"{delivery['late']} late · {delivery['at_risk']} at risk")
    for c in delivery["by_customer"]:
        lines.append(f"  - {c['customer']}: {c['orders']} orders, {c['fulfillment_rate']}% fulfilled")
    lines.append("")

    # Maintenance: the overdue backlog and what's booked ahead (forecast) plus the
    # PM-compliance rate (execution). Composed from the same read-models the CMMS
    # cards use, so the report and the dashboard never disagree. Shown only when
    # the tenant actually runs maintenance.
    maint_f = build_maintenance_forecast(db, tenant)
    maint_e = build_maintenance_execution(db, tenant)
    if maint_f["scheduled"] or maint_f["overdue"] or maint_e["completed"] or maint_e["backlog"]["open"]:
        lines.append("## Maintenance")
        lines.append(f"- {maint_f['overdue']} overdue, {maint_f['scheduled']} scheduled over the next "
                     f"14 days ({maint_f['due_today']} due today)")
        if maint_e["compliance_rate"] is not None:
            lines.append(f"- PM compliance (30d): {maint_e['compliance_rate']}% completed on plan")
        lines.append("")

    # The week ahead: booked schedule load — shown only when there's a board.
    sched = build_schedule_load(db, tenant)
    if sched["scheduled_jobs"] or sched["overdue"]:
        lines.append("## Week ahead")
        line = (f"- {sched['scheduled_jobs']} job{'s' if sched['scheduled_jobs'] != 1 else ''} "
                f"({sched['scheduled_minutes']:,} min) booked over the next {sched['horizon_days']} days")
        if sched["peak_day"]:
            line += f"; busiest day {sched['peak_day']['date']} ({sched['peak_day']['jobs']} jobs)"
        lines.append(line + ".")
        if sched["overdue"]:
            lines.append(f"- {sched['overdue']} schedule{'s' if sched['overdue'] != 1 else ''} "
                         f"past due and still open.")
        lines.append("")

    # Supply: overdue POs + the plant on-time rate — shown only when POs exist.
    sup = build_supplier_performance(db, tenant)
    if sup["orders_considered"]:
        lines.append("## Supply")
        line = f"- {sup['orders_considered']} POs in the last {sup['days']} days"
        if sup["plant_otd"] is not None:
            line += f" · {sup['plant_otd']}% delivered on time"
        lines.append(line + ".")
        if sup["overdue"]:
            worst = sup["worst"]
            blame = (f" — worst: {worst['supplier']} ({worst['overdue']})"
                     if worst and worst["overdue"] else "")
            lines.append(f"- {sup['overdue']} PO{'s' if sup['overdue'] != 1 else ''} overdue{blame}.")
        lines.append("")

    # Work in progress: the open work-order backlog and its promises — shown only
    # when there is one. Same read-model as the Work Orders backlog card (#340).
    wip = build_wip_aging(db, tenant)
    if wip["open"]:
        lines.append("## Work in progress")
        line = f"- {wip['open']} open work order{'s' if wip['open'] != 1 else ''}"
        if wip["late"]:
            line += f", {wip['late']} past planned end"
        if wip["oldest_days"] is not None:
            line += f", oldest open {wip['oldest_days']} day{'s' if wip['oldest_days'] != 1 else ''}"
        lines.append(line + ".")
        lines.append("")

    # Crew: who ran the jobs and the pooled quality rate — shown only when jobs
    # were booked. Same read-model as the Operator Terminal cards (#338).
    crew = build_operator_summary(db, tenant)
    if crew["jobs"]:
        lines.append("## Crew")
        line = (f"- {crew['operators']} operator{'s' if crew['operators'] != 1 else ''} ran "
                f"{crew['jobs']} job{'s' if crew['jobs'] != 1 else ''} "
                f"({crew['completed']} completed)")
        if crew["quality_rate"] is not None:
            line += f" at {crew['quality_rate']}% quality"
        lines.append(line + ".")
        if crew["needs_attention"]:
            na = crew["needs_attention"]
            lines.append(f"- Look at {na['operator']} first — "
                         f"{na['quality_rate']}% on {na['units']:,} units.")
        lines.append("")

    # Stock integrity: capital sitting idle — shown only when something is flagged.
    stock = build_stock_health(db, tenant)
    if stock["flagged_items"]:
        lines.append("## Stock integrity")
        lines.append(f"- {stock['dead']} dead item{'s' if stock['dead'] != 1 else ''} "
                     f"({stock['dead_units']:,} units idle) · {stock['overstocked']} overstocked "
                     f"({stock['excess_units']:,} units excess)")
        lines.append("")

    # Escalations: the queue and how fast it clears — shown only when there is one.
    esc = build_escalation_summary(db, tenant)
    if esc["open"] or esc["resolution"]["raised"]:
        lines.append("## Escalations")
        line = f"- {esc['open']} open ({esc['urgent_open']} high-priority)"
        if esc["oldest_open_days"] is not None:
            line += f", oldest open {esc['oldest_open_days']} day{'s' if esc['oldest_open_days'] != 1 else ''}"
        lines.append(line + ".")
        res = esc["resolution"]
        if res["raised"]:
            line = (f"- Last {esc['days']} days: {res['raised']} raised, {res['resolved']} resolved"
                    f" ({res['resolution_rate']}%)")
            if res["avg_resolution_hours"] is not None:
                line += f", avg {res['avg_resolution_hours']}h to close"
            lines.append(line + ".")
        lines.append("")

    comp = build_compliance_summary(db, tenant)
    if comp["total"]:
        lines.append("## Compliance")
        lines.append(f"- {comp['total']} controlled documents · {comp['overdue']} overdue for review · "
                     f"{comp['due_soon']} due soon · {comp['pending_approval']} unapproved")
        lines.append("")

    lines.append("## Needs attention")
    if briefing["alerts"]:
        lines += [f"- [{a['severity']}] {a['title']}"
                  + (f" — {a['detail']}" if a.get("detail") else "")
                  for a in briefing["alerts"]]
    else:
        lines.append("- Nothing outstanding — plant running clean.")
    lines.append("")

    lines.append("## Wins")
    lines += [f"- {w['title']}" for w in briefing["wins"]] or ["- —"]

    return {
        "has_data": sc["has_data"],
        "generated_at": datetime.utcnow().isoformat(),
        "markdown": "\n".join(lines),
    }
