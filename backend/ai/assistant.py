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
from ai.compliance import build_compliance_summary
from ai.inventory import build_inventory_summary
from ai.production import build_production_summary
from ai.flow import build_flow_summary
from ai.shift import build_shift_summary
from ai.briefing import build_briefing
from ai.scorecard import build_scorecard
from currency import CURRENCY, money

name = "assistant"

# Re-exported, not redefined: this module used to carry its own identical copy
# alongside ai/briefing.py's, with nothing keeping the two equal. The name stays
# for every existing reference; the VALUE now has one home (machine_status).
from machine_status import DOWN_STATUSES


def _drills_into(view: str):
    """Declare the view a pillar's answer drills into, WITHOUT executing it.

    Every pillar already returns `(text, view)`, and the view half is a constant
    per pillar. Reading it off the function lets `route_view()` answer "which
    screen owns this question?" with ZERO queries -- which is the whole point.

    Measured, on a 60-machine factory with seven populated tables: running a
    pillar just to learn its view costs 2-6 queries for most routes and **22 for
    `_briefing`**, which is 116% of the queries `/ai/ask` spends building its
    model context. `_briefing` is also the FALLBACK route, so the naive version
    of this -- call `answer()` and keep only its view -- would roughly double the
    database work on exactly the free-form questions people type once an LLM is
    connected.

    This is a second place the view is written, and second places drift. It
    cannot drift silently: `test_copilot_drill_in.py` executes every routable
    pillar against a seeded factory and asserts the declaration equals what the
    function actually returned. Change one without the other and CI fails.
    """
    def deco(fn):
        fn.view = view
        return fn
    return deco


@_drills_into("inventory")
def _inventory(db, tenant):
    inv = build_inventory_summary(db, tenant)
    if inv["at_risk"] == 0:
        return "Stock is healthy — nothing is at or below its reorder level right now.", "inventory"
    lead = inv["items"][0]["item_name"] if inv["items"] else "the top item"
    oos = f" {inv['out_of_stock']} are already out of stock." if inv["out_of_stock"] else ""
    pos = f" The Reorder agent has {inv['auto_pos_pending']} draft PO(s) waiting." if inv["auto_pos_pending"] else ""
    return f"{inv['at_risk']} item(s) at or below reorder level — reorder {lead} first.{oos}{pos}", "inventory"


@_drills_into("orders")
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


@_drills_into("costing")
def _cost(db, tenant):
    c = build_cost_summary(db, tenant)
    if not c["has_data"]:
        return "No production this week, so there are no losses to cost.", "costing"
    ans = (f"Losses cost about {money(c['loss_cost'])} this week — "
           f"downtime {money(c['downtime_cost'])}, scrap {money(c['scrap_cost'])}.")
    if c["by_machine"]:
        w = c["by_machine"][0]
        ans += f" Costliest machine: {w['name']} ({money(w['cost'])})."
    return ans, "costing"


@_drills_into("quality")
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


@_drills_into("cmms")
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


@_drills_into("documents")
def _compliance(db, tenant):
    c = build_compliance_summary(db, tenant)
    if c["total"] == 0:
        return "No controlled documents on file.", "documents"
    ans = f"{c['total']} controlled documents"
    if c["overdue"]:
        ans += f", {c['overdue']} review(s) overdue"
    if c["due_soon"]:
        ans += f", {c['due_soon']} due soon"
    if c["pending_approval"]:
        ans += f", {c['pending_approval']} unapproved"
    ans += "."
    if c["documents"] and c["documents"][0]["overdue"]:
        ans += f" Review {c['documents'][0]['title']} first."
    return ans, "documents"


@_drills_into("downtime")
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


@_drills_into("machines")
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


@_drills_into("analytics")
def _production(db, tenant):
    p = build_production_summary(db, tenant)
    if p["runs"] == 0:
        return "No production runs recorded in the last 7 days.", "analytics"
    ans = f"{p['good']:,} good units of {p['total']:,} ({p['good_rate']}% good) over {p['runs']} runs this week."
    if p["by_machine"]:
        ans += f" Top producer: {p['by_machine'][0]['name']}."
    return ans, "analytics"


