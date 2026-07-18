"""Machine Health twin — a live per-machine read-model (ADR-0006).

Composes one snapshot per machine from signals the platform already produces:
current state, a health score derived from predictive risk, recent downtime, and
the open maintenance tasks and pending agent actions targeting it. A read-model
over existing tables (adds no storage). Tenant-scoped explicitly for the tables
that are only stamped (agent_actions), and via the auto-scoping layer for the
rest (ADR-0002).
"""
from collections import Counter
from datetime import datetime, timedelta

import models
from ai import prediction

name = "twin"


def _band(health: int) -> str:
    if health >= 80:
        return "Healthy"
    if health >= 55:
        return "Watch"
    if health >= 35:
        return "At risk"
    return "Critical"


def _oee_from_records(records) -> dict:
    """OEE = Availability x Performance x Quality over a machine's production
    records. Each component is clamped to [0, 1] then returned as a percentage."""
    planned = sum(r.planned_minutes or 0 for r in records)
    runtime = sum(r.runtime_minutes or 0 for r in records)
    total = sum(r.total_count or 0 for r in records)
    good = sum(r.good_count or 0 for r in records)
    ideal_s = sum((r.ideal_cycle_time_seconds or 0) * (r.total_count or 0) for r in records)
    a = min(runtime / planned, 1.0) if planned else 0.0
    p = min(ideal_s / (runtime * 60), 1.0) if runtime else 0.0
    q = min(good / total, 1.0) if total else 0.0
    return {
        "oee": round(a * p * q * 100),
        "availability": round(a * 100),
        "performance": round(p * 100),
        "quality": round(q * 100),
        "has_data": len(records) > 0,
    }


def _recent_production(db, machine_id=None, days: int = 7):
    cutoff = datetime.utcnow().date() - timedelta(days=days - 1)
    q = db.query(models.ProductionRecord)
    if machine_id is not None:
        q = q.filter(models.ProductionRecord.machine_id == machine_id)
    return [r for r in q.all() if r.created_at and r.created_at.date() >= cutoff]


def _oee_by_machine(db, days: int = 7) -> dict:
    """OEE per machine over the last week, from one pass over production_records."""
    grouped: dict = {}
    for r in _recent_production(db, days=days):
        if r.machine_id is not None:
            grouped.setdefault(r.machine_id, []).append(r)
    return {mid: _oee_from_records(recs) for mid, recs in grouped.items()}


_EMPTY_OEE = {"oee": 0, "availability": 0, "performance": 0, "quality": 0, "has_data": False}


def _machine_twin(db, machine, risk, tenant, oee=None) -> dict:
    score = int(risk["risk_score"]) if risk else 0
    health = max(0, 100 - score)
    recent_downtime = (
        db.query(models.DowntimeLog)
        .filter(models.DowntimeLog.machine_id == machine.id)
        .order_by(models.DowntimeLog.id.desc())
        .limit(3).all()
    )
    open_tasks = (
        db.query(models.MaintenanceTask)
        .filter(models.MaintenanceTask.machine_id == machine.id,
                models.MaintenanceTask.status.in_(("Proposed", "Open")))
        .count()
    )
    pending_actions = (
        db.query(models.AgentAction)
        .filter(models.AgentAction.tenant_code == tenant,
                models.AgentAction.related_machine_id == machine.id,
                models.AgentAction.status == "Proposed")
        .count()
    )
    return {
        "machine_id": machine.id,
        "name": machine.name,
        "line": machine.line or "",
        "status": machine.status,
        "utilization": machine.utilization,
        "downtime": machine.downtime,
        "health_score": health,
        "health_band": _band(health),
        "risk_score": score,
        "risk_level": risk["risk_level"] if risk else "Low",
        "top_reason": (risk["reasons"][0] if risk and risk.get("reasons") else "no major risk indicators"),
        "open_maintenance_tasks": open_tasks,
        "pending_agent_actions": pending_actions,
        "recent_downtime": [{"reason": d.reason, "duration": d.duration} for d in recent_downtime],
        "oee": oee or _EMPTY_OEE,
    }


def build_twins(db, tenant: str):
    """Live health twin for every machine of the tenant, worst health first."""
    machines = db.query(models.Machine).order_by(models.Machine.id).all()
    risks = {r["machine_id"]: r for r in prediction.assess_from_db(db)}
    oee = _oee_by_machine(db)
    twins = [_machine_twin(db, m, risks.get(m.id), tenant, oee.get(m.id)) for m in machines]
    twins.sort(key=lambda t: t["health_score"])
    return twins


def build_twin_overlay(db, tenant: str) -> dict:
    """Per-machine metrics for painting the digital-twin floor map — OEE and the
    week's cost of losses, keyed by machine so the map can heat by either. Composes
    the OEE and cost read-models (ADR-0007); no storage."""
    from ai.oee import build_oee_summary      # lazy: twin is imported by these modules
    from ai.cost import build_cost_summary

    oee = {m["machine_id"]: m["oee"] for m in build_oee_summary(db, tenant)["machines"]}
    cost = {m["machine_id"]: m["cost"] for m in build_cost_summary(db, tenant)["by_machine"]}
    ids = set(oee) | set(cost)
    return {
        "machines": [
            {"machine_id": mid, "oee": oee.get(mid), "cost": cost.get(mid, 0)}
            for mid in sorted(ids)
        ],
    }


# ── Single-machine detail (the drill-down cockpit) ─────────────────
def _iso(dt):
    return dt.isoformat() if dt else None


