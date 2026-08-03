"""Reporting route registration test (ADR-0009).

CSV/text exports + the report-request log live in reports_routes.register(app),
peeled out of main.py. Guards registration + sole ownership. All compute is
imported from the shared engines (analytics_engine, report_generator), so the
module must not reference the main-local intelligence helpers.

/reports/daily-summary.txt deliberately stays in main (it calls the
/analytics/summary endpoint function directly), so it is asserted to remain
owned by main.

Run:  python backend/test_reports_routes.py     (exit 0 = pass)
"""
import csv
import inspect
import io

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
import models
import reports_routes as RR
import tenancy as T
from database import Base

EXPECTED = {
    "/reports/downtime.csv",
    "/reports/shifts.csv",
    "/reports/oee.csv",
    "/reports/intelligence-summary.txt",
    "/reports",
}


def test_reports_paths_owned_by_module():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"reports paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"reports_routes"}}
    assert not wrong, f"reports paths not owned solely by reports_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} reports paths owned by reports_routes")


def test_daily_summary_owned_by_core():
    # daily-summary.txt calls analytics_summary (not the reports CSV/record CRUD);
    # it was left in main and is now grouped into core_routes, which imports
    # analytics_summary from analytics_routes.
    owners = {getattr(r, "path", ""): r.endpoint.__module__ for r in main.app.routes}
    assert owners.get("/reports/daily-summary.txt") == "core_routes", \
        "daily-summary.txt should be owned by core_routes"
    print("PASS /reports/daily-summary.txt is owned by core_routes")


def test_module_has_no_main_local_coupling():
    import reports_routes
    src = inspect.getsource(reports_routes)
    for helper in ("analytics_summary(", "generate_alerts("):
        assert helper not in src, f"reports_routes must not call main-local {helper}"
    print("PASS reports_routes has no main-local helper coupling")


def _iso_session():
    """Fresh in-memory DB with tenant scoping installed (ADR-0002)."""
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed_shift(db, tenant, name, target, actual):
    tok = T.set_current_tenant(tenant)
    try:
        row = models.ShiftData(shift_name=name, target_output=target, actual_output=actual)
        db.add(row)
        db.commit()
    finally:
        T.reset_current_tenant(tok)


def _shifts_csv_rows(db, tenant):
    """Run the export and return (header, data_rows) parsed from the CSV body."""
    tok = T.set_current_tenant(tenant)
    try:
        resp = RR.export_shifts_csv(db=db, current_user={"tenant": tenant, "role": "Admin"})
    finally:
        T.reset_current_tenant(tok)
    reader = list(csv.reader(io.StringIO(resp.body.decode("utf-8"))))
    return reader[0], reader[1:]


def test_shifts_csv_orders_by_id_deterministically():
    # Every OTHER CSV export in this module orders by id ascending; shifts.csv was
    # the one issuing a bare .all() with no ORDER BY. On SQLite a table scan returns
    # rows in rowid (insertion) order, so a data-order assertion can NOT prove the
    # fix here — exactly the limitation the quality-defect chart and cycle-count
    # picks document. So assert STRUCTURALLY that the query carries an explicit
    # order_by on ShiftData.id, which is what makes the report reproducible on
    # PostgreSQL (production), where a seqscan has no guaranteed order.
    src = inspect.getsource(RR.export_shifts_csv)
    assert ".order_by(" in src, "shifts.csv export must carry an explicit ORDER BY"
    assert "ShiftData.id" in src, "shifts.csv export must order by the stable primary key"
    print("PASS shifts.csv export orders by ShiftData.id (deterministic on Postgres)")


