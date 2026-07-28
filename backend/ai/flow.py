"""Work-in-progress flow — the two-line material pipeline as a read-model (ADR-0007).

Answers "where is my work right now?": it groups the tenant's work orders by
material state — RAW (queued for the SMT line), SEMI (surface-mounted, now on
the IC line), FIN (finished) — with counts and quantities, so the exec home can
draw the RAW -> SMT -> SEMI -> IC -> FIN pipeline. A read-model over work_orders,
auto-scoped to the tenant (ADR-0002); it adds no storage.

``build_wip_aging`` asks the follow-up question the pipeline snapshot can't:
*how long has the open work been sitting, and which of it has blown its
promise?* Age is measured from ``created_at`` (a real timestamp); lateness is
judged against ``planned_end`` (a real in-data deadline). The schema carries no
completion timestamp, so this model makes NO cycle-time or throughput claims —
it scores only the open backlog it can honestly measure (ADR-0007).
"""
from datetime import datetime

from sqlalchemy import func

import models

name = "flow"

# The material states in flow order, and the line each one is processed on.
_STAGES = [
    {"key": "RAW",  "label": "Raw",      "line": "SMT", "note": "queued for SMT"},
    {"key": "SEMI", "label": "Semi",     "line": "IC",  "note": "on the IC line"},
    {"key": "FIN",  "label": "Finished", "line": "",    "note": "packed"},
]


def build_flow_summary(db, tenant: str) -> dict:
    """Work orders grouped by material state (RAW -> SEMI -> FIN) with counts and
    quantities, so the UI can draw the two-line WIP pipeline. work_orders is
    auto-scoped (ADR-0002)."""
    # Counts and sums per material state — pure aggregation, so it belongs in the
    # database rather than hydrating every order ever raised. work_orders grows
    # one row per production order forever; this view only ever shows six
    # numbers. The state fold stays in Python because it runs over the handful of
    # groups that come back, not over the table.
    grouped = db.query(
        models.WorkOrder.material_state,
        func.count(models.WorkOrder.id),
        func.coalesce(func.sum(models.WorkOrder.target_quantity), 0),
        func.coalesce(func.sum(models.WorkOrder.actual_quantity), 0),
    ).group_by(models.WorkOrder.material_state).all()

    agg = {s["key"]: {"count": 0, "target": 0, "actual": 0} for s in _STAGES}
    total = 0
    for state, count, target, actual in grouped:
        key = state if state in agg else "RAW"   # unknown / NULL state reads as intake
        agg[key]["count"] += count
        agg[key]["target"] += target
        agg[key]["actual"] += actual
        total += count

    stages = [{**s, **agg[s["key"]]} for s in _STAGES]
    return {
        "total": total,
        "wip": agg["RAW"]["count"] + agg["SEMI"]["count"],   # not yet finished
        "finished": agg["FIN"]["count"],
        "stages": stages,
    }


TOP_N = 8
STALE_DAYS = 14        # an open WO older than this is festering, not fresh
# Terminal states — matched lowercased so vocabulary drift ("Complete"/"Closed")
# can't silently keep a finished order in the open backlog.
_CLOSED = {"completed", "complete", "done", "closed", "cancelled", "canceled"}
AGING_BUCKETS = [("0-3 days", 0, 3), ("4-7 days", 4, 7), ("8-14 days", 8, 14), ("15+ days", 15, None)]


def _is_closed(status) -> bool:
    return (status or "").strip().lower() in _CLOSED


def _bucket(days: int) -> str:
    for label, lo, hi in AGING_BUCKETS:
        if days >= lo and (hi is None or days <= hi):
            return label
    return AGING_BUCKETS[0][0]


