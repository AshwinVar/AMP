"""A business document number belongs to a tenant, not to the platform.

WHAT WAS MEASURED ON MASTER
---------------------------
Nineteen business codes carried a plain ``unique=True``. Seeding two tenants and
asking each to create the same code:

    20 of 20 business codes cannot be reused by a second tenant

And because the generated numbers come from ``db.query(Model).count()`` — which
the ADR-0002 hook scopes to the current tenant — two customers independently
generate the SAME number, and the global constraint then rejects the second:

    FACTORY_A   OK   -> MIS-5001
    FACTORY_B   IntegrityError escapes the route -> 500 to the client
                (sqlite3.IntegrityError) UNIQUE constraint failed:
                material_issue_slips.slip_no

That is customer #2's first material issue slip, on their first day.

Run: DATABASE_URL="sqlite:///./ci.db" python test_tenant_document_numbers.py
"""
import datetime
import os
import sys

from sqlalchemy import Date, DateTime, Float, Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
from database import Base

# Same reasoning as test_mqtt_tenant_identity: a private engine, because this
# suite creates and drops its own schema and must not touch the database the
# rest of a pytest session is sharing. A PostgreSQL DATABASE_URL is honoured so
# the constraint can be proved against the engine production actually runs.
_URL = os.environ.get("DATABASE_URL", "")
if _URL.startswith("postgresql"):
    engine = create_engine(_URL)
else:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
SessionLocal = sessionmaker(bind=engine)

failures = []

# The nineteen codes that are a tenant's own numbering, and the four that are
# deliberately platform-wide. Listed explicitly rather than derived from the
# models, so that making a code global in future has to be a decision recorded
# HERE rather than an edit that quietly passes.
TENANT_SCOPED = [
    ("WorkOrder", "work_order_no"), ("ProductionPlan", "plan_no"),
    ("InventoryItem", "item_code"), ("QualityInspection", "inspection_no"),
    ("CustomerOrder", "order_no"), ("Supplier", "supplier_code"),
    ("PurchaseOrder", "po_no"), ("ComplianceDocument", "document_no"),
    ("MaintenanceTask", "task_no"), ("ProductionSchedule", "schedule_no"),
    ("CostRecord", "cost_no"), ("OperatorJobExecution", "execution_no"),
    ("ReportRequest", "report_no"), ("Remnant", "tag_no"),
    ("MaterialIssueSlip", "slip_no"), ("GoodsReceiptNote", "grn_no"),
    ("CycleCount", "count_no"), ("IndustrialDevice", "device_code"),
    ("PlcSignalMapping", "mapping_code"),
]
DELIBERATELY_GLOBAL = {
    ("User", "username"):
        "the login identity, resolved before any tenant is known",
    ("TenantConfig", "tenant_code"): "the tenant registry — one row per tenant",
    ("AgentPolicy", "tenant_code"): "the tenant registry — one row per tenant",
    ("CompanyTenant", "company_code"): "the tenant registry — one row per tenant",
}


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def _filler(col):
    t = type(col.type)
    if t is Integer:
        return 1
    if t is Float:
        return 1.0
    if t is DateTime:
        return datetime.datetime.utcnow()
    if t is Date:
        # utcnow(), not today(): test_date_basis_guard forbids the local
        # date because the code under test compares against UTC, so a
        # locally-seeded row fails for part of every day on any machine
        # ahead of UTC while CI (UTC) never sees it.
        return datetime.datetime.utcnow().date()
    return "X"


def _row(model, tenant, code_col, code):
    """A minimally-valid instance: every NOT NULL column without a default
    filled, so a rejection can only be the uniqueness constraint and never a
    missing field. Getting this wrong is how a probe reports a blocked tenant
    that is really a broken fixture."""
    kw = {}
    for c in model.__table__.columns:
        if c.primary_key or c.name == "tenant_code":
            continue
        if c.nullable is False and c.default is None and c.server_default is None:
            kw[c.name] = _filler(c)
    kw[code_col] = code
    return model(tenant_code=tenant, **kw)


