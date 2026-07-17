"""Work-in-progress flow — the two-line material pipeline as a read-model (ADR-0007).

Answers "where is my work right now?": it groups the tenant's work orders by
material state — RAW (queued for the SMT line), SEMI (surface-mounted, now on
the IC line), FIN (finished) — with counts and quantities, so the exec home can
draw the RAW -> SMT -> SEMI -> IC -> FIN pipeline. A read-model over work_orders,
auto-scoped to the tenant (ADR-0002); it adds no storage.
"""
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
    wos = db.query(models.WorkOrder).all()
    agg = {s["key"]: {"count": 0, "target": 0, "actual": 0} for s in _STAGES}
    for w in wos:
        key = w.material_state if w.material_state in agg else "RAW"
        agg[key]["count"] += 1
        agg[key]["target"] += w.target_quantity or 0
        agg[key]["actual"] += w.actual_quantity or 0

    stages = [{**s, **agg[s["key"]]} for s in _STAGES]
    return {
        "total": len(wos),
        "wip": agg["RAW"]["count"] + agg["SEMI"]["count"],   # not yet finished
        "finished": agg["FIN"]["count"],
        "stages": stages,
    }