def build_wip_aging(db, tenant: str) -> dict:
    """The open work-order backlog scored by what the data can honestly measure:
    each open WO's age (from created_at), whether it has blown its planned_end
    (late — a real in-data deadline; a WO with no planned_end is reported in
    ``undated`` rather than assumed on time), an aging profile, a per-material-
    state breakdown (where work stalls: RAW = intake, SEMI = stuck between
    lines), and an oldest-first chase list with fill progress. work_orders is
    auto-scoped (ADR-0002); adds no storage. Empty-safe. The aging buckets and
    the per-state counts each sum to ``open`` (rule 3, asserted in tests)."""
    now = datetime.utcnow()
    today = now.date()
    # Open WIP is bounded by how much work is actually in the plant; work_orders
    # is not, and grows forever. Narrow to the open ones in SQL rather than
    # hydrating every order ever raised to discard almost all of them.
    #
    # The SQL predicate is deliberately WIDER than _is_closed, not equal to it,
    # because the only mistake that changes the numbers is dropping a row Python
    # would have KEPT. Two things make that easy to get wrong:
    #
    #   * coalesce is not cosmetic. `NULL NOT IN (...)` is UNKNOWN, not TRUE, so
    #     without it a work order whose status is NULL — which a migration, raw
    #     SQL, or an update that cleared the field can leave behind — silently
    #     disappears from the open backlog. Verified: removing the coalesce drops
    #     `open` from 7 to 6 in the parity fixture.
    #   * SQL trim() removes spaces while Python .strip() removes all whitespace,
    #     so "\tCompleted" is closed to Python and not-obviously-closed to SQL.
    #
    # Erring wide covers both: SQL only has to drop the bulk, and _is_closed
    # below still makes every final call over the small result it returns.
    maybe_open = (
        db.query(models.WorkOrder)
        .filter(
            func.lower(func.trim(func.coalesce(models.WorkOrder.status, ""))).notin_(
                sorted(_CLOSED)
            )
        )
        .all()
    )
    open_wos = [w for w in maybe_open if not _is_closed(w.status)]

    def _age(w) -> int:
        base = w.created_at or now
        return max(0, (today - base.date()).days)

    late = [w for w in open_wos if w.planned_end and w.planned_end < now]
    undated = sum(1 for w in open_wos if not w.planned_end)
    aging: dict = {}
    state_agg: dict = {}
    for w in open_wos:
        a = _age(w)
        aging[_bucket(a)] = aging.get(_bucket(a), 0) + 1
        st = w.material_state if w.material_state in ("RAW", "SEMI", "FIN") else "RAW"
        s = state_agg.setdefault(st, {"count": 0, "age_sum": 0, "oldest": 0})
        s["count"] += 1
        s["age_sum"] += a
        s["oldest"] = max(s["oldest"], a)

    oldest_days = max((_age(w) for w in open_wos), default=None)
    stale = sum(1 for w in open_wos if _age(w) > STALE_DAYS)

    chase = sorted(open_wos, key=lambda w: (-_age(w), w.work_order_no or ""))[:TOP_N]
    chase_rows = [{
        "work_order_no": w.work_order_no,
        "part_number": w.part_number,
        "material_state": w.material_state,
        "status": w.status,
        "age_days": _age(w),
        "late": bool(w.planned_end and w.planned_end < now),
        "planned_end": w.planned_end.isoformat() if w.planned_end else None,
        "target_quantity": w.target_quantity or 0,
        "actual_quantity": w.actual_quantity or 0,
        "progress": (round((w.actual_quantity or 0) / w.target_quantity * 100)
                     if w.target_quantity else None),
    } for w in chase]

    by_state = [{
        "state": st,
        "count": s["count"],
        "avg_age_days": round(s["age_sum"] / s["count"], 1) if s["count"] else 0.0,
        "oldest_days": s["oldest"],
    } for st, s in sorted(state_agg.items(), key=lambda kv: kv[0])]
    aging_rows = [{"bucket": label, "count": aging[label]}
                  for label, _, _ in AGING_BUCKETS if aging.get(label)]

    n_late = len(late)
    if not open_wos:
        verdict, tone = "No open work orders — the backlog is clear.", "good"
    elif n_late:
        verdict = (f"{n_late} open work order{'s' if n_late != 1 else ''} past "
                   f"{'their' if n_late != 1 else 'its'} planned end"
                   + (f", oldest open {oldest_days} days" if oldest_days else "") + ".")
        tone = "bad"
    elif stale:
        verdict = (f"{stale} work order{'s' if stale != 1 else ''} open longer than "
                   f"{STALE_DAYS} days (oldest {oldest_days}).")
        tone = "warn"
    else:
        verdict = f"{len(open_wos)} open work order{'s' if len(open_wos) != 1 else ''}, all fresh, none late."
        tone = "good"

    return {
        "open": len(open_wos),
        "late": n_late,
        "undated": undated,          # open WOs with no planned_end — can't be judged late
        "stale": stale,
        "stale_days": STALE_DAYS,
        "oldest_days": oldest_days,
        "aging": aging_rows,
        "by_state": by_state,
        "chase": chase_rows,
        "verdict": verdict,
        "tone": tone,
    }
