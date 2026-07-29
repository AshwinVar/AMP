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


if __name__ == "__main__":
    test_reports_paths_owned_by_module()
    test_daily_summary_owned_by_core()
    test_module_has_no_main_local_coupling()
    test_shifts_csv_orders_by_id_deterministically()
    test_shifts_csv_content_and_efficiency()
    print("ALL REPORTS ROUTE TESTS PASSED")