@_drills_into("workorders")
def _flow(db, tenant):
    f = build_flow_summary(db, tenant)
    if f["total"] == 0:
        return "No work orders on the floor right now.", "workorders"
    stages = ", ".join(f"{s['label']} {s['count']}" for s in f["stages"])
    return (f"{f['wip']} work orders in progress, {f['finished']} finished "
            f"({f['total']} total). Pipeline: {stages}."), "workorders"


@_drills_into("shifts")
def _shift(db, tenant):
    sh = build_shift_summary(db, tenant)
    if sh["entries"] == 0:
        return "No shift data recorded yet.", "shifts"
    if sh["attainment"] is None:
        return (f"{sh['actual']:,} units produced across shifts over the last "
                f"{sh['days']} days, but no shift had a target set to measure "
                "attainment against."), "shifts"
    ans = (f"Shift attainment is {sh['attainment']}% ({sh['actual']:,} of "
           f"{sh['target']:,} target) over the last {sh['days']} days.")
    if sh.get("best"):
        ans += f" Best: {sh['best']['shift']} at {sh['best']['attainment']}%."
    if sh.get("worst") and sh["worst"] is not sh.get("best"):
        ans += f" Worst: {sh['worst']['shift']} at {sh['worst']['attainment']}%."
    return ans, "shifts"


@_drills_into("executive")
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


_FIND_PREFIXES = ("find ", "where is ", "where's ", "locate ", "look up ", "lookup ", "search for ", "search ")


def _find(db, tenant, question):
    """'find CO-5001' / 'where is the reflow SOP' — strip the find-phrase and run
    the global entity search, phrasing the top hits with where to open them."""
    from ai.search import build_search  # lazy: avoids widening import chains

    q = (question or "").strip().lower()
    term = next((q[len(p):] for p in _FIND_PREFIXES if q.startswith(p)), q)
    term = term.strip(" ?.!\"'")
    for noise in ("the ", "my ", "our "):
        if term.startswith(noise):
            term = term[len(noise):]
    hits = build_search(db, tenant, term)["results"]
    if not hits:
        return f"I couldn't find anything matching \"{term}\".", "overview"
    top = hits[0]
    ans = f"Found {top['label']} ({top['type']} — {top['sublabel']})."
    if len(hits) > 1:
        others = ", ".join(f"{h['label']} ({h['type']})" for h in hits[1:4])
        ans += f" Also matched: {others}."
    return ans, top["view"]


@_drills_into("overview")
def _help(db, tenant):
    return (
        "I can answer about OEE & performance, the cost of losses, order delivery, "
        "downtime, quality, maintenance, compliance documents, inventory, machines "
        "(ask by name too), production, WIP, shifts, and week-on-week trends — from "
        "your live data. Say \"find <anything>\" to locate an order, part, task or "
        "document, or \"give me the rundown\" for the whole picture at once.",
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


@_drills_into("executive")
def _trend(db, tenant):
    sc = build_scorecard(db, tenant)
    if not sc["has_data"]:
        return "No production data yet, so there's nothing to compare.", "overview"
    moves = []
    for k in sc["kpis"]:
        if k.get("delta") is None or k["delta"] == 0:
            continue
        arrow = "up" if k["delta"] > 0 else "down"
        mag = money(abs(k["delta"])) if k["unit"] == CURRENCY else f"{abs(k['delta'])}{'' if k['unit'] == '%' else k['unit']}"
        verdict = "better" if k.get("delta_tone") == "good" else "worse"
        moves.append(f"{k['label']} {arrow} {mag} ({verdict})")
    if not moves:
        return "Steady vs last week — no material change in OEE, good rate or cost of losses.", "executive"
    return "Vs last week: " + "; ".join(moves) + ".", "executive"


@_drills_into("overview")
def _briefing(db, tenant):
    b = build_briefing(db, tenant)
    if not b["has_data"]:
        # "No production data" is about OEE, not about the plant. A machine can be
        # hard-down before anything has been produced, and build_briefing now
        # says so; repeating "nothing to report" over the top of an alert it is
        # holding would put the conflation back one layer up.
        if b["alerts"]:
            lead = "; ".join(a["title"] for a in b["alerts"][:3])
            detail = b["alerts"][0].get("detail")
            return (f"No production data yet, but {lead}"
                    + (f" ({detail})." if detail else "."), "overview")
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
    (("reorder", "restock", "stock", "inventory", "out of stock", "replenish",
      "raw material", "material", "run out", "running out"), _inventory),
    (("wip", "work in progress", "work-in-progress", "in progress", "pipeline",
      "work order", "raw ", "semi", "finished good", "waiting",
      "between operations", "half-built", "half built"), _flow),
    (("shift", "attainment", "crew", "night", "day shift", "evening"), _shift),
    (("deliver", "on-time", "on time", " late", "customer", "ship", "fulfil", "bugatti", "mercedes", "order", "dispatch", "behind schedule"), _delivery),
    # "$" stays alongside "£": these are tokens the USER types, not display symbols, and
    # someone asking "what's this costing me in $" should still reach the cost answer.
    (("cost", "money", "losing", "£", "$", "expensive", "spend", "margin",
      "financ", "loss"), _cost),
    (("quality", "defect", "reject", "scrap", "fail", "yield", "fpy", "first-pass", "first pass"), _quality),
    (("compliance", "document", "audit", "iso", "sop", "controlled doc",
      "paperwork", "procedure"), _compliance),
    (("maintenance", "overdue", "servic", "pm ", " task", "due for"), _maintenance),
    (("downtime", "stoppage", "down time", "broke down", "broken down", "stopping", "stopped"), _downtime),
    (("machine", "breakdown", "running", "idle", "offline", " down", "broken"), _machines),
    (("produc", "output", "units", "throughput", "made", "making", "good rate", "build"), _production),
    (("oee", "effective", "efficien", "availability", "performance"), _oee),
    (("attention", "wrong", "problem", "issue", "priorit", "focus", "happening",
      "summary", "summarise", "summarize", "overview", "everything", "status"), _briefing),
]


