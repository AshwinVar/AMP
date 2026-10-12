"""The data-gap notification: a flagged hole, not a straight line.

When the edge reports `gap_before` -- it was down across a restart, or its queue
overflowed -- the operator should be TOLD a stretch of production is missing,
with the window when the edge knew it. These tests pin that a gap raises exactly
one Warning notification carrying the window, that an ordinary message raises
none, and that a crash-looping gateway does not flood the feed.

Run:  python backend/test_data_gap_notification.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from mqtt_service import _record_data_gap


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _machine(db, name="IMM-01", tenant="DEFAULT"):
    m = models.Machine(name=name, tenant_code=tenant, status="Running")
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _gaps(db):
    return db.query(models.Notification).filter(
        models.Notification.notification_type == "data_gap").all()


def test_a_gap_with_a_window_raises_one_warning():
    db = _db()
    m = _machine(db)
    _record_data_gap(db, "DEFAULT", m,
                     {"gap_before": True, "gap_from": 1700000000.0, "gap_to": 1700003600.0})
    rows = _gaps(db)
    assert len(rows) == 1 and rows[0].severity == "Warning", rows
    assert rows[0].title == f"Data gap at {m.name}", rows[0].title
    assert "not reporting between" in rows[0].message and "not recorded" in rows[0].message, rows[0].message
    print("PASS a gap with a window raises one Warning naming the window")


def test_a_gap_without_a_window_still_warns():
    db = _db()
    m = _machine(db)
    _record_data_gap(db, "DEFAULT", m, {"gap_before": True})   # overflow gap: no window
    rows = _gaps(db)
    assert len(rows) == 1 and "some production may be missing" in rows[0].message, rows
    print("PASS a windowless gap still raises a warning (the overflow case)")


def test_an_ordinary_message_raises_nothing():
    db = _db()
    m = _machine(db)
    _record_data_gap(db, "DEFAULT", m, {"total_count": 5})     # no gap_before
    assert _gaps(db) == []
    print("PASS a normal message raises no data-gap notification")


def test_a_crash_loop_does_not_flood():
    db = _db()
    m = _machine(db)
    for _ in range(5):
        _record_data_gap(db, "DEFAULT", m,
                         {"gap_before": True, "gap_from": 1.0, "gap_to": 300.0})
    assert len(_gaps(db)) == 1, "a still-unread gap must dedup per machine"
    print("PASS repeated gaps while unread do not flood (deduped per machine)")


if __name__ == "__main__":
    test_a_gap_with_a_window_raises_one_warning()
    test_a_gap_without_a_window_still_warns()
    test_an_ordinary_message_raises_nothing()
    test_a_crash_loop_does_not_flood()
    print("ALL DATA-GAP NOTIFICATION TESTS PASSED")
