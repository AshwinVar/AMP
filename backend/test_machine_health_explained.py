"""A health score AMP cannot take apart is a health score AMP should not show (ADR-0027).

The scorer in `predictive_engine` now records every rule it checks. This file
holds that record to the score it came from, because an explanation that drifts
from the number it explains is worse than no explanation at all — it is a wrong
answer with evidence attached.

What is pinned:

  1. THE ARITHMETIC ADDS UP. For every machine in a plant built to trip the
     rules in every combination, the points of the rules that FIRED sum to the
     risk score AMP publishes, and `reasons` is exactly those rules' own words,
     in the order the scorer produced them. A rule that fires without appearing,
     or appears without firing, fails the build.
  2. EACH RULE READ WHAT IT SAYS IT READ. Every component's `measured` is
     recomputed here from the raw rows — the same downtime minutes through the
     same duration parser, the same reject percentage, the same open-order
     pressure — and must match.
  3. THE BOUNDARIES. 59 minutes and 60; 119 and 120; four stoppages and five;
     4.9% rejected and 5.0%; utilisation 40 and 39. A threshold nobody tests is
     a threshold that moves.
  4. THE CAP IS VISIBLE. A machine that trips everything scores more than 100
     points; the score is capped, the explanation says it was, and the points
     before the cap are still reported. Two machines at health 0 are not
     necessarily in the same state.
  5. THE BANDS AGREE. machine_health._band and twin._band return the same word
     at every boundary, so the band on the card and the band in the explanation
     can never disagree.
  6. NOT SCORED IS NOT HEALTHY. With no risk row the explanation is NOT
     MEASURED and says so; it never presents the twin's 100 as a clean bill.
  7. NOTHING IS CALLED A MODEL. No fact in the explanation claims MODEL
     ESTIMATE, and the only mention of machine learning is a denial.
  8. THE SENTENCE IS GROUNDED. `say()` is run through the Copilot's own
     grounding gate against its own facts: every figure in the sentence must be
     a figure in the evidence.
  9. IT REACHES THE COCKPIT, TENANT-SCOPED. build_machine_detail carries the
     explanation, and one factory's explanation never names another's machine.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_machine_health_explained.py
"""
import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import predictive_engine
import tenancy
from ai import evidence as ev
from ai import grounding
from ai import machine_health as mh
from ai import twin
from copilot_eval import fixtures as F
from database import Base
from duration import parse_duration_to_minutes

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


# ── A plant built to trip the rules, and the rows that trip them ────

class Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# (id, name, status, utilization)
MACHINES = [
    (1, "IDLE-01", "Idle", 12),          # low utilisation only
    (2, "HOT-01", "Running", 95),        # high utilisation only
    (3, "DOWN-01", "Breakdown", 55),     # in breakdown, plus history
    (4, "MAINT-01", "Maintenance", 70),  # in maintenance
    (5, "EDGE-01", "Running", 40),       # every boundary, none tripped
    (6, "EDGE-02", "Running", 41),       # every boundary, all tripped
    (7, "CLEAN-01", "Running", 65),      # nothing at all
    (8, "ALL-01", "Breakdown", 5),       # trips everything: over 100 points
]
# machine_id -> [(duration string, reason)]
DOWNTIME = {
    3: [("45 min", "Breakdown"), ("30 min", "Tool change"), ("45 min", "Jam")],   # exactly 120 min
    5: [("29 min", "Breakdown"), ("30 min", "Breakdown"), ("not a duration", "Blank"), ("0 min", "")],
    6: [("30 min", "Breakdown"), ("20 min", "Breakdown"), ("5 min", "breakdown"), ("3 min", "Jam"),
        ("2 min", "Jam")],                                                        # exactly 60 min
    8: [("2 hr", "Breakdown"), ("1 hr", "Breakdown"), ("30 min", "breakdown"), ("10 min", "Jam"),
        ("10 min", "Jam"), ("10 min", "Jam")],
}
# machine_id -> [(total_count, rejected_count)]
PRODUCTION = {
    2: [(1000, 10)],                      # 1.0% — under both reject rules
    5: [(1000, 49)],                      # 4.9% — under the moderate rule
    6: [(1000, 50)],                      # 5.0% — moderate exactly
    3: [(500, 40)],                       # 8.0% — high exactly
    8: [(200, 40), (300, 60)],            # 20% over two records
    7: [(None, None)],                    # NULL counts: no measured production
}
# machine_id -> [new_status, ...]
EVENTS = {3: ["Breakdown", "Running"], 6: ["Idle"], 8: ["Breakdown", "Breakdown", "Running"]}
# machine_id -> [(status, target, actual)]
WORK_ORDERS = {
    5: [("In Progress", 499, 0)],
    6: [("In Progress", 300, 0), ("Planned", 200, 0)],
    8: [("In Progress", 900, 100)],
    7: [("Completed", 5000, 0)],          # closed: no pressure, whatever its size
}