def digest(db, tenant: str) -> dict:
    """A conversational one-shot rundown of the whole plant — OEE and trend, the
    week's losses, the order book, the most pressing issue and the wins — composed
    from the pillar read-models into a plain-English paragraph."""
    b = build_briefing(db, tenant)
    if not b["has_data"]:
        # Same reasoning as _briefing above: report the alert if one is being held.
        if b["alerts"]:
            a = b["alerts"][0]
            return {"digest": f"No production data yet, but {a['title'].lower()}"
                              + (f" ({a['detail']})." if a.get("detail") else ".")}
        return {"digest": "No production data yet — nothing to report."}
    cost = build_cost_summary(db, tenant)
    delivery = build_delivery_summary(db, tenant)

    lines = [f"Plant OEE is {b['oee']}% and trending {b['oee_trend']}."]
    if cost["has_data"]:
        lines.append(f"Losses have cost about {money(cost['loss_cost'])} this week.")
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


def route_names():
    """The FIXED allowlist of pillar names a model may choose from.

    Derived from _ROUTES, so a pillar added to the table is offerable and a
    pillar removed from it stops being offerable, with no second list to keep in
    sync. "briefing" is included explicitly because it is the fallthrough rather
    than a table entry.

    This is the whole point of the allowlist: the model returns a NAME, and a
    name is all it can return. It cannot name a table, a tenant, a SQL fragment
    or a function that is not here. AMP looks the name up and calls the pillar
    itself, with the tenant IT already holds.
    """
    names = {fn.__name__.lstrip("_") for _keys, fn in _ROUTES}
    names.add("briefing")
    return sorted(names)


def route_view(question: str) -> str:
    """Which view a question drills into, WITHOUT reading the database.

    Exists for `/ai/ask`. When an LLM answers, the prose comes from the model but
    the "Open Machines ->" button under it does not: the view is AMP's call, made
    from AMP's own routing table, so switching the copilot to an LLM cannot move
    a user to a screen the model picked. Before this, that path returned no view
    at all and the button simply vanished the moment a key was configured -- the
    rules fallback beside it kept its own drill-in, so turning AI ON removed a
    feature.

    Deliberately NARROWER than `answer()`: it runs only the keyword table, and
    skips the two DB-touching branches that come first there -- the
    machine-name lookup and the `find ...` prefix. A question naming a machine
    almost always also carries a machines keyword ("down", "running",
    "machine"), so it lands on the same view; when it does not, the answer is
    the briefing's "overview", which is the honest default rather than a wrong
    screen. That trade buys a view for ZERO queries, which is the entire reason
    this exists (see `_drills_into`).

    A pillar with no data returns "overview" instead of its own view (`_trend`
    is the only route where that differs). This cannot know that without running
    the pillar, so it names the pillar's view either way -- an empty screen the
    user can read, rather than a link they never get.
    """
    q = f" {(question or '').lower()} "
    for keys, fn in _ROUTES:
        if any(k in q for k in keys):
            return fn.view
    return _briefing.view


