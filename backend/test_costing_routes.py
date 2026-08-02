"""Costing-routes registration test (ADR-0009).

The costing endpoints live in costing_routes.register(app), peeled out of
main.py. Guards that every expected costing path is registered and owned by
costing_routes.

Run:  python backend/test_costing_routes.py     (exit 0 = pass)
"""
from datetime import date

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import main
import costing_routes
import models
import schemas
from database import Base

EXPECTED = {"/cost-records", "/cost-records/{cost_id}", "/analytics/costing"}


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_costing_paths_owned_by_costing_routes():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"costing paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"costing_routes"}}
    assert not wrong, f"costing paths not owned solely by costing_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} costing paths owned by costing_routes")


def test_cost_per_good_unit_keeps_pence_precision():
    # £500 of logged cost over 1000 good units is £0.50/unit — round() to whole
    # pounds reported £0, fabricating a free product. Keep pence precision.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=80))
    db.add(models.CostRecord(cost_no="C-1", cost_type="Labour", description="x", amount=500))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=1000, good_count=1000, rejected_count=0))
    db.commit()
    out = costing_routes.get_costing_analytics(db=db, current_user={})
    assert out["cost_per_good_unit"] == 0.5      # not round(0.5) == 0
    print("PASS cost_per_good_unit keeps pence precision (£0.50, not £0)")


def test_cost_per_good_unit_is_none_when_no_production():
    # Real costs but zero good units -> per-unit cost is undefined, reported as
    # None ("—" in the UI), never a misleading £0 while costs exist.
    db = _fresh_session()
    db.add(models.CostRecord(cost_no="C-1", cost_type="Labour", description="x", amount=500))
    db.commit()
    out = costing_routes.get_costing_analytics(db=db, current_user={})
    assert out["cost_per_good_unit"] is None and out["manual_cost_total"] == 500
    print("PASS cost_per_good_unit is None (undefined), not £0, when there is no production")


def test_costing_survives_null_amount_and_received_quantity():
    # amount / received_quantity are Column(Integer, default=0) with NO
    # nullable=False — the ORM default only fills a value the inserter omitted, so
    # a raw-SQL / migration / cleared-field row can carry NULL. A bare Python
    # sum() over that None TypeError-500'd the whole /analytics/costing rollup.
    # Independently-derived expectations from THESE numbers:
    #   amounts (NULL->0): 300 + 0(NULL) + 200 = 500
    #   received_quantity (NULL->0): 0(NULL) + 50 = 50
    #   good_count: 400 + 600 = 1000
    #   cost_per_good_unit = 500 / 1000 = 0.5
    db = _fresh_session()
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=80))
    db.add(models.CostRecord(cost_no="C-1", cost_type="Labour", department="Assembly",
                             description="x", amount=300))
    db.add(models.CostRecord(cost_no="C-2", cost_type="Material", department=None,
                             description="y", amount=0))    # will be NULLed below
    db.add(models.CostRecord(cost_no="C-3", cost_type="Labour", department="Assembly",
                             description="z", amount=200))
    due = date(2026, 1, 1)
    db.add(models.PurchaseOrder(po_no="PO-1", item_name="Steel", order_quantity=100,
                                unit="kg", expected_delivery_date=due,
                                received_quantity=0))        # will be NULLed below
    db.add(models.PurchaseOrder(po_no="PO-2", item_name="Steel", order_quantity=100,
                                unit="kg", expected_delivery_date=due,
                                received_quantity=50))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=400, good_count=400, rejected_count=0))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=600, good_count=600, rejected_count=0))
    db.commit()
    # Simulate the legacy / migration rows the ORM default never touched.
    db.execute(text("UPDATE cost_records SET amount=NULL WHERE cost_no='C-2'"))
    db.execute(text("UPDATE purchase_orders SET received_quantity=NULL WHERE po_no='PO-1'"))
    db.commit()

    out = costing_routes.get_costing_analytics(db=db, current_user={})

    assert out["total_cost_records"] == 3, out
    assert out["manual_cost_total"] == 500, out               # 300 + NULL->0 + 200
    assert out["supplier_receipt_units"] == 50, out           # NULL->0 + 50
    assert out["production_units"] == 1000, out
    assert out["cost_per_good_unit"] == 0.5, out

    # Headline reconciles with BOTH breakdowns (parts sum to the whole).
    assert sum(out["by_type"].values()) == out["manual_cost_total"], out
    assert sum(out["by_department"].values()) == out["manual_cost_total"], out
    # The NULLed row (C-2, Material, no department) still lands in its buckets at 0.
    assert out["by_type"] == {"Labour": 500, "Material": 0}, out
    assert out["by_department"] == {"Assembly": 500, "Unassigned": 0}, out
    print("PASS costing rollup survives NULL amount/received_quantity and reconciles")


