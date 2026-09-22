"""The Factory Command Centre: an owner's first five minutes (ADR-0024).

One read-model answers the five questions an owner actually opens AMP with:

  1. WHERE ARE WE?        plant OEE with its coverage, output against the plan,
                          what the machines are doing right now
  2. WHAT IS WRONG?       the problems, ranked by MEASURED IMPACT rather than by
                          severity words: the biggest loss first
  3. WHY?                 for each problem, the measured contributors behind it
  4. WHAT IS IT COSTING?  money only when the company set a unit value; good
                          units not made otherwise. Never a made-up rate
  5. WHAT DO I DO?        the actions waiting, who decides them, and what AMP
                          suggests next

It composes the read-models that already exist (OEE, schedule adherence,
production, downtime, quality, cost, inventory, delivery, maintenance, agent
actions) and computes no new figure of its own. Every number it shows carries a
`Fact` with its provenance (ai/evidence.py), so the card and the Copilot say the
same things in the same words, from the same source.

RANKING IS THE POINT. "3 items at reorder level" and "225 minutes of downtime"
are not comparable as severities, and the old briefing ranked them by a
hand-assigned word. Here every problem is converted to the SAME measure the cost
model uses -- good units not made -- and to money when, and only when, the
company has given AMP a unit value. A problem AMP cannot size is still listed,
with its size stated as unknown, never as zero.

Two exceptions to "biggest first", both deliberate:
  * a machine stopped RIGHT NOW leads whatever it has cost so far, because the
    card's first job is to say what is happening;
  * the figures are NEVER totalled. A missed plan and the downtime on that
    plan's machine describe the same lost output from two sides, so the card
    ranks them, links them under "why", and says so in `overlap_note`.

COST. One read-model call, not a poll: composing nine read-models costs roughly
what the briefing and the OEE summary cost together (measured in
test_command_centre.py, which fails if it grows past its recorded budget).
"""
from datetime import datetime

import models
import oee_contract
from ai import evidence as ev
from ai.cost import build_cost_summary
from ai.delivery import build_delivery_summary
from ai.downtime import build_downtime_summary
from ai.flow import build_wip_aging
from ai.inventory import build_inventory_summary
from ai.maintenance import build_maintenance_summary
from ai.oee import build_oee_summary
from ai.production import build_production_summary
from ai.quality import build_quality_summary
from ai.schedule import build_schedule_adherence
from currency import CURRENCY
from machine_status import DOWN_STATUSES, RUNNING

name = "command_centre"

M, D, R, U = ev.MEASURED, ev.DERIVED, ev.RULE, ev.UNKNOWN
W7 = "last 7 days"
TOP_PROBLEMS = 5


def _fact(key, label, value, prov, unit="", source="", window=W7, detail=""):
    return ev.Fact(key=key, label=label, value=value, provenance=prov, unit=unit, source=source,
                   window=window, detail=detail).to_dict(key)


def _money(units, unit_value):
    """Money for a quantity of good units, or None when the company set no rate.
    The ONE place this card turns units into money (ADR-0010)."""
    if unit_value is None or units is None:
        return None
    return round(units * unit_value)


def _problem(key, title, detail, module, view, units, unit_value, facts, why=None, state=ev.OK,
             rank_basis=None):
    """One ranked problem. `units` is good units not made, as far as AMP can
    measure it, and None when it cannot: unknown is stated, never zeroed.
    `rank_basis` says why it sits where it sits."""
    return {"key": key, "title": title, "detail": detail, "module": module, "view": view,
            "impact_units": units, "impact_money": _money(units, unit_value),
            "currency": CURRENCY if unit_value is not None else None,
            "rank_basis": rank_basis or ("measured loss" if units is not None else "not measured"),
            "state": state, "facts": facts, "why": why or []}


