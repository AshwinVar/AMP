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


def _daily_oee(records, days: int) -> list:
    """Plant OEE per calendar day over the window (oldest -> newest), so the card
    can draw a trend. Each day pools that day's records; a day with no production
    reads 0 (nothing ran)."""
    today = datetime.utcnow().date()
    window = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    by_day: dict = {}
    for r in records:
        if r.created_at:
            by_day.setdefault(r.created_at.date(), []).append(r)
    return [{"date": d.isoformat(), "oee": _oee_from_records(by_day[d])["oee"] if d in by_day else 0}
            for d in window]


def build_oee_summary(db, tenant: str) -> dict:
    """Plant-level OEE over the last 7 days, a daily trend, and a worst-first
    per-machine breakdown. The plant figure pools every machine's minutes and
    counts (so it weights by real output, not a naive average of averages).
    production_records and machines are auto-scoped (ADR-0002)."""
    records = _recent_production(db, days=WINDOW_DAYS)
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

    return {
        "days": WINDOW_DAYS,
        "world_class": WORLD_CLASS,
        "plant": plant,                       # {oee, availability, performance, quality, has_data}
        "machine_count": len(names),
        "machines_with_data": len(machines),
        "biggest_drag": drag,                 # the component pulling plant OEE down
        "daily": _daily_oee(records, WINDOW_DAYS),  # 7-day OEE trend
        "by_line": by_line,                   # OEE per production line (SMT / IC)
        "worst": machines[0] if machines else None,
        "best": machines[-1] if machines else None,
        "machines": machines,
    }


def _split_halves(records, today):
    """Split production records into the current WINDOW_DAYS (including today) and
    the WINDOW_DAYS before it, dropping anything outside the 14-day window. Mirrors
    the half-split the downtime / quality / cost trends use."""
    current, prior = [], []
    for r in records:
        if not r.created_at:
            continue
        age = (today - r.created_at.date()).days
        if 0 <= age < WINDOW_DAYS:
            current.append(r)
        elif WINDOW_DAYS <= age < TREND_WINDOW_DAYS:
            prior.append(r)
    return current, prior


def _by_machine(records):
    grouped: dict = defaultdict(list)
    for r in records:
        if r.machine_id is not None:
            grouped[r.machine_id].append(r)
    return grouped


def build_oee_trend(db, tenant: str) -> dict:
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
    today = datetime.utcnow().date()
    records = _recent_production(db, days=TREND_WINDOW_DAYS)
    cur_recs, pri_recs = _split_halves(records, today)
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

    # Per-machine OEE each half, pooled from that half's own records, so a machine
    # that stopped running this week reads 0 rather than carrying last week's number.
    cur_bm, pri_bm = _by_machine(cur_recs), _by_machine(pri_recs)
    machines = []
    for mid in set(cur_bm) | set(pri_bm):
        c_oee = _oee_from_records(cur_bm[mid])["oee"] if mid in cur_bm else 0
        p_oee = _oee_from_records(pri_bm[mid])["oee"] if mid in pri_bm else 0
        machines.append({"machine_id": mid, "name": names.get(mid, f"#{mid}"),
                         "oee": c_oee, "prior_oee": p_oee, "delta": c_oee - p_oee})
    improving_machines = sorted((m for m in machines if m["delta"] >= OEE_TREND_DEAD_BAND),
                                key=lambda m: m["delta"], reverse=True)[:TOP_N]
    declining_machines = sorted((m for m in machines if m["delta"] <= -OEE_TREND_DEAD_BAND),
                                key=lambda m: m["delta"])[:TOP_N]

    now = current["oee"]
    if not has_cur and not has_prior:
        direction, verdict, tone = "none", "No production recorded in the last 14 days.", "warn"
    elif not has_cur:
        direction, tone = "unknown", "warn"
        verdict = f"No production recorded this week (OEE was {prior['oee']}% last week)."
    elif not has_prior:
        direction, tone = "unknown", "warn"
        verdict = f"Only one week of production — OEE {now}%, nothing to compare yet."
    else:
        dir_raw = oee_direction(current["oee"], prior["oee"])   # up / down / flat
        if dir_raw == "up":
            direction, tone = "improving", "good"
            verdict = f"OEE up {delta} pts to {now}% week on week."
        elif dir_raw == "down":
            direction, tone = "worsening", "bad"
            blame = (f" — {worst_comp['label']} fell {abs(worst_comp['delta'])} pts"
                     if worst_comp and worst_comp["delta"] < 0 else "")
            verdict = f"OEE down {abs(delta)} pts to {now}%{blame} week on week."
        else:
            direction, tone = "steady", "good"
            verdict = f"OEE steady at {now}% ({delta:+} pts week on week)."

    return {
        "days": TREND_WINDOW_DAYS,
        "half_days": WINDOW_DAYS,
        "current": current,
        "prior": prior,
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
