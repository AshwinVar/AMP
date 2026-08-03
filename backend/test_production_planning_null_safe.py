"""Production-plan / schedule response serialisation is NULL-safe on planned_quantity.

`ProductionPlan.planned_quantity` and `ProductionSchedule.planned_quantity` are
Column(Integer, nullable=False) WITHOUT a default. A nullable=False constraint is
NOT retro-applied to rows written by raw SQL / a migration / a legacy insert — the
same reason analytics_engine coalesces ProductionRecord/ShiftData's own
nullable=False count columns, and the exact reason the quality-inspection list had
to heal its nullable=False inspected_quantity (#447) after #423 healed only the
default-0 counts beside it.

Both response models typed planned_quantity as a non-optional int with NO heal, so
a single NULL row raised Pydantic ValidationError during response serialisation and
500-ed the WHOLE GET /production-plans (and GET /production-schedules) list — one
poisoned row hiding every good plan/schedule. And update_production_plan compared
`plan.actual_quantity >= plan.planned_quantity`, which raised TypeError (int >= None)
on such a row and 500-ed the PATCH.

The heal is to 0, reconciled (rule-3) with the /analytics/production-schedules
rollup which sums planned_quantity with COALESCE(SUM(..), 0), so a NULL row reads as
0 there and now on the per-row list too. A real value — including a genuine 0 — is
untouched (mode="before" only rewrites None). SQLite enforces NOT NULL, so a real
on-disk NULL can only predate the constraint; the exact attribute state a loaded
legacy-NULL row presents (the attribute is None when read) is reproduced with a
transient ORM object / an in-memory clear, as the QI null-safe test does.

Run:  cd backend && python test_production_planning_null_safe.py   (exit 0 = pass)
Also collectable by pytest.
"""
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import production_planning_routes as PP
import schemas
import tenancy as T
from database import Base

TENANT = "T1"