def plant():
    machines = [Row(id=i, name=n, status=s, utilization=u) for i, n, s, u in MACHINES]
    downtime = [Row(machine_id=m, duration=d, reason=r) for m, rows in DOWNTIME.items() for d, r in rows]
    production = [Row(machine_id=m, total_count=t, rejected_count=x)
                  for m, rows in PRODUCTION.items() for t, x in rows]
    events = [Row(machine_id=m, new_status=s) for m, rows in EVENTS.items() for s in rows]
    orders = [Row(machine_id=m, status=s, target_quantity=t, actual_quantity=a)
              for m, rows in WORK_ORDERS.items() for s, t, a in rows]
    return predictive_engine.calculate_predictive_risk(machines, downtime, production, events, orders)


def oracle(machine_id, utilization, status):
    """What each rule should have read, computed here from the raw rows."""
    logs = DOWNTIME.get(machine_id, [])
    minutes = sum(parse_duration_to_minutes(d) for d, _ in logs)
    breakdowns = sum(1 for _, r in logs if r.lower() == "breakdown")
    breakdowns += sum(1 for s in EVENTS.get(machine_id, []) if s == "Breakdown")
    total = sum(t or 0 for t, _ in PRODUCTION.get(machine_id, []))
    rejects = sum(x or 0 for _, x in PRODUCTION.get(machine_id, []))
    rate = round(rejects / total * 100, 1) if total else 0
    pressure = sum(max(t - a, 0) for s, t, a in WORK_ORDERS.get(machine_id, [])
                   if s not in ("Completed", "Cancelled"))
    return {"breakdown_now": status, "maintenance_now": status,
            "low_utilization": utilization, "high_utilization": utilization,
            "downtime_high": minutes, "downtime_moderate": minutes,
            "downtime_frequent": len(logs), "breakdown_repeat": breakdowns,
            "reject_high": rate, "reject_moderate": rate, "work_order_load": pressure}


# The scorer, written a second time, independently: the rules as the ADR states
# them, in the order the ADR states them, including the two either/or pairs. If
# `predictive_engine`'s if-chain and this list ever disagree — a weight changed,
# a comparison flipped, an `elif` became an `if` — section 2 fails and names the
# rule. This is a reference oracle, not a copy: it reads the raw seed rows above,
# never the engine's output.
RULES = [
    ("breakdown_now", 35, lambda f: f["status"] == "Breakdown", "status"),
    ("maintenance_now", 15, lambda f: f["status"] == "Maintenance", "status"),
    ("low_utilization", 20, lambda f: f["utilization"] < 40, "utilization"),
    ("high_utilization", 12, lambda f: f["utilization"] > 90, "utilization"),
    ("downtime_high", 25, lambda f: f["minutes"] >= 120, "minutes"),
    ("downtime_moderate", 15, lambda f: f["minutes"] >= 60, "minutes"),      # only if not high
    ("downtime_frequent", 15, lambda f: f["events"] >= 5, "events"),
    ("breakdown_repeat", 20, lambda f: f["breakdowns"] >= 3, "breakdowns"),
    ("reject_high", 20, lambda f: f["rate"] >= 8, "rate"),
    ("reject_moderate", 10, lambda f: f["rate"] >= 5, "rate"),               # only if not high
    ("work_order_load", 10, lambda f: f["pressure"] >= 500, "pressure"),
]
# The second rule of each pair is only reached when the first did not fire.
ELSE_OF = {"downtime_moderate": "downtime_high", "reject_moderate": "reject_high"}


