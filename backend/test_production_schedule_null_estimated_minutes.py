"""GET/PATCH /production-schedules survive a NULL estimated_minutes column.

ProductionSchedule.estimated_minutes is declared Column(Integer, default=480)
WITHOUT nullable=False — the ORM default only fills a value the *inserter* omitted,
so a row written by raw SQL, a migration, or a PATCH that cleared the field can
legitimately hold NULL. ProductionScheduleResponse.estimated_minutes is typed a
non-optional int, so a single such NULL row raised Pydantic ValidationError during
response serialisation and 500-ed the WHOLE list endpoint — one bad row hiding every
good schedule — and 500-ed the PATCH response that created it. This was the only
counted response schema still missing the NULL heal every sibling list already
carries (#375 / #395 / orders).

Reachable through the API alone: ProductionScheduleUpdate types estimated_minutes
Optional[int]=None, so PATCH {"estimated_minutes": null} slips past the non-negative
guard (None is not < 0), persists a NULL, and then poisons every later GET.

The response model now heals a NULL to 0. It heals to 0 (NOT the column's 480
default) because BOTH readers of the column already treat a NULL as 0 — the
/analytics/production-schedules rollup COALESCEs SUM(estimated_minutes) to 0 and the
schedule-load read-model reads `s.estimated_minutes or 0` — so showing 0 keeps the
per-schedule list on the same basis as the booked-minutes headline it sums into
(rule-3 reconciliation). A real recorded value — including a real 0 — is untouched.

Run:  cd backend && DATABASE_URL="sqlite:///./ci.db" python test_production_schedule_null_estimated_minutes.py   (exit 0 = pass)
"""
from datetime import date

from sqlalchemy import create_engine, func, text
from sqlalchemy.orm import sessionmaker

import models
import production_planning_routes as PPR
import schemas
import tenancy as T
from database import Base


def _iso_session():
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed_normal_schedule(db, tenant, schedule_no, estimated_minutes):
    """A fully-populated schedule — proves real values (incl. a real 0) survive the heal."""
    tok = T.set_current_tenant(tenant)
    try:
        s = models.ProductionSchedule(
            schedule_no=schedule_no, machine_id=1, shift_name="Shift A",
            scheduled_date=date(2026, 1, 1), priority="Medium",
            planned_quantity=100, estimated_minutes=estimated_minutes,
            status="Scheduled",
        )
        db.add(s)
        db.commit()
        db.refresh(s)
    finally:
        T.reset_current_tenant(tok)
    return s


def _insert_null_estimate_schedule(db, tenant, schedule_no="SCH-NULL"):
    """A legacy row that left estimated_minutes NULL (raw-SQL / migration)."""
    db.execute(
        text(
            "INSERT INTO production_schedules "
            "(tenant_code, schedule_no, machine_id, shift_name, scheduled_date, "
            " priority, planned_quantity, estimated_minutes, status) "
            "VALUES (:t, :n, 1, 'Shift B', '2026-02-01', 'Medium', 40, NULL, 'Scheduled')"
        ),
        {"t": tenant, "n": schedule_no},
    )
    db.commit()
    db.expire_all()


def test_response_heals_null_estimated_minutes():
    db = _iso_session()
    # Independent expected value: a real estimate of 360 must pass through as 360, NOT
    # be treated as a healed NULL — so the heal is not a tautology that zeroes everything.
    _seed_normal_schedule(db, "DEFAULT", "SCH-OK", estimated_minutes=360)
    _insert_null_estimate_schedule(db, "DEFAULT")

    by_no = {s.schedule_no: schemas.ProductionScheduleResponse.model_validate(s)
             for s in db.query(models.ProductionSchedule).order_by(models.ProductionSchedule.id).all()}

    assert set(by_no) == {"SCH-OK", "SCH-NULL"}, set(by_no)
    # The NULL row heals to 0 (the value both consumers read it as), not the 480 default.
    assert by_no["SCH-NULL"].estimated_minutes == 0, by_no["SCH-NULL"].estimated_minutes
    # The real recorded value is untouched (independently derived: seeded 360 -> 360).
    assert by_no["SCH-OK"].estimated_minutes == 360, by_no["SCH-OK"].estimated_minutes
    print("PASS ProductionScheduleResponse heals NULL estimated_minutes to 0, preserves a real 360")


def test_response_preserves_real_zero_and_default():
    db = _iso_session()
    # A genuinely-recorded 0 (a zero-time placeholder) must NOT raise and must stay 0.
    _seed_normal_schedule(db, "DEFAULT", "SCH-ZERO", estimated_minutes=0)
    # A normally-created row that omits the field gets the ORM default of 480 and stays 480.
    _seed_normal_schedule(db, "DEFAULT", "SCH-480", estimated_minutes=480)
    by_no = {s.schedule_no: schemas.ProductionScheduleResponse.model_validate(s)
             for s in db.query(models.ProductionSchedule).order_by(models.ProductionSchedule.id).all()}
    assert by_no["SCH-ZERO"].estimated_minutes == 0, by_no["SCH-ZERO"].estimated_minutes
    assert by_no["SCH-480"].estimated_minutes == 480, by_no["SCH-480"].estimated_minutes
    print("PASS a real recorded 0 stays 0 and a real 480 stays 480 (only NULL is healed)")


