"""Morning briefing — the plant's "what needs attention right now" digest (ADR-0007).

Answers the first question an operator or owner asks at the top of a shift:
"what changed, what's wrong, and what's going well?" It composes the existing
pillar read-models — OEE, losses, downtime, quality, WIP flow, inventory — plus
a glance at live machine status into one prioritized feed: a short list of
ranked alerts (most urgent first) and a couple of wins, so the exec home can
lead with a decision instead of a wall of dashboards.

A read-model that reads only other read-models (and one machine-status query);
auto-scoped to the tenant by the query layer (ADR-0002); it adds no storage.
"""
import models
from ai.oee import build_oee_summary
from ai.losses import build_losses_summary
from ai.downtime import build_downtime_summary
from ai.quality import build_quality_summary
from ai.flow import build_flow_summary
from ai.inventory import build_inventory_summary

name = "briefing"

FAIL_RATE_ALERT = 5                       # % fail rate worth flagging
DOWN_STATUSES = ("Breakdown", "Down", "Offline")   # hard-down, not planned maintenance
_SEV = {"high": 3, "medium": 2, "low": 1}          # for ranking the feed


def _plural(n: int) -> str:
    return "s" if n != 1 else ""


def _trend(daily: list) -> str:
    """Direction of OEE across the window: compare the last third of the days that
    actually ran to the first third. Flat when there's too little to tell (need a
    few producing days) or the move is under 2 points."""
    vals = [d["oee"] for d in daily if d["oee"] > 0]
    if len(vals) < 4:
        return "flat"
    n = max(1, len(vals) // 3)
    early = sum(vals[:n]) / n
    late = sum(vals[-n:]) / n
    if late - early >= 2:
        return "up"
    if early - late >= 2:
        return "down"
    return "flat"


def build_briefing(db, tenant: str) -> dict:
    """A prioritized digest for the top of the shift: headline OEE and its
    direction, ranked alerts drawn from every pillar read-model (most urgent
    first), and a couple of wins. Composes other read-models only; each
    underlying query is auto-scoped (ADR-0002)."""
    oee = build_oee_summary(db, tenant)
    plant = oee["plant"]
    if not plant["has_data"]:
        return {
            "has_data": False, "oee": 0, "oee_trend": "flat",
            "headline": "No production data yet.", "alerts": [], "wins": [],
        }

    losses = build_losses_summary(db, tenant)
    downtime = build_downtime_summary(db, tenant)
    quality = build_quality_summary(db, tenant)
    flow = build_flow_summary(db, tenant)
    inventory = build_inventory_summary(db, tenant)

    alerts: list = []

    # 1. Machines hard-down right now — the most time-sensitive signal.
    down = [m for m in db.query(models.Machine).all() if (m.status or "") in DOWN_STATUSES]
    if down:
        names = ", ".join(sorted(m.name for m in down))
        alerts.append({
            "key": "machines_down", "severity": "high",
            "title": f"{len(down)} machine{_plural(len(down))} down",
            "detail": names, "module": "machines",
        })

    # 2. Supply risk — an out-of-stock part will stop the line.
    if inventory["out_of_stock"] > 0:
        lead = inventory["items"][0]["item_name"] if inventory["items"] else ""
        alerts.append({
            "key": "out_of_stock", "severity": "high",
            "title": f"{inventory['out_of_stock']} item{_plural(inventory['out_of_stock'])} out of stock",
            "detail": (f"{lead} and more" if inventory["at_risk"] > 1 else lead),
            "module": "inventory",
        })
    elif inventory["at_risk"] > 0:
        pos = inventory["auto_pos_pending"]
        alerts.append({
            "key": "reorder", "severity": "medium",
            "title": f"{inventory['at_risk']} item{_plural(inventory['at_risk'])} at reorder level",
            "detail": (f"{pos} draft PO{_plural(pos)} awaiting approval" if pos else "no POs drafted yet"),
            "module": "inventory",
        })

    # 3. Biggest OEE loss lever (availability / performance / quality).
    if losses["has_data"] and losses["biggest"]:
        big = next((l for l in losses["losses"] if l["key"] == losses["biggest"]), None)
        if big and big["points"] > 0:
            alerts.append({
                "key": "oee_loss", "severity": "medium",
                "title": f"{big['label']} is the biggest OEE loss — {big['points']} pts",
                "detail": big["detail"], "module": "oee",
            })

    # 4. Quality — fail rate above threshold.
    if quality["inspections"] > 0 and quality["fail_rate"] >= FAIL_RATE_ALERT:
        worst = quality["by_machine"][0] if quality["by_machine"] else None
        alerts.append({
            "key": "quality", "severity": "medium",
            "title": f"Fail rate {quality['fail_rate']}% — above the {FAIL_RATE_ALERT}% line",
            "detail": (f"worst: {worst['name']} at {worst['fail_rate']}%" if worst else ""),
            "module": "quality",
        })

    # 5. Top downtime cause — a nudge toward the Pareto, lowest urgency.
    if downtime["total_events"] > 0 and downtime["top_reasons"]:
        top = downtime["top_reasons"][0]
        alerts.append({
            "key": "downtime", "severity": "low",
            "title": f"Top downtime cause: {top['reason']}",
            "detail": f"{top['count']} event{_plural(top['count'])} in {downtime['days']} days",
            "module": "downtime",
        })

    alerts.sort(key=lambda a: _SEV.get(a["severity"], 0), reverse=True)

    # Wins — a couple of positives so the briefing isn't only bad news.
    wins: list = []
    if plant["oee"] >= oee["world_class"]:
        wins.append({"title": f"OEE {plant['oee']}% — world-class",
                     "detail": f"at or above the {oee['world_class']}% benchmark"})
    if oee.get("best") and oee["best"]["oee"] >= oee["world_class"]:
        b = oee["best"]
        wins.append({"title": f"{b['name']} leading at {b['oee']}% OEE",
                     "detail": (f"{b['line']} line" if b.get("line") else "top machine")})
    if quality["inspections"] > 0 and quality["first_pass_yield"] >= 98:
        wins.append({"title": f"First-pass yield {quality['first_pass_yield']}%",
                     "detail": "quality running clean"})
    if flow["finished"] > 0:
        wins.append({"title": f"{flow['finished']} order{_plural(flow['finished'])} finished",
                     "detail": f"{flow['wip']} still in progress"})

    trend = _trend(oee["daily"])
    headline = (f"Plant OEE {plant['oee']}% ({trend}) · "
                f"{len(alerts)} thing{_plural(len(alerts))} need attention")

    return {
        "has_data": True,
        "oee": plant["oee"],
        "oee_trend": trend,
        "headline": headline,
        "alerts": alerts,
        "wins": wins[:3],
    }
