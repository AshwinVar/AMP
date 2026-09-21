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

THE WINDOW IS NOT THIS MODULE'S TO CHOOSE. Every figure below is measured over
`oee_contract.OeeWindow` and pooled by `quality_contract` — the same half-open
week the plant OEE, the cost trend, the shift attainment and the machine
cockpit are on. This module used to cut its own: `created_at >= midnight(today
- 6)` with no upper bound, which is a different set of rows from `[now - 7d,
now)` at BOTH ends, while two of the three surfaces publishing "fail rate" were
on no window at all. The trend's halves come from `prior_window`, so they tile
exactly and the current half IS the summary's figure rather than a second
computation of it.
"""
from collections import Counter, defaultdict
from datetime import datetime

import models
import oee_contract
import quality_contract

name = "quality"

TOP_N = 5
# The canonical reporting week, not a private copy of the number (#586).
WINDOW_DAYS = oee_contract.DEFAULT_WINDOW_DAYS

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


def _window(now=None):
    """The window every figure in this module is measured over."""
    return oee_contract.OeeWindow(WINDOW_DAYS, now=now)


def _norm_defect(i) -> str:
    return (i.defect_category or "Unspecified").strip() or "Unspecified"


def build_quality_summary(db, tenant: str, now=None) -> dict:
    """First-pass yield, fail rate, a defect Pareto, and the worst machines by
    fail rate — over the canonical window's inspections.

    The plant totals and both rates come from `quality_contract.plant_quality`,
    which is what `/analytics/quality` and the digital twin's tile read too, so
    the three surfaces cannot publish three different fail rates again. The
    breakdowns are rolled up from the same window's rows (auto-scoped,
    ADR-0002). `now`: the instant the window ends at, for a composing caller."""
    window = _window(now)
    plant = quality_contract.plant_quality(db, tenant, window)
    inspections = quality_contract.rows_in(db, window)

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
         "inspected": a["inspected"], "failed": a["failed"],
         "fail_rate": quality_contract.rate(a["failed"], a["inspected"])}
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
         "fail_rate": quality_contract.rate(a["failed"], a["inspected"])}
        for ln, a in sorted(line_agg.items()) if a["inspected"] > 0
    ]

    return {
        "inspections": plant["inspections"],
        "inspected": plant["inspected"],
        "passed": plant["passed"],
        "failed": plant["failed"],
        "rework": plant["rework"],
        "scrap": plant["scrap"],
        # None when the window inspected no units at all. 0% is the BEST value
        # on a fail-rate scale, so a plant that stopped inspecting used to read
        # as a plant making nothing wrong (quality_contract.rate).
        "first_pass_yield": plant["first_pass_yield"],
        "fail_rate": plant["fail_rate"],
        "measured": plant["measured"],
        "window": plant["window"],
        "days": plant["days"],
        "top_defects": top_defects,
        "by_machine": by_machine[:TOP_N],
        "by_line": by_line,
    }


def build_defect_detail(db, tenant: str, category: str, now=None) -> dict:
    """Drill-down for a single defect category: the units it has failed (with the
    rework/scrap split), the machines producing it, and the inspections that
    caught it. Composes quality_inspections (auto-scoped, ADR-0002); adds no
    storage. Windowed to the same 7 days as the summary. Returns a zeroed shape
    when the category has no failures."""
    insp = [
        i for i in quality_contract.rows_in(db, _window(now))
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


def build_quality_trend(db, tenant: str, now=None) -> dict:
    """Which way is quality going, and who moved it? Compares the canonical
    window against the one immediately before it, on the same numerator and
    denominator, then attributes the swing to machines and defect categories.

    THE HALVES TILE. They are `window` and `oee_contract.prior_window(window)`,
    which share a boundary instant and no row, and the current half is
    `plant_quality` over that window — the SAME call the summary card beside
    this one makes, so the two cards cannot disagree by construction. They used
    to be classified by calendar-day age, which is a different split from the
    rolling window every other figure on the page was measured over.

    `now`: the instant the window ends at (a composing read-model's clock)."""
    window = _window(now)
    prior_w = oee_contract.prior_window(window)
    # Each half is queried on its own bounds rather than classified in Python:
    # the boundary rule lives in ONE place, the SQL filter (OeeWindow's
    # docstring is explicit that a second implementation is how definitions
    # drift apart).
    cur_plant = quality_contract.plant_quality(db, tenant, window)
    pri_plant = quality_contract.plant_quality(db, tenant, prior_w)
    cur_rows = quality_contract.rows_in(db, window)
    pri_rows = quality_contract.rows_in(db, prior_w)

    # The calendar dates the fortnight touches, oldest first — fifteen when the
    # window opens mid-day, and the oldest is flagged `partial` exactly as the
    # cost and cockpit series are (#587, #590). A day bucket is a rendering of
    # the rows, not a second definition of the halves.
    span, opens_mid_day = oee_contract.window_span(
        oee_contract.OeeWindow(TREND_WINDOW_DAYS, now=window.end))
    daily = {d: {"inspected": 0, "failed": 0} for d in span}
    per_machine: dict = defaultdict(lambda: {
        "current": {"inspected": 0, "failed": 0}, "prior": {"inspected": 0, "failed": 0}})
    per_defect: dict = defaultdict(lambda: {"current": 0, "prior": 0})

    for half, rows in (("prior", pri_rows), ("current", cur_rows)):
        for i in rows:
            if not i.created_at:
                continue
            inspected, failed = i.inspected_quantity or 0, i.failed_quantity or 0
            day = i.created_at.date()
            if day in daily:
                daily[day]["inspected"] += inspected
                daily[day]["failed"] += failed
            if i.machine_id is not None:
                per_machine[i.machine_id][half]["inspected"] += inspected
                per_machine[i.machine_id][half]["failed"] += failed
            if failed:
                per_defect[_norm_defect(i)][half] += failed

    # A day that inspected nothing has NO fail rate — it is a gap in inspection,
    # and drawing it as 0.0 draws it as a perfect day (quality_contract.rate1).
    series = [{"date": d.isoformat(), "inspected": daily[d]["inspected"],
               "failed": daily[d]["failed"],
               "fail_rate": quality_contract.rate1(daily[d]["failed"], daily[d]["inspected"]),
               **({"partial": True} if (i == 0 and opens_mid_day) else {})}
              for i, d in enumerate(span)]
    # The card shades its halves apart from `half_days`. This used to publish a
    # `current_from` index computed from the span, which mutation testing then
    # proved could never differ from it: the span starts on the date
    # `prior_window.start` falls on and `window.start` is exactly WINDOW_DAYS
    # later, so the index is always WINDOW_DAYS — at every instant tried,
    # including midnight and one microsecond past it. A second expression of a
    # number already in the payload is what OeeWindow's docstring warns about.

    def _half(plant):
        return {"inspections": plant["inspections"], "inspected": plant["inspected"],
                "failed": plant["failed"],
                # The identical integer rounding build_quality_summary publishes —
                # this card sits right beside that one, so they must not show
                # 4% vs 3.5% — and None when the half inspected nothing.
                "fail_rate": plant["fail_rate"], "measured": plant["measured"]}

    current, prior = _half(cur_plant), _half(pri_plant)
    comparable = cur_plant["measured"] and pri_plant["measured"]
    # Movement is measured on the UNROUNDED rates so a sub-one-point drift isn't lost
    # to the integer rounding of the displayed levels above.
    delta_pts = None
    if comparable:
        cur_exact = quality_contract.rate1(cur_plant["failed"], cur_plant["inspected"])
        pri_exact = quality_contract.rate1(pri_plant["failed"], pri_plant["inspected"])
        delta_pts = round(cur_exact - pri_exact, 1)

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
        cur = quality_contract.rate1(a["current"]["failed"], a["current"]["inspected"])
        pri = quality_contract.rate1(a["prior"]["failed"], a["prior"]["inspected"])
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

    if not current["measured"]:
        # No units inspected this week: there is no rate to report, and the
        # sentence must not print one. This read "nothing to compare 0% against".
        verdict, tone = ("No units were inspected in the last "
                         f"{WINDOW_DAYS} days — quality is not measured.", "warn")
    elif not comparable:
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
        "window": window.label(),
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
