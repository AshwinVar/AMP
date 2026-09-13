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

from sqlalchemy import func

import models
import oee_contract
from ai.maintenance import OPEN_STATUSES
import tenancy
# Pooled OEE (ratio of sums) is the single source of truth in analytics_engine,
# shared with build_management_summary / analytics_summary so every surface agrees.
# Re-exported under the name the pillar modules (oee, losses, scorecard) import.
from analytics_engine import pooled_oee as _oee_from_records
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


def _recent_production(db, machine_id=None, days: int = 7, now=None):
    # THE canonical window (oee_contract), not a second definition of it.
    #
    # Window in SQL — production_records grows continuously, so a full-table scan
    # filtered in Python would get slower every week. That part was always right.
    # The BOUNDS were a private copy:
    #
    #     cutoff = midnight(today - (days - 1))
    #     filter(created_at >= cutoff)          # and no upper bound at all
    #
    # which is a different set of rows from `[now - days, now)`. A record 6.9
    # days old was in the contract's window and not in this one; a record dated
    # in the FUTURE by a skewed gateway clock was in this one and not the
    # contract's. Measured on one plant at one moment — six good days and one bad
    # shift seven days back — the two answered 66% and 87%.
    #
    # It showed up hardest in ai/oee.build_oee_summary, which took the plant OEE
    # from here and `coverage` from oee_contract, so the figure that exists to
    # say "measured from N of M machines" was describing a different record set
    # than the number it qualified: one machine with one 6.9-day-old record read
    # "no data" with "1 of 1 reporting, complete", and one future-dated record
    # read "OEE 85%" with "0 of 1 reporting".
    #
    # This is not a new rule, it is the existing one applied where a copy had
    # grown — the same shape as DOWN_STATUSES (#553) and the status buckets
    # (#549, #552).
    import oee_contract
    # `now` threads ONE anchor through a request so a caller can build the
    # adjacent prior window against exactly this window's start rather than
    # against a fresh utcnow() milliseconds later (oee_contract.prior_window).
    window = oee_contract.OeeWindow(days, now=now)
    q = db.query(models.ProductionRecord).filter(
        models.ProductionRecord.created_at >= window.start,
        models.ProductionRecord.created_at < window.end)
    if machine_id is not None:
        q = q.filter(models.ProductionRecord.machine_id == machine_id)
    return q.all()


def _oee_by_machine(db, days: int = 7) -> dict:
    """OEE per machine over the last week, from one pass over production_records."""
    grouped: dict = {}
    for r in _recent_production(db, days=days):
        if r.machine_id is not None:
            grouped.setdefault(r.machine_id, []).append(r)
    return {mid: _oee_from_records(recs) for mid, recs in grouped.items()}


_EMPTY_OEE = {"oee": 0, "availability": 0, "performance": 0, "quality": 0, "has_data": False}


def _downtime_by_machine(db, limit_each=3):
    """The `limit_each` most recent downtime rows for EVERY machine, in one query.

    Replaces a per-machine `.limit(3)`, which is where two thirds of this
    endpoint's N+1 came from. The per-machine cut is done by the DATABASE, with
    a window function.

    It used to be done in Python, on the stated grounds that a window function
    would not behave identically on SQLite (tests) and PostgreSQL (production).
    That was true while SQLite predated 3.25; the interpreter here bundles 3.38
    and CI runs the same Python, so `row_number()` works on both.

    The Python version selected three columns of EVERY row and threw away all
    but three per machine: 75,000 rows to render 600 at 200 machines with a
    month of history, and 200 ms of this endpoint's 261 ms. Selecting only the
    rendered columns did avoid hydrating ORM objects — real, but never the cost.
    The rows still crossed the wire and were still iterated.
    """
    # Belt and braces on the tenant, and it is worth being exact about which.
    #
    # I added this predicate expecting the ADR-0002 hook not to reach inside a
    # subquery — the hook scopes a select whose entity is a MODEL, and the
    # statement below selects from a SUBQUERY. Checked directly: it DOES reach,
    # and removing this filter leaks nothing. So this is defence in depth, not
    # the thing preventing a leak, and a mutation that deletes it survives both
    # this module's suites for that reason.
    #
    # Kept anyway. The hook's coverage here is a property of how SQLAlchemy
    # composes the statement, and tenant isolation is the wrong place to depend
    # on that. current_tenant() is the same value the hook uses; None (an
    # unscoped context such as a migration or an admin task) keeps the previous
    # unfiltered behaviour rather than silently returning nothing.
    # test_twin_downtime_bounded.py section 2 pins the outcome either way.
    ranked = db.query(
        models.DowntimeLog.machine_id.label("machine_id"),
        models.DowntimeLog.reason.label("reason"),
        models.DowntimeLog.duration.label("duration"),
        func.row_number().over(
            partition_by=models.DowntimeLog.machine_id,
            order_by=models.DowntimeLog.id.desc()).label("rank"))
    tenant = tenancy.current_tenant()
    if tenant is not None:
        ranked = ranked.filter(models.DowntimeLog.tenant_code == tenant)
    ranked = ranked.subquery()

    out = {}
    for machine_id, reason, duration in (
            db.query(ranked.c.machine_id, ranked.c.reason, ranked.c.duration)
              .filter(ranked.c.rank <= limit_each)
              .order_by(ranked.c.machine_id, ranked.c.rank).all()):
        out.setdefault(machine_id, []).append(
            {"reason": reason, "duration": duration})
    return out


