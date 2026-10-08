"""The factory tools: one typed tool per read-model the Copilot answers from (ADR-0022).

Each tool runs ONE existing builder (the function behind a REST read-model
route), turns its dict into labelled facts, and says it with the same wording
function the rule copilot has always used (ai.assistant.say_*). Nothing here
computes a new number: a figure appears in a fact only if the builder produced
it, and a figure the builder does not have is left out or stated as UNKNOWN.

Provenance, as used here (ai/evidence.py):
  MEASURED  counts and sums of recorded rows, recorded statuses and names;
  DERIVED   rates and ratios (OEE, yield, attainment), money from the tenant's
            own unit value, date comparisons ("late", "overdue");
  RULE      thresholds and hand-weighted scores (at reorder level, "at risk",
            health bands, the briefing's alerts, the biggest OEE lever).

The window is the builder's own. These read-models all look at the last 7 days
and take no period argument, so the tools take none either and every fact says
which window it covers. A tool that offered `days` would have to compute a
second version of each figure, which is the drift ADR-0014 exists to stop.
"""
import re
from datetime import datetime, time, timedelta

import models
import oee_contract
from ai import assistant
from ai import evidence as ev
from ai.brief import say_brief
from ai.briefing import build_briefing
from ai.compliance import build_compliance_summary
from ai.cost import build_cost_summary
from ai.delivery import build_delivery_summary
from ai.downtime import build_downtime_summary
from ai.flow import build_flow_summary
from ai.inventory import build_inventory_summary
from ai.maintenance import build_maintenance_summary
from ai.oee import build_oee_summary
from ai.outcomes import CAUSATION_NOTE
from ai.production import build_production_summary
from ai.quality import build_quality_summary
from ai.schedule import build_schedule_adherence
from ai.scorecard import build_scorecard
from ai.shift import build_shift_summary
from ai.tools.registry import Param, tool
from currency import CURRENCY, unit_rate

M, D, R, U = ev.MEASURED, ev.DERIVED, ev.RULE, ev.UNKNOWN
W7 = "last 7 days"


def _fact(key, label, value, prov, unit="", source="", window=W7, detail=""):
    return ev.Fact(key=key, label=label, value=value, provenance=prov, unit=unit,
                   source=source, window=window, detail=detail)


def _result(name, state, said, facts, notes=()):
    text, view = said
    return ev.ToolResult(tool=name, state=state, summary=text, facts=[f for f in facts if f],
                         view=view, notes=list(notes))


# ── The whole plant ─────────────────────────────────────────────────

@tool("get_factory_summary",
      "What needs attention across the plant right now: plant OEE, the ranked alerts "
      "from every area, and the wins. Use for 'what is happening', 'what needs my "
      "attention', 'give me a summary'.",
      mirrors="/briefing", view="overview", domain="plant")
def get_factory_summary(db, tenant):
    b = build_briefing(db, tenant)
    facts = []
    if b["has_data"]:
        facts.append(_fact("plant.oee", "Plant OEE", b["oee"], D, "%", "production_records",
                           detail=oee_contract.coverage_phrase(b.get("coverage")) or "every machine reported"))
        facts.append(_fact("plant.oee_trend", "OEE direction within the week", b["oee_trend"], D,
                           source="production_records"))
    else:
        facts.append(_fact("plant.oee", "Plant OEE", None, U, "%", "production_records",
                           detail="no production recorded in the window, so no OEE (not 0%)"))
    facts.append(_fact("plant.alerts", "Things needing attention", len(b["alerts"]), R, "alerts",
                       "briefing rules over every read-model", window="now"))
    for i, a in enumerate(b["alerts"][:3], 1):
        facts.append(_fact(f"plant.alert_{i}", f"Alert {i} ({a.get('severity', '')})", a["title"], R,
                           source=a.get("module", ""), window="now", detail=a.get("detail") or ""))
    for i, w in enumerate(b["wins"], 1):
        facts.append(_fact(f"plant.win_{i}", f"Win {i}", w["title"], R, window=W7,
                           detail=w.get("detail") or ""))
    cov = b.get("coverage") or {}
    if not b["has_data"]:
        state = ev.PARTIAL_DATA if b["alerts"] else ev.NO_DATA
    elif cov and not cov.get("complete", True):
        state = ev.PARTIAL_DATA
    else:
        state = ev.OK
    return _result("get_factory_summary", state, assistant.say_briefing(b), facts)


@tool("get_oee",
      "Plant OEE over the last 7 days with availability, performance and quality, how "
      "many machines it was measured from, the component dragging it down, and the "
      "worst machine. Use for OEE, efficiency, availability or performance questions.",
      mirrors="/oee-summary", view="executive", domain="production")
def get_oee(db, tenant):
    o = build_oee_summary(db, tenant)
    plant, cov = o["plant"], o["coverage"]
    facts = [
        _fact("oee.machines_reporting", "Machines that reported production", cov["machines_reporting"], M,
              "machines", "production_records"),
        _fact("oee.machines_expected", "Machines expected to report", cov["machines_expected"], M,
              "machines", "machines"),
    ]
    if not plant["has_data"]:
        facts.append(_fact("oee.plant", "Plant OEE", None, U, "%", "production_records",
                           detail="no production recorded in the window, so no OEE (not 0%)"))
        return _result("get_oee", ev.NO_DATA, assistant.say_oee(o), facts)
    facts += [
        _fact("oee.plant", "Plant OEE (pooled)", plant["oee"], D, "%", "production_records",
              detail=oee_contract.coverage_phrase(cov) or "every machine reported"),
        _fact("oee.availability", "Availability", plant["availability"], D, "%", "production_records"),
        _fact("oee.performance", "Performance", plant["performance"], D, "%", "production_records"),
        _fact("oee.quality", "Quality", plant["quality"], D, "%", "production_records"),
    ]
    if o.get("biggest_drag"):
        facts.append(_fact("oee.biggest_drag", "Component furthest below its world-class target",
                           o["biggest_drag"], R, source="OEE components vs world-class targets"))
    if o.get("worst"):
        facts.append(_fact("oee.worst_machine", "Lowest-OEE machine", o["worst"]["name"], M))
        facts.append(_fact("oee.worst_machine_oee", f"{o['worst']['name']} OEE", o["worst"]["oee"], D, "%",
                           "production_records"))
    state = ev.OK if cov.get("complete") else ev.PARTIAL_DATA
    return _result("get_oee", state, assistant.say_oee(o), facts)


@tool("get_machine_status",
      "Which machines are running, down, idle or in maintenance right now, with the "
      "names of the ones that are down. Use for machine status and breakdown questions.",
      mirrors="/machines", view="machines", domain="machines")
def get_machine_status(db, tenant):
    machines = (db.query(models.Machine).filter(models.Machine.tenant_code == tenant)
                .order_by(models.Machine.id).all())
    said = assistant.say_machines(machines)
    if not machines:
        return _result("get_machine_status", ev.NO_DATA, said,
                       [_fact("machines.total", "Machines", 0, M, "machines", "machines", "now")])
    down = sorted(m.name for m in machines if (m.status or "") in assistant.DOWN_STATUSES)
    count = lambda s: sum(1 for m in machines if (m.status or "") == s)   # noqa: E731
    facts = [
        _fact("machines.total", "Machines", len(machines), M, "machines", "machines", "now"),
        _fact("machines.running", "Running", count(assistant.RUNNING), M, "machines", "machines", "now"),
        _fact("machines.down", "Down", len(down), M, "machines", "machines", "now"),
        _fact("machines.maintenance", "In maintenance", count("Maintenance"), M, "machines", "machines", "now"),
        _fact("machines.idle", "Idle", count("Idle"), M, "machines", "machines", "now"),
    ]
    for i, name in enumerate(down[:5], 1):
        facts.append(_fact(f"machines.down_{i}", f"Down machine {i}", name, M, source="machines", window="now"))
    return _result("get_machine_status", ev.OK, said, facts)


def _resolve_machine(db, tenant, name):
    """(machine, None) or (None, why). Looked up inside the tenant only, so a name
    that exists in another workspace is simply not found here."""
    q = name.strip().lower()
    rows = db.query(models.Machine).filter(models.Machine.tenant_code == tenant).all()
    exact = [m for m in rows if (m.name or "").lower() == q]
    if len(exact) == 1:
        return exact[0], None
    matches = exact or [m for m in rows if q and q in (m.name or "").lower()]
    if len(matches) == 1:
        return matches[0], None
    if matches:
        names = ", ".join(sorted({m.name for m in matches})[:5])
        return None, f"\"{name}\" matches more than one machine ({names}); say which."
    return None, f"There is no machine called \"{name}\" in this workspace."


@tool("get_machine_history",
      "One machine's health, status, OEE, downtime, output and quality over the last 7 "
      "days, with the reasons behind its health score. Needs the machine's name.",
      mirrors="/machine-health/{machine_id}", view="machines", domain="machines",
      params={"machine": Param("str", "The machine's name, e.g. CNC-01", required=True)})
def get_machine_history(db, tenant, machine):
    from ai.twin import build_machine_detail   # lazy: twin pulls in the pillar modules
    row, why = _resolve_machine(db, tenant, machine)
    if row is None:
        return ev.refusal("get_machine_history", ev.NOT_FOUND, why)
    d = build_machine_detail(db, tenant, row.id)
    if d is None:
        return ev.refusal("get_machine_history", ev.NOT_FOUND,
                          f"There is no machine called \"{machine}\" in this workspace.")
    name = d["name"]
    facts = [
        _fact("machine.name", "Machine", name, M, source="machines", window="now"),
        _fact("machine.status", "Status", d["status"] or "unknown", M, source="machines", window="now"),
        _fact("machine.health", "Health score", d["health_score"], R, "/100",
              "rule-based risk points (predictive_engine)", "now",
              detail="100 minus the rule risk score; hand-weighted, not machine learning"
                     + ("; nothing was recorded for this machine in the risk window, so this is "
                        "an absence, not a clean bill of health"
                        if d.get("health_measured") is False else "")),
        _fact("machine.health_band", "Health band", d["health_band"], R, window="now"),
        _fact("machine.open_maintenance", "Open maintenance tasks", d["open_maintenance_tasks"], M, "tasks",
              "maintenance_tasks", "now"),
    ]
    # The score's own arithmetic: each rule that fired, with the points it cost,
    # what it read and the threshold it read against (ADR-0027). These are
    # numbers a reader can add up to the score, not adjectives.
    for cost in (d.get("health_explanation") or {}).get("deductions", [])[:4]:
        facts.append(_fact(f"machine.health_cost.{cost['key']}", f"{cost['label']} cost",
                           cost["points"], R, "points", "predictive_engine", "now",
                           detail=f"read {cost['reading']}, rule: {cost['threshold']}"))
    oee = d.get("oee") or {}
    if oee.get("has_data"):
        facts.append(_fact("machine.oee", f"{name} OEE", oee["oee"], D, "%", "production_records"))
    else:
        facts.append(_fact("machine.oee", f"{name} OEE", None, U, "%", "production_records",
                           detail="no production recorded for this machine in the window"))
    dt = d.get("downtime_7d") or []
    facts.append(_fact("machine.downtime_events", "Downtime events", sum(x.get("count", 0) for x in dt), M,
                       "events", "downtime_logs"))
    if d.get("recent_downtime"):
        latest = d["recent_downtime"][0]
        facts.append(_fact("machine.latest_downtime", "Latest downtime reason", latest["reason"], M,
                           source="downtime_logs", detail=str(latest.get("duration") or "")))
    prod = d.get("production_7d") or {}
    if prod.get("total"):
        facts.append(_fact("machine.good_units", "Good units", prod["good"], M, "units", "production_records"))
        facts.append(_fact("machine.total_units", "Units made", prod["total"], M, "units", "production_records"))
    qual = d.get("quality") or {}
    if qual.get("inspected"):
        facts.append(_fact("machine.fail_rate", "Inspection fail rate", qual["fail_rate"], D, "%",
                           "quality_inspections"))
    state = ev.OK if oee.get("has_data") else ev.PARTIAL_DATA
    return _result("get_machine_history", state, assistant.say_machine(name, d), facts)