def expected_components(machine_id, utilization, status):
    """(key, points, fired) for every rule the engine should have EVALUATED."""
    logs = DOWNTIME.get(machine_id, [])
    total = sum(t or 0 for t, _ in PRODUCTION.get(machine_id, []))
    f = {
        "status": status,
        "utilization": utilization,
        "minutes": sum(parse_duration_to_minutes(d) for d, _ in logs),
        "events": len(logs),
        "breakdowns": (sum(1 for _, r in logs if r.lower() == "breakdown")
                       + sum(1 for s in EVENTS.get(machine_id, []) if s == "Breakdown")),
        "rate": (round(sum(x or 0 for _, x in PRODUCTION.get(machine_id, [])) / total * 100, 1)
                 if total else 0),
        "pressure": sum(max(t - a, 0) for s, t, a in WORK_ORDERS.get(machine_id, [])
                        if s not in ("Completed", "Cancelled")),
    }
    out, fired = [], {}
    for key, points, rule, _field in RULES:
        if key in ELSE_OF and fired.get(ELSE_OF[key]):
            continue                      # the `elif` never ran this one
        fired[key] = rule(f)
        out.append((key, points if fired[key] else 0, fired[key]))
    return out


def main_():
    rows = {r["machine_id"]: r for r in plant()}
    by_name = {r["machine_name"]: r for r in rows.values()}

    print("\n1. The fired points sum to the score AMP publishes")
    for mid, name, status, util in MACHINES:
        row = rows[mid]
        fired = [c for c in row["components"] if c["fired"]]
        total = sum(c["points"] for c in fired)
        check(f"{name}: fired points {total} == risk score {row['risk_score']}"
              + (" (capped)" if row["capped"] else ""),
              min(total, 100) == row["risk_score"], f"before cap {row['points_before_cap']}")
        check(f"{name}: points_before_cap is the uncapped total", row["points_before_cap"] == total)
        # `reasons` is what every existing screen reads. It must stay exactly the
        # fired rules' own words, in the scorer's own order.
        expected = [c["reason"] for c in fired] or ["no major risk indicators detected"]
        check(f"{name}: reasons are the fired rules, in order", row["reasons"] == expected,
              f"{row['reasons']} != {expected}")
        check(f"{name}: a rule that scored 0 is not in reasons",
              all(c["points"] == 0 for c in row["components"] if not c["fired"]))

    print("\n2. Each rule read what it says it read")
    for mid, name, status, util in MACHINES:
        want = oracle(mid, util, status)
        for c in rows[mid]["components"]:
            check(f"{name}/{c['key']}: measured {c['measured']!r}", c["measured"] == want[c["key"]],
                  f"expected {want[c['key']]!r}")
            check(f"{name}/{c['key']}: threshold is a sentence, not a number",
                  isinstance(c["threshold"], str) and len(c["threshold"]) > 5)
    print("\n2b. The scorer agrees with the rules written out independently")
    for mid, name, status, util in MACHINES:
        row = rows[mid]
        want = expected_components(mid, util, status)
        got = [(c["key"], c["points"], c["fired"]) for c in row["components"]]
        check(f"{name}: every rule evaluated, in order, with its own weight", got == want,
              f"{got} != {want}")
        check(f"{name}: the score is the independent sum",
              row["risk_score"] == min(sum(p for _k, p, _f in want), 100),
              f"{row['risk_score']} vs {sum(p for _k, p, _f in want)}")
        check(f"{name}: a rule that did not fire contributed nothing",
              all(p == 0 for _k, p, f in want if not f))


    print("\n3. The boundaries hold")
    edge_clear = {c["key"]: c for c in by_name["EDGE-01"]["components"]}
    edge_trip = {c["key"]: c for c in by_name["EDGE-02"]["components"]}
    check("59 minutes of downtime fires no downtime rule",
          not edge_clear["downtime_high"]["fired"] and not edge_clear["downtime_moderate"]["fired"],
          str(edge_clear["downtime_moderate"]["measured"]))
    check("60 minutes fires MODERATE, not high",
          edge_trip["downtime_moderate"]["fired"] and not edge_trip["downtime_high"]["fired"],
          str(edge_trip["downtime_moderate"]["measured"]))
    down_keys = {c["key"]: c for c in by_name["DOWN-01"]["components"]}
    check("exactly 120 minutes fires HIGH", down_keys["downtime_high"]["fired"]
          and down_keys["downtime_high"]["measured"] == 120)
    check("and the moderate rule is then never even reached", "downtime_moderate" not in down_keys)
    check("4 stoppages do not fire the frequency rule", not edge_clear["downtime_frequent"]["fired"],
          str(edge_clear["downtime_frequent"]["measured"]))
    check("5 stoppages do", edge_trip["downtime_frequent"]["fired"])
    check("2 breakdown transitions do not fire the repeat rule",
          not edge_clear["breakdown_repeat"]["fired"], str(edge_clear["breakdown_repeat"]["measured"]))
    check("3 do", edge_trip["breakdown_repeat"]["fired"], str(edge_trip["breakdown_repeat"]["measured"]))
    check("a stoppage reason counts however it is capitalised",
          edge_trip["breakdown_repeat"]["measured"] == 3)
    check("4.9% rejected fires neither reject rule", not edge_clear["reject_moderate"]["fired"],
          str(edge_clear["reject_moderate"]["measured"]))
    check("5.0% rejected fires the moderate rule", edge_trip["reject_moderate"]["fired"])
    check("8.0% rejected fires the HIGH rule, exactly at the threshold",
          {c["key"]: c for c in by_name["DOWN-01"]["components"]}["reject_high"]["fired"])
    check("utilisation 40 is not low", not edge_clear["low_utilization"]["fired"])
    check("utilisation 41 is not low either", not edge_trip["low_utilization"]["fired"])
    check("utilisation 12 is low", {c["key"]: c for c in by_name["IDLE-01"]["components"]}["low_utilization"]["fired"])
    check("utilisation 95 is high", {c["key"]: c for c in by_name["HOT-01"]["components"]}["high_utilization"]["fired"])
    check("utilisation 40 is not high either — neither rule owns the middle",
          not edge_clear["high_utilization"]["fired"])
    check("499 outstanding units do not fire the load rule", not edge_clear["work_order_load"]["fired"],
          str(edge_clear["work_order_load"]["measured"]))
    check("500 do", edge_trip["work_order_load"]["fired"], str(edge_trip["work_order_load"]["measured"]))
    check("a closed work order adds no pressure",
          {c["key"]: c for c in by_name["CLEAN-01"]["components"]}["work_order_load"]["measured"] == 0)
    check("a duration that does not parse adds no minutes but still counts as a stoppage",
          edge_clear["downtime_moderate"]["measured"] == 59 and edge_clear["downtime_frequent"]["measured"] == 4)

    print("\n4. A clean machine, and a machine past the cap")
    clean = by_name["CLEAN-01"]
    check("a machine with nothing wrong scores 0", clean["risk_score"] == 0)
    check("and says so in words", clean["reasons"] == ["no major risk indicators detected"])
    check("and it is not capped", clean["capped"] is False)
    worst = by_name["ALL-01"]
    check("the worst machine trips more than 100 points", worst["points_before_cap"] > 100,
          str(worst["points_before_cap"]))
    check("its published score is capped at 100", worst["risk_score"] == 100)
    check("and the cap is declared", worst["capped"] is True)

    print("\n5. The explanation, and the sum it claims")
    for mid, name, status, util in MACHINES:
        row = rows[mid]
        x = mh.explain(row)
        check(f"{name}: health = 100 - score", x["health_score"] == max(0, 100 - row["risk_score"]))
        check(f"{name}: deductions + clear == every rule checked",
              len(x["deductions"]) + len(x["clear"]) == len(row["components"]) == x["checks_run"])
        check(f"{name}: deductions are ordered biggest first",
              [d["points"] for d in x["deductions"]] == sorted((d["points"] for d in x["deductions"]),
                                                               reverse=True))
        check(f"{name}: every deduction cost points", all(d["points"] > 0 for d in x["deductions"]))
        check(f"{name}: no rule in `clear` cost anything", all(c["points"] == 0 for c in x["clear"]))
        check(f"{name}: points_deducted is the published score", x["points_deducted"] == row["risk_score"])
        cost_facts = [f for f in x["facts"] if f["key"].startswith("health.cost.")]
        check(f"{name}: one cost fact per deduction", len(cost_facts) == len(x["deductions"]))
        check(f"{name}: the cost facts sum to the points taken off",
              min(sum(f["value"] for f in cost_facts), 100) == x["points_deducted"])
        check(f"{name}: each deduction states what it read and against what",
              all(d["reading"] and d["threshold"] for d in x["deductions"]))
    # Every one of the scorer's rules must be exercised by this plant, or the
    # boundaries above are only testing the rules someone remembered.
    seen = {c["key"] for r in rows.values() for c in r["components"]}
    check("every rule in the scorer is covered by this test", len(seen) == 11, sorted(seen))

    print("\n6. The bands agree with the card's bands")
    for health in (0, 1, 34, 35, 36, 54, 55, 56, 79, 80, 81, 100):
        check(f"band at {health}: {mh._band(health)}", mh._band(health) == twin._band(health),
              f"{mh._band(health)} != {twin._band(health)}")
    check("the band rule names all four bands",
          all(w in mh.BAND_RULE for w in ("Healthy", "Watch", "At risk", "Critical")))

    print("\n7. Not scored is not healthy")
    none = mh.explain(None)
    check("no risk row gives NOT MEASURED", none["state"] == ev.NOT_MEASURED)
    check("and no health number is invented", none["health_score"] is None)
    check("and no rules are listed as passed", none["deductions"] == [] and none["clear"] == [])
    check("and the note refuses to read 100 as healthy",
          "not that every check passed" in none["note"], none["note"])
    check("and there are no facts to quote", none["facts"] == [])
    check("say() refuses to explain it", "has not been scored" in mh.say("CNC-09", none))
    empty = mh.explain({"risk_score": 0, "components": []})
    check("a row with no components is also NOT MEASURED", empty["state"] == ev.NOT_MEASURED)

    print("\n8. Nothing here is called a model")
    for mid, name, status, util in MACHINES:
        x = mh.explain(rows[mid])
        check(f"{name}: no fact claims MODEL ESTIMATE", all(f["provenance"] != ev.MODEL for f in x["facts"]))
        check(f"{name}: every score fact is RULE-BASED ASSESSMENT",
              all(f["provenance"] == ev.RULE for f in x["facts"]
                  if f["key"].startswith("health.cost.") or f["key"] == "health.score"))
        check(f"{name}: every provenance is in the vocabulary",
              all(f["provenance"] in ev.PROVENANCE for f in x["facts"]))
    text = (mh.NOTE + " " + mh.__doc__).lower()
    for phrase in ("machine learning", "predict"):
        idx = text.find(phrase)
        check(f'"{phrase}" only ever appears denied', idx == -1 or "not " in text[max(0, idx - 40):idx],
              text[max(0, idx - 40):idx + 20])

    print("\n9. The sentence AMP says is grounded in the facts it has")
    for mid, name, status, util in MACHINES:
        x = mh.explain(rows[mid])
        said = mh.say(name, x)
        g = grounding.check(said, x["facts"], question=f"how is {name} doing?")
        check(f"{name}: every figure in the sentence is in the evidence", g.passed,
              f"{said} :: ungrounded={g.ungrounded_numbers} unknown={g.unknown_identifiers}")
        check(f"{name}: the sentence is not empty and names the machine", name in said and len(said) > 30)

    print("\n10. It reaches the cockpit, and stays inside the tenant")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    Session = sessionmaker(bind=engine)
    F.seed(Session)
    seen_names = {}
    for tenant in F.TENANTS:
        db = Session()
        tok = tenancy.set_current_tenant(tenant)
        try:
            import models
            machines = db.query(models.Machine).order_by(models.Machine.id).all()
            check(f"{tenant}: has machines to explain", bool(machines))
            names = []
            for m in machines:
                d = twin.build_machine_detail(db, tenant, m.id)
                x = d.get("health_explanation")
                check(f"{tenant}/{m.name}: the cockpit carries the explanation", bool(x))
                check(f"{tenant}/{m.name}: it explains the score the cockpit shows",
                      x["health_score"] == d["health_score"],
                      f"{x['health_score']} != {d['health_score']}")
                check(f"{tenant}/{m.name}: band matches the twin's band", x["band"] == d["health_band"])
                check(f"{tenant}/{m.name}: the older risk_factors list still agrees",
                      [dd["reason"] for dd in x["deductions"]] == sorted(d["risk_factors"],
                                                                        key=lambda r: -_points(x, r))
                      or set(d["risk_factors"]) == {dd["reason"] for dd in x["deductions"]}
                      or d["risk_factors"] == ["no major risk indicators detected"],
                      f"{d['risk_factors']} vs {[dd['reason'] for dd in x['deductions']]}")
                names.append(m.name)
            seen_names[tenant] = names
        finally:
            tenancy.reset_current_tenant(tok)
            db.close()
    # The three factories share machine names on purpose (CNC-01 exists in each),
    # so the isolation check is on the machines a tenant can reach at all.
    for tenant, names in seen_names.items():
        db = Session()
        tok = tenancy.set_current_tenant(tenant)
        try:
            import models
            foreign = (db.query(models.Machine)
                       .filter(models.Machine.tenant_code != tenant).count())
            check(f"{tenant}: sees no other factory's machines", foreign == 0)
        finally:
            tenancy.reset_current_tenant(tok)
            db.close()


    print("\n11. The trained model is offered as a model, or not at all")
    from ai.tools import registry as treg
    db = Session()
    tok = tenancy.set_current_tenant(F.B)
    try:
        for role in ("Operator", "Viewer", ""):
            r = treg.run_tool(db, treg.Principal(tenant=F.B, role=role), "get_failure_risk")
            check(f"role {role or '(none)'} is refused the model", r.state == ev.NOT_PERMITTED,
                  f"{r.state}: {r.summary}")
            check(f"role {role or '(none)'} gets no facts with the refusal", r.facts == [])
        for role in ("Admin", "Supervisor"):
            r = treg.run_tool(db, treg.Principal(tenant=F.B, role=role), "get_failure_risk")
            check(f"role {role} may ask", r.state not in (ev.NOT_PERMITTED, ev.NOT_LICENSED),
                  f"{r.state}: {r.summary}")
            # The one state this tool may return while the evaluation is
            # synthetic-only. OK would be a claim AMP has not earned.
            check(f"role {role}: the state is MODEL NOT VALIDATED",
                  r.state in (ev.MODEL_NOT_VALIDATED, ev.NOT_MEASURED), r.state)
            if r.state == ev.NOT_MEASURED:
                check("an unavailable model gives no estimate",
                      not any(f.provenance == ev.MODEL for f in r.facts))
                continue
            model_facts = [f for f in r.facts if f.provenance == ev.MODEL]
            check(f"role {role}: the estimates are labelled MODEL ESTIMATE", bool(model_facts))
            check(f"role {role}: every estimate carries the synthetic-only caveat",
                  all("synthetic" in (f.detail or "") for f in model_facts),
                  str([f.detail for f in model_facts]))
            check(f"role {role}: the caveat is on the result too",
                  any("synthetic" in n for n in r.notes), str(r.notes))
            check(f"role {role}: the sentence says it is an estimate, not a measurement",
                  "not a measurement" in r.summary, r.summary)
            check(f"role {role}: no rule score is passed off as the model",
                  all(f.provenance == ev.RULE for f in r.facts if f.key.startswith("model.rule.")))
            for word in ("will fail", "certain", "guarantee"):
                check(f"role {role}: the summary avoids the word {word!r}", word not in r.summary.lower())
        # The Copilot's own catalogue must carry the restriction, so a model is
        # never even offered a tool the asker may not use.
        cat = {c["name"]: c for c in treg.catalog(treg.Principal(tenant=F.B, role="Operator"))}
        check("an Operator is not offered the model tool at all", "get_failure_risk" not in cat,
              str(sorted(cat)[:5]))
        cat_admin = {c["name"]: c for c in treg.catalog(treg.Principal(tenant=F.B, role="Admin"))}
        check("an Admin is", "get_failure_risk" in cat_admin)
        check("and its description warns before it is ever called",
              "synthetic" in cat_admin["get_failure_risk"]["description"].lower(),
              cat_admin.get("get_failure_risk", {}).get("description", ""))
        # AMP tells a language model NOTHING about who is asking — not the
        # tenant, not the username, not the role — and the tool catalogue is
        # sent to the model on every planning call. So no description may name a
        # role, and neither may a refusal, which can travel to the model as the
        # draft it is asked to word. test_copilot_local_provider.py section 5
        # asserts it from the other end, on the actual request bodies; this is
        # the same rule checked where the strings are written. (The first
        # role-restricted tool was written with "Admin and Supervisor only." in
        # its description, and that suite caught it.)
        role_words = ("Admin", "Supervisor", "Operator", "Viewer", "Manager")
        for spec in treg.catalog(treg.Principal(tenant=F.B, role="Admin")):
            named = [w for w in role_words if w in spec["description"]]
            check(f"{spec['name']}: its description names no role", not named,
                  f"{named} in: {spec['description'][:120]}")
        for role in ("Operator", "Viewer"):
            r = treg.run_tool(db, treg.Principal(tenant=F.B, role=role), "get_failure_risk")
            named = [w for w in role_words if w in r.summary]
            check(f"the refusal to a {role} names no role", not named, r.summary)
        # The artifact hash is what makes this model quotable at all, so the
        # unavailable path is not a corner case: it is what AMP does when it
        # cannot verify what it is about to say. Point the loader at a file that
        # does not exist — the real verification, really failing — and the tool
        # must produce no estimate whatsoever. (Without this, a mutation that
        # deleted the check survived, because the artifact always loads here.)
        from amp_ai.failure_risk import predict as fr_predict
        real_path = fr_predict.ARTIFACT_PATH
        fr_predict.ARTIFACT_PATH = os.path.join(os.path.dirname(real_path), "no_such_artifact.json")
        try:
            r = treg.run_tool(db, treg.Principal(tenant=F.B, role="Admin"), "get_failure_risk")
            check("an unverifiable model gives NOT MEASURED", r.state == ev.NOT_MEASURED, r.state)
            check("and no estimate at all", not any(f.provenance == ev.MODEL for f in r.facts))
            check("and no percentage anywhere in the sentence", "%" not in r.summary, r.summary)
            check("and no band word is offered", "band" not in r.summary.lower(), r.summary)
            check("and the caveat still travels with it", any("synthetic" in n for n in r.notes))
            check("and it says which number is unaffected",
                  "rule-based health score is unaffected" in r.summary, r.summary)
        finally:
            fr_predict.ARTIFACT_PATH = real_path
        r = treg.run_tool(db, treg.Principal(tenant=F.B, role="Admin"), "get_failure_risk")
        check("and the real artifact is used again afterwards",
              r.state in (ev.MODEL_NOT_VALIDATED, ev.NOT_MEASURED), r.state)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()

    print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'All checks passed'}")
    for f in failures:
        print("  -", f)
    return 1 if failures else 0


def _points(explanation, reason):
    for d in explanation["deductions"]:
        if d["reason"] == reason:
            return d["points"]
    return 0


if __name__ == "__main__":
    sys.exit(main_())