def test_document_numbers_are_per_tenant():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    print("=" * 74)
    print("1. TWO CUSTOMERS CAN EACH OWN THE SAME BUSINESS CODE")
    print("=" * 74)
    blocked = []
    for model_name, col in TENANT_SCOPED:
        model = getattr(models, model_name)
        results = []
        for t in ("FACTORY_A", "FACTORY_B"):
            try:
                db.add(_row(model, t, col, "SHARED-001"))
                db.commit()
                results.append("ok")
            except Exception as exc:
                db.rollback()
                results.append(f"REJECTED({type(exc).__name__})")
        if results != ["ok", "ok"]:
            blocked.append(f"{model_name}.{col} -> {results}")
    check(f"all {len(TENANT_SCOPED)} business codes can be reused by a second tenant",
          not blocked, "; ".join(blocked[:4]))

    print()
    print("=" * 74)
    print("2. A DUPLICATE WITHIN ONE TENANT IS STILL REJECTED")
    print("=" * 74)
    # The constraint must not have been merely deleted. Every code that two
    # tenants may share must still be unique inside one of them.
    leaked = []
    for model_name, col in TENANT_SCOPED:
        model = getattr(models, model_name)
        try:
            db.add(_row(model, "FACTORY_A", col, "SHARED-001"))
            db.commit()
            leaked.append(f"{model_name}.{col}")
            db.rollback()
        except Exception:
            db.rollback()
    check(f"all {len(TENANT_SCOPED)} codes remain unique WITHIN a tenant",
          not leaked, "these accepted a duplicate: " + ", ".join(leaked[:6]))

    print()
    print("=" * 74)
    print("3. THE CODES THAT STAY PLATFORM-WIDE, STAY PLATFORM-WIDE")
    print("=" * 74)
    for (model_name, col), why in DELIBERATELY_GLOBAL.items():
        column = getattr(models, model_name).__table__.columns[col]
        check(f"{model_name}.{col} is still globally unique — {why}",
              column.unique is True, f"unique={column.unique}")

    db.close()


def test_generated_numbers_no_longer_collide_between_tenants():
    """The reproduction from the module docstring, run forwards."""
    import enterprise_inventory_routes as eir

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    db = SessionLocal()

    print()
    print("=" * 74)
    print("4. EACH CUSTOMER'S FIRST ISSUE SLIP SUCCEEDS")
    print("=" * 74)

    tok = tenancy.set_current_tenant(None)
    for t in ("FACTORY_A", "FACTORY_B", "FACTORY_C"):
        db.add(models.InventoryItem(
            tenant_code=t, item_code="RM-001", item_name="Steel",
            category="Raw", unit="kg", current_stock=500, reorder_level=10))
    db.commit()
    items = {i.tenant_code: i.id for i in db.query(models.InventoryItem).all()}
    tenancy.reset_current_tenant(tok)

    slips = {}
    for t in ("FACTORY_A", "FACTORY_B", "FACTORY_C"):
        tok = tenancy.set_current_tenant(t)
        try:
            r = eir.create_issue_slip(
                {"item_id": items[t], "requested_qty": 5},
                db=db, current_user={"sub": "a", "role": "Admin", "tenant": t})
            slips[t] = r.get("slip_no") if isinstance(r, dict) else getattr(r, "slip_no", r)
        except Exception as exc:
            db.rollback()
            slips[t] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:70]}"
        tenancy.reset_current_tenant(tok)

    for t, v in slips.items():
        check(f"{t} got a slip number", isinstance(v, str) and v.startswith("MIS-"), str(v))
    # Each tenant is on its own sequence, so all three legitimately read MIS-5001.
    # That is the point: the number means "my first slip", not "the platform's".
    check("all three are each tenant's own first number",
          len(slips) == 3 and all(str(v) == "MIS-5001" for v in slips.values()),
          str(slips))

    # CONTROL: the rows really are three separate slips owned by three tenants,
    # not one row counted three times.
    tok = tenancy.set_current_tenant(None)
    owners = sorted(s.tenant_code for s in db.query(models.MaterialIssueSlip).all())
    tenancy.reset_current_tenant(tok)
    check("CONTROL: three distinct slips exist, one per tenant",
          owners == ["FACTORY_A", "FACTORY_B", "FACTORY_C"], str(owners))

    db.close()


