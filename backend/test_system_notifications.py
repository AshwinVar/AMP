"""Behavioural tests for the system-notification generator (factory_ops_routes).

POST /notifications/generate-system-notifications rolls the current factory state
into Notification rows: one per machine in Breakdown, one "open escalations"
summary, and one "low stock" summary. The two summary notifications only need a
COUNT of matching rows, so they now count in SQL (func.count) instead of pulling
the whole (growing) escalation / inventory tables into Python to len() them —
the rule-4 bound already applied to the quality / costing / orders rollups.

These tests pin the counts to independently hand-derived numbers, and cover the
edges that matter for the count: an empty factory (no summary notifications), a
NULL stock / reorder level (excluded by SQL NULL comparison, exactly like the
low-stock filter used elsewhere), Resolved escalations excluded from the open
count, and dedup (a second run adds nothing while the notifications are unread).

Run:  python backend/test_system_notifications.py     (exit 0 = pass)
"""
import inspect

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from factory_ops_routes import generate_system_notifications

USER = {"role": "Admin", "tenant_code": "DEFAULT"}


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _machine(name, status="Running"):
    return models.Machine(name=name, status=status, utilization=50)


def _escalation(title, status="Open"):
    return models.Escalation(
        title=title, severity="Medium", owner="Ops", department="Maintenance",
        status=status, source="Test",
    )


def _item(code, stock, reorder):
    return models.InventoryItem(
        item_code=code, item_name=code, category="Raw", unit="pcs",
        current_stock=stock, reorder_level=reorder,
    )


def _run(db):
    return generate_system_notifications(db=db, current_user=USER)


def _notif(db, title):
    return db.query(models.Notification).filter(models.Notification.title == title).first()


def test_summary_counts_are_hand_derived():
    """3 unresolved escalations (a 4th is Resolved -> excluded) and 2 genuinely
    low-stock items (a 3rd is healthy) => the summary notifications must report
    exactly 3 and 2, and a Breakdown machine gets its own notification."""
    db = _fresh_session()
    db.add_all([
        _machine("CNC-1", status="Breakdown"),
        _machine("CNC-2", status="Running"),
        _escalation("E-open-1", status="Open"),
        _escalation("E-open-2", status="In Progress"),
        _escalation("E-open-3", status="Open"),
        _escalation("E-done", status="Resolved"),      # excluded from the open count
        _item("LOW-A", stock=2, reorder=10),           # 2 <= 10  -> low
        _item("LOW-B", stock=10, reorder=10),          # 10 <= 10 -> low (boundary)
        _item("OK-C", stock=50, reorder=10),           # 50 <= 10 -> NOT low
    ])
    db.commit()

    result = _run(db)

    esc = _notif(db, "Open escalations pending")
    assert esc is not None and "3 escalation(s)" in esc.message, esc and esc.message
    low = _notif(db, "Low stock items detected")
    assert low is not None and "2 item(s)" in low.message, low and low.message
    assert _notif(db, "Machine breakdown: CNC-1") is not None
    assert _notif(db, "Machine breakdown: CNC-2") is None   # not in breakdown
    # 1 breakdown + 1 escalation summary + 1 low-stock summary
    assert result["created"] == 3, result
    print("PASS system notifications report hand-derived counts (3 escalations, 2 low-stock)")


def test_empty_factory_creates_nothing():
    """No machines / escalations / items => no summary notifications at all
    (the count is a real 0, not a crash on an empty aggregate)."""
    db = _fresh_session()
    result = _run(db)
    assert result["created"] == 0, result
    assert _notif(db, "Open escalations pending") is None
    assert _notif(db, "Low stock items detected") is None
    print("PASS empty factory generates no system notifications")


