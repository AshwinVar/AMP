"""Each tenant's bill of materials is its own (ADR-0013).

WHAT WAS MEASURED ON MASTER
---------------------------
The recipe book was a module-level dict shared by every customer:

    PART_BOM['SHAFT-001'] = {'raw': 'RM-STEEL-001', 'consume_per_unit': 2,
                             'fg': 'FG-SHAFT-001'}

Two customers who had never heard of each other, both making a part they call
SHAFT-001:

    FACTORY_B completes a work order for 10 of THEIR OWN SHAFT-001:
       FACTORY_B  RM-STEEL-001   stock=80      (was 100)

FACTORY_B's stock moved at 2 per unit — GMATS's rate, a number nobody at
FACTORY_B entered and could not change. Isolation was never breached; the recipe
was simply somebody else's. And a product the dict had never heard of moved
nothing at all:

    FACTORY_B completes 50 of VALVE-77 — a product THEY make:
       B-ALLOY-9      stock=500     (unchanged)
       inventory transactions written: 0

Run: DATABASE_URL="sqlite:///./ci.db" python test_per_tenant_bom.py
"""
import os
import sys
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import bom
import bom_routes
import models
import subscribers
import tenancy
from database import Base
from events import ProductionCompleted

_URL = os.environ.get("DATABASE_URL", "")
if _URL.startswith("postgresql"):
    engine = create_engine(_URL)
else:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
SessionLocal = sessionmaker(bind=engine)

failures = []
_open = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def fresh_db():
    while _open:
        try:
            _open.pop().close()
        except Exception:
            pass
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    _open.append(db)
    return db


def admin(tenant):
    return {"sub": "admin", "role": "Admin", "tenant": tenant}


def stock(db, tenant, code):
    tok = tenancy.set_current_tenant(None)
    item = db.query(models.InventoryItem).filter(
        models.InventoryItem.tenant_code == tenant,
        models.InventoryItem.item_code == code).first()
    tenancy.reset_current_tenant(tok)
    return None if item is None else item.current_stock


def complete(db, tenant, part, qty, wo="WO-1"):
    tok = tenancy.set_current_tenant(tenant)
    subscribers.move_bom_on_production_completed(
        ProductionCompleted(tenant_code=tenant, work_order_id=1, work_order_no=wo,
                            part_number=part, quantity=qty), db)
    db.commit()
    tenancy.reset_current_tenant(tok)


# Three customers. All three make a part they call SHAFT-001, all three stock an
# item they call RM-STEEL-001, and all three consume a DIFFERENT amount of it —
# which is the entire point, and is impossible to express with one shared dict.
PROFILE = {
    "FACTORY_A": {"per_unit": 2.0,  "start": 100, "made": 10, "expect_left": 80.0},
    "FACTORY_B": {"per_unit": 3.5,  "start": 100, "made": 10, "expect_left": 65.0},
    "FACTORY_C": {"per_unit": 0.25, "start": 100, "made": 10, "expect_left": 97.5},
}


def seed_three_tenants(db):
    tok = tenancy.set_current_tenant(None)
    for t in PROFILE:
        db.add(models.InventoryItem(tenant_code=t, item_code="RM-STEEL-001",
                                    item_name="Steel bar", category="Raw",
                                    unit="kg", current_stock=PROFILE[t]["start"],
                                    reorder_level=5))
        db.add(models.InventoryItem(tenant_code=t, item_code="FG-SHAFT-001",
                                    item_name="Shaft", category="Finished",
                                    unit="pc", current_stock=0, reorder_level=0))
    db.commit()
    for t, p in PROFILE.items():
        b = models.BillOfMaterials(tenant_code=t, part_number="SHAFT-001",
                                   revision="A", output_item_code="FG-SHAFT-001",
                                   active=True, created_by="seed")
        b.components.append(models.BomComponent(
            tenant_code=t, component_code="RM-STEEL-001",
            quantity_per_unit=p["per_unit"], unit="kg"))
        db.add(b)
    db.commit()
    tenancy.reset_current_tenant(tok)