def _timeline(db, machine_id, tenant):
    """One newest-first history for a machine, merging the three things that
    happen to it — downtime, maintenance tasks, and agent actions — into a
    common shape. Downtime/tasks are auto-scoped (ADR-0002); agent actions are
    only stamped, so they are filtered by tenant explicitly."""
    events = []
    for d in (db.query(models.DowntimeLog)
              .filter(models.DowntimeLog.machine_id == machine_id)
              .order_by(models.DowntimeLog.id.desc()).limit(25).all()):
        events.append({"kind": "downtime", "at": _iso(d.created_at),
                       "title": f"Downtime — {d.reason}", "detail": d.duration or "", "status": None})
    for t in (db.query(models.MaintenanceTask)
              .filter(models.MaintenanceTask.machine_id == machine_id)
              .order_by(models.MaintenanceTask.id.desc()).limit(25).all()):
        events.append({"kind": "task", "at": _iso(t.created_at),
                       "title": f"{t.task_type} · {t.priority}", "detail": t.task_no, "status": t.status})
    for a in (db.query(models.AgentAction)
              .filter(models.AgentAction.related_machine_id == machine_id,
                      models.AgentAction.tenant_code == tenant)
              .order_by(models.AgentAction.id.desc()).limit(25).all()):
        events.append({"kind": "action", "at": _iso(a.created_at),
                       "title": f"{a.agent} agent · {a.action_type}", "detail": a.summary, "status": a.status})
    events.sort(key=lambda e: e["at"] or "", reverse=True)
    return events[:30]


def _open_actions(db, machine_id, tenant):
    """Agent actions still awaiting a human decision for this machine."""
    rows = (db.query(models.AgentAction)
            .filter(models.AgentAction.related_machine_id == machine_id,
                    models.AgentAction.tenant_code == tenant,
                    models.AgentAction.status == "Proposed")
            .order_by(models.AgentAction.id.desc()).all())
    return [{"id": a.id, "agent": a.agent, "action_type": a.action_type, "summary": a.summary,
             "severity": a.severity, "created_at": _iso(a.created_at)} for a in rows]


def _downtime_trend(db, machine_id, days: int = 7):
    """A calendar day-by-day count of this machine's downtime over the last week
    (oldest -> newest), so the cockpit can draw a downtime sparkline."""
    today = datetime.utcnow().date()
    window = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    window_set = set(window)
    counts = Counter(
        d.created_at.date()
        for d in db.query(models.DowntimeLog).filter(models.DowntimeLog.machine_id == machine_id).all()
        if d.created_at and d.created_at.date() in window_set
    )
    return [{"date": dd.isoformat(), "count": counts.get(dd, 0)} for dd in window]


def _machine_production(db, machine_id, days: int = 7):
    """This machine's throughput over the last week: good/total, good rate, and a
    daily good-count series (oldest -> newest)."""
    today = datetime.utcnow().date()
    window = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    window_set = set(window)
    recs = [
        r for r in db.query(models.ProductionRecord).filter(models.ProductionRecord.machine_id == machine_id).all()
        if r.created_at and r.created_at.date() in window_set
    ]
    good = sum(r.good_count or 0 for r in recs)
    total = sum(r.total_count or 0 for r in recs)
    per_day: dict = {}
    for r in recs:
        per_day[r.created_at.date()] = per_day.get(r.created_at.date(), 0) + (r.good_count or 0)
    return {
        "good": good,
        "total": total,
        "good_rate": round(good / total * 100) if total else 0,
        "daily": [{"date": d.isoformat(), "count": per_day.get(d, 0)} for d in window],
    }


def _machine_quality(db, machine_id):
    """This machine's quality across its inspections: yield, fail rate, defects."""
    insp = db.query(models.QualityInspection).filter(models.QualityInspection.machine_id == machine_id).all()
    inspected = sum(i.inspected_quantity or 0 for i in insp)
    failed = sum(i.failed_quantity or 0 for i in insp)
    defects: Counter = Counter()
    for i in insp:
        if i.failed_quantity:
            defects[(i.defect_category or "Unspecified").strip() or "Unspecified"] += i.failed_quantity
    return {
        "inspections": len(insp),
        "inspected": inspected,
        "passed": sum(i.passed_quantity or 0 for i in insp),
        "failed": failed,
        "fail_rate": round(failed / inspected * 100) if inspected else 0,
        "top_defects": [{"category": c, "count": n} for c, n in defects.most_common(3)],
    }


def build_machine_detail(db, tenant: str, machine_id: int):
    """A single-machine cockpit: the twin snapshot plus the full risk-factor
    breakdown, 7-day downtime and production trends, this machine's quality, a
    unified event timeline, and the agent actions awaiting approval. Returns None
    when the machine isn't the tenant's (the caller then 404s)."""
    machine = db.query(models.Machine).filter(models.Machine.id == machine_id).first()
    if not machine:
        return None
    risk = prediction.risk_for_machine(db, machine_id)
    detail = _machine_twin(db, machine, risk, tenant, _oee_from_records(_recent_production(db, machine_id)))
    detail["risk_factors"] = list(risk["reasons"]) if risk and risk.get("reasons") else []
    detail["downtime_7d"] = _downtime_trend(db, machine_id)
    detail["production_7d"] = _machine_production(db, machine_id)
    detail["quality"] = _machine_quality(db, machine_id)
    detail["timeline"] = _timeline(db, machine_id, tenant)
    detail["open_actions"] = _open_actions(db, machine_id, tenant)
    return detail
