"""The database loader feeds the model exactly what the synthetic path feeds it, for one tenant only.

WHAT IS ASSERTED
----------------
  1  PARITY      a synthetic history seeded into SQLite (INCLUDING rows after
                 as_of and rows long before the lookback) and read back by
                 ``db_history.load_histories`` gives the same truncated
                 records, status, features and rule score as the in-memory
                 history, at several as-of dates
  2  BOUNDS      a row exactly at the window start is in, one exactly at as_of
                 is out; the "state before the window" query is bounded below
  3  TENANT      tenant B's rows never reach tenant A with NO ambient tenant
                 bound -- including B rows that point at A's machine ids,
                 which only the explicit tenant filter can stop
  4  RULES       reactive/open come from ai.maintenance (one rule): at serving
                 time the overdue feature equals the count of
                 ai.maintenance.is_overdue over the same tasks
  5  SHAPE       a fixed number of statements whatever the fleet size (no
                 N+1), each history SELECT filters tenant_code and bounds
                 created_at at both ends

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_failure_risk_db_parity.py
"""
from datetime import date, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
from ai import maintenance
from amp_ai.failure_risk import baseline_rule as R
from amp_ai.failure_risk import db_history as D
from amp_ai.failure_risk import features as F
from amp_ai.failure_risk import history as H
from amp_ai.failure_risk import synthetic as S
from database import Base

A, B = "TENANT_A", "TENANT_B"
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


