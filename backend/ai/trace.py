"""Work-order traceability — one job's genealogy, end to end (ADR-0007).

Every other read-model looks *across* the plant; this one looks *down* one work
order. It answers the question an auditor, a customer complaint, or a recall
asks: "what actually happened to this batch?" — what was planned and made, on
which machine and shifts, which materials were consumed and which finished goods
came back, what quality found, what stopped the machine while the job was
running, and — the part that matters most in traceability — where the record is
*silent*. A read-model composing work_orders, production_plans,
quality_inspections, inventory_transactions and downtime_logs, all auto-scoped
to the tenant by the query layer (ADR-0002); it adds no storage.
"""
from collections import Counter, defaultdict
from datetime import datetime

import models
from duration import parse_duration_to_minutes

name = "trace"

TOP_N = 10
# Consumption vs receipt vocabularies — the same two seed dialects the coverage
# read-model reconciles (routes issue "Issue"/"Receive"; the simulator "OUT"/"IN").
_CONSUMED = {"out", "issue"}
_RECEIVED = {"in", "receive", "receipt", "return"}
# A job is finished once its status says so — the trace then stops accruing.
_CLOSED_STATUSES = {"completed", "complete", "done", "closed", "finished"}
# Scrap above this share of what was inspected is worth calling out on its own.
SCRAP_ALERT_PCT = 5


def _pct(part, whole):
    return round(part / whole * 100) if whole else 0


def _direction(txn) -> str:
    t = (txn.transaction_type or "").strip().lower()
    if t in _CONSUMED:
        return "consumed"
    if t in _RECEIVED:
        return "received"
    return "adjust"


def _iso(dt):
    return dt.isoformat() if dt else None


def _has_defect_category(inspections) -> bool:
    """True when at least one failing inspection names a defect category — an
    uncategorised failure is a dead end for root-cause analysis."""
    return any(i.failed_quantity and (i.defect_category or "").strip() for i in inspections)


def _empty(work_order_no: str) -> dict:
    """The zeroed shape for a work order that isn't on record in this tenant."""
    return {
        "found": False,
        "work_order_no": work_order_no,
        "part_number": None,
        "batch_number": None,
        "machine": None,
        "status": None,
        "plans": {"count": 0, "planned": 0, "actual": 0, "attainment_rate": 0, "rows": []},
        "quality": {"inspections": 0, "inspected": 0, "passed": 0, "failed": 0, "rework": 0,
                    "scrap": 0, "first_pass_yield": 0, "fail_rate": 0, "defects": [], "rows": []},
        "materials": {"consumed": 0, "received": 0, "rows": []},
        "downtime": {"events": 0, "minutes": 0, "rows": []},
        "timeline": [],
        "gaps": [],
    }