# ── Downtime and output ─────────────────────────────────────────────

@tool("get_downtime",
      "Downtime over the last 7 days: how many stoppages were logged, the minutes lost, "
      "the top cause and the most affected machine.",
      mirrors="/downtime-summary", view="downtime", domain="downtime")
def get_downtime(db, tenant):
    dt = build_downtime_summary(db, tenant)
    facts = [
        _fact("downtime.events", "Downtime events logged", dt["total_events"], M, "events", "downtime_logs"),
        _fact("downtime.minutes", "Minutes lost", dt["total_minutes"], M, "min", "downtime_logs"),
    ]
    if dt["by_machine"]:
        w = dt["by_machine"][0]
        facts.append(_fact("downtime.worst_machine", "Most affected machine", w["name"], M, source="downtime_logs"))
        facts.append(_fact("downtime.worst_machine_minutes", f"{w['name']} minutes lost", w["minutes"], M, "min",
                           "downtime_logs"))
    if dt["top_reasons"]:
        r = dt["top_reasons"][0]
        facts.append(_fact("downtime.top_reason", "Top cause by minutes lost", r["reason"], M,
                           source="downtime_logs"))
        facts.append(_fact("downtime.top_reason_events", f"{r['reason']} events", r["count"], M, "events",
                           "downtime_logs"))
        facts.append(_fact("downtime.top_reason_minutes", f"{r['reason']} minutes lost", r["minutes"], M, "min",
                           "downtime_logs"))
    return _result("get_downtime", ev.OK, assistant.say_downtime(dt), facts,
                   notes=["Counts the stoppages that were logged; an unlogged stoppage is not seen."])


@tool("get_top_downtime_causes",
      "The downtime causes that cost the most minutes over the last 7 days, each with "
      "its event count and minutes lost.",
      mirrors="/downtime-summary", view="downtime", domain="downtime",
      params={"limit": Param("int", "How many causes (1-5)", default=3, minimum=1, maximum=5)})
def get_top_downtime_causes(db, tenant, limit=3):
    dt = build_downtime_summary(db, tenant)
    reasons = dt["top_reasons"][:limit]
    if not reasons:
        return _result("get_top_downtime_causes", ev.OK,
                       ("No downtime was logged in the last 7 days.", "downtime"),
                       [_fact("downtime.events", "Downtime events logged", 0, M, "events", "downtime_logs")])
    facts = []
    for i, r in enumerate(reasons, 1):
        facts.append(_fact(f"causes.{i}.reason", f"Cause {i}", r["reason"], M, source="downtime_logs"))
        facts.append(_fact(f"causes.{i}.minutes", f"{r['reason']} minutes lost", r["minutes"], M, "min",
                           "downtime_logs"))
        facts.append(_fact(f"causes.{i}.events", f"{r['reason']} events", r["count"], M, "events",
                           "downtime_logs"))
    parts = "; ".join(f"{r['reason']} ({r['minutes']:,} min over {r['count']} event"
                      f"{'' if r['count'] == 1 else 's'})" for r in reasons)
    return _result("get_top_downtime_causes", ev.OK,
                   (f"Top downtime causes in the last 7 days, by minutes lost: {parts}.", "downtime"), facts)


@tool("get_production",
      "Output over the last 7 days: units made, good units, the good rate, the number "
      "of runs and the top producing machine.",
      mirrors="/production-summary", view="analytics", domain="production")
def get_production(db, tenant):
    p = build_production_summary(db, tenant)
    if p["runs"] == 0:
        return _result("get_production", ev.NO_DATA, assistant.say_production(p),
                       [_fact("production.runs", "Production runs recorded", 0, M, "runs", "production_records")])
    facts = [
        _fact("production.runs", "Production runs recorded", p["runs"], M, "runs", "production_records"),
        _fact("production.total", "Units made", p["total"], M, "units", "production_records"),
        _fact("production.good", "Good units", p["good"], M, "units", "production_records"),
        _fact("production.rejected", "Rejected units", p["rejected"], M, "units", "production_records"),
        _fact("production.good_rate", "Good rate", p["good_rate"], D, "%", "production_records"),
    ]
    if p["by_machine"]:
        w = p["by_machine"][0]
        facts.append(_fact("production.top_machine", "Top producer", w["name"], M, source="production_records"))
        facts.append(_fact("production.top_machine_good", f"{w['name']} good units", w["good"], M, "units",
                           "production_records"))
    return _result("get_production", ev.OK, assistant.say_production(p), facts)


def _say_vs_target(s):
    if s["total"] == 0:
        return ("No production plan is set for the last 7 days, so there is no target to "
                "compare output against."), "planning"
    if s["planned_units"] == 0:
        t = s["today"]
        return (f"No plan has come due yet this week; today's plan is {t['planned']:,} units "
                f"with {t['actual']:,} made so far."), "planning"
    text = (f"Against plan: {s['actual_units']:,} of {s['planned_units']:,} planned units made "
            f"({s['attainment_rate']}%) on the plans already due in the last 7 days. "
            f"{s['behind']} plan{'' if s['behind'] == 1 else 's'} behind, {s['missed']} missed.")
    if s["chase"]:
        c = s["chase"][0]
        text += (f" Biggest shortfall: {c['plan_no']} on {c['machine']}, "
                 f"{c['shortfall']:,} units short.")
    return text, "planning"


@tool("get_production_vs_target",
      "Output against the production plan over the last 7 days: planned vs made units "
      "on the plans already due, how many plans are behind or missed, and the biggest "
      "shortfall. Use for 'are we behind', 'did we hit target', 'why are we behind'.",
      mirrors="/schedule-summary", view="planning", domain="production")
def get_production_vs_target(db, tenant):
    s = build_schedule_adherence(db, tenant)
    said = _say_vs_target(s)
    if s["total"] == 0:
        return _result("get_production_vs_target", ev.NOT_CONFIGURED, said,
                       [_fact("plan.plans", "Production plans in the window", 0, M, "plans", "production_plans")])
    facts = [
        _fact("plan.plans", "Production plans in the window", s["total"], M, "plans", "production_plans"),
        _fact("plan.behind", "Plans behind", s["behind"], D, "plans", "production_plans",
              detail="due before today, some made, less than planned"),
        _fact("plan.missed", "Plans missed", s["missed"], D, "plans", "production_plans",
              detail="due before today, nothing made"),
    ]
    if s["planned_units"] == 0:
        facts += [
            _fact("plan.today_planned", "Planned today", s["today"]["planned"], M, "units", "production_plans", "today"),
            _fact("plan.today_actual", "Made today against today's plans", s["today"]["actual"], M, "units",
                  "production_plans", "today"),
        ]
        return _result("get_production_vs_target", ev.INSUFFICIENT_HISTORY, said, facts)
    facts += [
        _fact("plan.planned_units", "Planned units (plans due)", s["planned_units"], M, "units", "production_plans"),
        _fact("plan.actual_units", "Made units (plans due)", s["actual_units"], M, "units", "production_plans"),
        _fact("plan.attainment", "Plan attainment", s["attainment_rate"], D, "%", "production_plans"),
    ]
    if s["chase"]:
        c = s["chase"][0]
        facts += [
            _fact("plan.worst_plan", "Biggest shortfall plan", c["plan_no"], M, source="production_plans"),
            _fact("plan.worst_plan_machine", f"{c['plan_no']} machine", c["machine"], M, source="production_plans"),
            _fact("plan.worst_plan_shortfall", f"{c['plan_no']} shortfall", c["shortfall"], D, "units",
                  "production_plans"),
        ]
    return _result("get_production_vs_target", ev.OK, said, facts)


@tool("explain_production_gap",
      "Why the plant is behind: the gap against the plans that came due, the losses AMP can "
      "measure in the same window (slow running, scrap, logged stoppages), how much of that has "
      "a recorded reason, and the part nothing in the data explains. Use for 'why are we behind', "
      "'explain the shortfall', 'what went wrong'.",
      mirrors="/root-cause", view="executive", domain="production")
def explain_production_gap(db, tenant):
    from ai.root_cause import explain_production_gap as explain   # lazy: pulls in the pillar modules
    r = explain(db, tenant)
    facts = [ev.Fact(key=f["key"], label=f["label"], value=f["value"], provenance=f["provenance"],
                     unit=f["unit"], source=f["source"], window=f["window"], detail=f["detail"])
             for f in r["facts"]]
    for c in r["contributors"][:6]:
        if c["units"] is not None:
            facts.append(_fact(f"cause.{c['key']}", f"{c['label']} ({c['cause_label']})", c["units"], D, "units",
                               c["basis"]))
        elif c["minutes"] is not None:
            facts.append(_fact(f"cause.{c['key']}", f"{c['label']} ({c['cause_label']})", c["minutes"], M, "min",
                               c["basis"]))
    state = r["state"] if r["state"] in ev.DATA_STATES else ev.OK
    return _result("explain_production_gap", state, (r["headline"], "executive"), facts,
                   notes=[r["denominator_note"]])
@tool("get_production_risks",
      "What is likely to become a problem: orders that cannot be made in the days they have "
      "left at the plant's measured rate, items about to run out, machines over the rule-score "
      "threshold, overdue maintenance and quality drift. Each risk states the rule behind it. "
      "Use for 'what is likely to go wrong', 'what should I worry about', 'what is at risk'.",
      mirrors="/risk-radar", view="overview", domain="plant")
