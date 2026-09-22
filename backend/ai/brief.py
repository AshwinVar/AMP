"""The Daily Factory Brief: the whole plant, written out, once (ADR-0028).

AMP now answers the owner's questions in four places — the Command Centre ranks
what is wrong by what it cost, the Root-Cause Explorer says why, the Risk Radar
says what is likely next, and the approval queue says who decides. A dashboard
asks someone to visit four cards and join them up. A brief does the joining, in
sentences, once a day.

WHAT IT IS NOT. It is not a fifth engine. Every figure in here was produced by a
read-model that already existed; this module composes, orders and words them. If
a number appears in the brief that no engine produced, that is a bug, and
test_daily_brief.py is written to catch exactly that: each section's numbers are
checked against the payload the engine returned.

THE WINDOW IS STATED, NOT IMPLIED. AMP does not know a plant's shift pattern —
it records shift OUTPUT ("Shift A - 17 Jul") but no start or end times — so this
brief never claims to cover "this shift" or "since 6am". It covers the rolling
window the read-models already share (ADR-0014), says so in its first sentence,
and reports per-shift attainment from the shift log as the measurement it is.

WHAT AMP CANNOT SEE. The last section is the one no dashboard has: the brief
lists its own blind spots — machines that did not report, a plant with no unit
value so nothing can be costed, stoppage time with no reason logged, a rate AMP
has no history for. Every one is read out of the payloads above, never guessed.
A brief that only reported what AMP knows would quietly imply it knows the rest.

STATE. The brief's own data state is the WORST of its sections, so a brief built
on partial data cannot read as a complete one.
"""
from datetime import datetime

from ai import evidence as ev
from ai.command_centre import build_command_centre
from ai.risk_radar import build_risk_radar
from ai.root_cause import explain_production_gap
from ai.scorecard import build_scorecard
from ai.shift import build_shift_summary

name = "brief"

# Worst-first, so a section's state can be folded into the brief's own.
_SEVERITY = [ev.OK, ev.MODEL_NOT_VALIDATED, ev.INSUFFICIENT_HISTORY, ev.NOT_CONFIGURED,
             ev.NOT_MEASURED, ev.PARTIAL_DATA, ev.NO_DATA]

NOTE = ("Every figure here comes from a read-model you can open: the Command Centre, the Root-Cause "
        "Explorer and the Risk Radar. The brief states them together; it computes nothing of its own.")


def _worst(states) -> str:
    seen = [s for s in states if s in _SEVERITY]
    return max(seen, key=_SEVERITY.index) if seen else ev.OK


def _section(key, title, lines, facts=(), state=ev.OK, view=None) -> dict:
    return {"key": key, "title": title, "lines": [ln for ln in lines if ln],
            "facts": list(facts), "state": state, "view": view}


def _units(value) -> str:
    return f"{value:,} units" if value is not None else "an unknown number of units"


def _where_we_are(cc) -> dict:
    """Question 1: where are we? Worded from the Command Centre's own position —
    its OEE with the coverage phrase attached, what the machines are doing, what
    was made, and where that sits against the plan (or that there is no plan)."""
    p = cc["position"]
    m, out, plan = p["machines"], p["output"], p["plan"]
    lines = []
    if p["oee"] is not None:
        # The coverage phrase travels with the OEE figure, never after it
        # (ADR-0014): "62% OEE" from two machines of five is a different claim.
        lines.append(f"Plant OEE is {p['oee']}%"
                     + (f" {p['coverage_phrase']}." if p.get("coverage_phrase") else "."))
    else:
        lines.append("No production was recorded in this window, so there is no OEE — not 0%.")
    machines_line = f"{m['running']} of {m['total']} machines are running"
    if m["down_names"]:
        machines_line += f"; {len(m['down_names'])} down ({', '.join(m['down_names'])})"
    if m["maintenance"]:
        machines_line += f"; {m['maintenance']} in maintenance"
    lines.append(machines_line + ".")
    lines.append(f"{out['good']:,} good units of {out['total']:,} made across {out['runs']:,} runs "
                 f"in the last {out['days']} days.")
    if plan["state"] == ev.OK:
        lines.append(f"Against the plans that came due: {plan['actual_units']:,} made of "
                     f"{plan['planned_units']:,} planned ({plan['attainment_rate']}%).")
    elif plan["state"] == ev.NOT_CONFIGURED:
        lines.append("No plan came due in this window, so there is nothing to measure output against.")
    else:
        lines.append("Plans exist but carry no planned quantity, so attainment cannot be worked out.")
    return _section("position", "Where we are", lines, p.get("facts") or [], p.get("state", ev.OK),
                    "overview")