def test_three_tenants_three_different_recipes():
    db = fresh_db()
    seed_three_tenants(db)

    print("=" * 74)
    print("1. THREE CUSTOMERS, ONE PART NUMBER, THREE DIFFERENT RECIPES")
    print("=" * 74)
    for t in PROFILE:
        complete(db, t, "SHAFT-001", PROFILE[t]["made"])

    for t, p in PROFILE.items():
        left = stock(db, t, "RM-STEEL-001")
        check(f"{t} consumed at ITS OWN {p['per_unit']}/unit",
              abs((left or 0) - p["expect_left"]) < 1e-9,
              f"stock={left}, expected {p['expect_left']}")

    # CONTROL: the three answers are genuinely different, so none of the above
    # is satisfied by one shared rate that happens to match.
    lefts = {t: stock(db, t, "RM-STEEL-001") for t in PROFILE}
    check("CONTROL: the three factories consumed three different amounts",
          len(set(lefts.values())) == 3, str(lefts))

    for t in PROFILE:
        check(f"{t} received its finished goods",
              stock(db, t, "FG-SHAFT-001") == PROFILE[t]["made"],
              str(stock(db, t, "FG-SHAFT-001")))


def test_a_product_the_platform_never_heard_of_now_works():
    db = fresh_db()
    print()
    print("=" * 74)
    print("2. A CUSTOMER'S OWN PRODUCT MOVES THEIR OWN STOCK")
    print("=" * 74)
    tok = tenancy.set_current_tenant(None)
    db.add(models.InventoryItem(tenant_code="FACTORY_B", item_code="B-ALLOY-9",
                                item_name="Alloy", category="Raw", unit="kg",
                                current_stock=500, reorder_level=5))
    db.add(models.InventoryItem(tenant_code="FACTORY_B", item_code="B-SEAL-2",
                                item_name="Seal", category="Raw", unit="pc",
                                current_stock=500, reorder_level=5))
    db.add(models.InventoryItem(tenant_code="FACTORY_B", item_code="B-FG-VALVE",
                                item_name="Valve", category="Finished", unit="pc",
                                current_stock=0, reorder_level=0))
    db.commit()
    # TWO components — the dict could only ever express one.
    b = models.BillOfMaterials(tenant_code="FACTORY_B", part_number="VALVE-77",
                               revision="A", output_item_code="B-FG-VALVE",
                               active=True, created_by="seed")
    b.components.append(models.BomComponent(tenant_code="FACTORY_B",
                                            component_code="B-ALLOY-9",
                                            quantity_per_unit=1.5, unit="kg"))
    b.components.append(models.BomComponent(tenant_code="FACTORY_B",
                                            component_code="B-SEAL-2",
                                            quantity_per_unit=2, unit="pc"))
    db.add(b)
    db.commit()
    tenancy.reset_current_tenant(tok)

    complete(db, "FACTORY_B", "VALVE-77", 50, wo="WO-9")

    check("the first component was consumed (1.5/unit x 50)",
          stock(db, "FACTORY_B", "B-ALLOY-9") == 425.0,
          str(stock(db, "FACTORY_B", "B-ALLOY-9")))
    check("the SECOND component was consumed too (2/unit x 50)",
          stock(db, "FACTORY_B", "B-SEAL-2") == 400.0,
          str(stock(db, "FACTORY_B", "B-SEAL-2")))
    check("the finished good was received",
          stock(db, "FACTORY_B", "B-FG-VALVE") == 50, str(stock(db, "FACTORY_B", "B-FG-VALVE")))

    tok = tenancy.set_current_tenant(None)
    n = db.query(models.InventoryTransaction).count()
    tenancy.reset_current_tenant(tok)
    check("three ledger rows were written (2 issues + 1 receive)", n == 3, f"n={n}")


