"""GET /machines survives NULL-valued nullable columns (response-serialisation heal).

Machine.utilization (Integer, default=0), Machine.downtime (String, default="0 min")
and Machine.line (String, default="") are all declared WITHOUT nullable=False — the
ORM default only fills a value the *inserter* omitted, so a row written by raw SQL,
a migration, or an update that cleared the field can legitimately hold NULL.
MachineResponse types all three as non-optional (int / str), so a single such NULL
row raised Pydantic ValidationError during response serialisation and 500-ed the
WHOLE GET /machines list — the core roster every dashboard polls, one bad row
hiding every good machine. This is the same class already healed on the
production-plan / operator-execution lists (#375).

The response model now coalesces each NULL to the column's own declared default on
the way out (0 / "0 min" / ""); a real recorded value — including a real 0 — is
untouched.

Run:  cd backend && DATABASE_URL="sqlite:///./ci.db" python test_machine_response_null_safe.py   (exit 0 = pass)
"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import machines_routes as MR
import models
import schemas
import tenancy as T
from database import Base


def _iso_session():
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed_normal_machine(db, tenant):
    """A fully-populated machine — proves real values (incl. a real 0) survive the heal."""
    tok = T.set_current_tenant(tenant)
    try:
        m = models.Machine(name="M-OK", status="Idle", utilization=0, downtime="5 min", line="SMT")
        db.add(m)
        db.commit()
        db.refresh(m)
    finally:
        T.reset_current_tenant(tok)
    return m


def _insert_null_machine(db, tenant):
    """A legacy row that left utilization / downtime / line NULL (raw-SQL / migration)."""
    db.execute(
        text(
            "INSERT INTO machines (tenant_code, name, status, utilization, downtime, line) "
            "VALUES (:t, 'M-NULL', 'Running', NULL, NULL, NULL)"
        ),
        {"t": tenant},
    )
    db.commit()
    db.expire_all()


def test_machine_response_heals_null_columns():
    # Serialise a NULL row EXACTLY as FastAPI does (response_model + from_attributes);
    # this is the step that used to raise ValidationError.
    db = _iso_session()
    _seed_normal_machine(db, "DEFAULT")
    _insert_null_machine(db, "DEFAULT")

    by_name = {m.name: schemas.MachineResponse.model_validate(m)
               for m in db.query(models.Machine).order_by(models.Machine.id).all()}

    assert set(by_name) == {"M-OK", "M-NULL"}, set(by_name)

    # The NULL row heals to each column's declared default.
    null_row = by_name["M-NULL"]
    assert null_row.utilization == 0, null_row.utilization
    assert null_row.downtime == "0 min", null_row.downtime
    assert null_row.line == "", repr(null_row.line)

    # The real row is untouched — crucially a real recorded 0 utilization is NOT
    # a healed NULL: the mode="before" heal only rewrites None.
    ok_row = by_name["M-OK"]
    assert ok_row.utilization == 0, ok_row.utilization
    assert ok_row.downtime == "5 min", ok_row.downtime
    assert ok_row.line == "SMT", ok_row.line
    print("PASS MachineResponse heals NULL utilization/downtime/line, preserves real values")


def test_get_machines_list_survives_a_null_row():
    # End-to-end: the whole GET /machines list serialises without a 500 even when a
    # legacy NULL row is present, and every good machine still comes back.
    db = _iso_session()
    _seed_normal_machine(db, "DEFAULT")
    _insert_null_machine(db, "DEFAULT")

    tok = T.set_current_tenant("DEFAULT")
    try:
        rows = MR.get_machines(db=db, current_user={"tenant": "DEFAULT"})
        # Mirror FastAPI's response_model=List[MachineResponse] serialisation.
        serialised = [schemas.MachineResponse.model_validate(r) for r in rows]
    finally:
        T.reset_current_tenant(tok)

    names = {s.name for s in serialised}
    assert names == {"M-OK", "M-NULL"}, names
    # The NULL row didn't hide the good one, and both carry concrete (non-None) fields.
    for s in serialised:
        assert s.utilization is not None and isinstance(s.utilization, int)
        assert isinstance(s.downtime, str) and isinstance(s.line, str)
    print("PASS GET /machines returns the full roster with a NULL row present (no 500)")


if __name__ == "__main__":
    test_machine_response_heals_null_columns()
    test_get_machines_list_survives_a_null_row()
    print("ALL MACHINE-RESPONSE NULL-SAFE TESTS PASSED")
