"""Compliance document summary read-model tests (ADR-0007).

The controlled-document review load: overdue / due-soon / pending-approval counts,
status breakdown, and the docs to review next. Run:
    python backend/test_compliance.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

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
                                     approval_status=status, review_due_date=datetime.utcnow().date() + timedelta(days=due_offset))


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


def test_obsolete_docs_carry_no_review_or_approval_debt():
    """A retired (Obsolete) document is out of the live review load: it must not
    inflate overdue / due-soon / unapproved, nor appear in the to-review list —
    but it still counts in the whole-register total and status breakdown."""
    db = _fresh_session()
    db.add_all([
        _doc("A-1", "Approved", -3),      # active, overdue review
        _doc("A-2", "Draft", 5),          # active, due soon + unapproved
        _doc("O-1", "Obsolete", -50),     # retired: past its old review date
        _doc("O-2", "Obsolete", 7),       # retired: nominal near-term review date
    ])
    db.commit()

    s = compliance.build_compliance_summary(db, "DEFAULT")

    # Whole-register views count everything, including the retired docs.
    assert s["total"] == 4
    assert {"status": "Obsolete", "count": 2} in s["by_status"]
    # by_status is a full breakdown of the register: its parts sum to the whole.
    assert sum(b["count"] for b in s["by_status"]) == s["total"]

    # Review-load metrics cover ACTIVE docs only (independently derived):
    #   overdue    -> A-1 only              (O-1 is retired, not overdue debt)
    #   due_soon   -> A-2 only              (O-2 is retired)
    #   unapproved -> A-2 only              (Obsolete is neither approved nor pending)
    assert s["overdue"] == 1
    assert s["due_soon"] == 1
    assert s["pending_approval"] == 1
    # The to-review list never surfaces a retired document.
    nums = [d["document_no"] for d in s["documents"]]
    assert nums == ["A-1", "A-2"]
    assert all(not n.startswith("O-") for n in nums)


def test_all_obsolete_register_has_zero_review_load():
    """Edge: a register of only retired docs shows real zeros for the review load,
    yet still reports the documents in total / by_status (honest, not hidden)."""
    db = _fresh_session()
    db.add_all([_doc("O-1", "Obsolete", -10), _doc("O-2", "Obsolete", 3)])
    db.commit()

    s = compliance.build_compliance_summary(db, "DEFAULT")
    assert s["total"] == 2
    assert s["overdue"] == 0 and s["due_soon"] == 0 and s["pending_approval"] == 0
    assert s["documents"] == []
    assert {"status": "Obsolete", "count": 2} in s["by_status"]


if __name__ == "__main__":
    test_compliance_summary_rolls_up_the_review_load()
    test_obsolete_docs_carry_no_review_or_approval_debt()
    test_all_obsolete_register_has_zero_review_load()
    print("COMPLIANCE OK: review load (overdue/due-soon/pending-approval) excludes retired "
          "(Obsolete) docs; status breakdown sums to total; to-review skips retired; empty-safe")
