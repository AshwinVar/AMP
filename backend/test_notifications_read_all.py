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


if __name__ == "__main__":
    test_marks_only_the_callers_tenant_and_counts_honestly()
    test_idempotent_and_empty_safe()
    print("\nALL NOTIFICATIONS READ-ALL TESTS PASSED")