class Db:
    def __init__(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        tenancy.install_scoping()
        self.statements = []

        @event.listens_for(self.engine, "before_cursor_execute")
        def _capture(conn, cursor, statement, params, context, many):
            self.statements.append(" ".join(statement.split()))

        self.session = sessionmaker(bind=self.engine)()
        self.task_no = 0


def seed(db, tenant, fleet, *, statuses=None):
    """Seed EVERY record of the fleet; return {db id: history re-keyed to the db id}."""
    s = db.session
    out = {}
    for mh in fleet.histories:
        machine = models.Machine(tenant_code=tenant, site="", name=mh.name, status="Idle", utilization=0,
                                 downtime="0 min")
        s.add(machine)
        s.flush()
        mid = machine.id
        for ts, old, new, util in mh.events:
            s.add(models.MachineEvent(tenant_code=tenant, machine_id=mid, machine_name=mh.name, old_status=old,
                                      new_status=new, utilization=util, source="synthetic", created_at=ts))
        for ts, reason, duration in mh.downtime:
            s.add(models.DowntimeLog(tenant_code=tenant, machine_id=mid, reason=reason, duration=duration,
                                     created_at=ts))
        for ts, planned, runtime, ideal, total, good, rejected in mh.production:
            s.add(models.ProductionRecord(tenant_code=tenant, machine_id=mid, planned_minutes=planned,
                                          runtime_minutes=runtime, ideal_cycle_time_seconds=ideal,
                                          total_count=total, good_count=good, rejected_count=rejected,
                                          created_at=ts))
        for ts, inspected, failed in mh.inspections:
            db.task_no += 1
            s.add(models.QualityInspection(tenant_code=tenant, inspection_no=f"QI-{db.task_no}", machine_id=mid,
                                           inspector="qa", inspected_quantity=inspected,
                                           passed_quantity=inspected - failed, failed_quantity=failed,
                                           created_at=ts))
        for planned, completed, reactive, is_open in mh.maintenance:
            db.task_no += 1
            s.add(models.MaintenanceTask(tenant_code=tenant, task_no=f"MT-{db.task_no}", machine_id=mid,
                                         task_type="Corrective" if reactive else "Preventive",
                                         assigned_to="tech", planned_date=planned, completed_date=completed,
                                         status="Open" if is_open else "Completed"))
        s.flush()
        out[mid] = H.MachineHistory(mid, mh.name, events=mh.events, downtime=mh.downtime,
                                    production=mh.production, inspections=mh.inspections,
                                    maintenance=mh.maintenance)
    s.commit()
    return out


def records(h):
    return (h.machine_id, h.name, h.state_at_window_start, h.events, h.downtime, h.production,
            h.inspections, h.maintenance, h.window_end)


# --------------------------------------------------------------------------- 1 + 3
def section_parity_and_tenancy():
    print("\n1/3. Parity with the in-memory path; tenant B never reaches tenant A")
    db = Db()
    fleet_a = S.generate_fleet(10, 300, seed=404)
    fleet_b = S.generate_fleet(10, 300, seed=505)            # same machine names, different lives
    mem_a = seed(db, A, fleet_a)
    seed(db, B, fleet_b)

    # Adversarial rows: tenant B's tenant_code but tenant A's machine ids, inside every window.
    s = db.session
    for mid in mem_a:
        for day in range(125, 300, 5):
            ts = S.as_of_for_day(day) - timedelta(hours=3)
            s.add(models.MachineEvent(tenant_code=B, machine_id=mid, machine_name="X", old_status="Running",
                                      new_status="Breakdown", utilization=0, source="evil", created_at=ts))
            s.add(models.DowntimeLog(tenant_code=B, machine_id=mid, reason="Breakdown", duration="9 hrs",
                                     created_at=ts))
            s.add(models.ProductionRecord(tenant_code=B, machine_id=mid, planned_minutes=960, runtime_minutes=1,
                                          ideal_cycle_time_seconds=1, total_count=1, good_count=0,
                                          rejected_count=1, created_at=ts))
            db.task_no += 1
            s.add(models.QualityInspection(tenant_code=B, inspection_no=f"QI-X{db.task_no}", machine_id=mid,
                                           inspector="x", inspected_quantity=50, failed_quantity=50, created_at=ts))
            s.add(models.MaintenanceTask(tenant_code=B, task_no=f"MT-X{db.task_no}", machine_id=mid,
                                         task_type="Preventive", assigned_to="x", planned_date=ts.date(),
                                         completed_date=ts.date(), status="Completed"))
    s.commit()

    token = tenancy.set_current_tenant(None)
    try:
        for day in (130, 200, 250, 299):
            as_of = S.as_of_for_day(day)
            loaded = {h.machine_id: h for h in D.load_histories(db.session, A, as_of)}
            check(f"day {day}: exactly tenant A's machines are loaded", sorted(loaded) == sorted(mem_a),
                  f"{sorted(loaded)} vs {sorted(mem_a)}")
            same_records = same_features = same_rule = True
            detail = ""
            for mid, mem in mem_a.items():
                expected = H.truncate(mem, as_of)
                got = loaded.get(mid)
                if got is None or records(got) != records(expected):
                    same_records = False
                    detail = f"machine {mid}"
                    if got is not None:
                        for name in ("state_at_window_start", "events", "downtime", "production",
                                     "inspections", "maintenance"):
                            if getattr(got, name) != getattr(expected, name):
                                detail += f" differs in {name}"
                    continue
                if F.extract(got, as_of) != F.extract(mem, as_of):
                    same_features = False
                if R.rule_score(got, as_of) != R.rule_score(mem, as_of):
                    same_rule = False
            check(f"day {day}: truncated records, state and window_end equal the in-memory truncation",
                  same_records, detail)
            check(f"day {day}: features equal", same_features)
            check(f"day {day}: rule scores equal", same_rule)

        loaded_b = D.load_histories(db.session, B, S.as_of_for_day(200))
        check("tenant B loads its own 10 machines", len(loaded_b) == 10 and not set(h.machine_id for h in loaded_b) & set(mem_a))
    finally:
        tenancy.reset_current_tenant(token)

    for bad in (None, "", "   "):
        try:
            D.load_histories(db.session, bad, S.as_of_for_day(200))
            refused = False
        except ValueError:
            refused = True
        check(f"a missing tenant ({bad!r}) is refused, never read as 'all tenants'", refused)


# --------------------------------------------------------------------------- 2
def section_bounds():
    print("\n2. Both ends of every window are bounded")
    db = Db()
    as_of = S.as_of_for_day(200)
    start = as_of - timedelta(days=H.LOOKBACK_DAYS)
    s = db.session
    m = models.Machine(tenant_code=A, site="", name="EDGE", status="Idle", utilization=0, downtime="0 min")
    quiet = models.Machine(tenant_code=A, site="", name="QUIET", status="Idle", utilization=0, downtime="0 min")
    s.add_all([m, quiet])
    s.flush()
    rows = [
        (start - timedelta(days=H.STATE_LOOKBACK_DAYS, microseconds=1), "Idle", "Maintenance"),  # too old for state
        (start - timedelta(microseconds=1), "Maintenance", "Running"),                        # the state
        (start, "Running", "Breakdown"),                                                       # first inside
        (as_of - timedelta(microseconds=1), "Breakdown", "Running"),                           # last inside
        (as_of, "Running", "Breakdown"),                                                       # future
    ]
    for ts, old, new in rows:
        s.add(models.MachineEvent(tenant_code=A, machine_id=m.id, machine_name="EDGE", old_status=old,
                                  new_status=new, utilization=55, source="t", created_at=ts))
        s.add(models.DowntimeLog(tenant_code=A, machine_id=m.id, reason=new, duration="10 min", created_at=ts))
    s.add(models.MachineEvent(tenant_code=A, machine_id=quiet.id, machine_name="QUIET", old_status="Idle",
                              new_status="Breakdown", utilization=0, source="t",
                              created_at=start - timedelta(days=H.STATE_LOOKBACK_DAYS, microseconds=1)))
    s.commit()
    loaded = {h.name: h for h in D.load_histories(s, A, as_of)}
    edge = loaded["EDGE"]
    check("an event exactly at the window start is loaded; one exactly at as_of is not",
          [e[0] for e in edge.events] == [start, as_of - timedelta(microseconds=1)], repr([e[0] for e in edge.events]))
    check("same for downtime rows", [d[0] for d in edge.downtime] == [start, as_of - timedelta(microseconds=1)])
    check("state at window start is the last event before the start", edge.state_at_window_start == ("Running", 55))
    check("an event older than start - STATE_LOOKBACK_DAYS is not used for state",
          loaded["QUIET"].state_at_window_start is None and not H.in_breakdown_at(loaded["QUIET"], as_of))


# --------------------------------------------------------------------------- 4
def section_maintenance_rules():
    print("\n4. Reactive and open come from ai.maintenance; overdue matches is_overdue at serving time")
    db = Db()
    s = db.session
    m = models.Machine(tenant_code=A, site="", name="MT", status="Idle", utilization=0, downtime="0 min")
    s.add(m)
    s.flush()
    as_of = S.as_of_for_day(200)
    today = as_of.date()
    tasks = [
        ("Preventive", "Open", today - timedelta(days=10), None),
        ("Preventive", None, today - timedelta(days=9), None),
        ("Preventive", "In Progress", today - timedelta(days=8), None),
        ("Preventive", "Proposed", today - timedelta(days=7), None),
        ("Preventive", "Cancelled", today - timedelta(days=6), None),
        ("Preventive", "Completed", today - timedelta(days=5), None),                 # undated completion
        ("Preventive", "Completed", today - timedelta(days=30), today - timedelta(days=20)),
        ("Corrective repair", "Completed", today - timedelta(days=3), today - timedelta(days=3)),
        ("Quality (auto)", "Open", today - timedelta(days=2), None),
        ("Lubrication", "Open", today, None),                                          # due today: not overdue
        ("Emergency", "Completed", today, today),
    ]
    for i, (task_type, status, planned, completed) in enumerate(tasks):
        s.add(models.MaintenanceTask(tenant_code=A, task_no=f"T{i}", machine_id=m.id, task_type=task_type,
                                     assigned_to="tech", planned_date=planned, completed_date=completed,
                                     status=status))
    s.commit()
    # The ORM fills status=None with the column default "Open"; a legacy NULL needs raw SQL.
    s.query(models.MaintenanceTask).filter(models.MaintenanceTask.task_no == "T1").update({"status": None})
    s.commit()
    check("the fixture really holds a NULL status",
          s.query(models.MaintenanceTask).filter(models.MaintenanceTask.status.is_(None)).count() == 1)
    (h,) = D.load_histories(s, A, as_of)
    def key(task):
        return task[0], task[2], str(task[1])

    got = sorted(h.maintenance, key=key)
    rows = s.query(models.MaintenanceTask).all()
    expected = sorted(((t.planned_date, t.completed_date, maintenance.is_reactive(t.task_type),
                        maintenance.is_overdue(SimpleNamespace(planned_date=t.planned_date, status=t.status),
                                               date.max))
                       for t in rows), key=key)
    check("every task in the window is loaded, with its dates", [(g[0], g[1]) for g in got] == [(e[0], e[1]) for e in expected])
    check("each task's reactive flag is ai.maintenance.is_reactive(task_type)",
          [g[2] for g in got] == [e[2] for e in expected])
    open_by_status = {t.status: flag for t, flag in
                      ((t, next(g[3] for g in got if g[0] == t.planned_date and g[2] == maintenance.is_reactive(t.task_type)))
                       for t in rows if t.completed_date is None)}
    check("open means not Completed/Cancelled (NULL status is open)",
          open_by_status == {"Open": True, None: True, "In Progress": True, "Proposed": True,
                             "Cancelled": False, "Completed": False}, repr(open_by_status))
    overdue = sum(1 for t in rows if t.completed_date is None and maintenance.is_overdue(t, today))
    check("at serving time overdue_maint_open == count of ai.maintenance.is_overdue",
          F.extract(h, as_of)["overdue_maint_open"] == float(overdue) and overdue == 5,
          f"{F.extract(h, as_of)['overdue_maint_open']} vs {overdue}")


# --------------------------------------------------------------------------- 5
def section_shape():
    print("\n5. Statement count does not grow with the fleet; every history read is tenant- and time-bounded")
    counts = {}
    for n in (5, 20):
        db = Db()
        seed(db, A, S.generate_fleet(n, 200, seed=7))
        db.statements.clear()
        D.load_histories(db.session, A, S.as_of_for_day(190))
        counts[n] = [st for st in db.statements if st.upper().startswith("SELECT")]
    check("the same number of SELECTs for 5 and for 20 machines", len(counts[5]) == len(counts[20]),
          f"{len(counts[5])} vs {len(counts[20])}")
    check("seven statements: machines, events, state, downtime, production, inspections, maintenance",
          len(counts[20]) == 7, str(len(counts[20])))
    timed = {"machine_events": 0, "downtime_logs": 0, "production_records": 0, "quality_inspections": 0}
    bad = []
    for st in counts[20]:
        if "tenant_code" not in st:
            bad.append(st[:80])
        for table in timed:
            if f"FROM {table}" in st:
                timed[table] += 1
                if not ("created_at >=" in st and "created_at <" in st):
                    bad.append(st[:120])
        if "FROM maintenance_tasks" in st and not ("planned_date >=" in st and "completed_date <" in st):
            bad.append(st[:120])
    check("every SELECT filters tenant_code and bounds its time column at both ends", not bad, repr(bad[:2]))
    check("the structural scan found the history tables it checks",
          timed == {"machine_events": 2, "downtime_logs": 1, "production_records": 1, "quality_inspections": 1},
          repr(timed))


def main():
    print("=" * 74)
    print("AMP-native AI failure risk: database loader parity and tenant isolation")
    print("=" * 74)
    section_parity_and_tenancy()
    section_bounds()
    section_maintenance_rules()
    section_shape()
    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


def test_amp_ai_failure_risk_db_parity():
    """pytest entry point; CI runs this file as a standalone script."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