def get_production_risks(db, tenant):
    from ai.risk_radar import build_risk_radar   # lazy: pulls in the pillar modules
    r = build_risk_radar(db, tenant)
    # The counts the headline states, as facts: every number AMP says has to be
    # in the evidence beside it (the grounding gate checks exactly this).
    likely = [x for x in r["risks"] if x["likelihood"] == ev.LIKELY]
    facts = [
        _fact("risk.likely_count", "Things likely to become a problem", len(likely), R, "risks",
              "risk radar rules", "now"),
        _fact("risk.total_count", "Things on the radar", len(r["risks"]), R, "risks", "risk radar rules", "now"),
    ]
    for risk in r["risks"][:4]:
        facts.append(_fact(f"risk.{risk['key']}", f"{risk['title']} ({risk['likelihood']})",
                           risk["rule"], R, source=risk["module"], window=risk["horizon"]))
        for f in risk["facts"][:2]:
            facts.append(ev.Fact(key=f["key"], label=f["label"], value=f["value"], provenance=f["provenance"],
                                 unit=f["unit"], source=f["source"], window=f["window"], detail=f["detail"]))
    if r["measured_rate_per_day"] is not None:
        facts.append(_fact("risk.measured_rate", "Plant good units a day (measured)",
                           r["measured_rate_per_day"], M, "units/day", "production_records"))
    state = r["state"] if r["state"] in ev.DATA_STATES else ev.OK
    return _result("get_production_risks", state, (r["headline"], "overview"), facts, notes=[r["note"]])


@tool("get_shift_attainment",
      "Shift output against shift targets over the last 7 days, with the best and the "
      "worst shift.",
      mirrors="/shift-summary", view="shifts", domain="production")
def get_shift_attainment(db, tenant):
    sh = build_shift_summary(db, tenant)
    said = assistant.say_shift(sh)
    if sh["entries"] == 0:
        return _result("get_shift_attainment", ev.NO_DATA, said,
                       [_fact("shift.entries", "Shift records", 0, M, "records", "shift_data")])
    facts = [_fact("shift.entries", "Shift records", sh["entries"], M, "records", "shift_data"),
             _fact("shift.actual", "Units made across shifts", sh["actual"], M, "units", "shift_data")]
    if sh["attainment"] is None:
        facts.append(_fact("shift.attainment", "Shift attainment", None, U, "%", "shift_data",
                           detail="no shift had a target set"))
        return _result("get_shift_attainment", ev.NOT_CONFIGURED, said, facts)
    facts += [_fact("shift.target", "Shift target", sh["target"], M, "units", "shift_data"),
              _fact("shift.attainment", "Shift attainment", sh["attainment"], D, "%", "shift_data")]
    for tag in ("best", "worst"):
        if sh.get(tag):
            facts.append(_fact(f"shift.{tag}", f"{tag.title()} shift", sh[tag]["shift"], D, source="shift_data"))
            facts.append(_fact(f"shift.{tag}_attainment", f"{sh[tag]['shift']} attainment",
                               sh[tag]["attainment"], D, "%", "shift_data"))
    return _result("get_shift_attainment", ev.OK, said, facts)


@tool("get_week_on_week",
      "How this week compares with last week: plant OEE, good rate, delivery reliability "
      "and the cost of losses, each with its change.",
      mirrors="/scorecard", view="executive", domain="plant")
def get_week_on_week(db, tenant):
    sc = build_scorecard(db, tenant)
    said = assistant.say_trend(sc)
    if not sc["has_data"]:
        return _result("get_week_on_week", ev.NO_DATA, said, [])
    facts, partial = [], False
    for k in sc["kpis"]:
        unit = "" if k["unit"] == CURRENCY else k["unit"].strip()
        money_unit = CURRENCY if k["unit"] == CURRENCY else unit
        if k.get("value") is None:
            partial = True
            facts.append(_fact(f"week.{k['key']}", k["label"], None, U, money_unit, "scorecard",
                               "this week", "not measured this week"))
            continue
        facts.append(_fact(f"week.{k['key']}", k["label"], k["value"], D, money_unit, "scorecard", "this week",
                           oee_contract.coverage_phrase(k.get("coverage")) if k.get("coverage") else ""))
        if k.get("delta") is not None:
            facts.append(_fact(f"week.{k['key']}_change", f"{k['label']} change vs last week", k["delta"], D,
                               money_unit if money_unit == CURRENCY else ("pts" if unit == "%" else unit),
                               "scorecard", "this week vs last week"))
    return _result("get_week_on_week", ev.PARTIAL_DATA if partial else ev.OK, said, facts)


@tool("get_work_order_status",
      "Work orders on the floor: how many are in progress, how many are finished, and "
      "how many sit at each stage.",
      mirrors="/flow-summary", view="workorders", domain="production")
def get_work_order_status(db, tenant):
    f = build_flow_summary(db, tenant)
    said = assistant.say_flow(f)
    if f["total"] == 0:
        return _result("get_work_order_status", ev.NO_DATA, said,
                       [_fact("wo.total", "Work orders", 0, M, "orders", "work_orders", "now")])
    facts = [_fact("wo.total", "Work orders", f["total"], M, "orders", "work_orders", "now"),
             _fact("wo.wip", "In progress", f["wip"], M, "orders", "work_orders", "now"),
             _fact("wo.finished", "Finished", f["finished"], M, "orders", "work_orders", "now")]
    for s in f["stages"]:
        facts.append(_fact(f"wo.stage_{s['key'].lower()}", f"At stage {s['label']}", s["count"], M, "orders",
                           "work_orders", "now"))
    return _result("get_work_order_status", ev.OK, said, facts)


@tool("find_record",
      "Find a record by name or number: an order, work order, part, machine, "
      "maintenance task, escalation or document.",
      mirrors="/search", view="overview", domain="plant",
      params={"query": Param("str", "What to look for, e.g. CO-5001 or reflow SOP", required=True,
                             max_length=80)})
def find_record(db, tenant, query):
    from ai.search import build_search   # lazy, as the rule copilot does
    hits = build_search(db, tenant, query)["results"]
    said = assistant.say_find(query, hits)
    facts = [_fact("find.matches", "Matches", len(hits), M, "records", "search", "now")]
    for i, h in enumerate(hits[:4], 1):
        facts.append(_fact(f"find.{i}", f"Match {i} ({h['type']})", h["label"], M, source=h["type"], window="now",
                           detail=h.get("sublabel") or ""))
    return _result("find_record", ev.OK if hits else ev.NO_DATA, said, facts)


# ── Money, orders, quality, maintenance, documents, stock ───────────

@tool("get_financial_losses",
      "What the last 7 days' losses (downtime and scrap) cost. In money only when the "
      "workspace has set a unit value; otherwise in good units not made.",
      mirrors="/cost-summary", view="costing", domain="finance")
def get_financial_losses(db, tenant):
    c = build_cost_summary(db, tenant)
    said = assistant.say_cost(c)
    if not c["has_data"]:
        return _result("get_financial_losses", ev.NO_DATA, said, [])
    facts = [
        _fact("losses.downtime_minutes", "Downtime minutes", c["downtime_minutes"], M, "min", "downtime_logs"),
        _fact("losses.rejected_units", "Scrapped units", c["rejected_units"], M, "units", "production_records"),
    ]
    notes = []
    if c["lost_units"] is not None:
        facts.append(_fact("losses.lost_units", "Good units not made", c["lost_units"], D, "units",
                           "production_records + downtime_logs", detail="downtime minutes at the run rate, plus scrap"))
        facts.append(_fact("losses.downtime_lost_units", "Good units not made during downtime",
                           c["downtime_lost_units"], D, "units", "production_records + downtime_logs",
                           detail="downtime minutes at the plant's run rate"))
    if c["priced"] and c["loss_cost"] is not None:
        facts += [
            _fact("losses.unit_value", "Unit value (set by this workspace)", c["unit_value_gbp"], M, CURRENCY,
                  "tenant configuration", "now"),
            _fact("losses.cost", "Cost of losses", c["loss_cost"], D, CURRENCY, "cost model (ADR-0010)"),
            _fact("losses.downtime_cost", "Downtime cost", c["downtime_cost"], D, CURRENCY, "cost model (ADR-0010)"),
            _fact("losses.scrap_cost", "Scrap cost", c["scrap_cost"], D, CURRENCY, "cost model (ADR-0010)"),
        ]
        state = ev.OK
    else:
        facts.append(_fact("losses.cost", "Cost of losses in money", None, U, CURRENCY, "tenant configuration",
                           detail="no unit value is set, so AMP will not put a money figure on it"))
        notes.append("Set a unit value under Costing to see losses in money.")
        state = ev.NOT_CONFIGURED
    if c["by_machine"]:
        w = c["by_machine"][0]
        facts.append(_fact("losses.worst_machine", "Biggest loss", w["name"], M, source="cost model"))
        if c["priced"] and w.get("cost") is not None:
            facts.append(_fact("losses.worst_machine_cost", f"{w['name']} loss", w["cost"], D, CURRENCY,
                               "cost model (ADR-0010)"))
        elif w.get("lost_units") is not None:
            facts.append(_fact("losses.worst_machine_units", f"{w['name']} good units not made", w["lost_units"], D,
                               "units", "cost model (ADR-0010)"))
    return _result("get_financial_losses", state, said, facts, notes)


@tool("get_order_delivery",
      "Customer orders: how many are on the book, the share of units fulfilled, how "
      "many are late or at risk, and which order to chase first.",
      mirrors="/delivery-summary", view="orders", domain="orders")
def get_order_delivery(db, tenant):
    d = build_delivery_summary(db, tenant)
    said = assistant.say_delivery(d)
    if d["total"] == 0:
        return _result("get_order_delivery", ev.NO_DATA, said,
                       [_fact("orders.total", "Open customer orders", 0, M, "orders", "customer_orders", "now")])
    facts = [
        _fact("orders.total", "Customer orders", d["total"], M, "orders", "customer_orders", "now"),
        _fact("orders.fulfilment", "Units fulfilled", d["fulfillment_rate"], D, "%", "customer_orders", "now"),
        _fact("orders.late", "Late orders", d["late"], D, "orders", "customer_orders", "now",
              detail="past due date and not delivered"),
        _fact("orders.at_risk", "At-risk orders", d["at_risk"], R, "orders", "customer_orders", "now",
              detail="due within the at-risk horizon and not yet fulfilled"),
    ]
    if d["at_risk_orders"]:
        w = d["at_risk_orders"][0]
        facts.append(_fact("orders.chase_first", "Order to chase first", w["order_no"], M, source="customer_orders",
                           window="now", detail=w.get("customer") or ""))
    return _result("get_order_delivery", ev.OK, said, facts)


