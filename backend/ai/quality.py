"""Quality summary — a fleet-wide read-model over inspections (ADR-0007).

Answers "how good is what we're making?": across the tenant's quality
inspections it rolls up the first-pass yield, the fail rate, the top defect
categories (a Pareto), and the machines with the worst quality. A read-model
over quality_inspections — auto-scoped to the tenant by the query layer
(ADR-0002); it adds no storage.
"""
from collections import Counter, defaultdict

import models

name = "quality"

TOP_N = 5


def _pct(part, whole):
    return round(part / whole * 100) if whole else 0


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
            defects[(i.defect_category or "Unspecified").strip() or "Unspecified"] += i.failed_quantity
    top_defects = [{"category": c, "count": n} for c, n in defects.most_common(TOP_N)]

    # Worst machines by fail rate (only those that actually inspected units).
    agg: dict = defaultdict(lambda: {"inspected": 0, "failed": 0})
    for i in inspections:
        if i.machine_id is not None:
            agg[i.machine_id]["inspected"] += i.inspected_quantity or 0
            agg[i.machine_id]["failed"] += i.failed_quantity or 0
    names = {m.id: m.name for m in db.query(models.Machine).all()}
    by_machine = [
        {"machine_id": mid, "name": names.get(mid, f"#{mid}"),
         "inspected": a["inspected"], "failed": a["failed"], "fail_rate": _pct(a["failed"], a["inspected"])}
        for mid, a in agg.items() if a["inspected"] > 0
    ]
    by_machine.sort(key=lambda m: (m["fail_rate"], m["failed"]), reverse=True)

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
    }