def test_a_part_with_no_recipe_still_moves_nothing():
    db = fresh_db()
    print()
    print("=" * 74)
    print("3. NO RECIPE STILL MEANS NO MOVEMENT — BUT NOW IT IS FIXABLE")
    print("=" * 74)
    tok = tenancy.set_current_tenant(None)
    db.add(models.InventoryItem(tenant_code="FACTORY_B", item_code="X-1",
                                item_name="Thing", category="Raw", unit="kg",
                                current_stock=100, reorder_level=1))
    db.commit()
    tenancy.reset_current_tenant(tok)

    complete(db, "FACTORY_B", "UNKNOWN-PART", 10)
    check("a part with no BOM moves no stock", stock(db, "FACTORY_B", "X-1") == 100,
          str(stock(db, "FACTORY_B", "X-1")))

    # ...and the customer can fix it themselves, which is the whole difference.
    tok = tenancy.set_current_tenant(None)
    b = models.BillOfMaterials(tenant_code="FACTORY_B", part_number="UNKNOWN-PART",
                               revision="A", active=True, created_by="ops")
    b.components.append(models.BomComponent(tenant_code="FACTORY_B",
                                            component_code="X-1",
                                            quantity_per_unit=4, unit="kg"))
    db.add(b)
    db.commit()
    tenancy.reset_current_tenant(tok)
    complete(db, "FACTORY_B", "UNKNOWN-PART", 10, wo="WO-2")
    check("CONTROL: after the customer enters the recipe, it moves",
          stock(db, "FACTORY_B", "X-1") == 60.0, str(stock(db, "FACTORY_B", "X-1")))


def test_a_zero_rate_line_writes_no_ledger_row():
    """A component line with a zero rate describes no movement.

    The API rejects <= 0 on the way in and the 0004 backfill never writes one,
    so this is a DEFENSIVE guard rather than a reachable-today defect — a legacy
    row or a direct SQL edit is what would produce one. Tested because the
    alternative is a zero-quantity Issue transaction, which is a ledger entry
    recording that nothing happened, and those are worse than absent: they make
    a stock reconciliation look like it has an explanation.
    """
    db = fresh_db()
    print()
    print("=" * 74)
    print("3b. A ZERO-RATE LINE MOVES NOTHING AND LOGS NOTHING")
    print("=" * 74)
    tok = tenancy.set_current_tenant(None)
    db.add(models.InventoryItem(tenant_code="T", item_code="C1", item_name="c1",
                                category="Raw", unit="kg", current_stock=100,
                                reorder_level=1))
    db.add(models.InventoryItem(tenant_code="T", item_code="C2", item_name="c2",
                                category="Raw", unit="kg", current_stock=100,
                                reorder_level=1))
    db.commit()
    b = models.BillOfMaterials(tenant_code="T", part_number="P", revision="A",
                               active=True, created_by="s")
    # Written straight to the model, bypassing the API that would refuse it.
    b.components.append(models.BomComponent(tenant_code="T", component_code="C1",
                                            quantity_per_unit=0, unit="kg"))
    b.components.append(models.BomComponent(tenant_code="T", component_code="C2",
                                            quantity_per_unit=2, unit="kg"))
    db.add(b)
    db.commit()
    tenancy.reset_current_tenant(tok)

    complete(db, "T", "P", 10)
    check("the zero-rate component was not touched", stock(db, "T", "C1") == 100,
          str(stock(db, "T", "C1")))
    check("CONTROL: the real component beside it WAS consumed",
          stock(db, "T", "C2") == 80.0, str(stock(db, "T", "C2")))

    tok = tenancy.set_current_tenant(None)
    txns = db.query(models.InventoryTransaction).all()
    tenancy.reset_current_tenant(tok)
    check("exactly one ledger row was written, none of quantity zero",
          len(txns) == 1 and txns[0].quantity == 20.0,
          str([(t.transaction_type, t.quantity) for t in txns]))