def test_shifts_csv_content_and_efficiency():
    # First functional coverage of a reports CSV: header, one data row per shift in
    # id order, and the efficiency column. Efficiency = round(actual/target*100),
    # with a 0-target UNPLANNED shift guarded to 0 (no divide-by-zero). Numbers are
    # derived independently of the code: 900/1000 -> 90, 150/200 -> 75, target 0 -> 0.
    db = _iso_session()
    _seed_shift(db, "TA", "Morning", 1000, 900)     # -> 90
    _seed_shift(db, "TA", "Night", 200, 150)        # -> 75
    _seed_shift(db, "TA", "Unplanned", 0, 40)       # -> 0 (zero-target divide guard)

    header, rows = _shifts_csv_rows(db, "TA")
    assert header == ["id", "shift_name", "target_output", "actual_output",
                      "efficiency_percent", "created_at"], header
    # id-ascending == insertion order here; assert the ids are strictly increasing.
    ids = [int(r[0]) for r in rows]
    assert ids == sorted(ids) and len(set(ids)) == len(ids), ids
    by_name = {r[1]: r for r in rows}
    assert by_name["Morning"][4] == "90", by_name["Morning"]
    assert by_name["Night"][4] == "75", by_name["Night"]
    assert by_name["Unplanned"][4] == "0", by_name["Unplanned"]   # 0 target -> 0, not a crash
    print("PASS shifts.csv content: header, id order, and efficiency (incl. 0-target guard)")


def _seed_machine(db, tenant, name, status="Running", utilization=85):
    tok = T.set_current_tenant(tenant)
    try:
        m = models.Machine(name=name, status=status, utilization=utilization)
        db.add(m)
        db.commit()
        db.refresh(m)
        return m.id
    finally:
        T.reset_current_tenant(tok)


def _seed_production(db, tenant, machine_id, planned, runtime, ideal, total, good, rejected):
    tok = T.set_current_tenant(tenant)
    try:
        db.add(models.ProductionRecord(
            machine_id=machine_id, planned_minutes=planned, runtime_minutes=runtime,
            ideal_cycle_time_seconds=ideal, total_count=total, good_count=good,
            rejected_count=rejected))
        db.commit()
    finally:
        T.reset_current_tenant(tok)


def _seed_downtime(db, tenant, machine_id, reason, duration):
    tok = T.set_current_tenant(tenant)
    try:
        db.add(models.DowntimeLog(machine_id=machine_id, reason=reason, duration=duration))
        db.commit()
    finally:
        T.reset_current_tenant(tok)


def _intelligence_report(db, tenant):
    tok = T.set_current_tenant(tenant)
    try:
        resp = RR.export_intelligence_summary(db=db, current_user={"tenant": tenant, "role": "Admin"})
    finally:
        T.reset_current_tenant(tok)
    return resp.body.decode("utf-8")


def test_intelligence_summary_content_and_hour_format_downtime():
    # First functional coverage of the intelligence-summary export. Every number is
    # derived independently of the code:
    #   availability = runtime/planned      = 80/100  = 0.80 -> 80%
    #   performance  = ideal*total/(rt*60)  = 48*100/(80*60) = 4800/4800 = 1.00 -> 100%
    #   quality      = good/total           = 90/100  = 0.90 -> 90%
    #   OEE          = 0.80 * 1.00 * 0.90   = 0.72          -> 72%
    # and the downtime is written in HOUR FORMAT: "2 hrs 15 min" must total 135
    # minutes (2*60 + 15), NOT 2 — the exact class of duration-parser bug the shared
    # parse_duration_to_minutes exists to prevent. A leading-digit parser would read
    # "2" and print "Total Downtime: 2 minutes".
    db = _iso_session()
    mid = _seed_machine(db, "TA", "CNC-01")
    _seed_production(db, "TA", mid, planned=100, runtime=80, ideal=48, total=100, good=90, rejected=10)
    _seed_downtime(db, "TA", mid, "Breakdown", "2 hrs 15 min")     # -> 135 minutes
    _seed_shift(db, "TA", "Morning", 1000, 900)                    # -> efficiency 90

    report = _intelligence_report(db, "TA")

    assert "Average OEE: 72%" in report, report
    assert "Availability: 80%" in report, report
    assert "Performance: 100%" in report, report
    assert "Quality: 90%" in report, report
    # The hour-format pin: 135, not 2.
    assert "Total Downtime: 135 minutes" in report, report
    assert "Top Loss Reason: Breakdown" in report, report
    # Per-shift KPI line reconciles with shifts.csv (same build_shift_kpis basis).
    assert "Morning: Target=1000 | Actual=900 | Efficiency=90% | Gap=100" in report, report
    print("PASS intelligence-summary content: OEE 72%, hour-format downtime 135 min, shift KPI")


