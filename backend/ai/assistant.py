"""Rule-first copilot — answers plant questions from the read-models (ADR-0003).

The AI platform is rule-first, LLM-optional: this answers natural-language
questions about the plant deterministically from the same read-models the
dashboard uses — no API key required — by routing the question to the right
pillar and phrasing its numbers into a sentence, with a suggested view to open.
A thin keyword router today; an LLM can layer on top later without changing the
contract. Every underlying query is auto-scoped to the tenant (ADR-0002).
"""
import models
from ai.oee import build_oee_summary
from ai.cost import build_cost_summary
from ai.delivery import build_delivery_summary
from ai.downtime import build_downtime_summary
from ai.quality import build_quality_summary
from ai.maintenance import build_maintenance_summary
from ai.inventory import build_inventory_summary
from ai.production import build_production_summary
from ai.flow import build_flow_summary
from ai.shift import build_shift_summary
from ai.briefing import build_briefing
from ai.scorecard import build_scorecard

name = "assistant"

DOWN_STATUSES = ("Breakdown", "Down", "Offline")


def _inventory(db, tenant):
    inv = build_inventory_summary(db, tenant)
    if inv["at_risk"] == 0:
        return "Stock is healthy — nothing is at or below its reorder level right now.", "inventory"
    lead = inv["items"][0]["item_name"] if inv["items"] else "the top item"
    oos = f" {inv['out_of_stock']} are already out of stock." if inv["out_of_stock"] else ""
    pos = f" The Reorder agent has {inv['auto_pos_pending']} draft PO(s) waiting." if inv["auto_pos_pending"] else ""
    return f"{inv['at_risk']} item(s) at or below reorder level — reorder {lead} first.{oos}{pos}", "inventory"


def _delivery(db, tenant):
    d = build_delivery_summary(db, tenant)
    if d["total"] == 0:
        return "There are no customer orders on the book.", "orders"
    parts = [f"{d['total']} orders, {d['fulfillment_rate']}% fulfilled by units"]
    if d["late"]:
        parts.append(f"{d['late']} late")
    if d["at_risk"]:
        parts.append(f"{d['at_risk']} at risk")
    ans = "; ".join(parts) + "."
    if d["at_risk_orders"]:
        w = d["at_risk_orders"][0]
        ans += f" Chase {w['order_no']} ({w['customer']}) first."
    return ans, "orders"


def _cost(db, tenant):
    c = build_cost_summary(db, tenant)
    if not c["has_data"]:
        return "No production this week, so there are no losses to cost.", "costing"
    ans = (f"Losses cost about ${c['loss_cost']:,} this week — "
           f"downtime ${c['downtime_cost']:,}, scrap ${c['scrap_cost']:,}.")
    if c["by_machine"]:
        w = c["by_machine"][0]
        ans += f" Costliest machine: {w['name']} (${w['cost']:,})."
    return ans, "costing"


def _quality(db, tenant):
    q = build_quality_summary(db, tenant)
    if q["inspections"] == 0:
        return "No quality inspections recorded yet.", "quality"
    ans = f"First-pass yield is {q['first_pass_yield']}% and the fail rate is {q['fail_rate']}%."
    if q["by_machine"]:
        w = q["by_machine"][0]
        ans += f" Worst machine: {w['name']} at {w['fail_rate']}%."
    if q["top_defects"]:
        ans += f" Top defect: {q['top_defects'][0]['category']}."
    return ans, "quality"


def _maintenance(db, tenant):
    m = build_maintenance_summary(db, tenant)
    if m["open"] == 0:
        return "No open maintenance tasks — the queue is clear.", "cmms"
    ans = f"{m['open']} open maintenance task(s)"
    if m["overdue"]:
        ans += f", {m['overdue']} overdue"
    if m["pending_approval"]:
        ans += f", {m['pending_approval']} awaiting your approval"
    ans += "."
    if m["tasks"]:
        t = m["tasks"][0]
        ans += f" Next up: {t['task_type']} on {t['machine']}."
    return ans, "cmms"


def _downtime(db, tenant):
    dt = build_downtime_summary(db, tenant)
    if dt["total_events"] == 0:
        return "No downtime events in the last 7 days.", "downtime"
    ans = f"{dt['total_events']} downtime events in the last {dt['days']} days."
    if dt["top_reasons"]:
        r = dt["top_reasons"][0]
        ans += f" Top cause: {r['reason']} ({r['count']})."
    if dt["by_machine"]:
        ans += f" Most affected: {dt['by_machine'][0]['name']}."
    return ans, "downtime"