def _what_is_wrong(cc) -> dict:
    """Question 2 and 4: the ranked problems, each with what it cost."""
    problems = cc["problems"]
    if not problems:
        return _section("problems", "What is wrong",
                        ["Nothing was ranked as a problem in this window."], [], cc.get("state", ev.OK),
                        "overview")
    lines = []
    for i, p in enumerate(problems, 1):
        size = p.get("impact_money")
        if size is not None and p.get("currency"):
            cost = f"{p['currency']}{size:,.0f}"
        elif p.get("impact_units") is not None:
            cost = _units(p["impact_units"])
        else:
            # A problem AMP cannot size is still listed, and says so.
            cost = "a size AMP cannot measure"
        lines.append(f"{i}. {p['title']} — {p['detail']} ({cost}; ranked on {p.get('rank_basis', 'measured loss')}).")
    lines.append(cc["overlap_note"])
    facts = [f for p in problems for f in (p.get("facts") or [])]
    return _section("problems", "What is wrong, biggest first", lines, facts, cc.get("state", ev.OK),
                    "overview")


def _why(rc) -> dict:
    """Question 3: the Root-Cause Explorer's own account, quoted."""
    lines = [rc["headline"]]
    for c in (rc.get("contributors") or [])[:4]:
        lines.append(f"{c['label']}: {c['mechanism']} [{c['cause_label']}]")
    if rc.get("unattributed_units"):
        lines.append(f"{rc['unattributed_units']:,} units of lost capacity carry no recorded reason. "
                     "AMP will not guess one.")
    return _section("why", "Why", lines, [f for c in (rc.get("contributors") or [])[:4]
                                          for f in (c.get("facts") or [])],
                    rc.get("state", ev.OK), "executive")


def _what_is_likely(radar) -> dict:
    """Question 5: the Risk Radar, with each rule kept attached to its word."""
    risks = radar.get("risks") or []
    if not risks:
        return _section("risks", "What is likely to become a problem",
                        [radar.get("headline") or "Nothing is on the radar right now."], [],
                        radar.get("state", ev.OK), "overview")
    lines = [radar["headline"]]
    for r in risks[:5]:
        lines.append(f"[{r['likelihood']}] {r['title']} — {r['detail']} (rule: {r['rule']}; {r['horizon']}).")
    lines.append(radar["note"])
    return _section("risks", "What is likely to become a problem", lines,
                    [f for r in risks[:5] for f in (r.get("facts") or [])],
                    radar.get("state", ev.OK), "overview")


def _what_to_do(cc) -> dict:
    """Question 6: the actions waiting, and who decides them. Nothing is auto-run."""
    actions = cc.get("actions") or []
    if not actions:
        return _section("actions", "What to do next",
                        ["Nothing is waiting for a decision."], [], ev.OK, "agents")
    lines = [f"{a['title']} — {a['detail']} (decided by {a.get('who') or 'whoever owns it'})"
             for a in actions[:5]]
    lines.append("Nothing here runs by itself: each one waits for a person to approve it.")
    return _section("actions", "What to do next", lines, [], ev.OK, "agents")


def _how_the_shifts_did(shift) -> dict:
    rows = shift.get("shifts") or []
    if not rows:
        return _section("shifts", "How the shifts did",
                        ["No shift output was logged in this window, so AMP cannot compare shifts."],
                        [], ev.NO_DATA, "shift")
    lines = []
    for row in rows:
        att = row.get("attainment")
        # A shift with no target has no attainment. That is not 0%.
        said = f"{att}% of target" if att is not None else "no target set, so no attainment"
        lines.append(f"{row['shift']}: {row.get('actual', 0):,} units, {said}.")
    return _section("shifts", "How the shifts did", lines, [], ev.OK, "shift")


def _movement(sc) -> dict:
    """What changed against the previous window, from the scorecard that already measures it."""
    if not sc.get("has_data"):
        return _section("movement", "What changed",
                        ["No production data in the window, so there is nothing to compare."], [],
                        ev.NO_DATA, "executive")
    lines = []
    for k in sc["kpis"]:
        delta = k.get("delta")
        if delta is None:
            lines.append(f"{k['label']}: {k['value']}{k.get('unit') or ''} (no comparison available).")
        else:
            way = "up" if delta > 0 else ("down" if delta < 0 else "level")
            lines.append(f"{k['label']}: {k['value']}{k.get('unit') or ''}, {way} "
                         f"{abs(delta)}{k.get('unit') or ''} on the previous 7 days.")
    return _section("movement", "What changed against last week", lines, [], ev.OK, "executive")