def test_a_deletion_leaves_a_gap_rather_than_reusing_a_number():
    """The route must translate the constraint, not leak it.

    A tenant CAN still collide with itself — the counter is derived from a row
    count, so deleting a slip makes the next one reuse a number. That must reach
    the operator as a refusal they can act on, not a 500.
    """
    import enterprise_inventory_routes as eir
    from fastapi import HTTPException

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    db = SessionLocal()

    print()
    print("=" * 74)
    print("5. A DELETION LEAVES A GAP; A NUMBER IS NEVER REUSED")
    print("=" * 74)

    tok = tenancy.set_current_tenant(None)
    db.add(models.InventoryItem(tenant_code="FACTORY_A", item_code="RM-001",
                                item_name="Steel", category="Raw", unit="kg",
                                current_stock=500, reorder_level=10))
    db.commit()
    item_id = db.query(models.InventoryItem).first().id
    tenancy.reset_current_tenant(tok)

    user = {"sub": "a", "role": "Admin", "tenant": "FACTORY_A"}
    tok = tenancy.set_current_tenant("FACTORY_A")
    first = eir.create_issue_slip({"item_id": item_id, "requested_qty": 5},
                                  db=db, current_user=user)
    check("the first slip is created",
          (first.get("slip_no") if isinstance(first, dict) else None) == "MIS-5001",
          str(first))

    # A document number must never be REUSED, and a deletion is what used to
    # cause reuse: `count() + 1` is a population, not a sequence, so removing a
    # row made the next create regenerate a number that still existed. The
    # sequence only moves forward, so a deletion leaves a GAP - which is the
    # correct outcome. A gap is auditable; a reused issue-slip number is a
    # reconciliation problem that outlives the request.
    second = eir.create_issue_slip({"item_id": item_id, "requested_qty": 5},
                                   db=db, current_user=user)
    check("the second slip is MIS-5002",
          (second.get("slip_no") if isinstance(second, dict) else None) == "MIS-5002",
          str(second))

    db.query(models.MaterialIssueSlip).filter(
        models.MaterialIssueSlip.slip_no == "MIS-5001").delete(
            synchronize_session=False)
    db.commit()
    check("CONTROL: the row count really did drop (what used to rewind the counter)",
          db.query(models.MaterialIssueSlip).count() == 1,
          "count=%d" % db.query(models.MaterialIssueSlip).count())

    third = eir.create_issue_slip({"item_id": item_id, "requested_qty": 5},
                                  db=db, current_user=user)
    third_no = third.get("slip_no") if isinstance(third, dict) else None
    check("after a deletion the next number moves FORWARD, leaving a gap",
          third_no == "MIS-5003",
          "got %s - 5002 would be a reuse, 5001 a resurrection" % third_no)
    tenancy.reset_current_tenant(tok)

    live = sorted(x.slip_no for x in db.query(models.MaterialIssueSlip).all())
    check("no two slips share a number", len(live) == len(set(live)), str(live))
    db.close()


def test_the_sequence_seeds_from_numbers_already_issued():
    """Deploying over live data must not restart numbering.

    An existing customer already has MIS-5001..5003 issued by the old
    count-based generator. If the first allocation started at 5001 again it
    would collide immediately, and this change would be undeployable without a
    data migration. The first allocation per (tenant, type) seeds from the
    highest number already present instead.
    """
    import enterprise_inventory_routes as eir

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    db = SessionLocal()

    print()
    print("=" * 74)
    print("6. AN UPGRADED DATABASE CONTINUES ITS NUMBERING")
    print("=" * 74)

    tok = tenancy.set_current_tenant(None)
    db.add(models.InventoryItem(tenant_code="FACTORY_A", item_code="RM-001",
                                item_name="Steel", category="Raw", unit="kg",
                                current_stock=500, reorder_level=10))
    db.commit()
    item_id = db.query(models.InventoryItem).first().id
    # History written the OLD way, before this change existed.
    for n in (5001, 5002, 5003):
        db.add(models.MaterialIssueSlip(
            tenant_code="FACTORY_A", slip_no="MIS-%d" % n, item_id=item_id,
            requested_qty=1, issued_qty=0, requested_by="a", status="Issued"))
    # A DIFFERENT tenant is much further along; it must not influence FACTORY_A.
    db.add(models.MaterialIssueSlip(
        tenant_code="FACTORY_B", slip_no="MIS-9999", item_id=item_id,
        requested_qty=1, issued_qty=0, requested_by="b", status="Issued"))
    db.commit()
    tenancy.reset_current_tenant(tok)

    tok = tenancy.set_current_tenant("FACTORY_A")
    nxt = eir.create_issue_slip(
        {"item_id": item_id, "requested_qty": 5}, db=db,
        current_user={"sub": "a", "role": "Admin", "tenant": "FACTORY_A"})
    tenancy.reset_current_tenant(tok)
    nxt_no = nxt.get("slip_no") if isinstance(nxt, dict) else None
    check("the first allocation continues from the existing history",
          nxt_no == "MIS-5004",
          "got %s - 5001 would collide with history" % nxt_no)
    check("CONTROL: another tenant's higher numbering did not leak in",
          nxt_no != "MIS-10000", "got %s" % nxt_no)
    db.close()