def test_resolution_picks_the_right_revision():
    db = fresh_db()
    print()
    print("=" * 74)
    print("4. ACTIVE, EFFECTIVE, LATEST — IN THAT ORDER")
    print("=" * 74)
    # utcnow(), not today(): bom.resolve compares effective_from against
    # datetime.utcnow().date(), so a locally-dated fixture is wrong for part
    # of every day on any machine ahead of UTC and CI (UTC) never sees it.
    today = datetime.utcnow().date()
    tok = tenancy.set_current_tenant(None)
    db.add(models.InventoryItem(tenant_code="T", item_code="C", item_name="c",
                                category="Raw", unit="kg", current_stock=1000,
                                reorder_level=1))
    db.commit()

    def rev(revision, qty, active=True, eff=None):
        b = models.BillOfMaterials(tenant_code="T", part_number="P", revision=revision,
                                   active=active, effective_from=eff, created_by="s")
        b.components.append(models.BomComponent(tenant_code="T", component_code="C",
                                                quantity_per_unit=qty, unit="kg"))
        db.add(b)
        return b

    rev("A", 1, eff=today - timedelta(days=30))
    rev("B", 2, eff=today - timedelta(days=1))       # latest effective -> wins
    rev("C", 4, eff=today + timedelta(days=7))       # future -> not yet
    rev("D", 8, active=False, eff=today)             # superseded -> never
    db.commit()
    tenancy.reset_current_tenant(tok)

    resolved = bom.resolve(db, "T", "P")
    check("the latest EFFECTIVE revision wins",
          resolved is not None and resolved.revision == "B",
          resolved.revision if resolved else "None")
    check("a future-dated revision is not used yet",
          resolved is None or resolved.revision != "C")
    check("an inactive revision is never used",
          resolved is None or resolved.revision != "D")

    # CONTROL: the future revision DOES become the answer once its date passes,
    # so "not C" above is the date rule and not C being ignored outright.
    tok = tenancy.set_current_tenant(None)
    fut = db.query(models.BillOfMaterials).filter(
        models.BillOfMaterials.revision == "C").first()
    fut.effective_from = today - timedelta(days=0)
    db.commit()
    tenancy.reset_current_tenant(tok)
    check("CONTROL: once effective, the newer revision takes over",
          (bom.resolve(db, "T", "P") or rev).revision == "C",
          (bom.resolve(db, "T", "P") or rev).revision)

    check("another tenant's identical part resolves to nothing",
          bom.resolve(db, "OTHER", "P") is None)


