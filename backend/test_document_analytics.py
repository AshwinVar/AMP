"""Compliance-document analytics endpoint tests (/analytics/documents).

The endpoint reports the document register: a status split (draft / approved /
under-review / obsolete), a count of documents past their review date, and
per-type / per-department breakdowns. It was the last analytics rollup still
hydrating the whole (growing) compliance_documents table into Python to bucket
it with list comprehensions; it now aggregates in SQL with GROUP BY / COUNT,
exactly like /analytics/maintenance next to it. Pinned here (the route test only
covers registration):

  * The status / type / department buckets and totals are independently derived
    from the fixture, and the breakdowns reconcile back to the whole.
  * 'review_due' counts a doc past its review date and NOT Obsolete. A
    NULL-approval_status past-due doc must STILL count (Python `None != "Obsolete"`
    is True) — SQL's `!= 'Obsolete'` is NULL, not TRUE, for a NULL status and would
    silently drop it, so the OR status IS NULL branch keeps the same basis.
  * An empty register returns honest zeros / empty dicts, never a crash.

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_document_analytics.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from analytics_routes import get_document_analytics

USER = {"tenant": "DEFAULT"}


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _doc(no, doc_type, department, status, review_offset_days):
    today = datetime.utcnow().date()
    return models.ComplianceDocument(
        document_no=no, title="T", document_type=doc_type, department=department,
        owner="qa", approval_status=status,
        review_due_date=today + timedelta(days=review_offset_days),
    )


def test_document_analytics_buckets_and_reconciles():
    # A mixed register across statuses / types / departments, some past their
    # review date. Every expected number below is derived by hand from the fixture.
    db = _fresh_session()
    db.add_all([
        # no, type, department, status, review_offset (negative = past due)
        _doc("D-1", "SOP", "Quality", "Draft", 10),          # future review
        _doc("D-2", "SOP", "Quality", "Approved", -5),       # past due, Approved -> review_due
        _doc("D-3", "WI", "Production", "Under Review", -1),  # past due, Under Review -> review_due
        _doc("D-4", "WI", "Production", "Obsolete", -30),     # past due but Obsolete -> NOT review_due
        _doc("D-5", "Policy", "Safety", "Approved", 3),       # future review
    ])
    db.commit()

    out = get_document_analytics(db=db, current_user=USER)

    assert out["total_documents"] == 5, out["total_documents"]
    # status buckets
    assert out["draft"] == 1, out["draft"]                 # D-1
    assert out["approved"] == 2, out["approved"]           # D-2, D-5
    assert out["under_review"] == 1, out["under_review"]   # D-3
    assert out["obsolete"] == 1, out["obsolete"]           # D-4
    # review_due = past-dated AND not Obsolete -> D-2, D-3 (D-4 is Obsolete, D-1/D-5 future)
    assert out["review_due"] == 2, out["review_due"]
    # per-type / per-department breakdowns
    assert out["type_counts"] == {"SOP": 2, "WI": 2, "Policy": 1}, out["type_counts"]
    assert out["department_counts"] == {"Quality": 2, "Production": 2, "Safety": 1}, out["department_counts"]

    # Reconcile denominators (rule-3): the four status buckets partition this
    # register (every doc has a canonical status), and both breakdowns sum back
    # to the total document count.
    assert out["draft"] + out["approved"] + out["under_review"] + out["obsolete"] == out["total_documents"]
    assert sum(out["type_counts"].values()) == out["total_documents"]
    assert sum(out["department_counts"].values()) == out["total_documents"]
    print("PASS document analytics: SQL buckets reconcile (5 docs, review_due=2)")


def test_null_status_past_due_still_counts_as_review_due():
    # approval_status is String default="Draft" (not NOT NULL). A past-due doc whose
    # status is a genuine NULL (raw SQL / migration / cleared update) must still be
    # review_due: the old Python `None != "Obsolete"` was True. SQL's plain
    # `approval_status != 'Obsolete'` is NULL (not TRUE) for a NULL and would drop
    # the row — the OR status IS NULL branch keeps it counted.
    db = _fresh_session()
    d = _doc("D-N", "SOP", "Quality", "Draft", -2)   # past due
    db.add(d)
    db.commit()
    # The ORM's column default fills an explicit None, so drop to raw SQL for a real NULL.
    db.execute(text("UPDATE compliance_documents SET approval_status = NULL WHERE document_no = 'D-N'"))
    db.commit()
    db.expire_all()

    out = get_document_analytics(db=db, current_user=USER)
    assert out["total_documents"] == 1, out["total_documents"]
    # NULL status is none of Draft/Approved/Under Review/Obsolete...
    assert out["draft"] == 0 and out["approved"] == 0
    assert out["under_review"] == 0 and out["obsolete"] == 0
    # ...but the doc is past its review date and not Obsolete, so it IS review_due.
    assert out["review_due"] == 1, out["review_due"]
    print("PASS document analytics: NULL-status past-due doc still counts as review_due")


def test_review_due_boundary_today_is_not_yet_due():
    # 'due' is strictly PAST the review date (< today), matching the old Python
    # `review_due_date < today`. A doc whose review date is exactly today is not
    # yet overdue.
    db = _fresh_session()
    db.add_all([
        _doc("B-1", "SOP", "Quality", "Approved", 0),    # review date == today -> NOT due
        _doc("B-2", "SOP", "Quality", "Approved", -1),   # yesterday -> due
    ])
    db.commit()

    out = get_document_analytics(db=db, current_user=USER)
    assert out["review_due"] == 1, out["review_due"]     # only B-2
    print("PASS document analytics: review date == today is not yet due (strict <)")


def test_empty_register_is_all_zeros_no_crash():
    out = get_document_analytics(db=_fresh_session(), current_user=USER)
    assert out["total_documents"] == 0
    assert out["draft"] == 0 and out["approved"] == 0
    assert out["under_review"] == 0 and out["obsolete"] == 0
    assert out["review_due"] == 0
    assert out["type_counts"] == {}
    assert out["department_counts"] == {}
    print("PASS empty document register -> zeros, empty dicts, no crash")


if __name__ == "__main__":
    test_document_analytics_buckets_and_reconciles()
    test_null_status_past_due_still_counts_as_review_due()
    test_review_due_boundary_today_is_not_yet_due()
    test_empty_register_is_all_zeros_no_crash()
    print("ALL DOCUMENT ANALYTICS TESTS PASSED")
