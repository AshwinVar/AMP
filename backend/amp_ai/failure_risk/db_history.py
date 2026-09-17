"""Read one tenant's machine histories from the database, bounded, for failure-risk scoring.

READ-ONLY, ONE TENANT, BOUNDED
------------------------------
* Every query filters ``tenant_code == tenant`` EXPLICITLY, in addition to the
  ADR-0002 session hook, so a caller with no ambient tenant bound still cannot
  read another factory's rows. ``tenant`` must be a non-empty string: there is
  no "all tenants" call. A row is also attached only to a machine of this
  tenant, but that is not the isolation mechanism (a foreign row can carry this
  tenant's machine id); the explicit filter is, and a test plants such rows.
* Time bounds come from ``oee_contract.OeeWindow(days=LOOKBACK_DAYS, now=as_of)``
  and every history query bounds its time column at BOTH ends. Date columns use
  ``history.date_bounds``, the same function the in-memory truncation uses.
* Column-only selects: no ORM entity is hydrated. Seven statements regardless
  of fleet size: machines, events, state before the window (one grouped query),
  downtime, production, inspections, maintenance.
* The model never builds or sees a query. This module is the AMP data layer the
  model is fed from; ``predict`` receives the histories it returns.

CLASSIFICATION (one rule each, from ai.maintenance)
---------------------------------------------------
``is_reactive`` is ``ai.maintenance.is_reactive(task_type)``. ``is_open`` is
``ai.maintenance.is_overdue(task, date.max)``: ``is_overdue(task, today)`` is
"planned before today AND not finished or withdrawn", and with
``today = date.max`` the date half holds for every real date, leaving exactly
THE not-finished-or-withdrawn rule rather than a second copy of its status list.
test_amp_ai_failure_risk_db_parity.py pins that, at serving time, the overdue
feature equals the count of ``is_overdue(task, as_of.date())``.

``industrial_signals`` / ``iot_telemetry`` are not read here (failure risk uses
no telemetry). Legacy NULL counts are read as 0, as ``predictive_engine._int``
does.
"""
from datetime import date, timedelta
from types import SimpleNamespace

from sqlalchemy import and_, func, or_

import models
from ai import maintenance
from oee_contract import OeeWindow

from .history import LOOKBACK_DAYS, STATE_LOOKBACK_DAYS, MachineHistory, date_bounds, truncate

__all__ = ["load_histories"]


def _int(value):
    return int(value) if value is not None else 0


def load_histories(db, tenant, as_of) -> list:
    """[MachineHistory truncated for ``as_of``] for every machine of ``tenant``, ordered by machine id."""
    if not isinstance(tenant, str) or not tenant.strip():
        raise ValueError("load_histories needs the caller's tenant; there is no all-tenants read")
    window = OeeWindow(days=LOOKBACK_DAYS, now=as_of)
    start, end = window.start, window.end
    state_start = start - timedelta(days=STATE_LOOKBACK_DAYS)

    M = models.Machine
    machines = (db.query(M.id, M.name).filter(M.tenant_code == tenant).order_by(M.id).all())
    parts = {mid: {"name": name, "events": [], "downtime": [], "production": [], "inspections": [],
                   "maintenance": [], "state": None} for mid, name in machines}
    if not parts:
        return []

    E = models.MachineEvent
    for mid, ts, old, new, util in (
            db.query(E.machine_id, E.created_at, E.old_status, E.new_status, E.utilization)
              .filter(E.tenant_code == tenant, E.created_at >= start, E.created_at < end)
              .order_by(E.machine_id, E.created_at, E.id).all()):
        if mid in parts:
            parts[mid]["events"].append((ts, old, new, util))

    latest = (db.query(E.machine_id.label("machine_id"), func.max(E.created_at).label("last_at"))
                .filter(E.tenant_code == tenant, E.created_at >= state_start, E.created_at < start)
                .group_by(E.machine_id).subquery())
    for mid, ts, event_id, new, util in (
            db.query(E.machine_id, E.created_at, E.id, E.new_status, E.utilization)
              .join(latest, and_(E.machine_id == latest.c.machine_id, E.created_at == latest.c.last_at))
              .filter(E.tenant_code == tenant, E.created_at >= state_start, E.created_at < start)
              .order_by(E.machine_id, E.id).all()):
        if mid in parts:
            parts[mid]["state"] = (new, util)          # ordered by id: the last row written wins a tie

    DL = models.DowntimeLog
    for mid, ts, reason, duration in (
            db.query(DL.machine_id, DL.created_at, DL.reason, DL.duration)
              .filter(DL.tenant_code == tenant, DL.created_at >= start, DL.created_at < end)
              .order_by(DL.machine_id, DL.created_at, DL.id).all()):
        if mid in parts:
            parts[mid]["downtime"].append((ts, reason, duration))

    P = models.ProductionRecord
    for mid, ts, planned, runtime, ideal, total, good, rejected in (
            db.query(P.machine_id, P.created_at, P.planned_minutes, P.runtime_minutes,
                     P.ideal_cycle_time_seconds, P.total_count, P.good_count, P.rejected_count)
              .filter(P.tenant_code == tenant, P.created_at >= start, P.created_at < end)
              .order_by(P.machine_id, P.created_at, P.id).all()):
        if mid in parts:
            parts[mid]["production"].append((ts, _int(planned), _int(runtime), _int(ideal), _int(total),
                                             _int(good), _int(rejected)))

    Q = models.QualityInspection
    for mid, ts, inspected, failed in (
            db.query(Q.machine_id, Q.created_at, Q.inspected_quantity, Q.failed_quantity)
              .filter(Q.tenant_code == tenant, Q.machine_id.isnot(None),
                      Q.created_at >= start, Q.created_at < end)
              .order_by(Q.machine_id, Q.created_at, Q.id).all()):
        if mid in parts:
            parts[mid]["inspections"].append((ts, _int(inspected), _int(failed)))

    T = models.MaintenanceTask
    lo, hi = date_bounds(start, end)
    for mid, planned, completed, task_type, status in (
            db.query(T.machine_id, T.planned_date, T.completed_date, T.task_type, T.status)
              .filter(T.tenant_code == tenant,
                      or_(and_(T.planned_date >= lo, T.planned_date < hi),
                          and_(T.completed_date >= lo, T.completed_date < hi)))
              .order_by(T.machine_id, T.planned_date, T.id).all()):
        if mid in parts and planned is not None:
            is_open = maintenance.is_overdue(SimpleNamespace(planned_date=planned, status=status), date.max)
            parts[mid]["maintenance"].append((planned, completed, maintenance.is_reactive(task_type), is_open))

    histories = []
    for mid, p in parts.items():
        loaded = MachineHistory(mid, p["name"], state_at_window_start=p["state"], events=p["events"],
                                downtime=p["downtime"], production=p["production"],
                                inspections=p["inspections"], maintenance=p["maintenance"], window_end=as_of)
        histories.append(truncate(loaded, as_of))
    return histories
