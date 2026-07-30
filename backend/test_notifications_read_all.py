"""Bulk "mark all notifications read" tests.

The endpoint is a bulk UPDATE, and the ADR-0002 auto-scoping hook only rewrites
SELECTs — so tenant isolation here is EXPLICIT and must be pinned: one tenant's
"mark all read" must never touch another tenant's inbox. Also covers the honest
count, idempotency, and empty-safety.

Run:  python backend/test_notifications_read_all.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import factory_ops_routes
import models
import tenancy
from database import Base


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    return sessionmaker(bind=engine)()


def _notify(db, tenant, title, status="Unread"):
    db.add(models.Notification(tenant_code=tenant, notification_type="System",
                               severity="Info", title=title,
                               message="m", status=status))


def test_marks_only_the_callers_tenant_and_counts_honestly():
    db = _fresh_session()
    _notify(db, "ACME", "a1")
    _notify(db, "ACME", "a2")
    _notify(db, "ACME", "a3", status="Read")     # already read -> not counted
    _notify(db, "BOLT", "b1")                    # another tenant -> must stay unread
    _notify(db, "BOLT", "b2")
    db.commit()

    r = factory_ops_routes.mark_all_notifications_read(
        db=db, current_user={"tenant": "ACME", "role": "Admin"})
    assert r == {"marked": 2}, r

    acme_unread = db.query(models.Notification).filter(
        models.Notification.tenant_code == "ACME",
        models.Notification.status != "Read").count()
    bolt_unread = db.query(models.Notification).filter(
        models.Notification.tenant_code == "BOLT",
        models.Notification.status != "Read").count()
    assert acme_unread == 0, "the caller's inbox should be cleared"
    assert bolt_unread == 2, "another tenant's inbox must be untouched by a bulk UPDATE"
    print("PASS mark-all-read clears only the caller's tenant and counts honestly")


def test_idempotent_and_empty_safe():
    db = _fresh_session()
    r = factory_ops_routes.mark_all_notifications_read(
        db=db, current_user={"tenant": "ACME", "role": "Admin"})
    assert r == {"marked": 0}          # empty inbox -> zero, no crash

    _notify(db, "ACME", "a1")
    db.commit()
    first = factory_ops_routes.mark_all_notifications_read(
        db=db, current_user={"tenant": "ACME", "role": "Admin"})
    second = factory_ops_routes.mark_all_notifications_read(
        db=db, current_user={"tenant": "ACME", "role": "Admin"})
    assert first == {"marked": 1} and second == {"marked": 0}   # idempotent
    print("PASS mark-all-read is idempotent and empty-safe")


def test_null_status_notification_is_marked_read():
    """A notification with a genuine NULL status must be flipped by mark-all-read.
    status is Column(String, default="Unread") WITHOUT nullable=False, so a raw-SQL
    / migration / cleared-field write can store a real NULL — and in SQL a bare
    `status != 'Read'` is NULL (not TRUE) for that row, so the bulk UPDATE SKIPPED
    it: it could never be cleared and was left out of the honest `marked` count.
    OR-ing the NULL in (a NULL status is not the terminal 'Read', i.e. still unread)
    flips and counts it — the same convention the generator's dedup uses."""
    db = _fresh_session()
    _notify(db, "ACME", "plain")                     # ordinary Unread
    # Seed one whose status we clear to a genuine SQL NULL (insert real, then UPDATE
    # to None — the Column default only fills an OMITTED value at INSERT).
    stale = models.Notification(tenant_code="ACME", notification_type="System",
                                severity="Info", title="stale", message="m",
                                status="Unread")
    db.add(stale)
    db.commit()
    stale.status = None
    db.commit()
    assert stale.status is None

    r = factory_ops_routes.mark_all_notifications_read(
        db=db, current_user={"tenant": "ACME", "role": "Admin"})
    # Both the plain Unread and the NULL-status one must flip -> honestly counted.
    assert r == {"marked": 2}, r
    stale_after = db.query(models.Notification).filter(
        models.Notification.title == "stale").first()
    assert stale_after.status == "Read", stale_after.status
    print("PASS mark-all-read flips a NULL-status notification and counts it")


if __name__ == "__main__":
    test_marks_only_the_callers_tenant_and_counts_honestly()
    test_idempotent_and_empty_safe()
    test_null_status_notification_is_marked_read()
    print("\nALL NOTIFICATIONS READ-ALL TESTS PASSED")