@tool("get_quality_summary",
      "Inspection results over the last 7 days: first-pass yield, fail rate, the worst "
      "machine and the top defect.",
      mirrors="/quality-summary", view="quality", domain="quality")
def get_quality_summary(db, tenant):
    q = build_quality_summary(db, tenant)
    said = assistant.say_quality(q)
    if q["inspections"] == 0:
        return _result("get_quality_summary", ev.NO_DATA, said,
                       [_fact("quality.inspections", "Inspections", 0, M, "inspections", "quality_inspections")])
    if not q.get("measured"):
        # Rows, but no units: there is no yield and no fail rate to state, and a
        # fact carrying None would be refused by the evidence vocabulary. Say
        # what WAS recorded and mark the reading partial.
        return _result("get_quality_summary", ev.PARTIAL_DATA, said, [
            _fact("quality.inspections", "Inspections", q["inspections"], M, "inspections",
                  "quality_inspections"),
            _fact("quality.inspected", "Units inspected", 0, M, "units", "quality_inspections"),
            _fact("quality.fail_rate", "Fail rate", None, ev.UNKNOWN, "%", "quality_inspections"),
        ])
    facts = [
        _fact("quality.inspections", "Inspections", q["inspections"], M, "inspections", "quality_inspections"),
        _fact("quality.inspected", "Units inspected", q["inspected"], M, "units", "quality_inspections"),
        _fact("quality.failed", "Units failed", q["failed"], M, "units", "quality_inspections"),
        _fact("quality.fpy", "First-pass yield", q["first_pass_yield"], D, "%", "quality_inspections"),
        _fact("quality.fail_rate", "Fail rate", q["fail_rate"], D, "%", "quality_inspections"),
    ]
    if q["by_machine"]:
        w = q["by_machine"][0]
        facts.append(_fact("quality.worst_machine", "Worst machine", w["name"], M, source="quality_inspections"))
        facts.append(_fact("quality.worst_machine_fail_rate", f"{w['name']} fail rate", w["fail_rate"], D, "%",
                           "quality_inspections"))
    if q["top_defects"]:
        t = q["top_defects"][0]
        facts.append(_fact("quality.top_defect", "Top defect", t["category"], M, source="quality_inspections"))
        facts.append(_fact("quality.top_defect_count", f"{t['category']} occurrences", t["count"], M, "defects",
                           "quality_inspections"))
    return _result("get_quality_summary", ev.OK, said, facts)


@tool("get_maintenance_status",
      "Open maintenance work: how many tasks are open, overdue or awaiting approval, "
      "and the next task up.",
      mirrors="/maintenance-summary", view="cmms", domain="maintenance")
def get_maintenance_status(db, tenant):
    m = build_maintenance_summary(db, tenant)
    facts = [
        _fact("maint.open", "Open maintenance tasks", m["open"], M, "tasks", "maintenance_tasks", "now"),
        _fact("maint.overdue", "Overdue", m["overdue"], D, "tasks", "maintenance_tasks", "now",
              detail="planned date has passed"),
        _fact("maint.pending_approval", "Awaiting approval", m["pending_approval"], M, "tasks",
              "maintenance_tasks", "now"),
    ]
    if m["tasks"]:
        t = m["tasks"][0]
        facts.append(_fact("maint.next_task", "Next task", f"{t['task_type']} on {t['machine']}", M,
                           source="maintenance_tasks", window="now"))
    return _result("get_maintenance_status", ev.OK, assistant.say_maintenance(m), facts)


@tool("get_compliance_status",
      "Controlled documents: how many are on file, overdue for review, due soon or "
      "unapproved.",
      mirrors="/compliance-summary", view="documents", domain="compliance")
def get_compliance_status(db, tenant):
    c = build_compliance_summary(db, tenant)
    said = assistant.say_compliance(c)
    if c["total"] == 0:
        return _result("get_compliance_status", ev.NO_DATA, said,
                       [_fact("docs.total", "Controlled documents", 0, M, "documents", "compliance_documents", "now")])
    facts = [
        _fact("docs.total", "Controlled documents", c["total"], M, "documents", "compliance_documents", "now"),
        _fact("docs.overdue", "Reviews overdue", c["overdue"], D, "documents", "compliance_documents", "now"),
        _fact("docs.due_soon", "Reviews due soon", c["due_soon"], R, "documents", "compliance_documents", "now"),
        _fact("docs.unapproved", "Unapproved", c["pending_approval"], M, "documents", "compliance_documents", "now"),
    ]
    return _result("get_compliance_status", ev.OK, said, facts)


@tool("get_inventory_status",
      "Stock: how many items are at or below their reorder level, how many are out of "
      "stock, which to reorder first, and draft purchase orders waiting.",
      mirrors="/inventory-summary", view="inventory", domain="inventory")
def get_inventory_status(db, tenant):
    inv = build_inventory_summary(db, tenant)
    said = assistant.say_inventory(inv)
    if inv["total_items"] == 0:
        return _result("get_inventory_status", ev.NO_DATA, said,
                       [_fact("stock.items", "Stock items", 0, M, "items", "inventory_items", "now")])
    facts = [
        _fact("stock.items", "Stock items", inv["total_items"], M, "items", "inventory_items", "now"),
        _fact("stock.at_risk", "At or below reorder level", inv["at_risk"], R, "items", "inventory_items", "now",
              detail="current stock at or below the item's reorder level"),
        _fact("stock.out", "Out of stock", inv["out_of_stock"], M, "items", "inventory_items", "now"),
        _fact("stock.draft_pos", "Draft purchase orders waiting", inv["auto_pos_pending"], M, "POs",
              "purchase_orders", "now"),
    ]
    if inv["items"]:
        i = inv["items"][0]
        facts.append(_fact("stock.first", "Reorder first", i["item_name"], R, source="inventory_items", window="now"))
        facts.append(_fact("stock.first_level", f"{i['item_name']} in stock", i["current_stock"], M,
                           i.get("unit") or "", "inventory_items", "now",
                           detail=f"reorder level {i['reorder_level']}"))
    return _result("get_inventory_status", ev.OK, said, facts)


# ── The trained model, said as a model ──────────────────────────────

# AMP has exactly ONE adopted model, and the most honest thing a Copilot can do
# with it is refuse to make it sound like more than it is. This tool is the only
# one that returns MODEL ESTIMATE provenance, it always returns the data state
# MODEL NOT VALIDATED, and it carries the artifact's own caveat as a note. Its
# roles are copied from /ai/native/failure-risk (Admin, Supervisor) — the first
# tool narrower than a plain authenticated read, so the role check in run_tool
# has a real case, not only a test double. See ADR-0027.
FAILURE_RISK_ROLES = ("Admin", "Supervisor")
_MODEL_LEAD = ("This is a model estimate, not a measurement, and the model has only been "
               "evaluated on synthetic machines.")


@tool("get_failure_risk",
      # No role is named in a description: the catalogue is sent to the model on
      # every planning call, and AMP tells a model nothing about who is asking.
      # Whether this tool may run is decided by run_tool, from the Principal the
      # route built -- never by the model, and never from anything it can read.
      "AMP's trained failure-risk model: the estimated chance each machine starts a new "
      "breakdown in the next 7 days, beside the rule score for the same machine. The model "
      "has only been evaluated on synthetic data, so its numbers are estimates, not measurements.",
      mirrors="/ai/native/failure-risk", view="machines", domain="machines",
      roles=FAILURE_RISK_ROLES)
def get_failure_risk(db, tenant):
    from datetime import datetime

    from amp_ai.failure_risk import db_history, predict    # lazy: pulls the artifact loader
    as_of = datetime.utcnow()                              # naive UTC, as every AMP column stores it
    result = predict.predict(db_history.load_histories(db, tenant, as_of), as_of)
    caveat = predict.CAVEAT
    if result.get("status") != "ok":
        # The artifact is missing, edited or unreadable. Say that, in those words:
        # a model that cannot be verified produces no number here, ever.
        return ev.ToolResult(
            tool="get_failure_risk", state=ev.NOT_MEASURED,
            summary=("AMP's failure-risk model is not available right now, so there is no model "
                     "estimate to give. The rule-based health score is unaffected."),
            facts=[_fact("model.available", "Model available", "no", M, source="amp_ai", window="now",
                         detail="the pinned artifact did not verify")],
            view="machines", notes=[caveat])

    scored = [m for m in result["machines"] if m.get("probability") is not None]
    scored.sort(key=lambda m: -m["probability"])
    excluded = [m for m in result["machines"] if m.get("excluded_reason") == "already_in_breakdown"]
    facts = [
        _fact("model.name", "Model", result["model_version"], M, source="amp_ai", window="now",
              detail=f"trained on {result['training_data_source']}"),
        _fact("model.machines_scored", "Machines scored", len(scored), M, "machines", "amp_ai", "now",
              detail=f"over the last {result['lookback_days']} days"),
    ]
    horizon = f"next {result['horizon_days']} days"
    for m in scored[:3]:
        # A probability is the one number in AMP that is neither measured nor
        # derived from a measurement. It is shown to one decimal place, with the
        # machine's rule score beside it so the two can disagree in the open.
        facts.append(_fact(f"model.risk.{m['machine_id']}", f"{m['name']} estimated breakdown chance",
                           round(m["probability"] * 100, 1), ev.MODEL, "%", "amp_ai", horizon,
                           detail=f"band {m['band']}; {caveat}"))
        facts.append(_fact(f"model.rule.{m['machine_id']}", f"{m['name']} rule score",
                           round(m["rule_score"]), R, "/100", "predictive_engine", "now",
                           detail="the hand-weighted rule, for comparison"))
    if excluded:
        facts.append(_fact("model.excluded", "Not scored: already in breakdown", len(excluded), M,
                           "machines", "amp_ai", "now",
                           detail="the model estimates a NEW breakdown starting, so a machine already "
                                  "down is left out"))
    if not scored:
        summary = ("No machine could be scored by the failure-risk model right now. " + _MODEL_LEAD)
    else:
        top = scored[0]
        summary = (f"{top['name']} carries the highest model estimate, {round(top['probability'] * 100, 1)}% "
                   f"for a new breakdown in the {horizon} (band {top['band']}), against a rule score of "
                   f"{round(top['rule_score'])}/100. " + _MODEL_LEAD)
    # MODEL NOT VALIDATED is not a warning the UI may drop: it is the state of
    # every result this tool can return while the evaluation is synthetic-only.
    return ev.ToolResult(tool="get_failure_risk", state=ev.MODEL_NOT_VALIDATED, summary=summary,
                         facts=facts, view="machines",
                         notes=[caveat, "Not a maintenance instruction: no action here has been "
                                        "validated against real failures."])
