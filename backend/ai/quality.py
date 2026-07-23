"""Quality summary — a fleet-wide read-model over inspections (ADR-0007).

Answers "how good is what we're making right now?": over the last week's
quality inspections it rolls up the first-pass yield, the fail rate, the top
defect categories (a Pareto), and the machines with the worst quality — a
7-day window like every other pillar (and bounded in SQL, since the table
grows continuously). A read-model over quality_inspections — auto-scoped to
the tenant by the query layer (ADR-0002); it adds no storage.

``build_quality_trend`` asks the question a level rate can never answer: not
"how good is it" but *which way is it going, and who moved?* It compares the
last 7 days against the 7 before, on the same numerator and denominator, and
attributes the swing to specific machines and specific defect categories —
so a plant sees a drift while it is still small rather than after the scrap.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import models

name = "quality"

TOP_N = 5
WINDOW_DAYS = 7

# Trend window — two WINDOW_DAYS halves, so "this week vs last week" on the
# same window the summary already reports.
TREND_HALVES = 2
TREND_WINDOW_DAYS = WINDOW_DAYS * TREND_HALVES
# Percentage points of fail-rate movement we call a real drift rather than
# ordinary week-to-week noise.
DRIFT_PTS = 1.0
# A machine needs this many inspected units in *both* halves before its
# movement means anything — 10 units failing 1 swings a rate by 10 points.
MIN_MACHINE_UNITS = 25
# Below this many units inspected in a half, the plant-level swing is reported
# but not judged.
MIN_SAMPLE_UNITS = 50


def _inspections_since(db, days: int):
    """The last `days` days of inspections, bounded in SQL (the table grows
    continuously). Day-aligned in UTC, like every other window read-model."""
    cutoff = datetime.combine(datetime.utcnow().date() - timedelta(days=days - 1),
                              datetime.min.time())
    return (db.query(models.QualityInspection)
            .filter(models.QualityInspection.created_at >= cutoff).all())


def _recent_inspections(db):
    """The window's inspections, bounded in SQL (the table grows continuously)."""
    return _inspections_since(db, WINDOW_DAYS)


def _pct(part, whole):
    return round(part / whole * 100) if whole else 0


def _rate1(part, whole):
    """Fail rate to one decimal — a trend has to resolve movement an integer
    percentage would round away."""
    return round(part / whole * 100, 1) if whole else 0.0


def _norm_defect(i) -> str:
    return (i.defect_category or "Unspecified").strip() or "Unspecified"


def build_quality_summary(db, tenant: str) -> dict:
    """First-pass yield, fail rate, a defect Pareto, and the worst machines by
    fail rate — over the last 7 days' inspections (auto-scoped, ADR-0002)."""
    inspections = _recent_inspections(db)
    inspected = sum(i.inspected_quantity or 0 for i in inspections)
    passed = sum(i.passed_quantity or 0 for i in inspections)
    failed = sum(i.failed_quantity or 0 for i in inspections)

    # Defect Pareto: total failed units by defect category, biggest first.
    defects: Counter = Counter()
    for i in inspections:
        if i.failed_quantity:
            defects[_norm_defect(i)] += i.failed_quantity
    top_defects = [{"category": c, "count": n} for c, n in defects.most_common(TOP_N)]

    # Worst machines by fail rate (only those that actually inspected units).
    agg: dict = defaultdict(lambda: {"inspected": 0, "failed": 0})
    for i in inspections:
        if i.machine_id is not None:
            agg[i.machine_id]["inspected"] += i.inspected_quantity or 0
            agg[i.machine_id]["failed"] += i.failed_quantity or 0
    all_machines = db.query(models.Machine).all()
    names = {m.id: m.name for m in all_machines}
    line_of = {m.id: (m.line or "") for m in all_machines}
    by_machine = [
        {"machine_id": mid, "name": names.get(mid, f"#{mid}"),
         "inspected": a["inspected"], "failed": a["failed"], "fail_rate": _pct(a["failed"], a["inspected"])}
        for mid, a in agg.items() if a["inspected"] > 0
    ]
    by_machine.sort(key=lambda m: (m["fail_rate"], m["failed"]), reverse=True)

    # Fail rate rolled up per production line (SMT vs IC) — same numerator/denominator.
    line_agg: dict = defaultdict(lambda: {"inspected": 0, "failed": 0})
    for mid, a in agg.items():
        ln = line_of.get(mid, "")
        if ln:
            line_agg[ln]["inspected"] += a["inspected"]
            line_agg[ln]["failed"] += a["failed"]
    by_line = [
        {"line": ln, "inspected": a["inspected"], "failed": a["failed"],
         "fail_rate": _pct(a["failed"], a["inspected"])}
        for ln, a in sorted(line_agg.items()) if a["inspected"] > 0
    ]

    return {
        "inspections": len(inspections),
        "inspected": inspected,
        "passed": passed,
        "failed": failed,
        "rework": sum(i.rework_quantity or 0 for i in inspections),
        "scrap": sum(i.scrap_quantity or 0 for i in inspections),
        "first_pass_yield": _pct(passed, inspected),
        "fail_rate": _pct(failed, inspected),
        "top_defects": top_defects,
        "by_machine": by_machine[:TOP_N],
        "by_line": by_line,
    }


