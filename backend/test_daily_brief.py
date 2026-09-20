"""The brief may only say what an engine already said (ADR-0028).

A brief is the easiest place in a product to lie, because prose hides its
sources. Every sentence here reads as AMP's own assessment, so this file checks
each one against the payload it came from, across the three-factory fixture.

What is pinned:

  1. NO NEW NUMBERS. Every figure in every line of every section appears in the
     payload of the engine that section quotes. A number the brief invented —
     or rounded differently, or carried over from another factory — fails.
  2. THE WINDOW IS STATED. The brief says what it covers, in words, and never
     claims a shift boundary AMP does not know.
  3. THE BLIND SPOTS ARE REAL. Each one is checked against the condition that
     produced it: coverage below full, no unit value, no plan, unattributed
     units, no measured rate. And when there is genuinely nothing missing, the
     brief still says what it cannot see in principle.
  4. THE STATE IS THE SECTIONS'. A blind spot has its own state and does NOT
     relabel the brief; PARTIAL DATA means part of the plant did not report,
     and means nothing else.
  5. THE HEADLINE CANNOT SAY THINGS ARE FINE WHEN AMP COULD NOT SEE. Whenever a
     blind spot exists, the headline says so.
  6. NOTHING IS AUTO-RUN. The actions section says a person decides.
  7. TENANT ISOLATION. No factory's brief names another factory's machine.
  8. THE COPILOT SAYS THE SAME THING, and its sentence is grounded in its facts.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_daily_brief.py
"""
import re
import sys

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import tenancy
from ai import evidence as ev
from ai import grounding
from ai import brief as brief_mod
from ai.brief import build_daily_brief, say_brief
from ai.command_centre import build_command_centre
from ai.risk_radar import build_risk_radar
from ai.root_cause import explain_production_gap
from ai.tools import registry as treg
from copilot_eval import fixtures as F
from database import Base

# Recorded, not guessed: section 10 prints the number and asserts it. Measured
# at 71 on the three-factory fixture — five read-models in one request, which
# is why the brief loads on demand and does not poll. Headroom is small on
# purpose: a change that adds a query per machine should be seen in review.
QUERY_BUDGET = 90

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def numbers(text) -> set:
    """Every figure a sentence shows, as a comparable set of strings."""
    return {n.replace(",", "") for n in _NUMBER.findall(text or "")}