@tool("get_daily_brief",
      "The written daily brief for the whole plant: where it stands, what changed against last "
      "week, what is wrong ranked by what it cost, why, what is likely to become a problem, how "
      "the shifts did, what is waiting for a decision — and what AMP could NOT see. Use for "
      "'give me the brief', 'the daily update', 'brief me', 'what do I need to know today'.",
      mirrors="/daily-brief", view="overview", domain="plant")
def get_daily_brief(db, tenant):
    from ai.brief import build_daily_brief   # lazy: pulls in every pillar module
    b = build_daily_brief(db, tenant)
    blind = [s for s in b["blind_spots"] if s["state"] != ev.OK]
    facts = [
        _fact("brief.sections", "Sections in the brief", len(b["sections"]), M, "sections",
              "daily brief", b["window"]),
        # The count of things AMP could not see is itself a fact, so the Copilot
        # can state it and the grounding gate can check it. A brief that hid this
        # number would read as more complete than it is.
        _fact("brief.blind_spots", "Things AMP could not see", len(blind), M, "gaps",
              "daily brief", b["window"]),
    ]
    # One fact per section carrying its own state, so a reader is told WHICH part
    # of the brief is thin rather than only that some part is.
    for s in b["sections"]:
        facts.append(_fact(f"brief.section.{s['key']}", s["title"], s["state"], R,
                           source="daily brief", window=b["window"],
                           detail=(s["lines"][0] if s["lines"] else "")))
    for i, spot in enumerate(blind[:3], 1):
        facts.append(_fact(f"brief.blind_{i}", f"Not measured {i}", spot["text"], U,
                           source="daily brief", window=b["window"]))
    state = b["state"] if b["state"] in ev.DATA_STATES else ev.OK
    return _result("get_daily_brief", state, say_brief(b), facts, notes=[b["note"]])


# ── Did it help? ────────────────────────────────────────────────────

@tool("get_action_outcomes",
      "What changed after the actions that were approved: the metric each one was meant to "
      "move, what it read before and after, and which way it went. A measured change, never "
      "a claim that the action caused it. Use for 'did it help', 'did that work', "
      "'what happened after we approved', 'are the recommendations working'.",
      mirrors="/action-outcomes", view="agentactivity", domain="agents")
def get_action_outcomes(db, tenant):
    from ai.outcomes import build_outcome_summary, say_outcomes   # lazy: pulls the pillar modules
    s = build_outcome_summary(db, tenant)
    facts = [
        _fact("outcomes.followed_up", "Approved actions being followed up", s["followed_up"], M,
              "actions", "action_outcomes", "now"),
        _fact("outcomes.measured", "Followed up long enough to judge", s["measured"], M, "actions",
              "action_outcomes", "now",
              detail=f"a window of {s['window_days']} days each side of the decision"),
        _fact("outcomes.waiting", "Still inside the window", s["waiting"], M, "actions",
              "action_outcomes", "now", detail="nothing is judged before the window has elapsed"),
    ]
    for verdict, count in s["counts"].items():
        # The counts are RULE: which side of the noise floor a change fell on is
        # a threshold AMP chose, and that threshold is stated on the card.
        facts.append(_fact(f"outcomes.{verdict.lower().replace(' ', '_')}",
                           f"Metric {verdict.lower()} after the action", count, R, "actions",
                           "action_outcomes", "now"))
    for row in s["outcomes"][:3]:
        if row["change"] is None:
            continue
        # The CHANGE is the one figure a reader will quote, so it carries the
        # caveat in its own detail, not only in the card's footnote.
        facts.append(_fact(f"outcomes.change.{row['id']}",
                           f"{row['metric_label']} change on {row['scope_label'] or 'it'}",
                           row["change"], ev.CORRELATION, row["unit"], "action_outcomes",
                           f"{row['window_days']} days each side",
                           detail=f"{row['action'] or 'an approved action'}; {CAUSATION_NOTE}"))
    state = s["state"] if s["state"] in ev.DATA_STATES else ev.OK
    return _result("get_action_outcomes", state, say_outcomes(s), facts, notes=[s["note"]])
@tool("get_shortage_risk",
      "What a stock shortage will actually stop: for each item at or below its reorder level, "
      "the open work orders that need it, and how many units of production cannot be made from "
      "the stock on hand. Use for 'what will the shortage cost us', 'what is the stock-out "
      "stopping', 'which orders are at risk from stock'.",
      mirrors="/shortage-impact", view="inventory", domain="inventory")
def get_shortage_risk(db, tenant):
    from ai.shortage import build_shortage_impact, say_shortage   # lazy: pulls the pillar modules
    s = build_shortage_impact(db, tenant)
    # A workspace with no stock records has no units at risk to derive, so the
    # figure is None and must say UNKNOWN rather than claim a derivation it did
    # not do. ai.evidence enforces this pairing and raised on it, which is the
    # rule working: a null labelled DERIVED reads as a measured zero.
    at_risk = s["units_at_risk"]
    facts = [
        _fact("shortage.units_at_risk", "Units that cannot be made from stock on hand",
              at_risk, D if at_risk is not None else U, "units",
              "work_orders x bills_of_materials", "now",
              detail=s["note"] if at_risk is not None
              else "no stock items are set up, so there is nothing to size a shortage against"),
        _fact("shortage.items_sized", "Short items AMP could size", len(s["shortages"]), M, "items",
              "inventory_items", "now"),
        # The items AMP could NOT size are a fact too. Leaving them out would
        # make the sized list read as the whole shortage.
        _fact("shortage.items_unlinked", "Short items with no recipe linking them to production",
              len(s["unlinked"]), M, "items", "bills_of_materials", "now",
              detail="AMP will not guess what an item outside every bill of materials would stop"),
    ]
    if s["money_at_risk"] is not None:
        facts.append(_fact("shortage.money_at_risk", "Value of the units at risk", s["money_at_risk"],
                           D, CURRENCY, "cost model (ADR-0010)", "now"))
    else:
        facts.append(_fact("shortage.money_at_risk", "Value of the units at risk", None, U, CURRENCY,
                           "tenant configuration", "now",
                           detail="no unit value is set, so AMP will not put a money figure on it"))
    for row in s["shortages"][:3]:
        facts.append(_fact(f"shortage.{row['item_code']}", f"{row['item_name']}: units at risk",
                           row["units_at_risk"], D, "units", "work_orders x bills_of_materials", "now",
                           detail=f"{row['on_hand']} {row['unit']} on hand against "
                                  f"{row['required_units']} the open orders need"))
        # The headline says "across N open orders", so N has to be a fact. The
        # grounding gate caught this: AMP's own sentence quoted a figure that was
        # in no evidence, which is the thing the gate exists to stop.
        facts.append(_fact(f"shortage.{row['item_code']}.orders",
                           f"{row['item_name']}: open orders it would hold up",
                           row["orders_affected"], M, "orders", "work_orders", "now"))
    state = s["state"] if s["state"] in ev.DATA_STATES else ev.OK
    return _result("get_shortage_risk", state, say_shortage(s), facts, notes=[s["note"]])


@tool("get_what_to_raise",
      "What AMP judges worth interrupting someone for right now, and -- the point of it -- "
      "everything it is holding back and why. Use for 'should you be telling me anything', "
      "'what would you alert me about', 'are you sitting on anything', 'why did you not tell me'.",
      mirrors="/proactive", view="overview", domain="plant")
def get_what_to_raise(db, tenant):
    from ai.proactive import build_proactive, say_proactive   # lazy: composes the pillars
    p = build_proactive(db, tenant)
    held = {}
    for s in p["suppressed"]:
        held[s["suppressed_by"]] = held.get(s["suppressed_by"], 0) + 1
    facts = [
        _fact("raise.considered", "Things the engines raised", p["considered"], M, "findings",
              "command centre, risk radar, outcomes", "now"),
        _fact("raise.qualified", "Worth interrupting you for", len(p["qualified"]), R, "findings",
              "the published bar", "now", detail=p["bar"]),
        # The held-back count is the honest half. A proactive feature that
        # reported only what it sent would be unable to be argued with.
        _fact("raise.held_back", "Held back", len(p["suppressed"]), R, "findings",
              "the published bar", "now",
              detail=f"cooldown {p['cooldown_hours']}h, at most {p['max_per_run']} in one run"),
    ]
    for reason, count in sorted(held.items()):
        facts.append(_fact(f"raise.held.{reason.lower().replace(' ', '_')}",
                           f"Held back: {reason.lower()}", count, R, "findings",
                           "the published bar", "now"))
    for q in p["qualified"][:3]:
        facts.append(_fact(f"raise.{q['signature']}", f"{q['severity']}: {q['title']}",
                           q["why"], R, source=q["kind"], window="now"))
    state = p["state"] if p["state"] in ev.DATA_STATES else ev.OK
    return _result("get_what_to_raise", state, say_proactive(p), facts, notes=[p["bar"]])


@tool("get_anomaly_sweep",
      "AMP's experimental telemetry check, run over every machine at once: a score where there "
      "is enough history and consent, and the reason there is not one everywhere else. The check "
      "did NOT beat the existing rules on held-out data, so its numbers are estimates and never "
      "alarms. Use for 'is anything behaving oddly', 'check the telemetry', 'run the anomaly check'.",
      mirrors="/ai/native/anomaly/sweep", view="machines", domain="machines",
      roles=FAILURE_RISK_ROLES)
def get_anomaly_sweep(db, tenant):
    from amp_ai import consent                 # the same gate the route uses
    from amp_ai.telemetry_anomaly import service
    from ai.anomaly_sweep import NOT_ADOPTED, build_anomaly_sweep, say_anomaly_sweep

    s = build_anomaly_sweep(db, tenant, scorer=service.score_machine,
                            gate=consent.DbConsentGate())
    facts = [
        _fact("anomaly.scored", "Machines scored", s["scored"], M, "machines", "telemetry baseline",
              "last hour"),
        # The machines AMP could NOT score are a fact. Reporting only the scored
        # ones would make a thin fleet look like a clean one.
        _fact("anomaly.not_scored", "Machines it could not score", s["not_scored"], M, "machines",
              "telemetry baseline", "last hour",
              detail="each one says why: no history, no telemetry, or no model"),
    ]
    if not s["consent"]:
        facts.append(_fact("anomaly.consent", "Learning consent", "not granted", M,
                           source="ai_learning_consent", window="now",
                           detail="AMP fitted no baselines and scored nothing"))
    for row in [r for r in s["machines"] if r["score"] is not None][:3]:
        # MODEL ESTIMATE, the same label the failure-risk model gets, for the
        # same reason: it is neither measured nor derived from a measurement.
        facts.append(_fact(f"anomaly.{row['machine_id']}", f"{row['name']}: anomaly score",
                           row["score"], ev.MODEL, "", "telemetry baseline", "last hour",
                           detail=NOT_ADOPTED))
    state = s["state"] if s["state"] in ev.DATA_STATES else ev.MODEL_NOT_VALIDATED
    return _result("get_anomaly_sweep", state, say_anomaly_sweep(s), facts, notes=[NOT_ADOPTED])