def build_work_order_trace(db, tenant: str, work_order_no: str) -> dict:
    """One work order's full genealogy: the job header and its progress, the
    plans it was scheduled under, the quality it was inspected to, the materials
    issued against it and the goods received from it, the downtime on its machine
    while it ran, a merged timeline, and the traceability gaps in that record.
    Every table is auto-scoped to the tenant (ADR-0002)."""
    wo = (db.query(models.WorkOrder)
          .filter(models.WorkOrder.work_order_no == work_order_no).first())
    if wo is None:
        return _empty(work_order_no)

    now = datetime.utcnow()
    today = now.date()
    machine = db.query(models.Machine).filter(models.Machine.id == wo.machine_id).first()
    target = wo.target_quantity or 0
    actual = wo.actual_quantity or 0
    closed = (wo.status or "").strip().lower() in _CLOSED_STATUSES

    # --- Scheduled: the plans this job was booked under -------------------
    plans = (db.query(models.ProductionPlan)
             .filter(models.ProductionPlan.work_order_id == wo.id).all())
    plans.sort(key=lambda p: (p.plan_date or today, p.plan_no or ""))
    machine_names = {m.id: m.name for m in db.query(models.Machine).all()}
    plan_rows = [{
        "plan_no": p.plan_no,
        "plan_date": p.plan_date.isoformat() if p.plan_date else None,
        "shift_name": p.shift_name,
        "machine": machine_names.get(p.machine_id, "—"),
        "planned": p.planned_quantity or 0,
        "actual": p.actual_quantity or 0,
        "shortfall": max((p.planned_quantity or 0) - (p.actual_quantity or 0), 0),
        "status": p.status,
    } for p in plans]
    plan_planned = sum(r["planned"] for r in plan_rows)
    plan_actual = sum(r["actual"] for r in plan_rows)
    shifts = sorted({p.shift_name for p in plans if p.shift_name})

    # --- Inspected: what quality found on this job ------------------------
    insp = (db.query(models.QualityInspection)
            .filter(models.QualityInspection.work_order_id == wo.id).all())
    insp.sort(key=lambda i: (i.created_at or datetime.min, i.id), reverse=True)
    inspected = sum(i.inspected_quantity or 0 for i in insp)
    passed = sum(i.passed_quantity or 0 for i in insp)
    failed = sum(i.failed_quantity or 0 for i in insp)
    rework = sum(i.rework_quantity or 0 for i in insp)
    scrap = sum(i.scrap_quantity or 0 for i in insp)
    defects: Counter = Counter()
    for i in insp:
        if i.failed_quantity:
            defects[(i.defect_category or "Unspecified").strip() or "Unspecified"] += i.failed_quantity
    insp_rows = [{
        "inspection_no": i.inspection_no,
        "machine": machine_names.get(i.machine_id, "—") if i.machine_id is not None else "—",
        "inspector": i.inspector,
        "inspected": i.inspected_quantity or 0,
        "passed": i.passed_quantity or 0,
        "failed": i.failed_quantity or 0,
        "rework": i.rework_quantity or 0,
        "scrap": i.scrap_quantity or 0,
        "defect_category": i.defect_category,
        "status": i.status,
        "at": _iso(i.created_at),
    } for i in insp[:TOP_N]]

    # --- Material genealogy: what went in, what came back out -------------
    # Both the BOM subscriber and the manual routes stamp the work-order number
    # into the transaction reference — that string *is* the genealogy link.
    txns = (db.query(models.InventoryTransaction)
            .filter(models.InventoryTransaction.reference == wo.work_order_no).all())
    items = {it.id: it for it in db.query(models.InventoryItem).all()}
    per_item: dict = defaultdict(lambda: {"consumed": 0, "received": 0, "movements": 0, "last": None})
    for t in txns:
        agg = per_item[t.item_id]
        agg["movements"] += 1
        direction = _direction(t)
        if direction in ("consumed", "received"):
            agg[direction] += t.quantity or 0
        if t.created_at and (agg["last"] is None or t.created_at > agg["last"]):
            agg["last"] = t.created_at
    material_rows = []
    for item_id, agg in per_item.items():
        it = items.get(item_id)
        material_rows.append({
            "item_code": it.item_code if it else f"#{item_id}",
            "item_name": it.item_name if it else "Unknown item",
            "category": it.category if it else None,
            "unit": it.unit if it else None,
            "supplier": it.supplier if it else None,
            "consumed": agg["consumed"],
            "received": agg["received"],
            "movements": agg["movements"],
            "last_at": _iso(agg["last"]),
        })
    # Inputs first (biggest consumption), then the goods that came back.
    material_rows.sort(key=lambda m: (-m["consumed"], -m["received"], m["item_code"]))
    consumed_total = sum(m["consumed"] for m in material_rows)
    received_total = sum(m["received"] for m in material_rows)

    # --- Interrupted: downtime on this machine while the job was live -----
    # The job's live window: from when it was booked (or planned to start) until
    # it closed out — an open job runs to now.
    started = wo.planned_start or wo.created_at
    ended = wo.planned_end if (closed and wo.planned_end) else now
    downtime_rows = []
    if wo.machine_id is not None and started is not None:
        logs = (db.query(models.DowntimeLog)
                .filter(models.DowntimeLog.machine_id == wo.machine_id,
                        models.DowntimeLog.created_at >= started,
                        models.DowntimeLog.created_at <= ended).all())
        logs.sort(key=lambda d: (d.created_at or datetime.min, d.id), reverse=True)
        downtime_rows = [{
            "reason": d.reason,
            "duration": d.duration,
            "minutes": parse_duration_to_minutes(d.duration),
            "notes": d.notes,
            "at": _iso(d.created_at),
        } for d in logs]
    downtime_minutes = sum(r["minutes"] for r in downtime_rows)

    # --- The merged record, newest first ----------------------------------
    timeline = []
    for p in plans:
        timeline.append({
            "kind": "plan", "at": _iso(p.created_at),
            "label": f"Planned {p.planned_quantity or 0} on {p.shift_name or '—'}",
            "detail": f"{p.plan_no} · made {p.actual_quantity or 0}",
        })
    for i in insp:
        timeline.append({
            "kind": "inspection", "at": _iso(i.created_at),
            "label": f"Inspected {i.inspected_quantity or 0} · {i.failed_quantity or 0} failed",
            "detail": f"{i.inspection_no} · {i.defect_category or 'no defect logged'} · {i.inspector}",
        })
    for t in txns:
        it = items.get(t.item_id)
        direction = _direction(t)
        verb = {"consumed": "Issued", "received": "Received"}.get(direction, "Adjusted")
        timeline.append({
            "kind": "material", "at": _iso(t.created_at),
            "label": f"{verb} {t.quantity or 0} × {it.item_code if it else f'#{t.item_id}'}",
            "detail": t.notes or (it.item_name if it else ""),
        })
    for d in downtime_rows:
        timeline.append({
            "kind": "downtime", "at": d["at"],
            "label": f"Stopped {d['minutes']}m · {d['reason']}",
            "detail": d["notes"] or "",
        })
    timeline.sort(key=lambda e: (e["at"] or ""), reverse=True)

    # --- Where the record is silent (traceability gaps), worst first ------
    gaps: list = []
    if actual > 0 and not insp:
        gaps.append({"severity": "high", "message":
                     f"{actual} unit{'s' if actual != 1 else ''} were booked as made but no quality "
                     "inspection was recorded against this job — the batch is unverified."})
    if actual > 0 and consumed_total == 0:
        gaps.append({"severity": "high", "message":
                     "No material was issued against this work order — there is no record of what "
                     "this batch was built from."})
    if inspected and _pct(scrap, inspected) >= SCRAP_ALERT_PCT:
        gaps.append({"severity": "high", "message":
                     f"{scrap} of {inspected} inspected units were scrapped "
                     f"({_pct(scrap, inspected)}%) — worth a containment check on this batch."})
    if not closed and wo.planned_end and wo.planned_end < now:
        late_days = (now.date() - wo.planned_end.date()).days
        gaps.append({"severity": "high", "message":
                     f"Still open {late_days} day{'s' if late_days != 1 else ''} past its planned end "
                     f"({wo.planned_end.date().isoformat()}), at {_pct(actual, target)}% of target."})
    if not plans:
        gaps.append({"severity": "medium", "message":
                     "No production plan was ever booked for this job — it ran unscheduled, so there "
                     "is nothing to hold its output against."})
    if downtime_minutes and target and actual < target:
        gaps.append({"severity": "medium", "message":
                     f"{downtime_minutes} minutes of downtime were logged on "
                     f"{machine.name if machine else 'its machine'} while this job was live — "
                     f"the {target - actual}-unit shortfall is at least partly explained."})
    if plan_planned and plan_actual < plan_planned:
        gaps.append({"severity": "medium", "message":
                     f"Its plans booked {plan_planned} units but only {plan_actual} were reported "
                     f"({_pct(plan_actual, plan_planned)}% attainment)."})
    if failed and not _has_defect_category(insp):
        gaps.append({"severity": "medium", "message":
                     f"{failed} units failed inspection with no defect category recorded — the "
                     "failure cannot be Pareto'd or traced to a cause."})
    gaps.sort(key=lambda g: 0 if g["severity"] == "high" else 1)

    return {
        "found": True,
        "work_order_no": wo.work_order_no,
        "part_number": wo.part_number,
        "batch_number": wo.batch_number,
        "machine_id": wo.machine_id,
        "machine": machine.name if machine else "—",
        "line": (machine.line or "") if machine else "",
        "status": wo.status,
        "closed": closed,
        "material_state": wo.material_state,
        "target": target,
        "actual": actual,
        "shortfall": max(target - actual, 0),
        "progress_rate": min(_pct(actual, target), 100) if target else 0,
        "created_at": _iso(wo.created_at),
        "planned_start": _iso(wo.planned_start),
        "planned_end": _iso(wo.planned_end),
        "days_late": ((now.date() - wo.planned_end.date()).days
                      if (not closed and wo.planned_end and wo.planned_end < now) else 0),
        "shifts": shifts,
        "plans": {
            "count": len(plan_rows),
            "planned": plan_planned,
            "actual": plan_actual,
            "attainment_rate": _pct(plan_actual, plan_planned),
            "rows": plan_rows[:TOP_N],
        },
        "quality": {
            "inspections": len(insp),
            "inspected": inspected,
            "passed": passed,
            "failed": failed,
            "rework": rework,
            "scrap": scrap,
            "first_pass_yield": _pct(passed, inspected),
            "fail_rate": _pct(failed, inspected),
            "defects": [{"category": c, "count": n} for c, n in defects.most_common(TOP_N)],
            "rows": insp_rows,
        },
        "materials": {
            "consumed": consumed_total,
            "received": received_total,
            "rows": material_rows[:TOP_N],
        },
        "downtime": {
            "events": len(downtime_rows),
            "minutes": downtime_minutes,
            "rows": downtime_rows[:TOP_N],
        },
        "timeline": timeline[:20],
        "gaps": gaps,
    }
