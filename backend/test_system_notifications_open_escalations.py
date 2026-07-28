"""System-notification generator reconciles its open-escalation count with the
dashboard headline (ADR-0007 read-model honesty, rule-3 same-basis reconciliation).

POST /notifications/generate-system-notifications raises an "Open escalations
pending — N escalation(s) still require action" notice. That N MUST equal the
open-escalations count the tenant already sees on /analytics/system-health
(get_system_health), or the notification undercounts the very backlog it exists
to flag.

Escalation.status is Column(String, default="Open") WITHOUT nullable=False, so a
raw-SQL / migration / cleared write can store a genuine NULL. The headline OR's
that NULL back in (counts it as open); the generator used a bare
`status != 'Resolved'`, which is NULL — not TRUE — in SQL for a NULL-status row
and silently dropped it. This pins the two to the same basis.

Run:  cd backend && python test_system_notifications_open_escalations.py   (exit 0 = pass)
"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models
import analytics_routes
import factory_ops_routes
from database import Base


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _esc(no, status):
    # title/severity/owner/department are NOT NULL — supply them; status carries a
    # Python-side default only, so an explicit None would still become "Open" on
    # insert. A genuine NULL is forced with raw SQL below (see _null_status).
    return models.Escalation(title=f"E-{no}", severity="High", owner="Ops",
                             department="Maintenance", status=status)


def _null_status(db, *titles):
    # Force a genuine SQL NULL — the state a raw-SQL / migration / cleared update
    # produces in production, which the ORM's column default would otherwise mask.
    for t in titles:
        db.execute(text("UPDATE escalations SET status = NULL WHERE title = :t"), {"t": t})
    db.commit()
    db.expire_all()


def _open_pending_count(db):
    """The N the generator wrote into the 'Open escalations pending' notice, or
    None if it raised no such notice."""
    row = (db.query(models.Notification)
           .filter(models.Notification.title == "Open escalations pending")
           .first())
    if row is None:
        return None
    # message: "N escalation(s) still require action."
    return int(row.message.split(" ", 1)[0])


def test_notification_open_count_includes_null_status_and_matches_headline():
    db = _fresh_session()
    db.add_all([
        _esc(1, "Open"),          # open
        _esc(2, "In Progress"),   # open (anything not Resolved)
        _esc(3, "Resolved"),      # NOT open
        _esc(4, "Open"),          # will be NULL'd -> still open
    ])
    db.commit()
    _null_status(db, "E-4")

    # Independently-derived truth: open = E-1, E-2, E-4 (NULL) = 3; E-3 excluded.
    expected_open = 3

    # The dashboard headline the notification must agree with.
    health = analytics_routes.get_system_health(db=db, current_user={})
    assert health["open_escalations"] == expected_open, health["open_escalations"]

    factory_ops_routes.generate_system_notifications(db=db, current_user={})
    notified = _open_pending_count(db)
    assert notified == expected_open, notified

    # Same basis, both surfaces — no NULL-status row silently dropped from either.
    assert notified == health["open_escalations"]
    print(f"PASS system-notification open count ({notified}) includes the NULL-status "
          f"escalation and reconciles with the /analytics/system-health headline "
          f"({health['open_escalations']})")


def test_all_resolved_raises_no_open_escalation_notice():
    # Every escalation Resolved -> zero open -> the generator must not fabricate a
    # backlog notice (an honest 0, not a rendered floor).
    db = _fresh_session()
    db.add_all([_esc(1, "Resolved"), _esc(2, "Resolved")])
    db.commit()

    health = analytics_routes.get_system_health(db=db, current_user={})
    assert health["open_escalations"] == 0, health["open_escalations"]

    factory_ops_routes.generate_system_notifications(db=db, current_user={})
    assert _open_pending_count(db) is None
    print("PASS all-resolved register raises no 'open escalations pending' notice (honest 0)")


def test_empty_register_is_safe_and_silent():
    # No escalations at all -> no crash, no open-escalation notice.
    db = _fresh_session()
    health = analytics_routes.get_system_health(db=db, current_user={})
    assert health["open_escalations"] == 0

    factory_ops_routes.generate_system_notifications(db=db, current_user={})
    assert _open_pending_count(db) is None
    print("PASS empty register: no open-escalation notice, no crash")


if __name__ == "__main__":
    test_notification_open_count_includes_null_status_and_matches_headline()
    test_all_resolved_raises_no_open_escalation_notice()
    test_empty_register_is_safe_and_silent()
    print("ALL SYSTEM-NOTIFICATION OPEN-ESCALATION TESTS PASSED")