def test_costing_empty_tables_all_zero():
    # No cost records, no POs, no production -> every aggregate is 0 and the
    # undefined per-unit cost is None, not a crash and not a fabricated £0.
    db = _fresh_session()
    out = costing_routes.get_costing_analytics(db=db, current_user={})
    assert out["total_cost_records"] == 0
    assert out["manual_cost_total"] == 0
    assert out["supplier_receipt_units"] == 0
    assert out["production_units"] == 0
    assert out["cost_per_good_unit"] is None
    assert out["by_type"] == {} and out["by_department"] == {}
    print("PASS costing rollup survives empty tables (all zeros, per-unit None)")


def test_patch_null_amount_heals_to_zero_not_500():
    # A client (or a UI clearing the field) PATCHes {"amount": null}. amount is a
    # nullable Integer column and CostRecordUpdate types it Optional[int]=None, so
    # exclude_unset keeps the explicit null and the setattr writes None. Without the
    # coalesce the handler commits amount=NULL and the return 500s against
    # CostRecordResponse (amount: int) — and the persisted NULL then 500s every
    # later GET /cost-records for the tenant. The fix heals None -> 0 (the column's
    # own default) before the return.
    db = _fresh_session()
    db.add(models.CostRecord(cost_no="C-1", cost_type="Labour", description="x", amount=300))
    db.commit()
    row_id = db.query(models.CostRecord).filter(models.CostRecord.cost_no == "C-1").one().id

    out = costing_routes.update_cost_record(
        cost_id=row_id,
        payload=schemas.CostRecordUpdate(amount=None),
        db=db, current_user={})

    assert out.amount == 0, out.amount                 # healed, not None
    # The healed value is committed, so a re-read is 0 too (the list path is safe).
    assert db.query(models.CostRecord).filter_by(id=row_id).one().amount == 0
    # And it now validates against the response schema — the exact 500 that fired.
    assert schemas.CostRecordResponse.model_validate(out).amount == 0
    print("PASS PATCH {amount: null} heals to 0 and validates (no ResponseValidation 500)")


def test_patch_unrelated_field_heals_preexisting_null_amount():
    # A legacy / raw-SQL / migration row already carries a NULL amount. A PATCH of
    # an UNRELATED field (department) must not 500 on the way out — the coalesce
    # heals the pre-existing NULL even though the payload never mentions amount.
    db = _fresh_session()
    db.add(models.CostRecord(cost_no="C-9", cost_type="Labour", description="x", amount=0))
    db.commit()
    row_id = db.query(models.CostRecord).filter(models.CostRecord.cost_no == "C-9").one().id
    db.execute(text("UPDATE cost_records SET amount=NULL WHERE cost_no='C-9'"))
    db.commit()

    out = costing_routes.update_cost_record(
        cost_id=row_id,
        payload=schemas.CostRecordUpdate(department="Assembly"),
        db=db, current_user={})

    assert out.department == "Assembly"
    assert out.amount == 0, out.amount                 # pre-existing NULL healed
    assert schemas.CostRecordResponse.model_validate(out).amount == 0
    print("PASS PATCH of an unrelated field heals a pre-existing NULL amount to 0")


def test_patch_keeps_a_real_amount_and_zero_unchanged():
    # Guard the coalesce doesn't clobber real values: a set amount is preserved,
    # and a genuine 0 stays 0 (0 or 0 == 0 — no accidental change).
    db = _fresh_session()
    db.add(models.CostRecord(cost_no="C-2", cost_type="Labour", description="x", amount=750))
    db.commit()
    row_id = db.query(models.CostRecord).filter(models.CostRecord.cost_no == "C-2").one().id

    # Patch an unrelated field: the real 750 must survive the coalesce.
    kept = costing_routes.update_cost_record(
        cost_id=row_id, payload=schemas.CostRecordUpdate(cost_type="Material"),
        db=db, current_user={})
    assert kept.amount == 750 and kept.cost_type == "Material", kept.amount

    # Explicitly set amount to 0 — a legitimate zero cost, not a null.
    zeroed = costing_routes.update_cost_record(
        cost_id=row_id, payload=schemas.CostRecordUpdate(amount=0),
        db=db, current_user={})
    assert zeroed.amount == 0, zeroed.amount
    print("PASS PATCH preserves a real amount and a legitimate 0")