def test_get_list_survives_a_null_row():
    db = _iso_session()
    _seed_normal_schedule(db, "DEFAULT", "SCH-OK", estimated_minutes=360)
    _insert_null_estimate_schedule(db, "DEFAULT")

    tok = T.set_current_tenant("DEFAULT")
    try:
        rows = PPR.get_production_schedules(db=db, current_user={"tenant": "DEFAULT"})
        # Mirror FastAPI's response_model=List[ProductionScheduleResponse] serialisation —
        # the step that used to raise ValidationError on the NULL row and 500 the list.
        serialised = [schemas.ProductionScheduleResponse.model_validate(r) for r in rows]
    finally:
        T.reset_current_tenant(tok)

    nos = {s.schedule_no for s in serialised}
    assert nos == {"SCH-OK", "SCH-NULL"}, nos
    for s in serialised:
        assert isinstance(s.estimated_minutes, int)
    print("PASS GET /production-schedules returns the full book with a NULL row present (no 500)")


def test_patch_null_estimated_minutes_does_not_500_and_does_not_poison_list():
    """The API-only trigger: PATCH {"estimated_minutes": null} used to persist a NULL,
    500 its own response, and then 500 every subsequent GET. Now it heals to 0."""
    db = _iso_session()
    _seed_normal_schedule(db, "DEFAULT", "SCH-OK", estimated_minutes=360)
    target = _seed_normal_schedule(db, "DEFAULT", "SCH-PATCH", estimated_minutes=480)

    tok = T.set_current_tenant("DEFAULT")
    try:
        payload = schemas.ProductionScheduleUpdate(estimated_minutes=None)
        # An explicit null is preserved by exclude_unset — the exact reachable input.
        assert "estimated_minutes" in payload.model_dump(exclude_unset=True)
        updated = PPR.update_production_schedule(
            schedule_id=target.id, payload=payload, db=db,
            current_user={"tenant": "DEFAULT", "role": "Admin"},
        )
        # PATCH response serialises without raising, healing the NULL to 0.
        assert schemas.ProductionScheduleResponse.model_validate(updated).estimated_minutes == 0

        # And the whole list still serialises — the NULL row no longer poisons it.
        rows = PPR.get_production_schedules(db=db, current_user={"tenant": "DEFAULT"})
        serialised = [schemas.ProductionScheduleResponse.model_validate(r) for r in rows]
    finally:
        T.reset_current_tenant(tok)

    by_no = {s.schedule_no: s for s in serialised}
    assert by_no["SCH-PATCH"].estimated_minutes == 0, by_no["SCH-PATCH"].estimated_minutes
    assert by_no["SCH-OK"].estimated_minutes == 360, by_no["SCH-OK"].estimated_minutes
    print("PASS PATCH estimated_minutes=null heals to 0 and leaves the list serialisable")


def test_list_heal_reconciles_with_schedule_load_and_analytics_basis():
    """rule-3: the per-schedule estimated_minutes shown in the list must equal the
    basis the booked-minutes rollups sum. Both the schedule-load read-model
    (`s.estimated_minutes or 0`) and the analytics SQL rollup (COALESCE(SUM,0)) read a
    NULL as 0, so the list heal (also 0) keeps parts == whole."""
    db = _iso_session()
    _seed_normal_schedule(db, "DEFAULT", "SCH-OK", estimated_minutes=360)
    _insert_null_estimate_schedule(db, "DEFAULT")

    tok = T.set_current_tenant("DEFAULT")
    try:
        rows = db.query(models.ProductionSchedule).order_by(models.ProductionSchedule.id).all()
        list_view = {r.schedule_no: schemas.ProductionScheduleResponse.model_validate(r).estimated_minutes
                     for r in rows}
        # Independently reproduce each consumer's coalescing of the SAME rows.
        schedule_load_basis = sum((r.estimated_minutes or 0) for r in rows)          # ai/schedule_load
        analytics_basis = int(
            db.query(
                func.coalesce(func.sum(models.ProductionSchedule.estimated_minutes), 0)
            ).scalar()
        )                                                                            # /analytics rollup
    finally:
        T.reset_current_tenant(tok)

    # The list value for the NULL row is 0 — the exact number the consumers count.
    assert list_view["SCH-NULL"] == 0, list_view["SCH-NULL"]
    # The sum of what the LIST shows equals what BOTH rollups compute: 360 + 0.
    list_total = sum(list_view.values())
    assert list_total == 360, list_total
    assert schedule_load_basis == 360, schedule_load_basis
    assert analytics_basis == 360, analytics_basis
    assert list_total == schedule_load_basis == analytics_basis
    print("PASS list estimated_minutes reconciles with schedule-load and analytics basis (360 == 360 == 360)")


if __name__ == "__main__":
    test_response_heals_null_estimated_minutes()
    test_response_preserves_real_zero_and_default()
    test_get_list_survives_a_null_row()
    test_patch_null_estimated_minutes_does_not_500_and_does_not_poison_list()
    test_list_heal_reconciles_with_schedule_load_and_analytics_basis()
    print("ALL PRODUCTION-SCHEDULE NULL ESTIMATED_MINUTES TESTS PASSED")