def _position(db, tenant, oee, plan, prod, machines, health):
    running = sum(1 for m in machines if (m.status or "") == RUNNING)
    down = sorted(m.name for m in machines if (m.status or "") in DOWN_STATUSES)
    facts = [
        _fact("machines.total", "Machines", len(machines), M, "machines", "machines", "now"),
        _fact("machines.running", "Running now", running, M, "machines", "machines", "now"),
        _fact("machines.down", "Down now", len(down), M, "machines", "machines", "now",
              detail=", ".join(down)),
    ]
    # MACHINE HEALTH, which this card did not show at all. The 0-100 score, its
    # band and the eleven-rule explanation behind it already existed on
    # /machine-health; the owner's home screen counted running-vs-total and
    # nothing else, so somebody who had seen the Machine Health page would ask
    # why the main screen had forgotten it. Same figure, one definition
    # (twin.fleet_health) — a second average is how two screens start
    # disagreeing about one plant.
    if health is None:
        pass                      # the caller did not ask for it (with_health=False)
    elif health["avg_health"] is not None:
        facts.append(_fact("health.fleet", "Fleet health", health["avg_health"], R, "/100",
                           "rule-based risk points (predictive_engine)", "now",
                           detail=(f"averaged over the {health['measured']} of {health['machines']} "
                                   f"machines whose score read something; a machine with nothing "
                                   f"recorded scores 100 by absence and is left out")))
    else:
        facts.append(_fact("health.fleet", "Fleet health", None, U, "/100",
                           "rule-based risk points (predictive_engine)", "now",
                           detail="no machine has a reading in the risk window, so there is no "
                                  "fleet average (not 0 — 0 is the worst score there is)"))
    if health and health["worst"] and health["worst"]["health_measured"]:
        facts.append(_fact("health.worst", "Lowest health", health["worst"]["name"], M,
                           source="machines", window="now",
                           detail=f"{health['worst']['health_score']}/100 "
                                  f"({health['worst']['health_band']})"))
    coverage = oee["coverage"]
    if oee["plant"]["has_data"]:
        facts.append(_fact("oee.plant", "Plant OEE", oee["plant"]["oee"], D, "%", "production_records",
                           detail=oee_contract.coverage_phrase(coverage) or "every machine reported"))
    else:
        facts.append(_fact("oee.plant", "Plant OEE", None, U, "%", "production_records",
                           detail="no production recorded in the window, so no OEE (not 0%)"))
    if prod["runs"]:
        facts.append(_fact("production.good", "Good units made", prod["good"], M, "units", "production_records"))
    if plan["total"] and plan["planned_units"]:
        facts.append(_fact("plan.attainment", "Against plan (plans due)", plan["attainment_rate"], D, "%",
                           "production_plans"))
        facts.append(_fact("plan.planned_units", "Planned units (plans due)", plan["planned_units"], M, "units",
                           "production_plans"))
        facts.append(_fact("plan.actual_units", "Made units (plans due)", plan["actual_units"], M, "units",
                           "production_plans"))
        plan_state = ev.OK
    elif plan["total"]:
        plan_state = ev.INSUFFICIENT_HISTORY
    else:
        plan_state = ev.NOT_CONFIGURED
    if not oee["plant"]["has_data"]:
        state = ev.NO_DATA
    elif not coverage.get("complete"):
        state = ev.PARTIAL_DATA
    else:
        state = ev.OK
    return {
        "state": state,
        "oee": oee["plant"]["oee"] if oee["plant"]["has_data"] else None,
        "oee_coverage": coverage,
        "coverage_phrase": oee_contract.coverage_phrase(coverage),
        "machines": {"total": len(machines), "running": running, "down": len(down), "down_names": down,
                     "maintenance": sum(1 for m in machines if (m.status or "") == "Maintenance"),
                     "idle": sum(1 for m in machines if (m.status or "") == "Idle")},
        "health": health,
        "output": {"good": prod["good"], "total": prod["total"], "good_rate": prod["good_rate"],
                   "runs": prod["runs"], "days": prod["days"]},
        "plan": {"state": plan_state, "planned_units": plan["planned_units"], "actual_units": plan["actual_units"],
                 "attainment_rate": plan["attainment_rate"] if plan["planned_units"] else None,
                 "behind": plan["behind"], "missed": plan["missed"]},
        "facts": facts,
    }


