"""OEE summary — the plant's headline metric as a read-model (ADR-0007).

Answers "how effective is the plant right now?": it rolls the last week of
production into one plant-level OEE (Availability x Performance x Quality) with
its three components, flags the component dragging OEE down, and ranks every
machine that has production so the worst is first for triage. A read-model over
production_records + machines — auto-scoped to the tenant by the query layer
(ADR-0002); it reuses the per-machine OEE math from the twin (ai/twin.py) and
adds no storage.
"""
from collections import defaultdict
from datetime import datetime, timedelta

import models
import oee_contract
from analytics_engine import (OEE_TREND_DEAD_BAND, WORLD_CLASS_OEE, biggest_lever,
                              oee_direction)
from ai.twin import _oee_from_records, _oee_by_machine, _recent_production

name = "oee"

WINDOW_DAYS = 7
TOP_N = 5
WORLD_CLASS = WORLD_CLASS_OEE  # the classic world-class OEE benchmark (shared)

# Trend window — two WINDOW_DAYS halves ("this week vs last week"), mirroring the
# downtime / quality / cost trend read-models.
TREND_HALVES = 2
TREND_WINDOW_DAYS = WINDOW_DAYS * TREND_HALVES


def _daily_oee(records, window) -> list:
    """Plant OEE per calendar day across `window` (oldest -> newest), so the card
    can draw a trend that ADDS UP to the headline above it.

    The bars are derived from the WINDOW, not from a day count. A rolling 7x24h
    window that starts part-way through a day touches EIGHT calendar dates, and
    the series used to draw only `[today-6 ... today]` -- so every record on that
    partial eighth date was pooled into the headline and appeared in no bar.
    Measured at 11:48 UTC: headline 45%, bars pooling to 83%, and nothing on the
    card accounting for the 38-point gap. See test_oee_bars_explain_headline.py.

    The oldest bar is flagged `partial` when the window opens mid-day, because it
    is: the window covers only part of it. Drawing it as a whole day would move
    the lie rather than remove it.

    KNOWN AND DELIBERATE: a day with no production still reads 0 rather than
    "no data", so an idle day draws as a catastrophic one. Same class as the
    fabricated zeros fixed in #585, but on a typed frontend series -- its own
    change.
    """
    start_date = window.start.date()
    end_date = (window.end - timedelta(microseconds=1)).date()
    span = [start_date + timedelta(days=i)
            for i in range((end_date - start_date).days + 1)]
    # Partial only when the window does not open exactly at midnight; at midnight
    # it tiles seven whole days and nothing is partial.
    opens_mid_day = window.start.time() != datetime.min.time()
    by_day: dict = {}
    for r in records:
        if r.created_at:
            by_day.setdefault(r.created_at.date(), []).append(r)
    # A DAY WITH NO PRODUCTION HAS NO OEE -- it is not a day of 0% OEE.
    #
    # OEE is an intensive ratio; with no runtime and no counts there is nothing
    # to divide. Publishing 0 drew an idle Sunday as a full-height red bar beside
    # a good Monday, so a plant that runs five days a week rendered as a plant
    # failing twice a week. Same rule as the scorecard KPIs in #585: zero means
    # "measured, and it was zero"; None means "nothing to measure".
    return [{"date": d.isoformat(),
             "oee": _oee_from_records(by_day[d])["oee"] if d in by_day else None,
             **({"partial": True} if (i == 0 and opens_mid_day) else {})}
            for i, d in enumerate(span)]