def build_defect_detail(db, tenant: str, category: str) -> dict:
    """Drill-down for a single defect category: the units it has failed (with the
    rework/scrap split), the machines producing it, and the inspections that
    caught it. Composes quality_inspections (auto-scoped, ADR-0002); adds no
    storage. Windowed to the same 7 days as the summary. Returns a zeroed shape
    when the category has no failures."""
    insp = [
        i for i in _recent_inspections(db)
        if i.failed_quantity and _norm_defect(i) == category
    ]
    names = {m.id: m.name for m in db.query(models.Machine).all()}

    per_machine: dict = defaultdict(lambda: {"failed": 0, "inspections": 0})
    for i in insp:
        if i.machine_id is not None:
            per_machine[i.machine_id]["failed"] += i.failed_quantity or 0
            per_machine[i.machine_id]["inspections"] += 1
    by_machine = sorted(
        ({"machine_id": mid, "name": names.get(mid, f"#{mid}"),
          "failed": a["failed"], "inspections": a["inspections"]} for mid, a in per_machine.items()),
        key=lambda m: m["failed"], reverse=True,
    )[:TOP_N]

    recent = sorted(insp, key=lambda i: (i.created_at or datetime.min, i.id), reverse=True)[:10]
    inspections = [{
        "id": i.id,
        "inspection_no": i.inspection_no,
        "machine_id": i.machine_id,
        "machine": names.get(i.machine_id, f"#{i.machine_id}") if i.machine_id is not None else "—",
        "inspector": i.inspector,
        "inspected": i.inspected_quantity,
        "failed": i.failed_quantity,
        "at": i.created_at.isoformat() if i.created_at else None,
    } for i in recent]

    return {
        "category": category,
        "inspections": len(insp),
        "failed": sum(i.failed_quantity or 0 for i in insp),
        "rework": sum(i.rework_quantity or 0 for i in insp),
        "scrap": sum(i.scrap_quantity or 0 for i in insp),
        "by_machine": by_machine,
        "recent": inspections,
    }


def _half_of(day, today):
    """Which half a day falls in: 'current' = the last WINDOW_DAYS including
    today, 'prior' = the WINDOW_DAYS before that, None = outside the window."""
    age = (today - day).days
    if 0 <= age < WINDOW_DAYS:
        return "current"
    if WINDOW_DAYS <= age < TREND_WINDOW_DAYS:
        return "prior"
    return None


