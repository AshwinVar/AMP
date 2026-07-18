"""Compliance document summary read-model tests (ADR-0007).

The controlled-document review load: overdue / due-soon / pending-approval counts,
status breakdown, and the docs to review next. Run:
    python backend/test_compliance.py     (exit 0 = pass)
"""
from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import compliance


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _doc(no, status, due_offset, dtype="SOP"):
    return models.ComplianceDocument(document_no=no, title=f"Doc {no}", document_type=dtype,
                                     department="Quality", version="1.0", owner="QA Lead",
                                     approval_status=status, review_due_date=date.today() + timedelta(days=due_offset))


def test_compliance_summary_rolls_up_the_review_load():
    db = _fresh_session()
    db.add_all([
        _doc("D-1", "Approved", -5),      # overdue review
        _doc("D-2", "Approved", 10),      # due soon (<= 30 days)
        _doc("D-3", "Draft", 60),         # future + not approved
        _doc("D-4", "In Review", 200),    # far future + not approved
    ])
    db.commit()

    s = compliance.build_compliance_summary(db, "DEFAULT")
    assert s["total"] == 4
    assert s["overdue"] == 1                  # D-1
    assert s["due_soon"] == 1                 # D-2 (within 30 days)
    assert s["pending_approval"] == 2         # D-3 Draft + D-4 In Review
    # status breakdown present
    assert {"status": "Approved", "count": 2} in s["by_status"]
    # to-review: overdue first (D-1), then soonest due (D-2, D-3, D-4)
    assert [d["document_no"] for d in s["documents"]] == ["D-1", "D-2", "D-3", "D-4"]
    assert s["documents"][0]["overdue"] is True

    # empty -> zeros, no crash
    empty = compliance.build_compliance_summary(_fresh_session(), "DEFAULT")
    assert empty["total"] == 0 and empty["documents"] == [] and empty["by_status"] == []


if __name__ == "__main__":
    test_compliance_summary_rolls_up_the_review_load()
    print("COMPLIANCE OK: review load (overdue/due-soon/pending-approval), status breakdown, "
          "to-review order (overdue then due); empty-safe")
