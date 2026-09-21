"""Shift performance — attainment by shift as a read-model (ADR-0007).

Answers "which shift is hitting its numbers?": over the last week it rolls the
shift-output log up by shift (A / B / C), comparing actual vs target output as an
attainment %, and flags the best and worst shift. A read-model over shift_data,
auto-scoped to the tenant (ADR-0002); it adds no storage.
"""
from collections import defaultdict

import oee_contract
import shift_contract
# The one formula (None without a target), shared with every other shift figure.
from analytics_engine import shift_attainment as _attainment

name = "shift"

# The canonical week, the same window the plant OEE pools. This module used to
# keep its own rolling cutoff (`utcnow() - 7 days`, no upper bound) while the
# analytics rollups pooled all time or the last 50 rows — see shift_contract.
WINDOW_DAYS = oee_contract.DEFAULT_WINDOW_DAYS


def _base_shift(shift_name: str) -> str:
    """Shift entries are logged as "Shift A - 17 Jul"; group by the base name.
    None/blank-safe: a missing name collapses to "" rather than crashing the
    separator scan."""
    name = (shift_name or "").strip()
    for sep in (" – ", " - ", " — "):   # en dash, hyphen, em dash
        if sep in name:
            return name.split(sep)[0].strip()
    return name


def build_shift_summary(db, tenant: str, now=None) -> dict:
    """Attainment (actual vs target) per shift over the canonical week, with the
    best and worst shift. shift_data is auto-scoped (ADR-0002).

    The headline is `shift_contract.pooled_attainment` — the same function and
    window every plant-wide attainment reads — and the per-shift breakdown is
    the rows of that same window, so it always sums to the headline."""
    window = oee_contract.OeeWindow(WINDOW_DAYS, now=now)
    plant = shift_contract.pooled_attainment(db, tenant, window)
    rows = shift_contract.rows_in(db, window)

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
    # Best/worst rank only the shifts with a target to measure against — an
    # unplanned shift (no target) has no attainment and must never surface as the
    # "worst" performer just because its rate defaulted to 0.
    measurable = [s for s in shifts if s["attainment"] is not None]
    return {
        "days": plant["days"],
        "window": plant["window"],
        "entries": plant["entries"],
        # None (not 0%) when nothing had a target in the window: no plan, no
        # attainment. target/actual stay the true grand totals of the window.
        "attainment": plant["attainment"],
        "measured": plant["measured"],
        "target": plant["target"],
        "actual": plant["actual"],
        "shifts": shifts,
        "best": max(measurable, key=lambda s: s["attainment"], default=None),
        "worst": min(measurable, key=lambda s: s["attainment"], default=None),
    }