def test_the_api_validates_and_isolates():
    db = fresh_db()
    print()
    print("=" * 74)
    print("5. THE API: VALIDATION, RBAC AND ISOLATION")
    print("=" * 74)
    tenancy.install_scoping()

    tok = tenancy.set_current_tenant(None)
    for t in ("FACTORY_A", "FACTORY_B"):
        db.add(models.InventoryItem(tenant_code=t, item_code="RM-1", item_name="Bar",
                                    category="Raw", unit="kg", current_stock=10,
                                    reorder_level=1))
    db.commit()
    tenancy.reset_current_tenant(tok)

    def create(tenant, **kw):
        payload = {"part_number": "P-1",
                   "components": [{"component_code": "RM-1",
                                   "quantity_per_unit": 2, "unit": "kg"}]}
        payload.update(kw)
        tok = tenancy.set_current_tenant(tenant)
        try:
            return bom_routes.create_bom(payload, db=db, current_user=admin(tenant))
        finally:
            tenancy.reset_current_tenant(tok)

    a_rows = create("FACTORY_A")
    check("a BOM is created and returned one row per component",
          len(a_rows) == 1 and a_rows[0]["component_code"] == "RM-1", str(a_rows))
    b_rows = create("FACTORY_B")
    check("a SECOND tenant may use the same part number", len(b_rows) == 1)

    try:
        create("FACTORY_A")
        check("the same tenant cannot repeat a part+revision", False, "accepted")
    except HTTPException as e:
        check("the same tenant cannot repeat a part+revision", e.status_code == 409,
              str(e.status_code))

    # --- validation -------------------------------------------------------
    for label, kw in (
        ("a negative quantity is rejected",
         {"part_number": "P-2", "components": [{"component_code": "RM-1",
                                                "quantity_per_unit": -1}]}),
        ("a zero quantity is rejected",
         {"part_number": "P-3", "components": [{"component_code": "RM-1",
                                                "quantity_per_unit": 0}]}),
        ("a non-numeric quantity is rejected",
         {"part_number": "P-4", "components": [{"component_code": "RM-1",
                                                "quantity_per_unit": "lots"}]}),
        ("a duplicated component line is rejected",
         {"part_number": "P-5", "components": [
             {"component_code": "RM-1", "quantity_per_unit": 1},
             {"component_code": "RM-1", "quantity_per_unit": 2}]}),
        ("a missing component code is rejected",
         {"part_number": "P-6", "components": [{"quantity_per_unit": 1}]}),
    ):
        try:
            create("FACTORY_A", **kw)
            check(label, False, "accepted")
        except HTTPException as e:
            check(label, e.status_code == 400, f"status={e.status_code}")

    # A rejected create must leave NOTHING behind — a half-entered recipe is
    # worse than a rejected one.
    tok = tenancy.set_current_tenant(None)
    parts = sorted(b.part_number for b in db.query(models.BillOfMaterials).all())
    tenancy.reset_current_tenant(tok)
    check("no partial BOM survived a rejected create", parts == ["P-1", "P-1"],
          str(parts))

    # --- isolation --------------------------------------------------------
    tok = tenancy.set_current_tenant("FACTORY_A")
    listed = bom_routes.list_bom(db=db, current_user=admin("FACTORY_A"))
    tenancy.reset_current_tenant(tok)
    check("a tenant lists only its own BOMs", len(listed) == 1, str(len(listed)))

    b_id = b_rows[0]["id"]
    tok = tenancy.set_current_tenant("FACTORY_A")
    try:
        bom_routes.update_bom(b_id, {"active": False}, db=db,
                              current_user=admin("FACTORY_A"))
        check("FACTORY_A cannot edit FACTORY_B's BOM", False, "the edit succeeded")
    except HTTPException as e:
        check("FACTORY_A cannot edit FACTORY_B's BOM", e.status_code == 404,
              str(e.status_code))
    try:
        bom_routes.delete_bom(b_id, db=db, current_user=admin("FACTORY_A"))
        check("FACTORY_A cannot delete FACTORY_B's BOM", False, "the delete succeeded")
    except HTTPException as e:
        check("FACTORY_A cannot delete FACTORY_B's BOM", e.status_code == 404,
              str(e.status_code))
    tenancy.reset_current_tenant(tok)

    # CONTROL: FACTORY_B's own admin CAN edit it, so the 404s above are the
    # tenant filter and not a broken handler.
    tok = tenancy.set_current_tenant("FACTORY_B")
    updated = bom_routes.update_bom(b_id, {"active": False}, db=db,
                                    current_user=admin("FACTORY_B"))
    tenancy.reset_current_tenant(tok)
    check("CONTROL: the owning tenant can edit it",
          updated[0]["active"] is False, str(updated[0]["active"]))


def test_rbac_is_admin_only():
    print()
    print("=" * 74)
    print("6. RBAC — EDITING A RECIPE MOVES INVENTORY, SO IT IS ADMIN-ONLY")
    print("=" * 74)
    import inspect as _inspect
    for name in ("list_bom", "create_bom", "update_bom", "delete_bom"):
        fn = getattr(bom_routes, name)
        src = _inspect.getsource(fn)
        check(f"{name} is guarded by require_roles([\"Admin\"])",
              'require_roles(["Admin"])' in src,
              "guard not found in the signature")


if __name__ == "__main__":
    test_three_tenants_three_different_recipes()
    test_a_product_the_platform_never_heard_of_now_works()
    test_a_part_with_no_recipe_still_moves_nothing()
    test_a_zero_rate_line_writes_no_ledger_row()
    test_resolution_picks_the_right_revision()
    test_the_api_validates_and_isolates()
    test_rbac_is_admin_only()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        sys.exit(1)
    print("EVERY TENANT'S BILL OF MATERIALS IS ITS OWN")
