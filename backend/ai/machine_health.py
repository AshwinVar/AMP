"""Why a machine's health score is that number — every rule, fired or not (ADR-0027).

A health score with nothing behind it is a number a plant either believes or
ignores, and neither is useful. AMP's score is not a mystery: it is eleven fixed
thresholds over recorded data, hand-weighted, run in a fixed order by
``predictive_engine.calculate_predictive_risk``. This module takes the score the
scorer already produced and states it as arithmetic a maintenance lead can
check, disagree with, and act on:

    Health starts at 100.
    Each rule that fired took its points away.
    Each rule that did not fire is listed too, with what it read.

Nothing here scores anything. Every number comes from the risk row the scorer
built: `explain` reads `components`, never the machine. That is deliberate — if
this module could compute a point it would be a second scorer, and the two would
drift apart the way the OEE numbers did before ADR-0014.

PROVENANCE (ai/evidence.py). The score is RULE-BASED ASSESSMENT: a threshold, a
weight, a judgement. It is never MODEL ESTIMATE and never called machine
learning, because it is neither — AMP's one trained model (failure risk) is
evaluated on synthetic machines only, lives on its own card with that caveat,
and does not feed this number.

The rules it explains, in the order the scorer runs them:

    breakdown_now      35   status is Breakdown
    maintenance_now    15   status is Maintenance
    low_utilization    20   utilisation below 40%
    high_utilization   12   utilisation above 90%
    downtime_high      25   120+ minutes of downtime    (or, if not,)
    downtime_moderate  15   60+ minutes of downtime
    downtime_frequent  15   5+ stoppages
    breakdown_repeat   20   3+ transitions into Breakdown
    reject_high        20   8%+ rejected                (or, if not,)
    reject_moderate    10   5%+ rejected
    work_order_load    10   500+ outstanding units

test_machine_health_explained.py checks the explanation against the score
itself: the fired points must sum to the score AMP shows, or the build fails.
"""
from ai import evidence as ev

name = "machine_health"

START = 100
# The bands twin._band applies to the health score, stated where they are read.
BAND_RULE = "80 or more is Healthy · 55 Watch · 35 At risk · below 35 Critical"
NOTE = ("Health starts at 100 and each rule below takes its points away. Every rule is a fixed "
        "threshold over recorded data, hand-weighted by AMP — not machine learning, and not a "
        "prediction of failure.")
# Said when the scorer has no row for this machine. NOT the same as a clean bill
# of health, and the twin's 100 in that case is an absence, not a measurement.
NOT_SCORED = ("AMP has not scored this machine, so there is nothing to explain. A score of 100 "
              "here means no assessment ran, not that every check passed.")


def _reading(component) -> str:
    """What the rule read, as a person would say it: "38%", "145 min", "Breakdown"."""
    measured, unit = component.get("measured"), component.get("unit") or ""
    if measured is None:
        return "not measured"
    if not unit:
        return str(measured)
    return f"{measured}{unit}" if unit == "%" else f"{measured} {unit}"


def _line(component) -> dict:
    """One rule, as the UI and the tools both read it."""
    return {
        "key": component["key"],
        "label": component["label"],
        "points": component["points"],
        "max_points": component["max_points"],
        "reading": _reading(component),
        "measured": component.get("measured"),
        "unit": component.get("unit") or "",
        "threshold": component["threshold"],
        "reason": component.get("reason") or component["label"],
    }


