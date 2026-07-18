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
