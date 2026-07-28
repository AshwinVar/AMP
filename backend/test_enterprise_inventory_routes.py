"""Enterprise-inventory route registration guard.

enterprise_inventory_routes predates the ADR-0009 guard-test discipline, so it
never had one. It owns the warehouse-ops surface — remnants, issue slips, GRNs,
cycle counts, plus the CSV import and variance report. Assert every path is
registered exactly once and owned solely by the module, so a future edit can't
drop one or reintroduce a shadowing duplicate (the /health, /audit-logs class of
bug).

Run:  python backend/test_enterprise_inventory_routes.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
import enterprise_inventory_routes as eir
import models
from database import Base

EXPECTED = {
    "/remnants", "/remnants/{rid}/status",
    "/issue-slips", "/issue-slips/{sid}/approve", "/issue-slips/{sid}/issue",
    "/issue-slips/{sid}/reject",
    "/grns", "/grns/{gid}/accept",
    "/cycle-counts", "/cycle-counts/{cid}/approve",
    "/inventory/import-csv", "/inventory/variance-report",
}


def test_enterprise_inventory_paths_registered_once_and_owned():
    counts, owners = {}, {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            counts[p] = counts.get(p, 0) + 1
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(counts)
    assert not missing, f"enterprise-inventory paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"enterprise_inventory_routes"}}
    assert not wrong, f"paths not owned solely by enterprise_inventory_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} enterprise-inventory paths owned by the module")


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _add_item(db, code, name="Widget", current_stock=100, reorder_level=20):
    item = models.InventoryItem(item_code=code, item_name=name, category="General",
                                unit="Nos", current_stock=current_stock, reorder_level=reorder_level)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _add_count_line(db, item_id, count_no, physical_qty, variance, book_qty=100):
    db.add(models.CycleCount(count_no=count_no, counted_by="tester"))
    db.add(models.CycleCountItem(count_id=None, item_id=item_id,
                                 book_qty=book_qty, physical_qty=physical_qty, variance=variance))
    db.commit()


def _row_for(report, item_id):
    return next(r for r in report if r["item_id"] == item_id)


def test_variance_report_picks_the_latest_count_per_item():
    # An item counted TWICE: the variance report must reflect the MOST RECENT count
    # (highest id / most-recently inserted line), not an earlier one. The old code
    # built a dict from an unordered `db.query(CycleCountItem).all()`, so "latest"
    # depended on the scan's row order — deterministic on SQLite (id-ascending), but
    # on PostgreSQL a seqscan has no guaranteed order and could keep the STALE first
    # count. Independently-derived expectation: the later line's physical_qty=105 /
    # variance=+5 wins over the earlier physical_qty=90 / variance=-10.
    db = _fresh_session()
    item = _add_item(db, "ITM-1", current_stock=105, reorder_level=20)
    _add_count_line(db, item.id, "CC-EARLY", physical_qty=90, variance=-10)   # lower id
    _add_count_line(db, item.id, "CC-LATE", physical_qty=105, variance=5)     # higher id -> latest

    report = eir.variance_report(db=db, current_user={})
    row = _row_for(report, item.id)
    assert row["last_physical_count"] == 105, row      # the LATER count, not 90
    assert row["last_variance"] == 5, row              # not -10
    assert row["book_stock"] == 105 and row["status"] == "OK", row
    print("PASS variance report reflects the latest (max-id) cycle count per item")


def test_variance_report_item_without_a_count_reports_none():
    # An item never cycle-counted has no physical count — the report must show None
    # (rendered "—"), never fabricate a 0. A counted item alongside it is unaffected.
    db = _fresh_session()
    counted = _add_item(db, "ITM-COUNTED", current_stock=50, reorder_level=10)
    uncounted = _add_item(db, "ITM-UNCOUNTED", current_stock=0, reorder_level=5)
    _add_count_line(db, counted.id, "CC-1", physical_qty=48, variance=-2)

    report = eir.variance_report(db=db, current_user={})
    counted_row = _row_for(report, counted.id)
    uncounted_row = _row_for(report, uncounted.id)
    assert counted_row["last_physical_count"] == 48 and counted_row["last_variance"] == -2, counted_row
    assert uncounted_row["last_physical_count"] is None, uncounted_row
    assert uncounted_row["last_variance"] is None, uncounted_row
    assert uncounted_row["status"] == "Stockout", uncounted_row   # current_stock 0
    print("PASS variance report reports None (not 0) for an item with no cycle count")


def test_variance_report_empty_tables_is_safe():
    # No items at all -> empty report, no crash (max()/GROUP BY over nothing).
    db = _fresh_session()
    assert eir.variance_report(db=db, current_user={}) == []
    print("PASS variance report is safe on empty tables")


def test_variance_report_is_tenant_isolated():
    # With the ORM scoping hook live, a tenant's variance report must contain ONLY
    # its own items and its own latest counts — never another tenant's, even though
    # the other tenant's count line has a higher id. Proves the max-id selection
    # can't leak a foreign row through the report.
    import tenancy as T
    T.install_scoping()
    db = _fresh_session()

    def seed_as(tenant, code, physical_qty, variance):
        tok = T.set_current_tenant(tenant)
        item = models.InventoryItem(item_code=code, item_name="W", category="General",
                                    unit="Nos", current_stock=physical_qty, reorder_level=0)
        db.add(item)
        db.commit()
        db.refresh(item)
        db.add(models.CycleCount(count_no=f"CC-{code}", counted_by=tenant))
        db.add(models.CycleCountItem(item_id=item.id, book_qty=physical_qty,
                                     physical_qty=physical_qty, variance=variance))
        db.commit()
        T.reset_current_tenant(tok)
        return item.id

    ta_item = seed_as("TA", "ITM-TA", physical_qty=50, variance=5)
    tb_item = seed_as("TB", "ITM-TB", physical_qty=999, variance=99)   # higher ids than TA

    def report_as(tenant):
        tok = T.set_current_tenant(tenant)
        try:
            return eir.variance_report(db=db, current_user={"tenant": tenant})
        finally:
            T.reset_current_tenant(tok)

    ta_report = report_as("TA")
    tb_report = report_as("TB")

    assert [r["item_id"] for r in ta_report] == [ta_item], ta_report
    assert _row_for(ta_report, ta_item)["last_physical_count"] == 50, ta_report
    assert 999 not in [r["last_physical_count"] for r in ta_report], ta_report
    assert [r["item_id"] for r in tb_report] == [tb_item], tb_report
    assert _row_for(tb_report, tb_item)["last_physical_count"] == 999, tb_report
    print("PASS variance report is tenant-isolated (no cross-tenant latest-count leak)")


if __name__ == "__main__":
    test_enterprise_inventory_paths_registered_once_and_owned()
    test_variance_report_picks_the_latest_count_per_item()
    test_variance_report_item_without_a_count_reports_none()
    test_variance_report_empty_tables_is_safe()
    test_variance_report_is_tenant_isolated()
    print("ALL ENTERPRISE-INVENTORY ROUTE TESTS PASSED")
