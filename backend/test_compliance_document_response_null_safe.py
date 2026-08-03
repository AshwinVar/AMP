"""GET /documents survives NULL-valued nullable columns (response heal).

ComplianceDocument.version (String, default="1.0") and
ComplianceDocument.approval_status (String, default="Draft") are declared WITHOUT
nullable=False — the ORM default only fills a value the *inserter* omitted, so a
row written by raw SQL, a migration, or an update that cleared the field can
legitimately hold NULL. ComplianceDocumentResponse types both as non-optional
`str`, so a single such NULL row raised Pydantic ValidationError during response
serialisation and 500-ed the WHOLE GET /documents list — one bad row hiding every
good document — AND the PATCH response for that row. Same class already healed on
the maintenance-task (#445) / machine / production-plan / operator-execution lists.

The NULL is not hypothetical: ComplianceDocumentUpdate types both fields
Optional[str], and update_document does `setattr(row, key, value)` over
exclude_unset — so a client PATCH of `{"approval_status": null}` stores a real
NULL (exercised end-to-end below).

The response model now coalesces each NULL to the column's own declared default on
the way out (version -> "1.0", approval_status -> "Draft"). Healing approval_status
-> "Draft" keeps the list on the SAME basis as its readers (rule-3): the compliance
read-model already counts a NULL as "Draft" (ai/compliance.py `d.approval_status or
"Draft"`), so the healed row lands in the same by_status bucket the headline sums.
A real recorded value is untouched.

Run:  cd backend && DATABASE_URL="sqlite:///./ci.db" python test_compliance_document_response_null_safe.py   (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import factory_ops_routes as FOR
import models
import schemas
import tenancy as T
from ai import compliance
from database import Base


def _iso_session():
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed_normal_doc(db, tenant, due_date, version="2.3", approval_status="Approved"):
    """A fully-populated document — proves real values survive the heal untouched."""
    tok = T.set_current_tenant(tenant)
    try:
        d = models.ComplianceDocument(
            document_no="DOC-OK",
            title="Calibration SOP",
            document_type="SOP",
            department="Quality",
            version=version,
            owner="QA Lead",
            approval_status=approval_status,
            review_due_date=due_date,
        )
        db.add(d)
        db.commit()
        db.refresh(d)
    finally:
        T.reset_current_tenant(tok)
    return d


def _insert_null_doc(db, tenant, document_no, due_date):
    """A legacy row that left version AND approval_status NULL (raw SQL bypasses
    the ORM Python-side default)."""
    db.execute(
        text(
            "INSERT INTO compliance_documents "
            "(tenant_code, document_no, title, document_type, department, version, "
            " owner, approval_status, review_due_date) "
            "VALUES (:t, :no, 'Legacy Doc', 'Work Instruction', 'Production', NULL, "
            " 'Ops Lead', NULL, :dd)"
        ),
        {"t": tenant, "no": document_no, "dd": due_date.isoformat()},
    )
    db.commit()
    db.expire_all()


def test_response_heals_null_columns():
    # Serialise a NULL row EXACTLY as FastAPI does (response_model + from_attributes);
    # this is the step that used to raise ValidationError.
    db = _iso_session()
    today = datetime.utcnow().date()
    _seed_normal_doc(db, "DEFAULT", due_date=today, version="2.3", approval_status="Approved")
    _insert_null_doc(db, "DEFAULT", "DOC-NULL", due_date=today)

    by_no = {d.document_no: schemas.ComplianceDocumentResponse.model_validate(d)
             for d in db.query(models.ComplianceDocument).order_by(models.ComplianceDocument.id).all()}
    assert set(by_no) == {"DOC-OK", "DOC-NULL"}, set(by_no)

    # The NULL row heals to each column's declared default.
    null_row = by_no["DOC-NULL"]
    assert null_row.version == "1.0", null_row.version
    assert null_row.approval_status == "Draft", null_row.approval_status

    # The real row is untouched — the mode="before" heal only rewrites None.
    ok_row = by_no["DOC-OK"]
    assert ok_row.version == "2.3", ok_row.version
    assert ok_row.approval_status == "Approved", ok_row.approval_status
    print("PASS ComplianceDocumentResponse heals NULL version/approval_status, preserves real values")


def test_list_endpoint_survives_null_row():
    # End-to-end: the whole GET /documents list serialises without a 500 even when a
    # legacy NULL row is present, and every good document still comes back.
    db = _iso_session()
    today = datetime.utcnow().date()
    _seed_normal_doc(db, "DEFAULT", due_date=today)
    _insert_null_doc(db, "DEFAULT", "DOC-NULL", due_date=today)

    tok = T.set_current_tenant("DEFAULT")
    try:
        rows = FOR.get_documents(db=db, current_user={"tenant": "DEFAULT"})
        serialised = [schemas.ComplianceDocumentResponse.model_validate(r) for r in rows]
    finally:
        T.reset_current_tenant(tok)

    nos = {s.document_no for s in serialised}
    assert nos == {"DOC-OK", "DOC-NULL"}, nos
    for s in serialised:
        assert isinstance(s.version, str) and s.version
        assert isinstance(s.approval_status, str) and s.approval_status
    print("PASS GET /documents returns the full list with a NULL row present (no 500)")


def test_client_patch_null_then_list_survives():
    # The reachable client path: PATCH {"approval_status": null, "version": null}
    # stores a genuine NULL (exclude_unset keeps an explicit null), and BOTH the PATCH
    # response and a subsequent GET /documents list then serialise cleanly.
    db = _iso_session()
    today = datetime.utcnow().date()
    doc = _seed_normal_doc(db, "DEFAULT", due_date=today, version="4.0", approval_status="Approved")

    tok = T.set_current_tenant("DEFAULT")
    try:
        patched = FOR.update_document(
            document_id=doc.id,
            payload=schemas.ComplianceDocumentUpdate(approval_status=None, version=None),
            db=db,
            current_user={"tenant": "DEFAULT"},
        )
        # The stored row is a genuine NULL (the PATCH really nulled it out)...
        raw = db.query(models.ComplianceDocument).filter_by(id=doc.id).one()
        assert raw.approval_status is None, raw.approval_status
        assert raw.version is None, raw.version
        # ...yet the PATCH response heals it rather than 500-ing.
        resp = schemas.ComplianceDocumentResponse.model_validate(patched)
        assert resp.approval_status == "Draft", resp.approval_status
        assert resp.version == "1.0", resp.version

        # And the whole list still serialises.
        rows = FOR.get_documents(db=db, current_user={"tenant": "DEFAULT"})
        serialised = [schemas.ComplianceDocumentResponse.model_validate(r) for r in rows]
    finally:
        T.reset_current_tenant(tok)

    assert len(serialised) == 1 and serialised[0].approval_status == "Draft"
    print("PASS a client PATCH of {approval_status: null, version: null} stores NULL but neither the PATCH nor GET /documents 500s")


def test_list_reconciles_with_compliance_read_model():
    # rule-3: the healed list status and the compliance read-model's by_status
    # breakdown must agree on the identical NULL-status row. The read-model counts a
    # NULL approval_status as "Draft"; the list heals it to "Draft" — same bucket.
    db = _iso_session()
    today = datetime.utcnow().date()
    # One real Approved doc + one NULL-status doc. Independently-derived expectations:
    # read-model by_status -> {"Approved": 1, "Draft": 1}; the list shows the NULL row
    # as "Draft" (matching the "Draft" bucket it was counted in).
    _seed_normal_doc(db, "DEFAULT", due_date=today + timedelta(days=10), approval_status="Approved")
    _insert_null_doc(db, "DEFAULT", "DOC-NULL", due_date=today + timedelta(days=10))

    tok = T.set_current_tenant("DEFAULT")
    try:
        summary = compliance.build_compliance_summary(db, "DEFAULT")
        rows = FOR.get_documents(db=db, current_user={"tenant": "DEFAULT"})
        serialised = {s.document_no: s
                      for s in (schemas.ComplianceDocumentResponse.model_validate(r) for r in rows)}
    finally:
        T.reset_current_tenant(tok)

    by_status = {b["status"]: b["count"] for b in summary["by_status"]}
    assert by_status.get("Approved") == 1, by_status
    assert by_status.get("Draft") == 1, by_status               # the NULL row counted as Draft
    assert summary["total"] == 2, summary["total"]
    assert by_status.get("Approved", 0) + by_status.get("Draft", 0) == summary["total"], by_status
    # The list shows that same NULL row as "Draft" — reconciling with the bucket.
    assert serialised["DOC-NULL"].approval_status == "Draft", serialised["DOC-NULL"].approval_status
    print("PASS the document list and the compliance read-model agree on a NULL-status row (Draft bucket)")


if __name__ == "__main__":
    test_response_heals_null_columns()
    test_list_endpoint_survives_null_row()
    test_client_patch_null_then_list_survives()
    test_list_reconciles_with_compliance_read_model()
    print("ALL COMPLIANCE-DOCUMENT-RESPONSE NULL-SAFE TESTS PASSED")
