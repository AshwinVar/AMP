"""Quality summary — a fleet-wide read-model over inspections (ADR-0007).

Answers "how good is what we're making?": across the tenant's quality
inspections it rolls up the first-pass yield, the fail rate, the top defect
categories (a Pareto), and the machines with the worst quality. A read-model
over quality_inspections — auto-scoped to the tenant by the query layer
(ADR-0002); it adds no storage.
"""
from collections import Counter, defaultdict
from datetime import datetime

import models

name = "quality"

TOP_N = 5


def _pct(part, whole):
    return round(part / whole * 100) if whole else 0


def _norm_defect(i) -> str:
    return (i.defect_category or "Unspecified").strip() or "Unspecified"


def build_quality_summary(db, tenant: str) -> dict:
    """First-pass yield, fail rate, a defect Pareto, and the worst machines by
    fail rate — over the tenant's inspections (auto-scoped, ADR-0002)."""
    inspections = db.query(models.QualityInspection).all()
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
    storage. Returns a zeroed shape when the category has no failures."""
    insp = [
        i for i in db.query(models.QualityInspection).all()
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