def _machines(db, tenant):
    machines = db.query(models.Machine).all()
    down = [m for m in machines if (m.status or "") in DOWN_STATUSES]
    maint = [m for m in machines if (m.status or "") == "Maintenance"]
    if not down and not maint:
        return f"All {len(machines)} machines are running.", "machines"
    parts = []
    if down:
        parts.append(f"{len(down)} down ({', '.join(sorted(m.name for m in down))})")
    if maint:
        parts.append(f"{len(maint)} in maintenance")
    return "Machines needing attention: " + "; ".join(parts) + ".", "machines"


def _production(db, tenant):
    p = build_production_summary(db, tenant)
    if p["runs"] == 0:
        return "No production runs recorded in the last 7 days.", "analytics"
    ans = f"{p['good']:,} good units of {p['total']:,} ({p['good_rate']}% good) over {p['runs']} runs this week."
    if p["by_machine"]:
        ans += f" Top producer: {p['by_machine'][0]['name']}."
    return ans, "analytics"


def _flow(db, tenant):
    f = build_flow_summary(db, tenant)
    if f["total"] == 0:
        return "No work orders on the floor right now.", "workorders"
    stages = ", ".join(f"{s['label']} {s['count']}" for s in f["stages"])
    return (f"{f['wip']} work orders in progress, {f['finished']} finished "
            f"({f['total']} total). Pipeline: {stages}."), "workorders"


def _shift(db, tenant):
    sh = build_shift_summary(db, tenant)
    if sh["entries"] == 0:
        return "No shift data recorded yet.", "shifts"
    ans = (f"Shift attainment is {sh['attainment']}% ({sh['actual']:,} of "
           f"{sh['target']:,} target) over the last {sh['days']} days.")
    if sh.get("best"):
        ans += f" Best: {sh['best']['shift']} at {sh['best']['attainment']}%."
    if sh.get("worst") and sh["worst"] is not sh.get("best"):
        ans += f" Worst: {sh['worst']['shift']} at {sh['worst']['attainment']}%."
    return ans, "shifts"


def _oee(db, tenant):
    o = build_oee_summary(db, tenant)
    plant = o["plant"]
    if not plant["has_data"]:
        return "No production yet this week, so there's no OEE to report.", "executive"
    ans = (f"Plant OEE is {plant['oee']}% (availability {plant['availability']}%, "
           f"performance {plant['performance']}%, quality {plant['quality']}%). "
           f"Biggest drag: {o['biggest_drag']}.")
    if o.get("worst"):
        ans += f" Worst machine: {o['worst']['name']} at {o['worst']['oee']}%."
    return ans, "executive"


def _help(db, tenant):
    return (
        "I can answer about OEE & performance, the cost of losses, order delivery, "
        "downtime, quality, maintenance, inventory, machines (ask by name too), "
        "production, WIP, shifts, and week-on-week trends — from your live data. "
        "Try \"give me the rundown\" for the whole picture at once.",
        "overview",
    )


def _machine_named(db, question):
    """The machine whose name is mentioned in the question, if any (longest name
    first so 'SMT-Reflow-01' wins over a bare 'SMT')."""
    q = (question or "").lower()
    machines = sorted(db.query(models.Machine).all(),
                      key=lambda m: len(m.name or ""), reverse=True)
    for m in machines:
        if m.name and m.name.lower() in q:
            return m
    return None


def _machine_answer(db, tenant, machine):
    from ai.twin import build_twins   # lazy: twin imports pull in the pillar modules
    tw = next((t for t in build_twins(db, tenant) if t["machine_id"] == machine.id), None)
    if tw is None:
        return f"{machine.name}: no data yet.", "machines"
    parts = [f"{tw['name']} is {tw['status']}", f"health {tw['health_score']}/100"]
    if tw.get("oee") and tw["oee"].get("has_data"):
        parts.append(f"OEE {tw['oee']['oee']}%")
    if tw.get("open_maintenance_tasks"):
        parts.append(f"{tw['open_maintenance_tasks']} open maintenance task(s)")
    ans = ", ".join(parts) + "."
    if tw.get("recent_downtime"):
        d = tw["recent_downtime"][0]
        ans += f" Latest downtime: {d['reason']} ({d['duration']})."
    return ans, "machines"


def _trend(db, tenant):
    sc = build_scorecard(db, tenant)
    if not sc["has_data"]:
        return "No production data yet, so there's nothing to compare.", "overview"
    moves = []
    for k in sc["kpis"]:
        if k.get("delta") is None or k["delta"] == 0:
            continue
        arrow = "up" if k["delta"] > 0 else "down"
        mag = f"${abs(k['delta']):,}" if k["unit"] == "$" else f"{abs(k['delta'])}{'' if k['unit'] == '%' else k['unit']}"
        verdict = "better" if k.get("delta_tone") == "good" else "worse"
        moves.append(f"{k['label']} {arrow} {mag} ({verdict})")
    if not moves:
        return "Steady vs last week — no material change in OEE, good rate or cost of losses.", "executive"
    return "Vs last week: " + "; ".join(moves) + ".", "executive"