def test_patch_null_cost_type_is_400_not_500_and_leaves_row_intact():
    # cost_type / description are Column(String, nullable=False), but
    # CostRecordUpdate types each Optional[str]=None, so a client PATCHing an
    # explicit {"cost_type": null} passes schema validation, exclude_unset keeps
    # the null, and setattr writes None. Committing that hit the NOT NULL
    # constraint and raised an unhandled IntegrityError -> a 500 (the create path
    # already refuses a bad write with a clean 4xx). The guard rejects it with a
    # 400 BEFORE any setattr, so the row is never mutated.
    db = _fresh_session()
    db.add(models.CostRecord(cost_no="C-NN", cost_type="Labour", description="shift", amount=120))
    db.commit()
    row_id = db.query(models.CostRecord).filter(models.CostRecord.cost_no == "C-NN").one().id

    for bad in (schemas.CostRecordUpdate(cost_type=None),
                schemas.CostRecordUpdate(description=None)):
        try:
            costing_routes.update_cost_record(cost_id=row_id, payload=bad, db=db, current_user={})
            assert False, "expected a 400, got a success"
        except HTTPException as e:
            assert e.status_code == 400, e.status_code   # a clean 400, NOT a 500

    # The rejected PATCH never mutated or committed the row: both required fields
    # survive unchanged (and a later GET would still validate).
    kept = db.query(models.CostRecord).filter_by(id=row_id).one()
    assert kept.cost_type == "Labour" and kept.description == "shift", (kept.cost_type, kept.description)
    print("PASS PATCH {cost_type|description: null} is a clean 400, row left intact (no IntegrityError 500)")


def test_patch_null_required_field_paired_with_valid_field_still_rejected():
    # A mixed PATCH — {"cost_type": null, "amount": 500} — must still be rejected
    # whole: the null required field is caught before setattr, so the valid amount
    # is NOT partially applied (all-or-nothing, like the sibling before-commit guards).
    db = _fresh_session()
    db.add(models.CostRecord(cost_no="C-MIX", cost_type="Material", description="d", amount=10))
    db.commit()
    row_id = db.query(models.CostRecord).filter(models.CostRecord.cost_no == "C-MIX").one().id

    try:
        costing_routes.update_cost_record(
            cost_id=row_id,
            payload=schemas.CostRecordUpdate(cost_type=None, amount=500),
            db=db, current_user={})
        assert False, "expected a 400"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code

    kept = db.query(models.CostRecord).filter_by(id=row_id).one()
    # Nothing applied: cost_type intact AND the paired amount did not slip through.
    assert kept.cost_type == "Material" and kept.amount == 10, (kept.cost_type, kept.amount)
    print("PASS a null required field rejects the whole PATCH; the paired valid field is not applied")


def test_patch_updates_required_fields_with_real_values_still_works():
    # Regression: a legitimate non-null update of the required fields is unaffected
    # by the guard (it only rejects an explicit null-out).
    db = _fresh_session()
    db.add(models.CostRecord(cost_no="C-OK", cost_type="Labour", description="old", amount=40))
    db.commit()
    row_id = db.query(models.CostRecord).filter(models.CostRecord.cost_no == "C-OK").one().id

    out = costing_routes.update_cost_record(
        cost_id=row_id,
        payload=schemas.CostRecordUpdate(cost_type="Overhead", description="new"),
        db=db, current_user={})
    assert out.cost_type == "Overhead" and out.description == "new"
    assert schemas.CostRecordResponse.model_validate(out).cost_type == "Overhead"
    print("PASS a real (non-null) cost_type/description update still applies")


if __name__ == "__main__":
    test_costing_paths_owned_by_costing_routes()
    test_cost_per_good_unit_keeps_pence_precision()
    test_cost_per_good_unit_is_none_when_no_production()
    test_costing_survives_null_amount_and_received_quantity()
    test_costing_empty_tables_all_zero()
    test_patch_null_amount_heals_to_zero_not_500()
    test_patch_unrelated_field_heals_preexisting_null_amount()
    test_patch_keeps_a_real_amount_and_zero_unchanged()
    test_patch_null_cost_type_is_400_not_500_and_leaves_row_intact()
    test_patch_null_required_field_paired_with_valid_field_still_rejected()
    test_patch_updates_required_fields_with_real_values_still_works()
    print("ALL COSTING ROUTE TESTS PASSED")