def test_intelligence_summary_empty_tables():
    # An empty tenant must yield a clean header-only report, never a crash on an empty
    # sequence / zero denominator: no production -> OEE 0%, no shifts -> the "no data"
    # line, no alerts -> the "no alerts" line.
    db = _iso_session()
    report = _intelligence_report(db, "EMPTY")
    assert "Average OEE: 0%" in report, report
    assert "Total Downtime: 0 minutes" in report, report
    assert "No shift data available." in report, report
    assert "No active alerts." in report, report
    print("PASS intelligence-summary empty tenant: 0% OEE, no-data/no-alert lines, no crash")


def test_intelligence_summary_bounds_production_in_sql():
    # The rule-4 guard this change adds: the export must NOT hydrate the whole growing
    # production_records table into Python. Plant OEE is pooled from SQL sums
    # (production_sums) and the smart-alert feed reads only the recent-100 window its
    # /alerts/smart sibling uses — so a bare full-table ProductionRecord scan must be
    # gone from the source entirely.
    src = inspect.getsource(RR.export_intelligence_summary)
    assert "production_sums" in src, "OEE summary must pool from SQL sums, not a full hydrate"
    assert ".limit(100)" in src, "smart-alert production feed must be bounded to the recent window"
    assert "db.query(models.ProductionRecord).all()" not in src, \
        "the growing production_records table must not be hydrated whole"
    print("PASS intelligence-summary bounds production_records (SQL sums + recent-100 alert window)")


def test_intelligence_summary_oee_covers_whole_table_not_just_alert_window():
    # The summary OEE is all-time (SQL sums over the WHOLE table), while the alert feed
    # is the recent-100 window — two different, deliberate bases. Prove the summary
    # still reflects records BEYOND the 100-row alert window: seed 100 flawless records
    # (quality 100%) then a single earlier terrible one, and the pooled quality must
    # drop below 100 (the old full-hydrate summed all; the SQL sum must too). If the
    # summary had been bounded to 100 like the alerts, the 101st (oldest) record would
    # be excluded and quality would read a clean 100%.
    db = _iso_session()
    mid = _seed_machine(db, "TB", "CNC-02")
    # One terrible record first (oldest id): 100 good of 1000 -> quality 10%.
    _seed_production(db, "TB", mid, planned=100, runtime=100, ideal=60, total=1000, good=100, rejected=900)
    # Then 100 flawless records that fully fill the recent-100 alert window.
    for _ in range(100):
        _seed_production(db, "TB", mid, planned=100, runtime=100, ideal=60, total=100, good=100, rejected=0)

    report = _intelligence_report(db, "TB")
    # Pooled quality over ALL 101 records = total good / total total
    #   = (100 + 100*100) / (1000 + 100*100) = 10100 / 11000 = 0.918... -> 92%.
    # If the oldest record were dropped (bounded to 100 like alerts) it would be 100%.
    assert "Quality: 92%" in report, report
    print("PASS intelligence-summary OEE pools the WHOLE table (92%), not just the recent-100 alert window")


if __name__ == "__main__":
    test_reports_paths_owned_by_module()
    test_daily_summary_owned_by_core()
    test_module_has_no_main_local_coupling()
    test_shifts_csv_orders_by_id_deterministically()
    test_shifts_csv_content_and_efficiency()
    test_intelligence_summary_content_and_hour_format_downtime()
    test_intelligence_summary_empty_tables()
    test_intelligence_summary_bounds_production_in_sql()
    test_intelligence_summary_oee_covers_whole_table_not_just_alert_window()
    print("ALL REPORTS ROUTE TESTS PASSED")