def build_oee_summary(db, tenant: str, now=None) -> dict:
    """Plant-level OEE over the last 7 days, a daily trend, and a worst-first
    per-machine breakdown. The plant figure pools every machine's minutes and
    counts (so it weights by real output, not a naive average of averages).
    production_records and machines are auto-scoped (ADR-0002)."""
    records = _recent_production(db, days=WINDOW_DAYS, now=now)
    plant = _oee_from_records(records)

    all_machines = db.query(models.Machine).all()
    names = {m.id: m.name for m in all_machines}
    line_of = {m.id: (m.line or "") for m in all_machines}
    machines = [
        {"machine_id": mid, "name": names.get(mid, f"#{mid}"), "line": line_of.get(mid, ""), **o}
        for mid, o in _oee_by_machine(db, days=WINDOW_DAYS).items()
        if o["has_data"]
    ]
    machines.sort(key=lambda m: m["oee"])  # worst OEE first, for triage

    # Per-line OEE: pool each line's machines' production (e.g. SMT vs IC).
    line_recs: dict = {}
    for r in records:
        ln = line_of.get(r.machine_id, "")
        if ln:
            line_recs.setdefault(ln, []).append(r)
    by_line = [{"line": ln, **_oee_from_records(recs)} for ln, recs in sorted(line_recs.items())]

    # Which of the three levers is holding the plant back — the story a manager
    # wants first ("we're losing OEE to Performance, not Quality"). The component
    # furthest below its OWN world-class target (shared with the recovery
    # read-model's "biggest lever" so the two cards never disagree), not the
    # lowest raw component — Availability at 90% is already at target.
    drag = biggest_lever(plant) if plant["has_data"] else None

    # How much of the plant this figure actually measured (ADR-0014). The two
    # counts below already existed; what was missing was carrying them WITH the
    # headline as a coverage statement. A machine whose gateway drops stops
    # producing records and silently leaves the pooled denominator — measured,
    # that made a plant look 27 points better (67% -> 94%) the moment its worst
    # machine went offline. The number cannot be corrected (the data is gone),
    # but it must not be presented as a whole-plant figure when it is not.
    coverage = oee_contract.coverage(db, tenant,
                                     oee_contract.OeeWindow(WINDOW_DAYS, now=now))

    return {
        "days": WINDOW_DAYS,
        "world_class": WORLD_CLASS,
        "plant": plant,                       # {oee, availability, performance, quality, has_data}
        "coverage": coverage,                 # {machines_expected, machines_reporting, coverage_pct, complete}
        "machine_count": len(names),
        "machines_with_data": len(machines),
        "biggest_drag": drag,                 # the component pulling plant OEE down
        # Built from the same window the records came from, so the bars tile it
        # and pool back to `plant` above (rule 1: one definition of the window).
        "daily": _daily_oee(records, oee_contract.OeeWindow(WINDOW_DAYS, now=now)),
        "by_line": by_line,                   # OEE per production line (SMT / IC)
        "worst": machines[0] if machines else None,
        "best": machines[-1] if machines else None,
        "machines": machines,
    }


def _by_machine(records):
    grouped: dict = defaultdict(list)
    for r in records:
        if r.machine_id is not None:
            grouped[r.machine_id].append(r)
    return grouped