def _downtime_problems(downtime, cost, unit_value):
    """The top downtime causes, sized in the units the run rate says they cost."""
    out = []
    rate = cost.get("run_rate")   # good units per minute of run time, or None
    for i, r in enumerate(downtime["top_reasons"][:3], 1):
        units = round(r["minutes"] * rate) if rate else None
        machines = [m["name"] for m in downtime["by_machine"][:3]]
        facts = [
            _fact(f"downtime.{i}.minutes", f"{r['reason']}: minutes lost", r["minutes"], M, "min", "downtime_logs"),
            _fact(f"downtime.{i}.events", f"{r['reason']}: stoppages", r["count"], M, "events", "downtime_logs"),
        ]
        if units is None:
            facts.append(_fact(f"downtime.{i}.units", f"{r['reason']}: good units not made", None, U, "units",
                               "production_records", detail="no run time in the window to convert minutes into units"))
        else:
            facts.append(_fact(f"downtime.{i}.units", f"{r['reason']}: good units not made", units, D, "units",
                               "downtime minutes at the plant's measured run rate"))
        out.append(_problem(
            key=f"downtime.{r['reason'].lower().replace(' ', '_')}",
            title=f"{r['reason']} stopped production {r['count']} time{'' if r['count'] == 1 else 's'}",
            detail=f"{r['minutes']:,} minutes lost in the last {downtime['days']} days"
                   + (f"; most on {machines[0]}" if machines else ""),
            module="downtime", view="downtime", units=units, unit_value=unit_value, facts=facts,
            state=ev.OK if units is not None else ev.NOT_MEASURED))
    return out


def _plan_problems(plan, downtime, unit_value):
    """Plans that came due and did not deliver, with the downtime measured on the
    same machine named as a LIKELY CONTRIBUTOR -- never as a confirmed cause."""
    out = []
    minutes_by_machine = {m["name"]: m["minutes"] for m in downtime["by_machine"]}
    for c in plan["chase"][:2]:
        facts = [
            _fact(f"plan.{c['plan_no']}.shortfall", f"{c['plan_no']}: units short", c["shortfall"], D, "units",
                  "production_plans"),
            _fact(f"plan.{c['plan_no']}.planned", f"{c['plan_no']}: planned", c["planned_quantity"], M, "units",
                  "production_plans"),
            _fact(f"plan.{c['plan_no']}.actual", f"{c['plan_no']}: made", c["actual_quantity"], M, "units",
                  "production_plans"),
        ]
        why = []
        minutes = minutes_by_machine.get(c["machine"])
        if minutes:
            why.append({"label": ev.LIKELY_CONTRIBUTOR,
                        "text": f"{c['machine']} lost {minutes:,} minutes to downtime in the same window",
                        "facts": [_fact(f"plan.{c['plan_no']}.machine_downtime",
                                        f"{c['machine']}: minutes lost", minutes, M, "min", "downtime_logs")]})
        else:
            why.append({"label": ev.INSUFFICIENT_EVIDENCE,
                        "text": f"No downtime was logged on {c['machine']} in this window, so AMP cannot say "
                                "from this data why the plan was missed",
                        "facts": []})
        out.append(_problem(
            key=f"plan.{c['plan_no']}",
            title=f"{c['plan_no']} on {c['machine']} is {c['state']}",
            detail=f"{c['actual_quantity']:,} of {c['planned_quantity']:,} planned units"
                   + (f", {c['days_ago']} day{'' if c['days_ago'] == 1 else 's'} ago" if c.get("days_ago") else ""),
            module="planning", view="planning", units=c["shortfall"], unit_value=unit_value, facts=facts, why=why))
    return out


def _quality_problem(quality, unit_value):
    # `measured` as well as `failed`: a fail rate of None cannot be shipped as a
    # fact (ev.Fact refuses a missing value that is not labelled UNKNOWN), and a
    # problem card with no rate on it has nothing to say.
    if not quality.get("measured") or not quality["failed"]:
        return None
    worst = quality["by_machine"][0] if quality["by_machine"] else None
    defect = quality["top_defects"][0]["category"] if quality["top_defects"] else None
    facts = [
        _fact("quality.failed", "Units failed inspection", quality["failed"], M, "units", "quality_inspections"),
        _fact("quality.fail_rate", "Fail rate", quality["fail_rate"], D, "%", "quality_inspections"),
    ]
    if defect:
        facts.append(_fact("quality.top_defect", "Top defect", defect, M, source="quality_inspections"))
    return _problem(
        key="quality.failures",
        title=f"{quality['failed']:,} units failed inspection" + (f" ({defect})" if defect else ""),
        detail=f"fail rate {quality['fail_rate']}%" + (f"; worst on {worst['name']}" if worst else ""),
        module="quality", view="quality", units=quality["failed"], unit_value=unit_value, facts=facts)


