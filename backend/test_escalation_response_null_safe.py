"""GET /escalations survives NULL status / source columns (response-serialisation heal).

Escalation.status (String, default="Open") and Escalation.source (String,
default="Manual") are both declared WITHOUT nullable=False — the ORM default only
fills a value the *inserter* omitted, so a row written by raw SQL, a migration, or
an update that cleared the field can legitimately hold NULL. EscalationResponse
types both as a non-optional str, so a single such NULL row raised Pydantic
ValidationError during response serialisation and 500-ed the WHOLE GET /escalations
list — the open-issue backlog every dashboard polls, one bad row hiding every good
escalation. This is the same response-serialisation NULL class already healed on
the machines roster (#395) and the production-plan / operator-execution /
customer-order / purchase-order lists (#375/#401/#423).

A NULL status is exactly the case the escalation READERS already treat as "still
open" (or_(status.is_(None), status != 'Resolved'): analytics /escalations #295,
system-notifications #403, tenant-activity, the low-stock/document generators'
dedup) — so healing it to the column's own default "Open" on the way out keeps the
reader and the list reader on the same basis. source heals to its default "Manual".
A real recorded value is untouched.

Run:  cd backend && DATABASE_URL="sqlite:///./ci.db" python test_escalation_response_null_safe.py   (exit 0 = pass)
"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import factory_ops_routes as FO
import models
import schemas
import tenancy as T
from database import Base


def _iso_session():
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed_normal_escalation(db, tenant):
    """A fully-populated escalation — proves real values survive the heal."""
    tok = T.set_current_tenant(tenant)
    try:
        e = models.Escalation(
            title="E-OK", severity="High", owner="Stores", department="Inventory",
            status="Resolved", source="Inventory",
        )
        db.add(e)
        db.commit()
        db.refresh(e)
    finally:
        T.reset_current_tenant(tok)
    return e


def _insert_null_escalation(db, tenant):
    """A legacy row that left status / source NULL (raw-SQL / migration): status is a
    PYTHON-side default, so the ORM would write "Open"/"Manual" and the case under
    test would never exist — raw SQL is the only way to build the row, which is also
    how it arises in production (a migration, a bulk import, a manual fix-up)."""
    db.execute(
        text(
            "INSERT INTO escalations (tenant_code, title, severity, owner, department, status, source) "
            "VALUES (:t, 'E-NULL', 'Critical', 'Lead', 'Maintenance', NULL, NULL)"
        ),
        {"t": tenant},
    )
    db.commit()
    db.expire_all()


def test_escalation_response_heals_null_columns():
    # Serialise a NULL row EXACTLY as FastAPI does (response_model + from_attributes);
    # this is the step that used to raise ValidationError.
    db = _iso_session()
    _seed_normal_escalation(db, "DEFAULT")
    _insert_null_escalation(db, "DEFAULT")

    by_title = {e.title: schemas.EscalationResponse.model_validate(e)
                for e in db.query(models.Escalation).order_by(models.Escalation.id).all()}

    assert set(by_title) == {"E-OK", "E-NULL"}, set(by_title)

    # The NULL row heals to each column's declared default.
    null_row = by_title["E-NULL"]
    assert null_row.status == "Open", null_row.status
    assert null_row.source == "Manual", null_row.source

    # The real row is untouched — a real "Resolved"/"Inventory" is not a healed NULL.
    ok_row = by_title["E-OK"]
    assert ok_row.status == "Resolved", ok_row.status
    assert ok_row.source == "Inventory", ok_row.source
    print("PASS EscalationResponse heals NULL status/source, preserves real values")


def test_get_escalations_list_survives_a_null_row():
    # End-to-end: the whole GET /escalations list serialises without a 500 even when
    # a legacy NULL row is present, and every good escalation still comes back.
    db = _iso_session()
    _seed_normal_escalation(db, "DEFAULT")
    _insert_null_escalation(db, "DEFAULT")

    tok = T.set_current_tenant("DEFAULT")
    try:
        rows = FO.get_escalations(db=db, current_user={"tenant": "DEFAULT"})
        # Mirror FastAPI's response_model=List[EscalationResponse] serialisation.
        serialised = [schemas.EscalationResponse.model_validate(r) for r in rows]
    finally:
        T.reset_current_tenant(tok)

    titles = {s.title for s in serialised}
    assert titles == {"E-OK", "E-NULL"}, titles
    # The NULL row didn't hide the good one, and both carry concrete (non-None) fields.
    for s in serialised:
        assert isinstance(s.status, str) and s.status
        assert isinstance(s.source, str) and s.source
    print("PASS GET /escalations returns the full backlog with a NULL row present (no 500)")


if __name__ == "__main__":
    test_escalation_response_heals_null_columns()
    test_get_escalations_list_survives_a_null_row()
    print("ALL ESCALATION-RESPONSE NULL-SAFE TESTS PASSED")