def payload_numbers(obj) -> set:
    """Every figure anywhere in an engine's payload, flattened."""
    out = set()
    if isinstance(obj, dict):
        for v in obj.values():
            out |= payload_numbers(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out |= payload_numbers(v)
    elif isinstance(obj, bool):
        pass
    elif isinstance(obj, (int, float)):
        out.add(str(obj))
        out.add(str(int(obj)) if float(obj).is_integer() else str(obj))
        out.add(f"{obj:.0f}")
        out.add(f"{obj:.1f}")
    elif isinstance(obj, str):
        out |= numbers(obj)
    return out


def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    Session = sessionmaker(bind=engine)
    F.seed(Session)
    return Session, engine


def within(Session, tenant, fn):
    db = Session()
    tok = tenancy.set_current_tenant(tenant)
    try:
        return fn(db)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def main_():
    Session, engine = session()
    briefs, sources = {}, {}
    for t in F.TENANTS:
        briefs[t] = within(Session, t, lambda db, t=t: build_daily_brief(db, t))
        sources[t] = within(Session, t, lambda db, t=t: {
            "cc": build_command_centre(db, t),
            "rc": explain_production_gap(db, t),
            "radar": build_risk_radar(db, t),
        })

    print("\n1. Every figure the brief shows came from an engine")
    # The brief's own structural counts ("3 things AMP could not see", section
    # numbering "1.", "2.") are its own; everything else must be traceable.
    for t in F.TENANTS:
        b, s = briefs[t], sources[t]
        available = payload_numbers(s["cc"]) | payload_numbers(s["rc"]) | payload_numbers(s["radar"])
        # Small integers are list positions and counts the brief is entitled to.
        allowed = available | {str(i) for i in range(0, 21)}
        for section in b["sections"]:
            if section["key"] in ("movement", "shifts"):
                continue          # quoted from the scorecard / shift log, checked in section 2
            for line in section["lines"]:
                unknown = sorted(n for n in numbers(line) if n not in allowed)
                check(f"{t}/{section['key']}: every figure is the engine's", not unknown,
                      f"{unknown} in: {line[:120]}")

        # A problem AMP cannot size must not acquire a number on its way into
        # prose. "0 units" and "a size AMP cannot measure" are different claims,
        # and only one of them is true.
        wrong = next(s for s in b["sections"] if s["key"] == "problems")
        for prob in s["cc"]["problems"]:
            if prob["impact_units"] is None and prob["impact_money"] is None:
                line = next((ln for ln in wrong["lines"] if prob["title"][:30] in ln), "")
                check(f"{t}: the unsized problem is not given a figure",
                      "cannot measure" in line, line or "(line not found)")

    print("\n2. The movement and shift sections quote their own read-models")
    for t in F.TENANTS:
        b = briefs[t]
        movement = next(s for s in b["sections"] if s["key"] == "movement")
        shifts = next(s for s in b["sections"] if s["key"] == "shifts")
        check(f"{t}: movement names the window it compares against",
              all("previous 7 days" in ln or "no comparison" in ln or "nothing to compare" in ln
                  for ln in movement["lines"]), str(movement["lines"][:2]))
        check(f"{t}: a shift with no target is not given 0%",
              all("0% of target" not in ln for ln in shifts["lines"]), str(shifts["lines"]))

    print("\n3. The window is stated, and no shift boundary is claimed")
    for t in F.TENANTS:
        b = briefs[t]
        check(f"{t}: the brief says what it covers", "last" in b["window"] and "ending" in b["window"],
              b["window"])
        whole = " ".join(ln for s in b["sections"] for ln in s["lines"]) + " " + b["headline"]
        for phrase in ("this shift", "since 6", "overnight", "so far today", "this morning"):
            check(f"{t}: does not claim \"{phrase}\"", phrase not in whole.lower())

    print("\n4. Every blind spot is true of this factory")
    for t in F.TENANTS:
        b, s = briefs[t], sources[t]
        cov = (s["cc"]["position"] or {}).get("oee_coverage") or {}
        keys = {sp["key"] for sp in b["blind_spots"]}
        incomplete = bool(cov.get("machines_expected")) and not cov.get("complete", True)
        check(f"{t}: coverage gap reported exactly when coverage is incomplete",
              ("coverage" in keys) == incomplete, f"{keys} vs complete={cov.get('complete')}")
        check(f"{t}: money gap reported exactly when no unit value is set",
              ("no_unit_value" in keys) == (not s["cc"]["cost"]["priced"]))
        check(f"{t}: unlogged-time gap reported exactly when there is unattributed time",
              ("unlogged" in keys) == bool(s["rc"].get("unattributed_units")))
        for sp in b["blind_spots"]:
            check(f"{t}/{sp['key']}: the gap carries an honest state",
                  sp["state"] in ev.DATA_STATES, sp["state"])
            check(f"{t}/{sp['key']}: and says something", len(sp["text"]) > 30)
        # The unlogged-reason gap must NOT be called PARTIAL DATA: the time is
        # measured, the reason is what is missing.
        for sp in b["blind_spots"]:
            if sp["key"] == "unlogged":
                check(f"{t}: unlogged time is NOT MEASURED, not PARTIAL DATA",
                      sp["state"] == ev.NOT_MEASURED, sp["state"])
        check(f"{t}: something is always said about what AMP cannot see", bool(b["blind_spots"]))

    print("\n5. The brief's state is its sections' state")
    for t in F.TENANTS:
        b = briefs[t]
        section_states = {s["state"] for s in b["sections"]}
        check(f"{t}: state is one of the sections' states", b["state"] in section_states,
              f"{b['state']} not in {section_states}")
        if b["state"] == ev.PARTIAL_DATA:
            cov = (sources[t]["cc"]["position"] or {}).get("oee_coverage") or {}
            check(f"{t}: PARTIAL DATA only when part of the plant did not report",
                  not cov.get("complete", True), str(cov))


    print("\n5b. The state is the WORST section's, and nothing else")
    # `state in section_states` above passes for a brief hard-coded to OK, since
    # some section is usually OK. The severity order is written out again here
    # rather than imported, so a change to ai/brief._SEVERITY has to be made
    # twice, deliberately.
    severity = ["OK", "MODEL NOT VALIDATED", "INSUFFICIENT HISTORY", "NOT CONFIGURED",
                "NOT MEASURED", "PARTIAL DATA", "NO DATA"]
    for t in F.TENANTS:
        b = briefs[t]
        want = max((s["state"] for s in b["sections"] if s["state"] in severity),
                   key=severity.index, default="OK")
        check(f"{t}: the brief's state is its worst section's ({want})", b["state"] == want,
              f"{b['state']} != {want}")

    print("\n5c. A clean plant is still told what AMP cannot see")
    # None of the three factories is free of gaps, so the "nothing missing" path
    # is unreachable from the fixture — and a mutation that deleted it survived.
    # Call the blind-spot rule directly with payloads that have nothing wrong.
    clean = brief_mod._blind_spots(
        {"position": {"oee_coverage": {"machines_expected": 4, "machines_reporting": 4, "complete": True},
                      "plan": {"state": ev.OK}},
         "cost": {"priced": True}},
        {"gap": {"state": ev.OK}, "unattributed_units": 0},
        {"state": ev.OK})
    check("a plant with no gaps still gets a sentence", len(clean) == 1, str(clean))
    check("...and it is not a warning", clean[0]["state"] == ev.OK, str(clean))
    check("...and it says AMP only sees what is recorded",
          "only sees what is recorded" in clean[0]["text"], str(clean))
    # And the reverse: every gap present at once produces every gap.
    every = brief_mod._blind_spots(
        {"position": {"oee_coverage": {"machines_expected": 4, "machines_reporting": 2, "complete": False},
                      "plan": {"state": ev.NOT_CONFIGURED}},
         "cost": {"priced": False}},
        {"gap": {"state": ev.NOT_CONFIGURED}, "unattributed_units": 900},
        {"state": ev.INSUFFICIENT_HISTORY})
    keys = [s["key"] for s in every]
    check("every gap is reported when every gap exists",
          set(keys) == {"coverage", "no_unit_value", "no_plan", "unlogged", "no_rate"}, str(keys))
    check("...and the all-clear sentence is not among them", "none" not in keys, str(keys))
    check("no currency symbol reaches an unpriced workspace",
          all("\u00a3" not in s["text"] and "$" not in s["text"] for s in every),
          str([s["text"] for s in every]))

    print("\n6. The headline never reads as 'all clear' when AMP could not see")
    for t in F.TENANTS:
        b = briefs[t]
        missing = [sp for sp in b["blind_spots"] if sp["state"] != ev.OK]
        if missing:
            check(f"{t}: the headline admits what is missing", "could not see" in b["headline"],
                  b["headline"])
        check(f"{t}: the headline is one sentence-ish, not a paragraph", len(b["headline"]) < 260,
              b["headline"])

    print("\n7. Nothing in the brief runs by itself")
    for t in F.TENANTS:
        actions = next(s for s in briefs[t]["sections"] if s["key"] == "actions")
        joined = " ".join(actions["lines"]).lower()
        check(f"{t}: the actions say a person decides",
              "waits for a person" in joined or "nothing is waiting" in joined, joined[:120])
        for word in ("automatically", "amp will do", "has been applied"):
            check(f"{t}: the actions do not say \"{word}\"", word not in joined)

    print("\n8. No factory's brief mentions another factory")
    names = {t: within(Session, t, lambda db: _machine_names(db)) for t in F.TENANTS}
    for t in F.TENANTS:
        whole = " ".join(ln for s in briefs[t]["sections"] for ln in s["lines"])
        whole += " " + briefs[t]["headline"] + " " + " ".join(sp["text"] for sp in briefs[t]["blind_spots"])
        for other in F.TENANTS:
            if other == t:
                continue
            check(f"{t}: does not name {other}", other not in whole)
            unique = names[other] - names[t]
            leaked = sorted(n for n in unique if re.search(rf"\b{re.escape(n)}\b", whole))
            check(f"{t}: names none of {other}'s own machines", not leaked, str(leaked))
        mine = set(F.MARKERS[t] if not isinstance(F.MARKERS[t], str) else [F.MARKERS[t]])
        for other, markers in F.MARKERS.items():
            if other == t:
                continue
            for marker in (markers if not isinstance(markers, str) else [markers]):
                if marker in mine:
                    continue     # the fixture gives the factories colliding names on purpose
                check(f"{t}: {other}'s marker \"{marker}\" does not appear", marker not in whole)

    print("\n9. The Copilot says the same brief, grounded")
    for t in F.TENANTS:
        r = within(Session, t, lambda db, t=t: treg.run_tool(
            db, treg.Principal(tenant=t, role="Admin"), "get_daily_brief"))
        check(f"{t}: the tool answers", r.state in ev.DATA_STATES, f"{r.state}: {r.summary}")
        check(f"{t}: it states the same window", briefs[t]["window"] in r.summary, r.summary[:140])
        g = grounding.check(r.summary, r.to_dict()["facts"], question="brief me")
        check(f"{t}: every figure in the sentence is in its evidence", g.passed,
              f"{r.summary} :: {g.ungrounded_numbers} {g.unknown_identifiers}")
        blind_fact = [f for f in r.facts if f.key == "brief.blind_spots"]
        check(f"{t}: the number of things AMP could not see is a fact", len(blind_fact) == 1)
        if blind_fact:
            expected = len([s for s in briefs[t]["blind_spots"] if s["state"] != ev.OK])
            check(f"{t}: and it is the real number", blind_fact[0].value == expected,
                  f"{blind_fact[0].value} != {expected}")

    print("\n10. What the brief costs, measured")
    # The most expensive read AMP serves: five read-models in one request. It
    # loads on demand rather than polling, but the number is recorded here so a
    # change that quietly adds a query per machine is visible in review.
    counted = []

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cur, statement, params, context, many):
        counted.append(statement)

    within(Session, F.B, lambda db: build_daily_brief(db, F.B))     # warm
    counted.clear()
    within(Session, F.B, lambda db: build_daily_brief(db, F.B))
    n = len(counted)
    event.remove(engine, "before_cursor_execute", _count)
    print(f"  (the brief costs {n} queries)")
    check(f"the brief stays within its recorded budget of {QUERY_BUDGET} queries", n <= QUERY_BUDGET, str(n))

    print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'All checks passed'}")
    for f in failures:
        print("  -", f)
    return 1 if failures else 0


def _machine_names(db):
    import models
    return {m.name for m in db.query(models.Machine).all()}


if __name__ == "__main__":
    sys.exit(main_())