# ── The plant board: parts against target, kilograms, rupees ────────
#
# The board (ai/plant_board.py) is the one read-model whose figures are a
# CONVERSION away from anything a controller holds. A machine counts shots;
# parts, kilograms and money exist only once somebody has entered a PartSpec for
# the part the fitted mould makes. So these tools carry two honesty rules the
# other tools never need:
#
#   1. A MACHINE WITH NO SPEC STILL HAS A COUNT. Its parts are measured. Its
#      target, its kilograms and its rupees are not derivable AT ALL. The builder
#      returns 0.0 for those because a chart needs a number to draw, and a tool
#      that passed those zeros on would report a plant that consumed no material
#      and earned nothing. Each is stated as UNKNOWN with the reason instead, and
#      the builder's own `material` / `priced` / `ideal_per_hour` decide which:
#      they are independent, because a spec with no declared cycle prices output
#      it cannot rate.
#   2. POWER AND PACKING HAVE NO SOURCE on this customer's floor -- no meter is
#      fitted and nobody records packed quantities. The builder marks both
#      unavailable and carries no points; test_plant_board.py pins that at length,
#      and it has to survive into the Copilot's answer, because "0 kWh" and
#      "nothing is measuring kWh" are different claims and only one is true.
#
# The window is the DAY or the MONTH asked for, not the usual seven, so every
# fact here says which period it covers.

BOARD_VIEW = "plantboard"
BOARD_MACHINES_SHOWN = 3

_NO_SPEC = ("no part spec is on record for the part this machine is making, so its shot count "
            "cannot be converted at all -- this is underivable, not zero")
_NO_CYCLE = ("the part spec declares no cycle time, so no ideal rate exists and the hours are "
             "unrated rather than missed")
_NO_PRICE = ("no price per piece is on record for this part, so its output has no money value here")
# plant_board.month's own label for output it could not attribute to a part.
# Pinned by test_copilot_plant_board_tool.py so a rename there fails loudly here.
_NO_PART_BUCKET = "(no part assigned)"


def _on_date(on):
    """(date, None) or (None, why).

    "today" and "yesterday" are accepted because that is how a person asks;
    anything else must be the route's own YYYY-MM-DD, so a tool can never read a
    day the REST route would have rejected.
    """
    text = (on or "").strip().lower()
    today = datetime.utcnow().date()
    if not text or text == "today":
        return today, None
    if text == "yesterday":
        return today - timedelta(days=1), None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date(), None
    except ValueError:
        return None, f"A day must be YYYY-MM-DD, \"today\" or \"yesterday\", not {str(on)[:40]!r}."


def _coverage(have, total, what):
    """What a plant total actually covers. A sum over the specced machines only
    is not a plant figure, and saying so is the difference between a total and a
    guess."""
    if have >= total:
        return f"every machine has {what}"
    return (f"covers the {have} of {total} machines that have {what}; the other {total - have} are "
            f"counted but not converted, so this is not a plant total")


def _board_rows(board):
    """The board's three per-machine series joined on machine_id, with what is
    derivable for each machine stated rather than implied.

    `kg_total` and the shift revenues come back from the builder as 0.0 for a
    machine with no spec. That zero is drawable but it is not a reading, so it is
    dropped to None here and the caller says UNKNOWN.
    """
    rm = {r["machine_id"]: r for r in board["rm_status"]}
    sr = {s["machine_id"]: s for s in board["shift_rate"]}
    rows = []
    for p in board["production"]:
        mid = p["machine_id"]
        material = (rm.get(mid) or {}).get("material")
        blocks = (sr.get(mid) or {}).get("points") or []
        priced = bool((sr.get(mid) or {}).get("priced")) and bool(blocks)
        points = p["points"]
        rows.append({
            "machine": p["machine"], "part": p["part"], "material": material,
            "parts": int(p["total"] or 0),
            "target": int(p["ideal_per_hour"] or 0) or None,
            # An hour is BELOW a level only where a level was declared. An
            # unrated hour is not a miss; it is an hour nobody set a standard for.
            "below": sum(1 for pt in points if pt["status"] == "low"),
            "hours": len(points),
            "kg": (rm.get(mid) or {}).get("kg_total") if material else None,
            "revenue": round(sum(b["revenue"] for b in blocks), 2) if priced else None,
            "best": max(blocks, key=lambda b: (b["rate_per_hour"], -b["shift"])) if priced else None,
        })
    return rows


def _unavailable_facts(prefix, board, window):
    """The two series with no source, as UNKNOWN facts carrying the builder's own
    reason and fix. Never zeros, and never omitted: a reader who asked for the
    board is owed the fact that two of its five graphs cannot be drawn."""
    out = []
    for key, unit, label in (("power", "kWh", "Power consumed"),
                             ("packing", "parts", "Quantity packed")):
        series = board.get(key) or {}
        out.append(_fact(f"{prefix}.{key}", label, None, U, unit, "no source is connected", window,
                         detail=f"{series.get('reason', '')} {series.get('fix', '')}".strip()
                                + " -- reported as unavailable, not as 0"))
    return out


@tool("get_plant_board",
      "One day of the plant board, per machine: parts made against the target the "
      "part spec implies, kilograms of material consumed, and the shift-hour money "
      "rate. Use for hourly output against target, which machine missed its target, "
      "kilograms or material used on a day, and shift-rate questions. Power and "
      "packing are not measured and are reported as such.",
      mirrors="/analytics/plant-board", view=BOARD_VIEW, domain="production",
      params={"on": Param("str", "The day: YYYY-MM-DD, or \"today\" or \"yesterday\"")})
def get_plant_board(db, tenant, on=""):
    from ai import plant_board            # lazy: the board pulls in the models layer
    when, why = _on_date(on)
    if when is None:
        return ev.refusal("get_plant_board", ev.INVALID_ARGUMENTS, why)
    board = plant_board.day(db, tenant, when)
    day = board["date"]
    rows = _board_rows(board)
    if not rows:
        return _result("get_plant_board", ev.NO_DATA,
                       (f"There are no machines on the plant board for {day}.", BOARD_VIEW),
                       [_fact("board.machines", "Machines on the board", 0, M, "machines", "machines", day)])

    specced = [r for r in rows if r["kg"] is not None]
    priced = [r for r in rows if r["revenue"] is not None]
    targeted = [r for r in rows if r["target"]]
    nospec = [r for r in rows if not r["material"]]
    parts = sum(r["parts"] for r in rows)
    facts = [
        _fact("board.date", "The day this covers", day, M, source="the day asked for", window=day),
        _fact("board.machines", "Machines on the board", len(rows), M, "machines", "machines", day),
        _fact("board.parts", "Parts made", parts, M, "parts", "production_records", day),
        _fact("board.acceptable", "The level an hour must reach to count as acceptable",
              round(board["acceptable_fraction"] * 100), R, "% of target",
              "plant policy (ai.plant_board.ACCEPTABLE)", day,
              detail="a plant policy, not a law; the board returns it rather than fixing it in the browser"),
        _fact("board.unspecified", "Machines with no part spec", len(nospec), M, "machines",
              "part_specs, via the tool fitted to each machine", day,
              detail="their parts are counted; their target, kilograms and money are not derivable "
                     "until somebody enters a part spec" if nospec else "every machine's part is on record"),
    ]

    # Target. "Below target" only means something where a target was declared.
    if targeted:
        worst = max(targeted, key=lambda r: (r["below"], r["machine"]))
        facts.append(_fact("board.worst_machine", "Machine below target for the most hours",
                           worst["machine"], M, source="production_records", window=day))
        facts.append(_fact("board.worst_machine_hours", f"{worst['machine']} hours below target",
                           worst["below"], R, "hours", "hourly parts against the spec's ideal rate", day,
                           detail=f"out of {worst['hours']} hours in the day"))
    else:
        facts.append(_fact("board.worst_machine_hours", "Hours below target", None, U, "hours",
                           "part_specs", day,
                           detail="no machine has a part spec with a declared cycle time, so no hour "
                                  "has a target to be below -- unrated, not missed"))

    # Material.
    if specced:
        facts.append(_fact("board.kg", "Material consumed", round(sum(r["kg"] for r in specced), 3), D,
                           "kg", "parts x the spec's part weight", day,
                           detail=_coverage(len(specced), len(rows), "a part spec")))
    else:
        facts.append(_fact("board.kg", "Material consumed", None, U, "kg", "part_specs", day,
                           detail="no machine has a part spec, so the shot counts cannot become "
                                  "kilograms at all (not 0 kg)"))

    # Money. The rate is the builder's own block figure, never re-derived here.
    if priced:
        facts.append(_fact("board.revenue", "Value of the day's output",
                           round(sum(r["revenue"] for r in priced), 2), D, CURRENCY,
                           "parts x the spec's price per piece", day,
                           detail=_coverage(len(priced), len(rows), "a priced part spec")))
        top = max(priced, key=lambda r: (r["best"]["rate_per_hour"], r["machine"]))
        facts.append(_fact("board.best_rate_where", "Where the highest shift-hour rate was",
                           f"{top['machine']} shift {top['best']['shift']}", M, source="shift_rate",
                           window=day))
        facts.append(_fact("board.best_rate", "Highest shift-hour rate", top["best"]["rate_per_hour"], D,
                           CURRENCY, "that block's revenue over its 8 hours", day))
    else:
        facts.append(_fact("board.revenue", "Value of the day's output", None, U, CURRENCY,
                           "part_specs", day,
                           detail="no machine has a priced part spec, so the day's output has no money "
                                  "value here (not zero)"))
        facts.append(_fact("board.best_rate", "Highest shift-hour rate", None, U, CURRENCY,
                           "part_specs", day, detail=_NO_PRICE))

    # Per machine, most parts first: the count ALWAYS, and each conversion only
    # where the spec supports it.
    ranked = sorted(rows, key=lambda r: (-r["parts"], r["machine"]))
    for i, r in enumerate(ranked[:BOARD_MACHINES_SHOWN], 1):
        k = f"board.machine_{i}"
        facts.append(_fact(k, f"Machine {i}", r["machine"], M, source="machines", window=day,
                           detail=f"making {r['part']}" if r["part"] else "no part on record"))
        facts.append(_fact(f"{k}.parts", f"{r['machine']} parts made", r["parts"], M, "parts",
                           "production_records", day))
        if r["target"]:
            facts.append(_fact(f"{k}.target", f"{r['machine']} target per hour", r["target"], D,
                               "parts/hour", "3600 / the spec's cycle time x its active cavities", day))
            facts.append(_fact(f"{k}.below", f"{r['machine']} hours below target", r["below"], R, "hours",
                               "hourly parts against that target", day))
        else:
            facts.append(_fact(f"{k}.target", f"{r['machine']} target per hour", None, U, "parts/hour",
                               "part_specs", day, detail=_NO_SPEC if not r["material"] else _NO_CYCLE))
        if r["kg"] is not None:
            facts.append(_fact(f"{k}.kg", f"{r['machine']} material consumed", r["kg"], D, "kg",
                               "parts x the spec's part weight", day, detail=f"material: {r['material']}"))
        else:
            facts.append(_fact(f"{k}.kg", f"{r['machine']} material consumed", None, U, "kg",
                               "part_specs", day, detail=_NO_SPEC))
        if r["revenue"] is not None:
            facts.append(_fact(f"{k}.rate", f"{r['machine']} best shift-hour rate",
                               r["best"]["rate_per_hour"], D, CURRENCY,
                               "that block's revenue over its 8 hours", day))
        else:
            facts.append(_fact(f"{k}.rate", f"{r['machine']} shift-hour rate", None, U, CURRENCY,
                               "part_specs", day, detail=_NO_SPEC if not r["material"] else _NO_PRICE))

    facts += _unavailable_facts("board", board, day)

    # Power and packing are ALWAYS absent, so this state never claims a complete
    # picture. A board with no conversions at all is NOT MEASURED rather than
    # PARTIAL: nothing on it has been turned into a figure a person asked for.
    state = ev.NOT_MEASURED if not specced and not priced else ev.PARTIAL_DATA
    return _result("get_plant_board", state,
                   _say_board(day, rows, parts, specced, priced, targeted, nospec), facts,
                   notes=["Parts, kilograms and money are conversions of a shot count through the part "
                          "spec in force on the day read; a machine with no spec is counted only.",
                          "Power and packing have no source on this floor and are never shown as zero."])