def _open_task_counts(db):
    """Maintenance tasks in an open state, per machine, in one grouped query.

    OPEN_STATUSES, not a hardcoded ("Proposed", "Open"). That pair is missing
    "In Progress" — exactly what the Approvals Inbox writes when a human accepts
    an agent's proposal (ai/agents.py:129) and what a technician sets when they
    pick the job up. So this reported ZERO open maintenance tasks for a machine
    somebody was working on right now, on the cockpit whose whole job is to say
    what is happening to that machine. Same defect as #565's agent dedup, in a
    different consumer.
    """
    rows = (db.query(models.MaintenanceTask.machine_id, func.count())
              .filter(models.MaintenanceTask.status.in_(OPEN_STATUSES))
              .group_by(models.MaintenanceTask.machine_id).all())
    return {machine_id: n for machine_id, n in rows}


def _pending_action_counts(db, tenant):
    """Proposed agent actions per machine, in one grouped query.

    AgentAction is NOT auto-scoped (it is outside SCOPED_MODELS, with a recorded
    reason in test_unscoped_model_reads.py), so the tenant filter here is
    explicit and must stay explicit.
    """
    rows = (db.query(models.AgentAction.related_machine_id, func.count())
              .filter(models.AgentAction.tenant_code == tenant,
                      models.AgentAction.status == "Proposed")
              .group_by(models.AgentAction.related_machine_id).all())
    return {machine_id: n for machine_id, n in rows}


def _machine_twin(machine, risk, oee=None, recent_downtime=None,
                  open_tasks=0, pending_actions=0) -> dict:
    """One machine's twin, from data ALREADY FETCHED.

    Takes no `db` on purpose. It used to, and issued three queries of its own per
    machine — so a 200-machine plant cost ~600 statements every three seconds,
    because the dashboard polls this endpoint on a 3s timer. Everything it needs
    is now gathered once by the caller and handed in.
    """
    score = int(risk["risk_score"]) if risk else 0
    health = max(0, 100 - score)
    recent_downtime = recent_downtime or []
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
        "recent_downtime": recent_downtime,
        "oee": oee or _EMPTY_OEE,
    }


def build_twins(db, tenant: str):
    """Live health twin for every machine of the tenant, worst health first."""
    machines = db.query(models.Machine).order_by(models.Machine.id).all()
    risks = {r["machine_id"]: r for r in prediction.assess_from_db(db)}
    oee = _oee_by_machine(db)
    # Gathered ONCE for the whole fleet rather than three queries per machine.
    downtime = _downtime_by_machine(db)
    tasks = _open_task_counts(db)
    actions = _pending_action_counts(db, tenant)
    twins = [_machine_twin(m, risks.get(m.id), oee.get(m.id),
                           downtime.get(m.id), tasks.get(m.id, 0),
                           actions.get(m.id, 0))
             for m in machines]
    twins.sort(key=lambda t: t["health_score"])
    return twins


def build_twin_overlay(db, tenant: str) -> dict:
    """Per-machine metrics for painting the digital-twin floor map — OEE and the
    week's cost of losses, keyed by machine so the map can heat by either. Composes
    the OEE and cost read-models (ADR-0007); no storage."""
    from ai.oee import build_oee_summary      # lazy: twin is imported by these modules
    from ai.cost import build_cost_summary

    # OEE side is the full per-machine list; the cost side MUST be the full,
    # uncapped map too (not build_cost_summary's TOP_N `by_machine` display list),
    # or machines ranked outside the top few paint as £0 on the map while carrying
    # real losses — the two heat sources have to share one basis (rule 3).
    oee = {m["machine_id"]: m["oee"] for m in build_oee_summary(db, tenant)["machines"]}
    cost = build_cost_summary(db, tenant)["machine_cost"]
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


def _window_span(window):
    """The calendar dates `window` touches, oldest first, with the oldest flagged
    partial when the window opens mid-day.

    ONE definition for every series on the cockpit. A rolling 7x24h window that
    opens part-way through a day touches EIGHT calendar dates; a series drawn
    over seven of them cannot account for the panel above it, and three panels
    each narrowing the window their own way is how one card came to measure the
    same machine over three different weeks (#590). Same shape as the OEE and
    cost trends (#586, #587)."""
    start_date = window.start.date()
    end_date = (window.end - timedelta(microseconds=1)).date()
    days = [start_date + timedelta(days=i)
            for i in range((end_date - start_date).days + 1)]
    opens_mid_day = window.start.time() != datetime.min.time()
    return days, opens_mid_day