def _pillar(name):
    """The pillar function for an allowlisted name, or None.

    None for anything unrecognised -- including a name that exists in this module
    but is not a routable pillar. `_find` and `_machine_answer` take a third
    argument and are deliberately NOT reachable this way; a model cannot call
    them by guessing.
    """
    # Belt and braces, and worth being honest about: removing this check does
    # NOT open a hole, and a mutation deleting it survives the suite. A dict,
    # list or int simply fails the `fn.__name__ == name` comparison below and
    # falls through to None anyway. It stays because a route arrives as JSON
    # from a model, and stating "this must be a string" at the boundary is
    # cheaper to read than deriving that it cannot matter.
    if not isinstance(name, str):
        return None
    for _keys, fn in _ROUTES:
        if fn.__name__.lstrip("_") == name:
            return fn
    return _briefing if name == "briefing" else None


def answer(db, tenant: str, question: str, chosen_route=None) -> dict:
    """Answer a plant question from the read-models: a sentence plus the view that
    drills into it. Routes by keyword; defaults to 'what needs attention'.

    `chosen_route` is the AI-roadmap phase-3 seam: a model may propose WHICH
    pillar answers a question, and AMP executes it. The safety contract is that
    the model's influence stops at a name:

      * the name is looked up in the fixed allowlist (`route_names()`); anything
        unrecognised is IGNORED and the keyword router runs, so the failure mode
        is today's behaviour rather than an error or an unguarded call;
      * `tenant` comes from this function's caller -- the request's authenticated
        principal -- and never from the model. There is no code path by which a
        proposed route can carry, alter or suggest a tenant;
      * the model never touches the database. It sees a question and a list of
        names; AMP does the reading.

    Default None, so every existing caller behaves exactly as before. Nothing in
    the product passes this yet: routing QUALITY cannot be measured in an
    environment with no AI key, and shipping an unmeasurable behaviour change is
    what test_ai_evaluation.py section 1c exists to warn against. The envelope
    lands first so it is already correct and under CI on the day a key appears.
    """
    if chosen_route is not None:
        fn = _pillar(chosen_route)
        if fn is not None:
            text, view = fn(db, tenant)
            return {"question": question, "answer": text, "view": view,
                    "matched": fn.__name__.lstrip("_"), "route_source": "model"}
        # Unrecognised: fall through to the keyword router below. Deliberately
        # silent to the caller -- an invalid proposal is not an error condition,
        # it is a model being wrong, and the user still gets an answer.
    # A specific machine named in the question wins — answer about that machine.
    named = _machine_named(db, question)
    if named is not None:
        text, view = _machine_answer(db, tenant, named)
        return {"question": question, "answer": text, "view": view, "matched": "machine_detail"}

    # An explicit find/locate phrase runs the global entity search.
    if (question or "").strip().lower().startswith(_FIND_PREFIXES):
        text, view = _find(db, tenant, question)
        return {"question": question, "answer": text, "view": view, "matched": "find"}

    q = f" {(question or '').lower()} "
    # FIRST MATCH WINS, and the order of _ROUTES is load-bearing.
    #
    # This was changed to "the longest matched key wins" — more specific should
    # beat a loose fragment, and machines' " down" really does capture "which
    # asset broke down most this week?". On the questions the change was tuned
    # against it scored 100%. On a HELD-OUT set it went from 38% to 23%, and it
    # was reverted.
    #
    # Table order is not arbitrary: it encodes which pillar should win when two
    # match. "did the late crew hit target?" is a shift question, and delivery's
    # " late" is a longer key than "crew" — specificity picks the wrong one,
    # while the table's ordering picks the right one. Prefer adding a
    # deliberate multi-word key ("raw material", "broke down") over changing how
    # the winner is chosen. See test_ai_evaluation.py sections 1b and 1c.
    for keys, fn in _ROUTES:
        if any(k in q for k in keys):
            text, view = fn(db, tenant)
            return {"question": question, "answer": text, "view": view, "matched": fn.__name__.lstrip("_")}
    text, view = _briefing(db, tenant)
    return {"question": question, "answer": text, "view": view, "matched": "briefing"}
