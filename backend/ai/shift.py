"""Shift performance — attainment by shift as a read-model (ADR-0007).

Answers "which shift is hitting its numbers?": over the last week it rolls the
shift-output log up by shift (A / B / C), comparing actual vs target output as an
attainment %, and flags the best and worst shift. A read-model over shift_data,
auto-scoped to the tenant (ADR-0002); it adds no storage.
"""
from collections import defaultdict
from datetime import datetime, timedelta

import models

name = "shift"

WINDOW_DAYS = 7


def _attainment(actual, target):
    """Actual as a % of target, or None when there's no target to measure
    against. A shift with no planned output has no attainment — scoring it 0%
    would flag an unplanned shift as the "worst" performer (ADR-0007: never
    present a metric the data can't support)."""
    return round(actual / target * 100) if target else None


def _base_shift(shift_name: str) -> str:
    """Shift entries are logged as "Shift A - 17 Jul"; group by the base name.
    None/blank-safe: a missing name collapses to "" rather than crashing the
    separator scan."""
    name = (shift_name or "").strip()
    for sep in (" – ", " - ", " — "):   # en dash, hyphen, em dash
        if sep in name:
            return name.split(sep)[0].strip()
    return name


def build_shift_summary(db, tenant: str) -> dict:
    """Attainment (actual vs target) per shift over the last 7 days, with the
    best and worst shift. shift_data is auto-scoped (ADR-0002)."""
    # Windowed in SQL — shift_data grows continuously.
    cutoff = datetime.utcnow() - timedelta(days=WINDOW_DAYS)
    rows = db.query(models.ShiftData).filter(models.ShiftData.created_at >= cutoff).all()

    agg: dict = defaultdict(lambda: {"target": 0, "actual": 0, "entries": 0})
    for s in rows:
        b = _base_shift(s.shift_name)
        agg[b]["target"] += s.target_output or 0
        agg[b]["actual"] += s.actual_output or 0
        agg[b]["entries"] += 1

    shifts = sorted(
        ({"shift": b, "target": v["target"], "actual": v["actual"], "entries": v["entries"],
          "attainment": _attainment(v["actual"], v["target"])} for b, v in agg.items()),
        key=lambda x: x["shift"],
    )
    total_target = sum(s["target"] for s in shifts)
    total_actual = sum(s["actual"] for s in shifts)
    # Best/worst rank only the shifts with a target to measure against — an
    # unplanned shift (no target) has no attainment and must never surface as the
    # "worst" performer just because its rate defaulted to 0.
    measurable = [s for s in shifts if s["attainment"] is not None]
    return {
        "days": WINDOW_DAYS,
        "entries": len(rows),
        # None (not 0%) when nothing had a target in the window: no plan, no
        # attainment. total_target/total_actual stay the true grand totals.
        "attainment": _attainment(total_actual, total_target),
        "target": total_target,
        "actual": total_actual,
        "shifts": shifts,
        "best": max(measurable, key=lambda s: s["attainment"], default=None),
        "worst": min(measurable, key=lambda s: s["attainment"], default=None),
    }