def _downtime_trend(db, machine_id, days: int = 7, now=None):
    """A day-by-day count of this machine's downtime across the cockpit's window
    (oldest -> newest), so the card can draw a downtime sparkline.

    Bounded in SQL at BOTH ends — downtime_logs grows continuously, and an
    unbounded upper end admits future-dated rows. The span comes from the shared
    window, not a day count, so this series covers the same week as every other
    panel on the card."""
    window = oee_contract.OeeWindow(days, now=now)
    span, opens_mid_day = _window_span(window)
    counts = Counter(
        d.created_at.date()
        for d in db.query(models.DowntimeLog)
                   .filter(models.DowntimeLog.machine_id == machine_id,
                           models.DowntimeLog.created_at >= window.start,
                           models.DowntimeLog.created_at < window.end).all()
        if d.created_at
    )
    return [{"date": dd.isoformat(), "count": counts.get(dd, 0),
             **({"partial": True} if (i == 0 and opens_mid_day) else {})}
            for i, dd in enumerate(span)]


def _machine_production(db, machine_id, days: int = 7, now=None):
    """This machine's throughput over the cockpit's window: good/total, good rate,
    and a daily good-count series (oldest -> newest).

    THE SAME WINDOW AS THE OEE PANEL. This used to narrow the rolling window to
    seven calendar dates, while the OEE panel beside it pooled all eight the
    window touches -- so `good_rate` here and the OEE Quality bar there, which
    are the SAME RATIO, could differ by forty-five points on one card. Bounded in
    SQL by _recent_production (production_records grows continuously); the window
    is half-open, so it needs no second filter to drop future-dated rows."""
    window = oee_contract.OeeWindow(days, now=now)
    span, opens_mid_day = _window_span(window)
    recs = _recent_production(db, machine_id, days, now=window.end)
    good = sum(r.good_count or 0 for r in recs)
    total = sum(r.total_count or 0 for r in recs)
    per_day: dict = {}
    for r in recs:
        per_day[r.created_at.date()] = per_day.get(r.created_at.date(), 0) + (r.good_count or 0)
    return {
        "good": good,
        "total": total,
        "good_rate": round(good / total * 100) if total else 0,
        "daily": [{"date": d.isoformat(), "count": per_day.get(d, 0),
                   **({"partial": True} if (i == 0 and opens_mid_day) else {})}
                  for i, d in enumerate(span)],
    }


def _machine_quality(db, machine_id, days: int = 7, now=None):
    """This machine's quality over the SAME window as the rest of the cockpit:
    yield, fail rate and top defects for the last `days`.

    Windowed deliberately, for two reasons. Honesty: the cockpit's OEE panel is
    explicitly "last 7 days" and its Quality bar is that week's quality, so a
    LIFETIME fail rate rendered beside it could flatly contradict it (a 99%
    Quality bar sitting above a 12% fail rate earned a year ago). Every other
    cockpit panel — downtime_7d, production_7d — is the same 7 days, so one basis
    for the whole card. And it bounds the query: quality_inspections grows, and
    created_at is indexed."""
    window = oee_contract.OeeWindow(days, now=now)
    insp = (db.query(models.QualityInspection)
            .filter(models.QualityInspection.machine_id == machine_id,
                    models.QualityInspection.created_at >= window.start,
                    models.QualityInspection.created_at < window.end).all())
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
    # ONE anchor for the whole card. Every panel below is labelled "last 7 days",
    # and they used to be three different weeks: the OEE panel on the rolling
    # window, production and downtime narrowed to seven calendar dates, and
    # quality on seven dates plus everything in the future. See
    # test_twin_cockpit_one_window.py.
    window = oee_contract.OeeWindow(7)
    # ONE machine, so the batched gatherers are called for this id only — the
    # drill-down keeps the same shape as the fleet view without paying for it.
    detail = _machine_twin(
        machine, risk,
        _oee_from_records(_recent_production(db, machine_id, now=window.end)),
        _downtime_by_machine(db).get(machine_id),
        _open_task_counts(db).get(machine_id, 0),
        _pending_action_counts(db, tenant).get(machine_id, 0))
    detail["risk_factors"] = list(risk["reasons"]) if risk and risk.get("reasons") else []
    detail["downtime_7d"] = _downtime_trend(db, machine_id, now=window.end)
    detail["production_7d"] = _machine_production(db, machine_id, now=window.end)
    detail["quality"] = _machine_quality(db, machine_id, now=window.end)
    detail["timeline"] = _timeline(db, machine_id, tenant)
    detail["open_actions"] = _open_actions(db, machine_id, tenant)
    return detail