def _stock_problem(stock):
    if not stock["out_of_stock"] and not stock["at_risk"]:
        return None
    lead = stock["items"][0]["item_name"] if stock["items"] else ""
    facts = [
        _fact("stock.out", "Items out of stock", stock["out_of_stock"], M, "items", "inventory_items", "now"),
        _fact("stock.at_risk", "Items at or below reorder level", stock["at_risk"], R, "items", "inventory_items",
              "now"),
    ]
    return _problem(
        key="stock.shortage",
        title=(f"{stock['out_of_stock']} item{'' if stock['out_of_stock'] == 1 else 's'} out of stock"
               if stock["out_of_stock"] else
               f"{stock['at_risk']} item{'' if stock['at_risk'] == 1 else 's'} at reorder level"),
        detail=(f"{lead} first" if lead else "") + (f"; {stock['auto_pos_pending']} draft PO(s) waiting"
                                                    if stock["auto_pos_pending"] else ""),
        module="inventory", view="inventory", units=None, unit_value=None, facts=facts,
        state=ev.NOT_MEASURED)


def _delivery_problem(delivery):
    if not delivery["late"]:
        return None
    worst = delivery["at_risk_orders"][0] if delivery["at_risk_orders"] else None
    facts = [
        _fact("orders.late", "Late orders", delivery["late"], D, "orders", "customer_orders", "now"),
        _fact("orders.units_at_risk", "Undelivered units on late or at-risk orders", delivery["units_at_risk"], D,
              "units", "customer_orders", "now"),
    ]
    return _problem(
        key="orders.late",
        title=f"{delivery['late']} customer order{'' if delivery['late'] == 1 else 's'} past due",
        detail=(f"chase {worst['order_no']} ({worst['customer']}) first" if worst else ""),
        module="orders", view="orders", units=None, unit_value=None, facts=facts, state=ev.NOT_MEASURED)


def _maintenance_problem(maint):
    if not maint["overdue"]:
        return None
    facts = [_fact("maint.overdue", "Maintenance tasks overdue", maint["overdue"], D, "tasks",
                   "maintenance_tasks", "now")]
    nxt = maint["tasks"][0] if maint["tasks"] else None
    return _problem(
        key="maintenance.overdue",
        title=f"{maint['overdue']} maintenance task{'' if maint['overdue'] == 1 else 's'} overdue",
        detail=(f"next: {nxt['task_type']} on {nxt['machine']}" if nxt else ""),
        module="cmms", view="cmms", units=None, unit_value=None, facts=facts, state=ev.NOT_MEASURED)


def _work_order_problem(flow, unit_value):
    """Open work orders that have blown their planned end date.

    WORK ORDERS REACHED NO OWNER SURFACE AT ALL. Neither this card nor the Risk
    Radar read `models.WorkOrder`: the plan rows come from ProductionPlan and
    the delivery rows from CustomerOrder, so "which jobs are late?" — the
    question a job shop asks first — was answerable only by opening the Work
    Orders tab and reading the list.

    Sized in the units still to make on those orders, which is the same
    good-units currency every other problem on this card is ranked in. An order
    with no target quantity contributes nothing to the size rather than a
    guess, and if none of them carry one the problem is reported UNSIZED rather
    than as zero — `_rank` then puts it in the unsized tail, which is the
    honest place for it.
    """
    late = [w for w in (flow.get("chase") or []) if w.get("late")]
    if not late:
        return None
    remaining = sum(max(0, (w.get("target_quantity") or 0) - (w.get("actual_quantity") or 0))
                    for w in late if w.get("target_quantity"))
    oldest = max(late, key=lambda w: w.get("age_days") or 0)
    facts = [
        _fact("flow.late_orders", "Open work orders past their planned end", len(late), D, "orders",
              "work_orders", "now"),
        _fact("flow.oldest_late", "Oldest of them", oldest["work_order_no"], M, source="work_orders",
              window="now", detail=f"{oldest.get('age_days')} days open"),
    ]
    if remaining:
        facts.append(_fact("flow.late_units", "Units still to make on them", remaining, D, "units",
                           "work_orders", "now"))
    # `undated` is stated because it changes what the count MEANS: an order with
    # no planned_end is not on time, it is unjudgeable, and saying "3 late" while
    # silently ignoring 9 undated ones would be a different claim.
    undated = flow.get("undated") or 0
    if undated:
        facts.append(_fact("flow.undated", "Open orders with no planned end date", undated, M, "orders",
                           "work_orders", "now",
                           detail="these cannot be judged late either way"))
    return _problem(
        key="flow.late",
        title=f"{len(late)} work order{'' if len(late) == 1 else 's'} past its planned end",
        detail=(f"oldest {oldest['work_order_no']}, open {oldest.get('age_days')} days"
                + (f"; {remaining:,} units still to make" if remaining else "")),
        module="operations", view="workorders",
        units=remaining or None, unit_value=unit_value, facts=facts,
        state=ev.OK if remaining else ev.NOT_MEASURED)