def _blind_spots(cc, rc, radar) -> list:
    """What AMP could not see. Read out of the payloads above, never guessed.

    This is the section no card has. Everything else in the brief is what AMP
    knows; without this, the reader is left to assume the rest is fine.
    """
    out = []
    cov = ((cc.get("position") or {}).get("oee_coverage")) or {}
    expected, reporting = cov.get("machines_expected"), cov.get("machines_reporting")
    if expected and reporting is not None and reporting < expected:
        out.append({"key": "coverage", "state": ev.PARTIAL_DATA,
                    "text": (f"{reporting} of {expected} machines reported production. Everything above "
                             f"covers only those; the rest are not zero, they are unmeasured.")})
    cost = cc.get("cost") or {}
    if not cost.get("priced"):
        # No currency SYMBOL on an unpriced workspace, even inside a sentence that
        # says nothing is costed: a reader scanning the page sees the symbol, not
        # the clause around it, and the evaluation counts any symbol on an
        # unpriced tenant as a money fabrication. It is right to.
        out.append({"key": "no_unit_value", "state": ev.NOT_CONFIGURED,
                    "text": ("No unit value is set for this workspace, so AMP puts no money figure on "
                             "anything here. Losses are given in good units not made.")})
    if ((rc.get("gap") or {}).get("state") == ev.NOT_CONFIGURED
            or (cc.get("position") or {}).get("plan", {}).get("state") == ev.NOT_CONFIGURED):
        out.append({"key": "no_plan", "state": ev.NOT_CONFIGURED,
                    "text": ("No production plan came due in this window, so AMP cannot say whether "
                             "output was on target — only what it was.")})
    if rc.get("unattributed_units"):
        # NOT MEASURED, not PARTIAL DATA: the time IS measured, the reason is
        # what AMP has no reading for.
        out.append({"key": "unlogged", "state": ev.NOT_MEASURED,
                    "text": (f"{rc['unattributed_units']:,} units of lost capacity have no stoppage reason "
                             "recorded. AMP can measure that time but cannot explain it.")})
    if radar.get("state") == ev.INSUFFICIENT_HISTORY:
        out.append({"key": "no_rate", "state": ev.INSUFFICIENT_HISTORY,
                    "text": ("AMP has no measured production rate for this window, so it cannot judge "
                             "whether a delivery date is reachable.")})
    if not out:
        out.append({"key": "none", "state": ev.OK,
                    "text": ("Every machine reported, the losses are costed, and every measured loss "
                             "carries a reason. AMP still only sees what is recorded in it.")})
    return out


def _headline(cc, radar, spots) -> str:
    """One sentence. It may not say the plant is fine when AMP could not see it."""
    problems = cc.get("problems") or []
    likely = [r for r in (radar.get("risks") or []) if r.get("likelihood") == "LIKELY"]
    partial = [s for s in spots if s["state"] != ev.OK]
    if not problems and not likely:
        base = "Nothing is ranked as a problem and nothing is likely to become one."
    elif problems:
        base = (f"{problems[0]['title']} leads the day"
                + (f", and {len(likely)} more thing{'s' if len(likely) != 1 else ''} "
                   f"{'are' if len(likely) != 1 else 'is'} likely to become a problem"
                   if likely else "") + ".")
    else:
        base = (f"Nothing is ranked as a problem, but {len(likely)} "
                f"{'are' if len(likely) != 1 else 'is'} likely to become one.")
    if partial:
        base += f" {len(partial)} thing{'s' if len(partial) != 1 else ''} AMP could not see — see the last section."
    return base


def build_daily_brief(db, tenant: str, now=None) -> dict:
    """The whole plant in one page of sentences, composed from the engines that
    already answer each question. Computes no figure of its own."""
    at = now or datetime.utcnow()
    # The brief composes this card for its PROBLEMS and never reads
    # `position.health`, so it does not pay the fleet queries behind that
    # block. The brief's recorded query budget is what caught it paying
    # for them (test_daily_brief.py section 10).
    cc = build_command_centre(db, tenant, now=at, with_health=False)
    rc = explain_production_gap(db, tenant, now=at)
    radar = build_risk_radar(db, tenant, now=at)
    shift = build_shift_summary(db, tenant)
    sc = build_scorecard(db, tenant)

    sections = [
        _where_we_are(cc),
        _movement(sc),
        _what_is_wrong(cc),
        _why(rc),
        _what_is_likely(radar),
        _how_the_shifts_did(shift),
        _what_to_do(cc),
    ]
    spots = _blind_spots(cc, rc, radar)
    return {
        "generated_at": at.isoformat(),
        "days": cc.get("days"),
        # Said plainly, because a brief with no window is a brief that can be
        # read as "today" whatever it covers.
        "window": f"the last {cc.get('days')} days, ending {at.date().isoformat()}",
        # The worst of the SECTIONS, not of the blind spots. A blind spot carries
        # its own state for its own line; folding them in here would relabel the
        # whole brief PARTIAL DATA whose UI text says "part of the plant did not
        # report" — which is a different claim from "this stoppage has no reason
        # logged". Same mistake ADR-0025 had to correct in the Root-Cause card.
        "state": _worst([s["state"] for s in sections]),
        "headline": _headline(cc, radar, spots),
        "sections": sections,
        "blind_spots": spots,
        "note": NOTE,
    }


def say_brief(brief: dict) -> tuple:
    """The brief as one paragraph, for the Copilot."""
    lead = f"Brief for {brief['window']}. {brief['headline']}"
    spots = [s for s in brief["blind_spots"] if s["state"] != ev.OK]
    if spots:
        lead += f" {spots[0]['text']}"
    return lead, "overview"