def build_quality_trend(db, tenant: str) -> dict:
    """Which way is quality going, and who moved it? Compares the last 7 days of
    inspections against the 7 before on the same numerator and denominator, then
    attributes the swing to machines and defect categories. Composes
    quality_inspections (auto-scoped, ADR-0002); it adds no storage."""
    today = datetime.utcnow().date()
    inspections = _inspections_since(db, TREND_WINDOW_DAYS)

    # Daily series across the whole window, zero-filled so a silent day reads as
    # a gap in inspection rather than as perfect quality.
    days = [today - timedelta(days=n) for n in range(TREND_WINDOW_DAYS - 1, -1, -1)]
    daily = {d: {"inspected": 0, "failed": 0} for d in days}
    halves = {"current": {"inspected": 0, "failed": 0, "inspections": 0},
              "prior": {"inspected": 0, "failed": 0, "inspections": 0}}
    per_machine: dict = defaultdict(lambda: {
        "current": {"inspected": 0, "failed": 0}, "prior": {"inspected": 0, "failed": 0}})
    per_defect: dict = defaultdict(lambda: {"current": 0, "prior": 0})

    for i in inspections:
        if not i.created_at:
            continue
        day = i.created_at.date()
        half = _half_of(day, today)
        if half is None:
            continue
        inspected, failed = i.inspected_quantity or 0, i.failed_quantity or 0
        if day in daily:
            daily[day]["inspected"] += inspected
            daily[day]["failed"] += failed
        halves[half]["inspected"] += inspected
        halves[half]["failed"] += failed
        halves[half]["inspections"] += 1
        if i.machine_id is not None:
            per_machine[i.machine_id][half]["inspected"] += inspected
            per_machine[i.machine_id][half]["failed"] += failed
        if failed:
            per_defect[_norm_defect(i)][half] += failed

    series = [{"date": d.isoformat(), "inspected": a["inspected"], "failed": a["failed"],
               "fail_rate": _rate1(a["failed"], a["inspected"])}
              for d, a in ((d, daily[d]) for d in days)]

    def _half(key):
        a = halves[key]
        return {"inspections": a["inspections"], "inspected": a["inspected"], "failed": a["failed"],
                # Displayed level uses the SAME integer rounding as build_quality_summary's
                # fail_rate — it's the identical 7-day plant number, and this card sits
                # right beside that one, so they must not show 4% vs 3.5%.
                "fail_rate": _pct(a["failed"], a["inspected"])}

    current, prior = _half("current"), _half("prior")
    comparable = current["inspected"] > 0 and prior["inspected"] > 0
    # Movement is measured on the UNROUNDED rates so a sub-one-point drift isn't lost
    # to the integer rounding of the displayed levels above.
    cur_exact = _rate1(halves["current"]["failed"], halves["current"]["inspected"])
    pri_exact = _rate1(halves["prior"]["failed"], halves["prior"]["inspected"])
    delta_pts = round(cur_exact - pri_exact, 1) if comparable else None

    if delta_pts is None:
        direction = "unknown"
    elif delta_pts >= DRIFT_PTS:
        direction = "worsening"
    elif delta_pts <= -DRIFT_PTS:
        direction = "improving"
    else:
        direction = "steady"

    # Units the drift costs at this week's volume — the swing priced in scrap-or-
    # rework units rather than in percentage points nobody can act on.
    units_swing = round(current["inspected"] * delta_pts / 100) if delta_pts else 0

    names = {m.id: m.name for m in db.query(models.Machine).all()}
    movers = []
    for mid, a in per_machine.items():
        # Both halves need real volume, or the "movement" is just a small sample.
        if a["current"]["inspected"] < MIN_MACHINE_UNITS or a["prior"]["inspected"] < MIN_MACHINE_UNITS:
            continue
        cur = _rate1(a["current"]["failed"], a["current"]["inspected"])
        pri = _rate1(a["prior"]["failed"], a["prior"]["inspected"])
        movers.append({
            "machine_id": mid, "name": names.get(mid, f"#{mid}"),
            "fail_rate": cur, "prior_fail_rate": pri, "delta_pts": round(cur - pri, 1),
            "inspected": a["current"]["inspected"], "failed": a["current"]["failed"],
        })
    movers.sort(key=lambda m: m["delta_pts"], reverse=True)
    all_drifting = [m for m in movers if m["delta_pts"] >= DRIFT_PTS]
    drifting = all_drifting[:TOP_N]          # capped display list
    drifting_count = len(all_drifting)       # true count (drifting is a page, not a total)
    improving = [m for m in reversed(movers) if m["delta_pts"] <= -DRIFT_PTS][:TOP_N]
    # Machines that moved but whose volume was too thin in one half to score —
    # named, not silently dropped, so the card can say what it could not judge.
    unscored = sum(1 for a in per_machine.values()
                   if min(a["current"]["inspected"], a["prior"]["inspected"]) < MIN_MACHINE_UNITS)

    defects = [{"category": c, "failed": a["current"], "prior_failed": a["prior"],
                "delta": a["current"] - a["prior"],
                "is_new": a["prior"] == 0 and a["current"] > 0}
               for c, a in per_defect.items() if a["current"] or a["prior"]]
    defects.sort(key=lambda d: (d["delta"], d["failed"]), reverse=True)

    thinner = min(current["inspected"], prior["inspected"])
    thin = thinner < MIN_SAMPLE_UNITS
    worst = drifting[0] if drifting else None
    now_rate = current["fail_rate"]

    if not comparable:
        verdict, tone = (f"Only one week of inspection history — nothing to compare "
                         f"{now_rate}% against yet.", "warn")
    elif direction == "worsening":
        blame = f" — {worst['name']} moved most (+{worst['delta_pts']} pts)" if worst else ""
        cost = f", {abs(units_swing)} more units failing at this volume" if units_swing else ""
        verdict, tone = (f"Fail rate up {abs(delta_pts)} pts to {now_rate}%{blame}{cost}.", "bad")
    elif direction == "improving":
        verdict, tone = (f"Fail rate down {abs(delta_pts)} pts to {now_rate}% week on week.", "good")
    else:
        verdict, tone = (f"Fail rate steady at {now_rate}% ({delta_pts:+} pts week on week).", "good")

    # A swing off a handful of units is arithmetic, not a signal. Report it,
    # don't call it a verdict.
    if thin and comparable and direction != "steady":
        verdict = (f"Fail rate moved {delta_pts:+} pts to {now_rate}%, but on {thinner} units "
                   f"in the thinner week — too little to call a trend.")
        tone = "warn"

    return {
        "days": TREND_WINDOW_DAYS,
        "half_days": WINDOW_DAYS,
        "current": current,
        "prior": prior,
        "delta_pts": delta_pts,
        "direction": direction,
        "units_swing": units_swing,
        "thin_sample": thin,
        "drift_threshold_pts": DRIFT_PTS,
        "series": series,
        "drifting": drifting,
        "drifting_count": drifting_count,
        "improving": improving,
        "unscored_machines": unscored,
        "defect_movers": defects[:TOP_N],
        "verdict": verdict,
        "tone": tone,
    }