def _say_board(day, rows, parts, specced, priced, targeted, nospec):
    """AMP's own sentence for the board. Every figure in it is one of the facts
    above, and the rupee figure appears only where a part is priced -- a plant
    with no price must not see a money sentence at all."""
    out = [f"The plant board for {day}: {len(rows)} machine{'' if len(rows) == 1 else 's'} "
           f"made {parts:,} parts."]
    if targeted:
        worst = max(targeted, key=lambda r: (r["below"], r["machine"]))
        out.append(f"{worst['machine']} was below its target for {worst['below']} "
                   f"of {worst['hours']} hours.")
    else:
        out.append("No machine has a declared target, so no hour is rated against one.")
    if specced:
        out.append(f"{round(sum(r['kg'] for r in specced), 3):,} kg of material was consumed.")
    else:
        out.append("Material consumed is not derivable: no machine has a part spec.")
    if priced:
        top = max(priced, key=lambda r: (r["best"]["rate_per_hour"], r["machine"]))
        out.append(f"The best shift-hour rate was {unit_rate(top['best']['rate_per_hour'])} "
                   f"at {top['machine']} shift {top['best']['shift']}.")
    else:
        out.append("No part is priced, so there is no money rate for the day.")
    if nospec:
        out.append(f"{len(nospec)} machine{'' if len(nospec) == 1 else 's'} "
                   f"have no part spec, so their parts are counted but not converted.")
    out.append("Power and packing have no source on this floor, so neither is reported.")
    return " ".join(out), BOARD_VIEW


@tool("get_plant_board_month",
      "The plant board's monthly tables: output by part, kilograms of each material "
      "consumed, and the shift-hour money rate by machine, over a whole month. Use "
      "for monthly kilograms or material consumption, monthly output by part, and "
      "monthly rate questions. Power and packing are not measured.",
      mirrors="/analytics/plant-board/month", view=BOARD_VIEW, domain="production",
      params={"year": Param("int", "The year, e.g. 2026", minimum=2000, maximum=2100),
              "month": Param("int", "The month of the year, 1-12", minimum=1, maximum=12)})
def get_plant_board_month(db, tenant, year=0, month=0):
    from ai import plant_board            # lazy: the board pulls in the models layer
    now = datetime.utcnow()
    year, month = year or now.year, month or now.month
    m = plant_board.month(db, tenant, year, month)
    window = f"{year}-{month:02d}"

    items = m["itemwise_production"]
    materials = m["rm_consumption"]
    machines = m["shift_rate_by_machine"]
    # Output the board could not attribute to a part: counted, never converted.
    unassigned = sum(i["total"] for i in items if i["part"] == _NO_PART_BUCKET)
    parts = sum(i["total"] for i in items)
    facts = [
        _fact("month.parts", "Parts made in the month", parts, M, "parts", "production_records", window),
        _fact("month.good", "Good parts", sum(i["good"] for i in items), M, "parts",
              "production_records", window),
        _fact("month.unassigned", "Parts made with no part spec", unassigned, M, "parts",
              "production_records with no part spec for the machine's tool", window,
              detail="counted, but not convertible to kilograms or money"
                     if unassigned else "every part made was attributable to a spec"),
    ]
    for i, row in enumerate([x for x in items if x["part"] != _NO_PART_BUCKET][:3], 1):
        facts.append(_fact(f"month.part_{i}", f"Part {i} by output", row["part"], M,
                           source="part_specs", window=window))
        facts.append(_fact(f"month.part_{i}.total", f"{row['part']} made", row["total"], M, "parts",
                           "production_records", window))

    if materials:
        facts.append(_fact("month.kg", "Material consumed", round(sum(x["kg"] for x in materials), 3), D,
                           "kg", "parts x each spec's part weight", window,
                           detail="covers only output with a part spec" if unassigned
                                  else "every part made had a spec"))
        for i, row in enumerate(materials[:3], 1):
            facts.append(_fact(f"month.material_{i}", f"Material {i} by weight", row["material"], M,
                               source="part_specs", window=window))
            facts.append(_fact(f"month.material_{i}.kg", f"{row['material']} consumed", row["kg"], D, "kg",
                               "parts x that spec's part weight", window))
    else:
        facts.append(_fact("month.kg", "Material consumed", None, U, "kg", "part_specs", window,
                           detail="no output in the month had a part spec, so no weight is derivable "
                                  "(not 0 kg)"))

    if machines:
        facts.append(_fact("month.revenue", "Value of the month's output", m["total_revenue"], D, CURRENCY,
                           "parts x each spec's price per piece", window))
        facts.append(_fact("month.rate", "Shift-hour rate for the plant", m["total_rate_per_hour"], D,
                           CURRENCY, "revenue over the hours in the month", window,
                           detail="the machine rates added together, each revenue over the month's hours"))
        top = machines[0]
        facts.append(_fact("month.top_machine", "Machine earning the most", top["machine"], M,
                           source="production_records", window=window))
        facts.append(_fact("month.top_machine_rate", f"{top['machine']} shift-hour rate",
                           top["rate_per_hour"], D, CURRENCY, "its revenue over the month's hours", window))
    else:
        facts.append(_fact("month.revenue", "Value of the month's output", None, U, CURRENCY,
                           "part_specs", window,
                           detail="no machine made output against a priced part spec, so the month has "
                                  "no money value here (not zero)"))
        facts.append(_fact("month.rate", "Shift-hour rate for the plant", None, U, CURRENCY,
                           "part_specs", window, detail=_NO_PRICE))

    facts += _unavailable_facts("month", m, window)
    state = ev.NOT_MEASURED if not materials and not machines else ev.PARTIAL_DATA
    return _result("get_plant_board_month", state,
                   _say_month(window, parts, unassigned, materials, machines, m), facts,
                   notes=["Monthly kilograms and money are conversions through each part's spec; output "
                          "with no spec is counted in the totals but converted in neither.",
                          "Power and packing have no source on this floor and are never shown as zero."])


def _say_month(window, parts, unassigned, materials, machines, m):
    out = [f"The plant board for {window}: {parts:,} parts made."]
    if materials:
        top = materials[0]
        out.append(f"{round(sum(x['kg'] for x in materials), 3):,} kg of material was consumed, "
                   f"most of it {top['material']} at {top['kg']:,} kg.")
    else:
        out.append("No output in the month had a part spec, so no material weight is derivable.")
    if machines:
        out.append(f"The plant's shift-hour rate was {unit_rate(m['total_rate_per_hour'])}, "
                   f"with {machines[0]['machine']} earning most.")
    else:
        out.append("No part is priced, so the month has no money rate.")
    if unassigned:
        out.append(f"{unassigned:,} parts were made with no part spec, so they are counted "
                   f"but converted to neither kilograms nor money.")
    out.append("Power and packing have no source on this floor, so neither is reported.")
    return " ".join(out), BOARD_VIEW


#: The windows a person names out loud, resolved by AMP against ITS clock.
#: A model has no reliable one -- asked for "last week" it would have to guess
#: today's date and then count backwards, and a window computed from a guessed
#: date is a window nobody asked for. So the model names the period and AMP
#: works out the dates, exactly as `on: "yesterday"` already works.
NAMED_PERIODS = ("today", "yesterday", "this week", "last week", "this month",
                 "last month", "this quarter", "last quarter", "this year", "last year",
                 "last 7 days", "last 14 days", "last 30 days")
#: The same cap the route enforces, so a tool cannot ask for a window the REST
#: door would refuse.
MAX_PERIOD_DAYS = 400
_RANGE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*(?:to|\.\.|-{1,2}|through)\s*(\d{4}-\d{2}-\d{2})$")


def _month_start(d):
    return datetime(d.year, d.month, 1)


def _next_month(d):
    return datetime(d.year + (d.month == 12), (d.month % 12) + 1, 1)


