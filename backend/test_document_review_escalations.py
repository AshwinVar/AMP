"""Document review-escalation generator tests (POST /documents/generate-review-escalations).

The generator raises one Escalation per OVERDUE controlled document — past its
review_due_date and NOT retired (Obsolete) — and is idempotent (an unresolved
escalation with the same title is not duplicated). This is the SAME "overdue"
set the /analytics/documents 'review_due' count and the /compliance read-model
report, so the three must agree.

Pinned here (the route test only covers registration):
  * an overdue document raises exactly one escalation, and a re-run is idempotent;
  * an Obsolete overdue document raises nothing (retired -> no review debt);
  * a future-dated document raises nothing;
  * a genuine NULL approval_status is NOT Obsolete -> still counts as overdue ->
    raises an escalation. approval_status is String default="Draft" (not NOT
    NULL), so a raw-SQL / migration / cleared-field row can hold a real NULL;
    SQL's `approval_status != 'Obsolete'` is NULL — not TRUE — for that row, so
    the bare predicate silently DROPPED it, leaving the generator raising fewer
    escalations than the analytics count / read-model claimed overdue. Regression
    guard for the or_(approval_status IS NULL, ...) fix that reconciles them.

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_document_review_escalations.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from factory_ops_routes import generate_document_review_escalations

USER = {"tenant": "DEFAULT", "sub": "tester"}


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _doc(no, approval_status, review_due_date):
    return models.ComplianceDocument(
        document_no=no, title=f"Doc {no}", document_type="SOP",
        department="Quality", owner="qa-lead",
        approval_status=approval_status, review_due_date=review_due_date,
    )


def _titles(db):
    return {e.title for e in db.query(models.Escalation).all()}


def test_overdue_document_raises_one_escalation_idempotently():
    db = _fresh_session()
    today = datetime.utcnow().date()
    db.add(_doc("DOC-1", "Approved", today - timedelta(days=2)))    # overdue
    db.add(_doc("DOC-2", "Obsolete", today - timedelta(days=3)))    # retired -> no
    db.add(_doc("DOC-3", "Draft", today + timedelta(days=1)))       # future -> no
    db.commit()

    first = generate_document_review_escalations(db=db, current_user=USER)
    assert first == {"created": 1}, first
    assert _titles(db) == {"Document review overdue: DOC-1"}, _titles(db)

    # Idempotent: an unresolved escalation with the same title is not re-created.
    second = generate_document_review_escalations(db=db, current_user=USER)
    assert second == {"created": 0}, second
    assert db.query(models.Escalation).count() == 1
    print("PASS overdue document raises one escalation; re-run is idempotent")


def test_null_approval_status_overdue_document_raises_an_escalation():
    """A NULL-approval-status overdue document is NOT Obsolete -> still overdue ->
    gets an escalation, reconciling the generator with the analytics 'review_due'
    count and the /compliance read-model (all three OR the NULL in). The bare
    `approval_status != 'Obsolete'` filter dropped it (SQL three-valued logic)."""
    db = _fresh_session()
    today = datetime.utcnow().date()
    db.add(_doc("DOC-1", "Approved", today - timedelta(days=2)))    # overdue
    db.add(_doc("DOC-2", "Draft", today - timedelta(days=1)))       # NULL'd below -> overdue
    db.add(_doc("DOC-3", "Draft", today + timedelta(days=2)))       # NULL'd below but FUTURE -> no
    db.commit()
    # Force genuine NULLs (the ORM default would refill an explicit None on insert).
    db.execute(text("UPDATE compliance_documents SET approval_status = NULL "
                    "WHERE document_no IN ('DOC-2', 'DOC-3')"))
    db.commit()
    db.expire_all()

    out = generate_document_review_escalations(db=db, current_user=USER)
    # DOC-1 (Approved, past) + DOC-2 (NULL, past) = 2. DOC-3 is NULL but future -> excluded.
    assert out == {"created": 2}, out
    assert _titles(db) == {"Document review overdue: DOC-1",
                           "Document review overdue: DOC-2"}, _titles(db)
    print("PASS NULL-approval overdue document raises an escalation (2, not 1); future NULL excluded")


def test_empty_and_all_obsolete_raise_nothing():
    db = _fresh_session()
    today = datetime.utcnow().date()
    # empty register
    assert generate_document_review_escalations(db=db, current_user=USER) == {"created": 0}
    # only an Obsolete past-due doc -> retired -> nothing overdue
    db.add(_doc("DOC-9", "Obsolete", today - timedelta(days=5)))
    db.commit()
    assert generate_document_review_escalations(db=db, current_user=USER) == {"created": 0}
    assert db.query(models.Escalation).count() == 0
    print("PASS empty register and all-obsolete raise no escalations")


if __name__ == "__main__":
    test_overdue_document_raises_one_escalation_idempotently()
    test_null_approval_status_overdue_document_raises_an_escalation()
    test_empty_and_all_obsolete_raise_nothing()
    print("ALL DOCUMENT REVIEW-ESCALATION TESTS PASSED")
