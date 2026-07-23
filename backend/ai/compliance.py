"""Compliance document summary — the quality-system paperwork at a glance (ADR-0007).

Answers "which controlled documents are overdue for review, which are due soon,
and what's still waiting for approval?": the review load an ISO-pursuing plant
has to stay on top of, with the specific documents to review next (overdue
first). A read-model over compliance_documents — auto-scoped to the tenant
(ADR-0002); it adds no storage.
"""
from collections import Counter
from datetime import datetime, timedelta

import models

name = "compliance"

DUE_SOON_DAYS = 30
TOP_N = 8
APPROVED = "Approved"
OBSOLETE = "Obsolete"   # a retired document — no longer part of the live review load


def _active(d) -> bool:
    """An Obsolete document is retired: it doesn't need review or re-approval, so
    it's excluded from the live review load. Matches the rest of the app — the
    analytics review-due count (analytics_routes) and the factory-ops document
    filter both skip Obsolete."""
    return (d.approval_status or "") != OBSOLETE


def build_compliance_summary(db, tenant: str) -> dict:
    """The document review load: totals, overdue and due-soon counts, how many are
    unapproved, a status breakdown, and the documents to review next (overdue
    first, then soonest due). Overdue / due-soon / unapproved and the to-review
    list cover only ACTIVE documents — Obsolete (retired) ones carry no review or
    approval debt — while ``total`` and ``by_status`` describe the whole register
    (so ``by_status`` still sums to ``total``). compliance_documents is
    auto-scoped (ADR-0002)."""
    today = datetime.utcnow().date()
    docs = db.query(models.ComplianceDocument).all()

    by_status = Counter(d.approval_status or "Draft" for d in docs)
    active = [d for d in docs if _active(d)]
    overdue = sum(1 for d in active if d.review_due_date and d.review_due_date < today)
    due_soon = sum(1 for d in active
                   if d.review_due_date and today <= d.review_due_date <= today + timedelta(days=DUE_SOON_DAYS))
    pending_approval = sum(1 for d in active if (d.approval_status or "") != APPROVED)

    def _key(d):
        is_overdue = 0 if (d.review_due_date and d.review_due_date < today) else 1
        return (is_overdue, d.review_due_date or today)

    review_next = sorted((d for d in active if d.review_due_date), key=_key)[:TOP_N]
    rows = [{
        "document_no": d.document_no,
        "title": d.title,
        "type": d.document_type,
        "department": d.department,
        "owner": d.owner,
        "status": d.approval_status,
        "review_due_date": d.review_due_date.isoformat() if d.review_due_date else None,
        "overdue": bool(d.review_due_date and d.review_due_date < today),
    } for d in review_next]

    return {
        "total": len(docs),
        "overdue": overdue,
        "due_soon": due_soon,
        "pending_approval": pending_approval,
        "by_status": [{"status": s, "count": c} for s, c in by_status.most_common()],
        "documents": rows,
    }