def _iso_session():
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _make_plan(db, no, planned, actual, status="Planned"):
    row = models.ProductionPlan(
        tenant_code=TENANT, plan_no=no, work_order_id=1, machine_id=1,
        planned_quantity=planned, actual_quantity=actual,
        plan_date=date(2026, 1, 1), shift_name="Shift A", status=status,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ── ProductionPlanResponse ──────────────────────────────────────────────────

def test_plan_response_heals_null_planned_quantity_not_500():
    # A good row and a legacy-NULL-planned row, both validated exactly as the
    # FastAPI response layer validates each ORM row returned by get_production_plans.
    good = models.ProductionPlan(
        id=1, tenant_code=TENANT, plan_no="PP-GOOD", work_order_id=1, machine_id=1,
        planned_quantity=200, actual_quantity=150,
        plan_date=date(2026, 1, 1), shift_name="Shift A", status="Planned",
    )
    bad = models.ProductionPlan(
        id=2, tenant_code=TENANT, plan_no="PP-NULL", work_order_id=1, machine_id=1,
        planned_quantity=None, actual_quantity=None,   # legacy raw-SQL / migration row
        plan_date=date(2026, 1, 1), shift_name="Shift A", status="Planned",
    )
    good_s = schemas.ProductionPlanResponse.model_validate(good)   # must NOT raise
    bad_s = schemas.ProductionPlanResponse.model_validate(bad)     # was a 500

    # NULL -> 0 (concrete int), a real value passes through verbatim.
    assert bad_s.planned_quantity == 0, bad_s.planned_quantity
    assert bad_s.actual_quantity == 0, bad_s.actual_quantity
    assert isinstance(bad_s.planned_quantity, int), type(bad_s.planned_quantity)
    assert good_s.planned_quantity == 200, good_s.planned_quantity
    assert good_s.actual_quantity == 150, good_s.actual_quantity
    print("PASS plan response heals a NULL planned_quantity to 0 (not a 500 that hides every plan)")


def test_plan_response_real_zero_planned_is_untouched():
    # A genuine 0 planned (an unplanned/placeholder plan) must survive as 0, not be
    # confused with a NULL — the heal only rewrites None.
    z = models.ProductionPlan(
        id=1, tenant_code=TENANT, plan_no="PP-ZERO", work_order_id=1, machine_id=1,
        planned_quantity=0, actual_quantity=0,
        plan_date=date(2026, 1, 1), shift_name="Shift A", status="Planned",
    )
    zs = schemas.ProductionPlanResponse.model_validate(z)
    assert zs.planned_quantity == 0 and zs.actual_quantity == 0
    print("PASS a genuine zero planned_quantity is preserved, not treated as NULL")


# ── ProductionScheduleResponse ──────────────────────────────────────────────

def test_schedule_response_heals_null_planned_quantity_and_estimated_minutes():
    good = models.ProductionSchedule(
        id=1, tenant_code=TENANT, schedule_no="PS-GOOD", machine_id=1,
        shift_name="Shift A", scheduled_date=date(2026, 1, 1), priority="Medium",
        planned_quantity=120, estimated_minutes=480, status="Scheduled",
    )
    bad = models.ProductionSchedule(
        id=2, tenant_code=TENANT, schedule_no="PS-NULL", machine_id=1,
        shift_name="Shift A", scheduled_date=date(2026, 1, 1), priority="Medium",
        planned_quantity=None, estimated_minutes=None, status="Scheduled",
    )
    good_s = schemas.ProductionScheduleResponse.model_validate(good)
    bad_s = schemas.ProductionScheduleResponse.model_validate(bad)   # was a 500

    assert bad_s.planned_quantity == 0, bad_s.planned_quantity
    assert bad_s.estimated_minutes == 0, bad_s.estimated_minutes     # #442 regression
    assert isinstance(bad_s.planned_quantity, int)
    assert good_s.planned_quantity == 120, good_s.planned_quantity
    assert good_s.estimated_minutes == 480, good_s.estimated_minutes
    print("PASS schedule response heals a NULL planned_quantity (and estimated_minutes) to 0")


# ── PATCH /production-plans/{id} over a legacy NULL planned_quantity ─────────

def test_patch_actual_over_null_planned_is_safe():
    # An operator records actual output on a plan whose planned_quantity is a legacy
    # NULL. The OLD code did `plan.actual_quantity >= plan.planned_quantity`, i.e.
    # `5 >= None` -> TypeError -> unhandled 500. Now the auto-complete is guarded, so
    # the actual applies, status is left untouched (no known target -> can't claim
    # Completed), and the row serialises through the healed response.
    #
    # A real on-disk NULL can't be planted under SQLite's enforced NOT NULL, so
    # reproduce the exact attribute state a loaded legacy-NULL row has: clear it in
    # memory with autoflush off, so the handler's own SELECT returns this
    # identity-mapped object (None intact) rather than flushing the transient NULL.
    db = _iso_session()
    row = _make_plan(db, "PP-NULL-PATCH", planned=100, actual=0)
    db.autoflush = False
    row.planned_quantity = None

    tok = T.set_current_tenant(TENANT)
    try:
        payload = schemas.ProductionPlanUpdate(actual_quantity=5)
        result = PP.update_production_plan(
            row.id, payload, db=db, current_user={"tenant": TENANT},
        )
    finally:
        T.reset_current_tenant(tok)

    assert result.actual_quantity == 5, result.actual_quantity
    # No known target, so the plan is NOT auto-completed (honest: don't invent a state).
    assert result.status == "Planned", result.status
    # And it serialises without a 500, healing the still-NULL planned_quantity to 0.
    out = schemas.ProductionPlanResponse.model_validate(result)
    assert out.planned_quantity == 0, out.planned_quantity
    print("PASS PATCH actual over a NULL planned_quantity does not 500 and does not fabricate Completed")


def test_patch_auto_complete_happy_path_unbroken():
    # Regression: the NULL guard must NOT change behaviour on a normal plan. Reaching
    # the target auto-completes; staying short does not. Independent numbers: 100/100
    # -> Completed, 100/50 -> still Planned.
    db = _iso_session()
    hit = _make_plan(db, "PP-HIT", planned=100, actual=0)
    short = _make_plan(db, "PP-SHORT", planned=100, actual=0)

    tok = T.set_current_tenant(TENANT)
    try:
        r_hit = PP.update_production_plan(
            hit.id, schemas.ProductionPlanUpdate(actual_quantity=100),
            db=db, current_user={"tenant": TENANT},
        )
        r_short = PP.update_production_plan(
            short.id, schemas.ProductionPlanUpdate(actual_quantity=50),
            db=db, current_user={"tenant": TENANT},
        )
    finally:
        T.reset_current_tenant(tok)

    assert r_hit.status == "Completed", r_hit.status
    assert r_short.status == "Planned", r_short.status
    print("PASS auto-complete still fires at target and stays open below it")


if __name__ == "__main__":
    test_plan_response_heals_null_planned_quantity_not_500()
    test_plan_response_real_zero_planned_is_untouched()
    test_schedule_response_heals_null_planned_quantity_and_estimated_minutes()
    test_patch_actual_over_null_planned_is_safe()
    test_patch_auto_complete_happy_path_unbroken()
    print("\nAll production-planning NULL-safe tests passed.")