def _machines_down_problem(machines):
    down = sorted(m.name for m in machines if (m.status or "") in DOWN_STATUSES)
    if not down:
        return None
    return _problem(
        key="machines.down",
        title=f"{len(down)} machine{'' if len(down) == 1 else 's'} down right now",
        detail=", ".join(down), module="machines", view="machines", units=None, unit_value=None,
        facts=[_fact("machines.down_now", "Machines down", len(down), M, "machines", "machines", "now",
                     detail=", ".join(down))],
        state=ev.NOT_MEASURED, rank_basis="stopped now")


def _rank(problems):
    """A machine stopped RIGHT NOW leads, whatever it has cost so far: the card's
    first job is what is happening. Then the biggest measured loss, then the
    problems AMP cannot size, which say so rather than reading as zero.

    The first version ranked purely by measured impact, and a hard-down machine --
    which has no weekly loss attached to it -- fell off the bottom of a
    five-problem card on a factory with five sized losses. That is the one problem
    an owner must never have to scroll for (test_command_centre.py).

    IMPACTS ARE NOT ADDITIVE. A missed plan and the downtime on that plan's machine
    describe the same lost output from two sides, so the card ranks them and never
    totals them; `overlap_note` says so wherever they are shown.
    """
    live = [p for p in problems if p["rank_basis"] == "stopped now"]
    rest = [p for p in problems if p["rank_basis"] != "stopped now"]
    sized = [p for p in rest if p["impact_units"] is not None]
    unsized = [p for p in rest if p["impact_units"] is None]
    sized.sort(key=lambda p: (-p["impact_units"], p["key"]))
    return live + sized + unsized


def _actions(db, tenant, problems, cost):
    """What to do next: the decisions already waiting, then AMP's suggestion for
    the biggest problem. Nothing here acts; approving is a person's job
    (ADR-0015), and the card says who."""
    pending = (db.query(models.AgentAction)
               .filter(models.AgentAction.tenant_code == tenant, models.AgentAction.status == "Proposed")
               .order_by(models.AgentAction.created_at.desc()).limit(5).all())
    out = [{"key": f"agent_action.{a.id}", "title": a.summary or f"{a.agent} action",
            "detail": f"proposed by the {a.agent} agent", "who": "An Admin or Supervisor approves it",
            "module": "agentactivity", "view": "agentactivity", "state": "awaiting approval",
            "ref": {"kind": a.ref_kind, "id": a.ref_id}} for a in pending]
    if problems:
        top = problems[0]
        out.append({"key": "next_best", "title": f"Deal with: {top['title']}",
                    "detail": top["detail"], "who": "The team that owns " + top["module"],
                    "module": top["module"], "view": top["view"], "state": "suggested", "ref": None})
    if cost["has_data"] and not cost["priced"]:
        out.append({"key": "set_unit_value", "title": "Set a unit value so losses can be shown in money",
                    "detail": "AMP shows losses in good units until a value per good unit is configured",
                    "who": "An Admin sets it under Costing", "module": "costing", "view": "costing",
                    "state": "suggested", "ref": None})
    return out