def _briefing(db, tenant):
    b = build_briefing(db, tenant)
    if not b["has_data"]:
        return "No production data yet — nothing to report.", "overview"
    if not b["alerts"]:
        return f"Plant OEE {b['oee']}% and nothing needs attention right now.", "overview"
    lead = "; ".join(a["title"] for a in b["alerts"][:3])
    return f"Right now: {lead}. Plant OEE {b['oee']}% ({b['oee_trend']}).", "overview"


# Ordered keyword routes — first match wins. Inventory before delivery so
# "reorder" doesn't match "order"; downtime before machines so "downtime"
# doesn't match "down".
_ROUTES = [
    (("help", "what can you", "what can i ask", "capabilit", "how do you work", "what do you do"), _help),
    (("last week", "vs last", "compared", "week on week", "week-on-week", "trend", "improv",
      "getting better", "getting worse", "better or worse", "since last"), _trend),
    (("reorder", "restock", "stock", "inventory", "out of stock", "replenish"), _inventory),
    (("wip", "work in progress", "work-in-progress", "in progress", "pipeline", "work order", "raw ", "semi", "finished good"), _flow),
    (("shift", "attainment", "crew", "night", "day shift"), _shift),
    (("deliver", "on-time", "on time", " late", "customer", "ship", "fulfil", "bugatti", "mercedes", "order"), _delivery),
    (("cost", "money", "losing", "$", "expensive", "spend", "margin"), _cost),
    (("quality", "defect", "reject", "scrap", "fail", "yield", "fpy", "first-pass", "first pass"), _quality),
    (("maintenance", "overdue", "service", "pm ", " task"), _maintenance),
    (("downtime", "stoppage", "down time"), _downtime),
    (("machine", "breakdown", "running", "idle", "offline", " down"), _machines),
    (("produc", "output", "units", "throughput", "made", "making", "good rate"), _production),
    (("oee", "effective", "availability", "performance"), _oee),
    (("attention", "wrong", "problem", "issue", "priorit", "focus", "happening",
      "summary", "summarise", "summarize", "overview", "everything", "status"), _briefing),
]


def digest(db, tenant: str) -> dict:
    """A conversational one-shot rundown of the whole plant — OEE and trend, the
    week's losses, the order book, the most pressing issue and the wins — composed
    from the pillar read-models into a plain-English paragraph."""
    b = build_briefing(db, tenant)
    if not b["has_data"]:
        return {"digest": "No production data yet — nothing to report."}
    cost = build_cost_summary(db, tenant)
    delivery = build_delivery_summary(db, tenant)

    lines = [f"Plant OEE is {b['oee']}% and trending {b['oee_trend']}."]
    if cost["has_data"]:
        lines.append(f"Losses have cost about ${cost['loss_cost']:,} this week.")
    if delivery["total"]:
        lines.append(f"On the order book, {delivery['fulfillment_rate']}% of units are fulfilled, "
                     f"with {delivery['late']} late and {delivery['at_risk']} at risk.")
    if b["alerts"]:
        top = b["alerts"][0]
        lines.append(f"The most pressing issue is {top['title']}"
                     + (f" ({top['detail']})" if top.get("detail") else "") + ".")
        if len(b["alerts"]) > 1:
            lines.append(f"There are {len(b['alerts'])} things needing attention in all.")
    else:
        lines.append("Nothing needs attention right now.")
    if b["wins"]:
        lines.append("On the upside: " + "; ".join(w["title"] for w in b["wins"]) + ".")
    return {"digest": " ".join(lines)}


def answer(db, tenant: str, question: str) -> dict:
    """Answer a plant question from the read-models: a sentence plus the view that
    drills into it. Routes by keyword; defaults to 'what needs attention'."""
    # A specific machine named in the question wins — answer about that machine.
    named = _machine_named(db, question)
    if named is not None:
        text, view = _machine_answer(db, tenant, named)
        return {"question": question, "answer": text, "view": view, "matched": "machine_detail"}

    q = f" {(question or '').lower()} "
    for keys, fn in _ROUTES:
        if any(k in q for k in keys):
            text, view = fn(db, tenant)
            return {"question": question, "answer": text, "view": view, "matched": fn.__name__.lstrip("_")}
    text, view = _briefing(db, tenant)
    return {"question": question, "answer": text, "view": view, "matched": "briefing"}