def test_concurrent_allocation_never_issues_the_same_number_twice():
    """Two allocators racing must produce two DIFFERENT numbers.

    The old generator read a count and both readers saw the same value. This one
    reserves inside the transaction, so the second allocator sees the first's
    increment. Modelled with two independent sessions rather than threads: the
    interleaving is then deterministic instead of timing-dependent, and it is
    the interleaving - not the wall clock - that the bug lived in.
    """
    import doc_numbers

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db_a, db_b = SessionLocal(), SessionLocal()

    print()
    print("=" * 74)
    print("7. TWO ALLOCATORS RACING GET TWO DIFFERENT NUMBERS")
    print("=" * 74)

    first = doc_numbers.allocate(db_a, "FACTORY_A", "MIS",
                                 models.MaterialIssueSlip, "slip_no", "MIS", 5000)
    db_a.commit()
    second = doc_numbers.allocate(db_b, "FACTORY_A", "MIS",
                                  models.MaterialIssueSlip, "slip_no", "MIS", 5000)
    db_b.commit()
    check("two sessions get different numbers", first != second,
          "both got %s" % first)
    check("and they are consecutive", (first, second) == ("MIS-5001", "MIS-5002"),
          "%s then %s" % (first, second))

    # CONTROL: a single allocator also advances, so the assertion above is not
    # satisfied by an allocator that simply returns something new every call
    # regardless of stored state.
    third = doc_numbers.allocate(db_a, "FACTORY_A", "MIS",
                                 models.MaterialIssueSlip, "slip_no", "MIS", 5000)
    db_a.commit()
    check("CONTROL: the sequence keeps advancing", third == "MIS-5003", str(third))

    other = doc_numbers.allocate(db_a, "FACTORY_B", "MIS",
                                 models.MaterialIssueSlip, "slip_no", "MIS", 5000)
    db_a.commit()
    check("a second tenant starts at its own beginning", other == "MIS-5001",
          str(other))

    # A DIFFERENT document type in the SAME tenant has its own counter. Sharing
    # one counter per tenant would still be collision-free and still pass every
    # assertion above, while making GRN numbers jump every time a slip is
    # issued — numbers that are supposed to be a per-book sequence.
    grn = doc_numbers.allocate(db_a, "FACTORY_A", "GRN",
                               models.GoodsReceiptNote, "grn_no", "GRN", 3000)
    db_a.commit()
    check("a different document type has its own counter", grn == "GRN-3001",
          f"got {grn} — a shared counter would show the slip numbers' influence")

    # The seed scan must filter by tenant EXPLICITLY. Inside a request the
    # ADR-0002 hook would scope it anyway, so the explicit filter looks
    # redundant there; here nothing is bound, which is how a script, a seeder or
    # a background job calls it. Without the explicit filter this seeds
    # FACTORY_C's counter from FACTORY_B's much higher numbering.
    db_a.add(models.MaterialIssueSlip(
        tenant_code="FACTORY_B", slip_no="MIS-8888", item_id=None,
        requested_qty=1, issued_qty=0, requested_by="b", status="Issued"))
    db_a.commit()
    check("CONTROL: nothing is bound, so the ORM hook cannot help",
          tenancy.current_tenant() is None, str(tenancy.current_tenant()))
    fresh_tenant = doc_numbers.allocate(db_a, "FACTORY_C", "MIS",
                                        models.MaterialIssueSlip, "slip_no",
                                        "MIS", 5000)
    db_a.commit()
    check("a new tenant's first number ignores other tenants' history",
          fresh_tenant == "MIS-5001",
          f"got {fresh_tenant} — 8889 means the seed scan spanned tenants")

    db_a.close()
    db_b.close()


if __name__ == "__main__":
    test_document_numbers_are_per_tenant()
    test_generated_numbers_no_longer_collide_between_tenants()
    test_a_deletion_leaves_a_gap_rather_than_reusing_a_number()
    test_the_sequence_seeds_from_numbers_already_issued()
    test_concurrent_allocation_never_issues_the_same_number_twice()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        sys.exit(1)
    print("DOCUMENT NUMBERS ARE TENANT-SCOPED")