def test_null_stock_or_level_not_counted_as_low():
    """current_stock / reorder_level are nullable (Integer default=0, not NOT
    NULL). A NULL on either side makes `current_stock <= reorder_level` NULL in
    SQL, so the item is excluded from the low-stock count — matching the low-stock
    filter used by the escalation generator, and never crashing on None <= None."""
    db = _fresh_session()
    # Insert with real values first, then clear the columns to a genuine SQL NULL
    # (the Column(Integer, default=0) default only fills an OMITTED value at INSERT,
    # so an explicit None on a pending row becomes 0 — a NULL only arises from a
    # later raw-SQL / cleared UPDATE, which is exactly what this reproduces).
    null_stock = _item("NULL-STOCK", stock=1, reorder=10)   # will clear stock
    null_level = _item("NULL-LEVEL", stock=5, reorder=1)    # will clear reorder
    real_low = _item("REAL-LOW", stock=1, reorder=5)        # 1 <= 5 -> the only low one
    db.add_all([null_stock, null_level, real_low])
    db.commit()
    null_stock.current_stock = None                          # NULL <= 10 -> excluded
    null_level.reorder_level = None                          # 5 <= NULL -> excluded
    db.commit()
    assert null_stock.current_stock is None and null_level.reorder_level is None
    _run(db)
    low = _notif(db, "Low stock items detected")
    assert low is not None and "1 item(s)" in low.message, low and low.message
    print("PASS NULL stock/level excluded from the low-stock count (no crash)")


def test_dedup_second_run_adds_nothing():
    """While the summary notifications are still Unread, a second generation run
    must not duplicate them (the existing-unread guard)."""
    db = _fresh_session()
    db.add_all([_escalation("E1", status="Open"), _item("LOW", stock=1, reorder=5)])
    db.commit()

    first = _run(db)
    assert first["created"] == 2, first
    second = _run(db)
    assert second["created"] == 0, second
    assert db.query(models.Notification).filter(
        models.Notification.title == "Open escalations pending"
    ).count() == 1
    print("PASS a second run does not duplicate the summary notifications")


def test_dedup_matches_a_null_status_notification():
    """A pre-existing summary notification with a genuine NULL status must still
    block a duplicate. status is Column(String, default="Unread") WITHOUT
    nullable=False, so a raw-SQL / migration / cleared-field write can store a real
    NULL — and in SQL a bare `status != 'Read'` is NULL (not TRUE) for that row, so
    the dedup MISSED it and piled a fresh duplicate on every run. OR-ing the NULL in
    (a NULL status is not 'Read', i.e. still unread) blocks the duplicate — the same
    convention this generator already applies to the open-escalation count."""
    db = _fresh_session()
    db.add(_escalation("E1", status="Open"))
    # Seed the "Open escalations pending" summary, then clear its status to a genuine
    # SQL NULL. The Column(String, default="Unread") default only fills an OMITTED
    # value at INSERT, so a NULL only arises from a later cleared UPDATE — exactly
    # what this reproduces (mirrors the null-stock fixture above).
    stale = models.Notification(notification_type="Escalation", severity="Warning",
                                title="Open escalations pending", message="m",
                                status="Unread")
    db.add(stale)
    db.commit()
    stale.status = None
    db.commit()
    assert stale.status is None

    result = _run(db)

    # The NULL-status summary already exists and is not Read, so the generator must
    # NOT create a second one — exactly one "Open escalations pending" survives.
    assert db.query(models.Notification).filter(
        models.Notification.title == "Open escalations pending"
    ).count() == 1, "duplicated a NULL-status notification"
    assert result["created"] == 0, result
    print("PASS dedup matches a NULL-status notification (no duplicate)")


def test_counts_done_in_sql_not_a_python_scan():
    """Guard against a regression back to db.query(Model).all() + len(...) on the
    growing escalation / inventory tables (rule-4). The counts must be func.count
    aggregates, and the old .all()-then-len() pattern must be gone."""
    src = inspect.getsource(generate_system_notifications)
    assert "func.count(models.Escalation.id)" in src, src
    assert "func.count(models.InventoryItem.id)" in src, src
    assert "db.query(models.Escalation).filter" not in src, (
        "open-escalation count regressed to a whole-table Python scan"
    )
    assert "db.query(models.InventoryItem).filter" not in src, (
        "low-stock count regressed to a whole-table Python scan"
    )
    print("PASS system-notification counts are bounded in SQL, not a Python scan")


if __name__ == "__main__":
    test_summary_counts_are_hand_derived()
    test_empty_factory_creates_nothing()
    test_null_stock_or_level_not_counted_as_low()
    test_dedup_second_run_adds_nothing()
    test_dedup_matches_a_null_status_notification()
    test_counts_done_in_sql_not_a_python_scan()
    print("ALL SYSTEM-NOTIFICATION TESTS PASSED")