def build_oee_trend(db, tenant: str, now=None) -> dict:
    """Which way is the plant's OEE going, and what moved it? Compares this week's
    pooled OEE against last week's — same pooled basis as build_oee_summary (ratio
    of summed inputs, so output-weighted, never a mean of per-record ratios) — and
    attributes the swing to the three components (A / P / Q) and to individual
    machines. A read-model over production_records (+ machines for labels),
    auto-scoped to the tenant (ADR-0002); it adds no storage.

    OEE is the composite headline, so this is the direction signal that matters
    most: an owner sees the plant slipping while it is still a couple of points
    rather than after a bad month. Direction uses the shared ``oee_direction``
    dead-band (±2 pts) so the same weekly pair can't read 'down' here and 'flat' on
    another surface. A week with no production can't be scored, so it is reported
    ('unknown') rather than shown as a crash to 0%."""
    # One anchor, two adjacent windows: `current` is THE window /oee-summary pools
    # (oee_contract.OeeWindow) and `prior` tiles against it exactly
    # (oee_contract.prior_window), so "this week" is one set of records on every
    # surface. The calendar-day split this replaces put [midnight(today-6), now)
    # beside /oee-summary's [now-7d, now): a record from late on day 7 was in the
    # headline's week and in this trend's PRIOR week, and the trend's 'current'
    # OEE was a different figure from the headline beside it
    # (test_oee_trend_uses_the_contract_windows.py).
    current_window = oee_contract.OeeWindow(WINDOW_DAYS, now=now)
    prior_window = oee_contract.prior_window(current_window)
    cur_recs = _recent_production(db, days=WINDOW_DAYS, now=current_window.end)
    pri_recs = _recent_production(db, days=WINDOW_DAYS, now=prior_window.end)
    current = _oee_from_records(cur_recs)   # {oee, availability, performance, quality, has_data}
    prior = _oee_from_records(pri_recs)
    names = {m.id: m.name for m in db.query(models.Machine).all()}

    has_cur, has_prior = current["has_data"], prior["has_data"]
    delta = current["oee"] - prior["oee"]

    components = [
        {"component": c, "label": c.capitalize(),
         "current": current[c], "prior": prior[c], "delta": current[c] - prior[c]}
        for c in ("availability", "performance", "quality")
    ]
    # The component that fell the most, for the blame clause (only meaningful when
    # both weeks have data to compare).
    worst_comp = min(components, key=lambda x: x["delta"]) if (has_cur and has_prior) else None
    biggest_drag = biggest_lever(current) if has_cur else None

    # Per-machine OEE each half, pooled from that half's own records. A half with
    # NO records has no OEE -- None, not 0 -- and a machine missing a half has no
    # delta, so it cannot be ranked as a mover.
    #
    # It used to read 0, and the consequence was a RANKING, not just a number: a
    # machine that ran at 80% last week and did not run at all this week scored
    # delta = -80 and topped `declining_machines` as the plant's worst decliner,
    # for not running. A machine commissioned this week scored prior 0 and
    # delta = +85, topping `improving_machines` with an improvement that never
    # happened. Those two lists are what a manager reads to decide where to walk
    # first.
    #
    # The plant-level branches below already got this right ("No production
    # recorded this week (OEE was N% last week)"); this applies the same rule per
    # machine. See test_oee_idle_is_not_zero.py.
    cur_bm, pri_bm = _by_machine(cur_recs), _by_machine(pri_recs)
    machines = []
    for mid in set(cur_bm) | set(pri_bm):
        c_oee = _oee_from_records(cur_bm[mid])["oee"] if mid in cur_bm else None
        p_oee = _oee_from_records(pri_bm[mid])["oee"] if mid in pri_bm else None
        machines.append({
            "machine_id": mid, "name": names.get(mid, f"#{mid}"),
            "oee": c_oee, "prior_oee": p_oee,
            "delta": (c_oee - p_oee) if (c_oee is not None and p_oee is not None) else None,
        })
    movers = [m for m in machines if m["delta"] is not None]
    improving_machines = sorted((m for m in movers if m["delta"] >= OEE_TREND_DEAD_BAND),
                                key=lambda m: m["delta"], reverse=True)[:TOP_N]
    declining_machines = sorted((m for m in movers if m["delta"] <= -OEE_TREND_DEAD_BAND),
                                key=lambda m: m["delta"])[:TOP_N]

    # How much of the plant each week's figure measured (OEE contract s4). A
    # machine that stops reporting leaves the pooled figure, so the week its worst
    # machine went silent read "OEE up 16 pts" in green with nothing saying the
    # plant it described had shrunk (test_every_plant_oee_states_coverage.py). The
    # change is still reported (the missing data cannot be recovered), but every
    # figure the verdict states says when it did not come from every machine, in
    # the one wording every other OEE surface uses.
    cur_cov = oee_contract.coverage(db, tenant, current_window)
    pri_cov = oee_contract.coverage(db, tenant, prior_window)
    cur_from = oee_contract.coverage_phrase(cur_cov) if has_cur else ""
    pri_from = oee_contract.coverage_phrase(pri_cov) if has_prior else ""
    if cur_from and pri_from:
        coverage_note = f" This week's figure is {cur_from}, last week's {pri_from}."
    elif cur_from:
        coverage_note = f" This week's figure is {cur_from}."
    elif pri_from:
        coverage_note = f" Last week's figure is {pri_from}."
    else:
        coverage_note = ""

    oee_now = current["oee"]
    if not has_cur and not has_prior:
        direction, verdict, tone = "none", "No production recorded in the last 14 days.", "warn"
    elif not has_cur:
        direction, tone = "unknown", "warn"
        verdict = f"No production recorded this week (OEE was {prior['oee']}% last week)."
    elif not has_prior:
        direction, tone = "unknown", "warn"
        verdict = f"Only one week of production — OEE {oee_now}%, nothing to compare yet."
    else:
        dir_raw = oee_direction(current["oee"], prior["oee"])   # up / down / flat
        if dir_raw == "up":
            direction, tone = "improving", "good"
            verdict = f"OEE up {delta} pts to {oee_now}% week on week."
        elif dir_raw == "down":
            direction, tone = "worsening", "bad"
            blame = (f" — {worst_comp['label']} fell {abs(worst_comp['delta'])} pts"
                     if worst_comp and worst_comp["delta"] < 0 else "")
            verdict = f"OEE down {abs(delta)} pts to {oee_now}%{blame} week on week."
        else:
            direction, tone = "steady", "good"
            verdict = f"OEE steady at {oee_now}% ({delta:+} pts week on week)."
    verdict += coverage_note

    return {
        "days": TREND_WINDOW_DAYS,
        "half_days": WINDOW_DAYS,
        "current": current,
        "prior": prior,
        "coverage": cur_cov,                  # this week: {machines_expected, machines_reporting, coverage_pct, complete}
        "prior_coverage": pri_cov,            # last week, the same shape
        "delta_pts": delta,
        "direction": direction,
        "dead_band_pts": OEE_TREND_DEAD_BAND,
        "world_class": WORLD_CLASS,
        "biggest_drag": biggest_drag,
        "components": components,
        "improving_machines": improving_machines,
        "declining_machines": declining_machines,
        "verdict": verdict,
        "tone": tone,
    }