def explain(risk) -> dict:
    """The health score taken apart. `risk` is one `predictive_engine` row, or None.

    Returns the same shape either way, so no caller has to special-case a machine
    the scorer skipped: with no row, the state is NOT MEASURED and the rule lists
    are empty rather than a page of checks AMP never ran.
    """
    if not risk:
        return {"health_score": None, "band": None, "band_rule": BAND_RULE, "start": START,
                "deductions": [], "clear": [], "checks_run": 0, "points_deducted": 0,
                "points_before_cap": 0, "capped": False, "state": ev.NOT_MEASURED,
                "note": NOT_SCORED, "facts": []}

    components = list(risk.get("components") or [])
    score = int(risk.get("risk_score") or 0)
    health = max(0, START - score)
    deductions = [_line(c) for c in components if c["fired"]]
    deductions.sort(key=lambda d: -d["points"])
    clear = [_line(c) for c in components if not c["fired"]]
    capped = bool(risk.get("capped"))

    # Serialised the way every other read-model serialises evidence
    # (ai/command_centre.py): dicts with the fact's own key as its id, so the card
    # renders them with the shared component and the grounding gate can read them.
    facts = [
        ev.Fact(key="health.score", label="Health score", value=health, provenance=ev.RULE,
                unit="/100", source="predictive_engine", window="now",
                detail=f"100 minus {score} rule points"),
        ev.Fact(key="health.checks", label="Rules checked", value=len(components),
                provenance=ev.MEASURED, unit="rules", source="predictive_engine", window="now"),
        # How many of them fired, always — including zero. "All 11 rules passed"
        # is a result, not an empty list, and a reader should be able to see the
        # count without counting the lines themselves.
        ev.Fact(key="health.rules_fired", label="Rules that fired", value=len(deductions),
                provenance=ev.MEASURED, unit="rules", source="predictive_engine", window="now",
                detail="every check passed" if not deductions else ""),
    ]
    # One fact per rule that fired, carrying its POINTS as the value: a figure a
    # reader can add up, and a figure the grounding gate can check a sentence
    # against. The reading and the threshold ride in the detail.
    for d in deductions:
        facts.append(ev.Fact(key=f"health.cost.{d['key']}", label=f"{d['label']} cost",
                             value=d["points"], provenance=ev.RULE, unit="points",
                             source="predictive_engine", window="now",
                             detail=f"read {d['reading']}, rule: {d['threshold']}"))
    return {
        "health_score": health,
        # The band and its rule always travel together: a word like "At risk"
        # with no threshold beside it is the thing this module exists to stop.
        "band": _band(health),
        "band_rule": BAND_RULE,
        "start": START,
        "deductions": deductions,
        "clear": clear,
        "checks_run": len(components),
        "points_deducted": score,
        "points_before_cap": int(risk.get("points_before_cap") or score),
        # A capped score is a real difference: two machines can both read 0 with
        # very different amounts wrong, and the card says so rather than hiding it.
        "capped": capped,
        "state": ev.OK if components else ev.NOT_MEASURED,
        "note": NOTE if components else NOT_SCORED,
        "facts": [f.to_dict(f.key) for f in facts],
    }


def _band(health: int) -> str:
    # twin._band's thresholds, kept here as the one place BAND_RULE describes.
    # test_machine_health_explained.py asserts the two agree at every boundary.
    if health >= 80:
        return "Healthy"
    if health >= 55:
        return "Watch"
    if health >= 35:
        return "At risk"
    return "Critical"


def say(machine_name: str, explanation: dict) -> str:
    """One sentence a person could read out, with the arithmetic in it."""
    if explanation["state"] == ev.NOT_MEASURED:
        return f"{machine_name} has not been scored, so AMP cannot explain a health number for it."
    score, deductions = explanation["health_score"], explanation["deductions"]
    if not deductions:
        return (f"{machine_name} is at {score} out of 100: all {explanation['checks_run']} health "
                f"rules passed, so nothing was taken off.")
    biggest = deductions[0]
    lost = explanation["points_deducted"]
    return (f"{machine_name} is at {score} out of 100 ({explanation['band']}). {lost} points came "
            f"off across {len(deductions)} of {explanation['checks_run']} rules, the largest being "
            f"{biggest['label'].lower()} at {biggest['points']} points "
            f"({biggest['reading']}, rule: {biggest['threshold']}).")