def build_command_centre(db, tenant: str, now=None, with_health=True) -> dict:
    """The owner's five answers, composed from the read-models. Tenant-scoped by
    the ORM hook (ADR-0002); agent actions are filtered explicitly.

    `with_health=False` skips the fleet-health block and the fleet queries
    behind it. ONE caller passes it: `ai.brief`, which composes this card for
    its PROBLEMS and never reads `position.health` — it was paying six queries
    for a block it does not render. The brief's own recorded query budget is
    what caught that, exactly as its comment says it should ("a change that
    adds a query per machine should be seen in review"), and the honest answer
    to the review is that one surface should not pay for another's feature.
    """
    oee = build_oee_summary(db, tenant)
    plan = build_schedule_adherence(db, tenant)
    prod = build_production_summary(db, tenant)
    downtime = build_downtime_summary(db, tenant)
    quality = build_quality_summary(db, tenant)
    cost = build_cost_summary(db, tenant)
    stock = build_inventory_summary(db, tenant)
    delivery = build_delivery_summary(db, tenant)
    maint = build_maintenance_summary(db, tenant)
    # Work orders reached NO owner surface before this: neither this card nor
    # the Risk Radar read models.WorkOrder, so "which jobs are late?" was
    # answerable only by opening the Work Orders tab.
    flow = build_wip_aging(db, tenant)
    machines = db.query(models.Machine).filter(models.Machine.tenant_code == tenant).all()
    unit_value = cost["unit_value_gbp"] if cost["priced"] else None

    # One definition of the fleet figure, shared with ai/pulse (twin.fleet_health).
    if with_health:
        from ai.twin import build_twins, fleet_health   # lazy: twin composes the pillars
        health = fleet_health(build_twins(db, tenant))
    else:
        health = None
    position = _position(db, tenant, oee, plan, prod, machines, health)
    problems = _rank([p for p in (
        [_machines_down_problem(machines), _work_order_problem(flow, unit_value)]
        + _downtime_problems(downtime, cost, unit_value)
        + _plan_problems(plan, downtime, unit_value)
        + [_quality_problem(quality, unit_value), _stock_problem(stock),
           _delivery_problem(delivery), _maintenance_problem(maint)]
    ) if p])[:TOP_PROBLEMS]

    money_state = ev.OK if cost["priced"] else ev.NOT_CONFIGURED
    cost_facts = [
        _fact("losses.downtime_minutes", "Downtime minutes", cost["downtime_minutes"], M, "min", "downtime_logs"),
        _fact("losses.rejected_units", "Scrapped units", cost["rejected_units"], M, "units", "production_records"),
    ]
    if cost["lost_units"] is not None:
        cost_facts.append(_fact("losses.lost_units", "Good units not made", cost["lost_units"], D, "units",
                                "downtime at the run rate, plus scrap"))
    if cost["priced"]:
        cost_facts.append(_fact("losses.cost", "Cost of losses", cost["loss_cost"], D, CURRENCY,
                                "cost model (ADR-0010)"))
    else:
        cost_facts.append(_fact("losses.cost", "Cost of losses in money", None, U, CURRENCY,
                                "tenant configuration",
                                detail="no unit value is set, so AMP will not put a money figure on it"))

    return {
        "generated_at": (now or datetime.utcnow()).isoformat(),
        "days": prod["days"],
        "state": position["state"],
        "headline": _headline(position, problems, cost),
        "position": position,
        "problems": problems,
        # Said once, on the card, because two problems can describe the same lost
        # output from different sides (a missed plan and the downtime on its machine).
        "overlap_note": ("Each problem is sized on its own. Two problems can describe the same lost output, "
                         "so these figures are not a total."),
        "cost": {"state": money_state, "priced": cost["priced"], "currency": CURRENCY if cost["priced"] else None,
                 "unit_value": cost["unit_value_gbp"], "loss_cost": cost["loss_cost"] if cost["priced"] else None,
                 "lost_units": cost["lost_units"], "downtime_minutes": cost["downtime_minutes"],
                 "rejected_units": cost["rejected_units"], "facts": cost_facts},
        "actions": _actions(db, tenant, problems, cost),
    }


def _headline(position, problems, cost) -> str:
    """One sentence: where the plant is, and the biggest measured loss."""
    if position["oee"] is None:
        head = "No production recorded in the window, so there is no OEE to report"
    else:
        covered = position["coverage_phrase"]
        head = f"Plant OEE {position['oee']}%" + (f", measured {covered}" if covered else "")
    p = position["plan"]
    if p["state"] == ev.OK and p["attainment_rate"] is not None:
        head += f"; {p['attainment_rate']}% of the plan due was made"
    elif p["state"] == ev.NOT_CONFIGURED:
        head += "; no production plan is set to compare against"
    if not problems:
        return head + ". Nothing needs attention right now."
    down = position["machines"]["down"]
    if down:
        head += (f"; {down} machine{'' if down == 1 else 's'} down right now "
                 f"({', '.join(position['machines']['down_names'])})")
    top = next((p for p in problems if p["impact_units"] is not None), None)
    if top is None:
        return head + f". {problems[0]['title']}."
    size = (f" ({CURRENCY}{top['impact_money']:,})" if top["impact_money"] is not None
            else f" ({top['impact_units']:,} good units)")
    return head + f". Biggest measured loss: {top['title']}{size}."