def _window(text):
    """(start, end, None) or (None, None, why). `end` is EXCLUSIVE throughout.

    An explicit range is INCLUSIVE of both dates, because that is what a person
    typing "1 Oct to 7 Oct" means; it is converted here rather than making the
    caller reason about a half-open interval.
    """
    name = " ".join((text or "").split()).lower() or "this month"
    today = datetime.combine(datetime.utcnow().date(), time.min)
    monday = today - timedelta(days=today.weekday())
    if name == "today":
        return today, today + timedelta(days=1), None
    if name == "yesterday":
        return today - timedelta(days=1), today, None
    if name == "this week":
        return monday, monday + timedelta(days=7), None
    if name == "last week":
        return monday - timedelta(days=7), monday, None
    if name == "this month":
        return _month_start(today), _next_month(today), None
    if name == "last month":
        this = _month_start(today)
        return _month_start(this - timedelta(days=1)), this, None
    if name in ("this quarter", "last quarter"):
        # Calendar quarters, not "about ninety days": a quarter is a period the
        # plant reports on, and an approximation of one is a different window.
        q_start = datetime(today.year, ((today.month - 1) // 3) * 3 + 1, 1)
        if name == "this quarter":
            nxt = q_start
            for _ in range(3):
                nxt = _next_month(nxt)
            return q_start, nxt, None
        prev = q_start
        for _ in range(3):
            prev = _month_start(prev - timedelta(days=1))
        return prev, q_start, None
    if name == "this year":
        return datetime(today.year, 1, 1), datetime(today.year + 1, 1, 1), None
    if name == "last year":
        return datetime(today.year - 1, 1, 1), datetime(today.year, 1, 1), None
    if name in ("last 7 days", "last seven days"):
        return today - timedelta(days=6), today + timedelta(days=1), None
    if name in ("last 14 days", "last fortnight", "fortnight"):
        return today - timedelta(days=13), today + timedelta(days=1), None
    if name in ("last 30 days", "last thirty days"):
        return today - timedelta(days=29), today + timedelta(days=1), None
    m = _RANGE.match(name)
    if m:
        try:
            start = datetime.strptime(m.group(1), "%Y-%m-%d")
            last = datetime.strptime(m.group(2), "%Y-%m-%d")
        except ValueError:
            return None, None, f"{text!r} is not a pair of real dates."
        if last < start:
            return None, None, "The end of a range cannot be before its start."
        end = last + timedelta(days=1)
        if (end - start) > timedelta(days=MAX_PERIOD_DAYS):
            return None, None, f"A window may not be longer than {MAX_PERIOD_DAYS} days."
        return start, end, None
    return None, None, (f"{text!r} is not a period AMP can read. Use one of: "
                        f"{', '.join(NAMED_PERIODS)}; or a range like "
                        f"\"2026-10-01 to 2026-10-07\".")


@tool("get_plant_board_period",
      "The plant board over ANY window: a week, last month, a quarter, a named "
      "range. Gives parts made, kilograms of material consumed AND received, the "
      "value of the output and the plant's shift-hour rate for that window, plus "
      "which day or hour in it was busiest. Use whenever a board question names a "
      "period other than today -- last week, this quarter, last month, a date "
      "range. Power and packing are not measured.",
      mirrors="/analytics/plant-board/period", view=BOARD_VIEW, domain="production",
      params={"period": Param(
          "str", "The window: " + ", ".join(NAMED_PERIODS)
                 + "; or a range like \"2026-10-01 to 2026-10-07\" (both days included)")})
def get_plant_board_period(db, tenant, period="this month"):
    from ai import plant_board            # lazy: the board pulls in the models layer
    start, end, why = _window(period)
    if start is None:
        return ev.refusal("get_plant_board_period", ev.INVALID_ARGUMENTS, why)
    p = plant_board.period(db, tenant, start, end)
    # The window as a reader says it, and as every fact below is scoped by.
    last = (end - timedelta(days=1)).date().isoformat()
    window = start.date().isoformat() if last == start.date().isoformat() \
        else f"{start.date().isoformat()} to {last}"

    items = p["itemwise_production"]
    materials = p["rm_consumption"]
    received = p["rm_added"]
    machines = p["shift_rate_by_machine"]
    parts = sum(i["total"] for i in items)
    unassigned = sum(i["total"] for i in items if i["part"] == _NO_PART_BUCKET)

    facts = [
        _fact("period.window", "The window this covers", window, M,
              source="the period asked for", window=window),
        _fact("period.bucket", "The shape it is read in", p["bucket"], R,
              source="chosen from the span of the window", window=window,
              detail="hours across a day or two, days up to a quarter, months beyond"),
        _fact("period.parts", "Parts made", parts, M, "parts", "production_records", window),
        _fact("period.good", "Good parts", sum(i["good"] for i in items), M, "parts",
              "production_records", window),
        _fact("period.unassigned", "Parts made with no part spec", unassigned, M, "parts",
              "production_records with no spec for the machine's tool", window,
              detail="counted, but convertible to neither kilograms nor money"
                     if unassigned else "every part made was attributable to a spec"),
    ]

    # The busiest bucket: the one figure a shape has that a total does not.
    busiest = max(p["series"], key=lambda s: s["parts"], default=None)
    if busiest and busiest["parts"]:
        facts.append(_fact("period.busiest", f"Busiest {p['bucket']}", busiest["start"], M,
                           source="production_records", window=window))
        facts.append(_fact("period.busiest_parts", "Parts made in it", busiest["parts"], M,
                           "parts", "production_records", window))

    if materials:
        facts.append(_fact("period.kg", "Material consumed",
                           round(sum(x["kg"] for x in materials), 3), D, "kg",
                           "parts x each spec's part weight", window,
                           detail="covers only output with a part spec" if unassigned
                                  else "every part made had a spec"))
        for i, row in enumerate(materials[:3], 1):
            facts.append(_fact(f"period.material_{i}", f"Material {i} by weight", row["material"],
                               M, source="part_specs", window=window))
            facts.append(_fact(f"period.material_{i}.kg", f"{row['material']} consumed", row["kg"],
                               D, "kg", "parts x that spec's part weight", window))
    else:
        facts.append(_fact("period.kg", "Material consumed", None, U, "kg", "part_specs", window,
                           detail="no output in the window had a part spec, so no weight is "
                                  "derivable (not 0 kg)"))

    # What came IN. Reported in each item's own unit, and totalled only across
    # the ones that are weights -- see rm_added in ai/plant_board.py.
    in_kg = [r for r in received if r["kg"] is not None]
    if in_kg:
        facts.append(_fact("period.received_kg", "Material received",
                           round(sum(r["kg"] for r in in_kg), 3), M, "kg",
                           "accepted goods receipts", window,
                           detail="counts only receipts booked in kilograms"
                                  if len(in_kg) != len(received) else "every receipt was in kilograms"))
    else:
        facts.append(_fact("period.received_kg", "Material received", None, U, "kg",
                           "accepted goods receipts", window,
                           detail="nothing received in the window was booked in kilograms, so "
                                  "there is no weight to report (not 0 kg)" if received
                                  else "no goods receipt was accepted in the window"))
    for i, r in enumerate([x for x in received if x["kg"] is None][:2], 1):
        # A receipt in bags or drums is a COUNT. It is named, never weighed.
        facts.append(_fact(f"period.received_other_{i}", f"{r['material']} received",
                           r["quantity"], M, r["unit"], "accepted goods receipts", window,
                           detail="a count in its own unit; it is not a weight and is not in "
                                  "the kilogram total"))

    if machines:
        facts.append(_fact("period.revenue", "Value of the output", p["total_revenue"], D, CURRENCY,
                           "parts x each spec's price per piece", window))
        facts.append(_fact("period.rate", "Shift-hour rate for the plant", p["total_rate_per_hour"],
                           D, CURRENCY, "revenue over the hours in the window", window,
                           detail=f"{p['hours']:g} hours in this window; the same revenue reads "
                                  f"differently over a longer one"))
        top = machines[0]
        facts.append(_fact("period.top_machine", "Machine earning the most", top["machine"], M,
                           source="production_records", window=window))
        facts.append(_fact("period.top_machine_rate", f"{top['machine']} shift-hour rate",
                           top["rate_per_hour"], D, CURRENCY, "its revenue over those hours", window))
    else:
        facts.append(_fact("period.revenue", "Value of the output", None, U, CURRENCY,
                           "part_specs", window,
                           detail="no machine made output against a priced part spec, so the "
                                  "window has no money value here (not zero)"))
        facts.append(_fact("period.rate", "Shift-hour rate for the plant", None, U, CURRENCY,
                           "part_specs", window, detail=_NO_PRICE))

    facts += _unavailable_facts("period", p, window)
    state = ev.NOT_MEASURED if not materials and not machines else ev.PARTIAL_DATA
    return _result("get_plant_board_period", state,
                   _say_period(window, p, parts, unassigned, materials, in_kg, received, machines),
                   facts,
                   notes=["Kilograms and money are conversions through each part's spec; output "
                          "with no spec is counted in the totals and converted in neither.",
                          "Material received is reported in the unit it was booked in; only "
                          "receipts in kilograms are added into a kilogram total.",
                          "Power and packing have no source on this floor and are never zero."])


def _say_period(window, p, parts, unassigned, materials, in_kg, received, machines):
    out = [f"The plant board for {window}: {parts:,} parts made, read by {p['bucket']}."]
    if materials:
        out.append(f"{round(sum(x['kg'] for x in materials), 3):,} kg of material was consumed.")
    else:
        out.append("No output in the window had a part spec, so no material weight is derivable.")
    if in_kg:
        out.append(f"{round(sum(r['kg'] for r in in_kg), 3):,} kg was received.")
    elif received:
        out.append("Nothing received was booked in kilograms, so the weight received is unknown.")
    if machines:
        out.append(f"The plant's shift-hour rate was {unit_rate(p['total_rate_per_hour'])} "
                   f"over {p['hours']:g} hours, with {machines[0]['machine']} earning most.")
    else:
        out.append("No part is priced, so the window has no money rate.")
    if unassigned:
        out.append(f"{unassigned:,} parts were made with no part spec and are counted only.")
    out.append("Power and packing have no source on this floor, so neither is reported.")
    return " ".join(out), BOARD_VIEW


@tool("get_plant_power",
      "Whether AMP can report power or packed quantities at all. It cannot: no "
      "energy meter is fitted on this floor and nothing records packing, so this "
      "says so and what fitting a source would take. Use for power, energy, kWh, "
      "electricity and packed-quantity questions, which have no measured answer.",
      mirrors="/analytics/plant-board", view=BOARD_VIEW, domain="production")
def get_plant_power(db, tenant):
    from ai import plant_board            # lazy: the board pulls in the models layer
    # Read through the builder rather than restating the reason in a second
    # place: the board owns why these two series cannot be drawn, and a copy here
    # is exactly the drift ADR-0014 exists to stop.
    board = plant_board.day(db, tenant, datetime.utcnow().date())
    power, packing = board["power"], board["packing"]
    facts = _unavailable_facts("board", board, "now")
    said = (f"AMP cannot report power consumed: {power['reason']} Nor packed quantities: "
            f"{packing['reason']} Neither is reported as zero, because no meter and no consumption "
            f"are different facts. To measure power: {power['fix']}")
    return _result("get_plant_power", ev.NOT_MEASURED, (said, BOARD_VIEW), facts,
                   notes=["Nothing is estimated in place of a missing meter: an inferred kWh figure "
                          "would read exactly like a measured one."])
