"""reseed_inventory.py touches one named tenant, and only when it is told to.

THE DEFECT THIS PINS
--------------------
reseed_inventory.py did its work at MODULE LEVEL, with no tenant bound and no
confirmation:

    db.query(models.InventoryTransaction).delete()
    db.query(models.PurchaseOrder).delete()
    db.query(models.InventoryItem).delete()
    db.commit()

Importing it was enough. The ADR-0002 hook scopes SELECTs, never a bulk DELETE,
and nothing was bound anyway, so those statements removed EVERY tenant's stock,
stock history and purchase orders. Agent-drafted POs went with them, and the
AgentAction rows that proposed them stayed 'Proposed' in the approval queue with
a ref_id pointing at nothing. Only backend/.env pointing at localhost made it a
"local dev reseed"; from a Railway shell the same command wiped every customer.

WHAT THIS SUITE PROVES
----------------------
  * importing the module deletes nothing                  (real import, subprocess)
  * no --tenant, a blank tenant, or no --yes: refused, nothing deleted
  * production (RAILWAY_ENVIRONMENT or PRODUCTION=1): refused, nothing deleted,
    and it says production before it asks for --yes
  * reseeding ACME leaves BETA's items, transactions, POs and agent actions
    byte-for-byte as they were, removes ACME's PO proposals with its POs, and
    stamps every seeded row ACME           (real CLI, subprocess, dev-shaped DB)
  * a row the reseed would orphan (a goods receipt line, or another tenant's row
    pointing at ACME's stock) stops it before anything is deleted
  * a second reseed rebuilds the same shape

The CLI cases run in a subprocess against a throwaway SQLite FILE because that is
the incident: a person, a shell, and whatever DATABASE_URL it happened to hold.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_reseed_inventory_scoped.py
"""
import contextlib
import importlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
from database import Base

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "reseed_inventory.py")
TENANT, OTHER = "ACME", "BETA"
_PROD_VARS = ("RAILWAY_ENVIRONMENT", "PRODUCTION")


def _module():
    """Imported here, not at the top of the file: the version this suite was
    written against deleted every tenant's inventory ON IMPORT, and a test must
    not be able to do that to whichever database it was started against."""
    return importlib.import_module("reseed_inventory")


def _plant(db):
    """Both tenants hold exactly what the old script destroyed.

    Both carry RM-STEEL-001, a code the seed also creates, so a reseed that is
    not scoped sees BETA's row, decides inventory already exists, and seeds
    nothing for ACME. Each tenant also has a maintenance proposal whose ref_id
    EQUALS its PO's id, so a cleanup that matches on the number and not on
    ref_kind deletes a proposal that has nothing to do with purchasing."""
    for t in (TENANT, OTHER):
        sup = models.Supplier(tenant_code=t, supplier_code=f"SUP-{t}", supplier_name=f"{t} Metals")
        db.add(sup)
        db.flush()
        steel = models.InventoryItem(tenant_code=t, item_code="RM-STEEL-001", item_name=f"{t} steel",
                                     category="Raw Material", unit="kg", current_stock=111, reorder_level=10)
        old = models.InventoryItem(tenant_code=t, item_code=f"{t}-OLD-001", item_name=f"{t} legacy part",
                                   category="Spare", unit="pcs", current_stock=3, reorder_level=1)
        db.add_all([steel, old])
        db.flush()
        db.add(models.InventoryTransaction(tenant_code=t, item_id=steel.id, transaction_type="IN",
                                           quantity=7, reference=f"{t}-RECEIPT"))
        po = models.PurchaseOrder(tenant_code=t, po_no=f"PO-{t}-1", supplier_id=sup.id, item_id=steel.id,
                                  item_name=steel.item_name, order_quantity=50, unit="kg",
                                  expected_delivery_date=datetime.utcnow().date(), status="Draft")
        db.add(po)
        db.flush()
        db.add(models.AgentAction(tenant_code=t, agent="reorder", action_type="draft_po",
                                  summary=f"Reorder {t} steel", ref_kind="purchase_order",
                                  ref_id=po.id, status="Proposed"))
        db.add(models.AgentAction(tenant_code=t, agent="maintenance", action_type="open_task",
                                  summary=f"Service {t} press", ref_kind="maintenance_task",
                                  ref_id=po.id, status="Proposed"))
    db.commit()


def _snapshot(db, tenant):
    """Everything the reseed could touch for one tenant, as comparable tuples."""
    def rows(model, *cols):
        q = db.query(*[getattr(model, c) for c in cols]).filter(model.tenant_code == tenant)
        return sorted(tuple(r) for r in q.all())
    return {
        "items": rows(models.InventoryItem, "id", "item_code", "current_stock"),
        "transactions": rows(models.InventoryTransaction, "id", "item_id", "quantity", "reference"),
        "purchase_orders": rows(models.PurchaseOrder, "id", "po_no", "item_id", "status"),
        "agent_actions": rows(models.AgentAction, "id", "ref_kind", "ref_id", "status"),
    }


def _memory_session(foreign_keys=True):
    """SQLite with foreign keys ON by default, so the delete order is held to
    what PostgreSQL would enforce. OFF is a developer's SQLite, where nothing
    stops a delete from orphaning a row."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_con, _record):
        dbapi_con.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")

    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


@contextlib.contextmanager
def _environment(**values):
    """Production markers cleared, then `values` applied; restored afterwards."""
    saved = {k: os.environ.get(k) for k in _PROD_VARS}
    for k in _PROD_VARS:
        os.environ.pop(k, None)
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _run(db, tenant, confirmed, **env):
    out = io.StringIO()
    with _environment(**env), contextlib.redirect_stdout(out):
        rc = _module().run(db, tenant, confirmed)
    return rc, out.getvalue()


@contextlib.contextmanager
def _file_database():
    """A planted SQLite file for the subprocess cases. Yields (url, session_factory)."""
    folder = tempfile.mkdtemp(prefix="reseed_inv_")
    url = "sqlite:///" + os.path.join(folder, "reseed.db").replace("\\", "/")
    engine = create_engine(url)
    try:
        Base.metadata.create_all(bind=engine)
        factory = sessionmaker(bind=engine)
        db = factory()
        _plant(db)
        db.close()
        yield url, factory
    finally:
        engine.dispose()
        shutil.rmtree(folder, ignore_errors=True)


def _cli(url, args=(), code=None, **env):
    """The script as a person runs it. SECRET_KEY is set so a refusal is the
    script's own and not auth.py declining to import in 'production'."""
    cmd = [sys.executable, "-c", code] if code else [sys.executable, SCRIPT, *args]
    full = {**os.environ, "DATABASE_URL": url, "SECRET_KEY": "reseed-suite-key",
            "RAILWAY_ENVIRONMENT": "", "PRODUCTION": "", "PYTHONIOENCODING": "utf-8", **env}
    r = subprocess.run(cmd, cwd=HERE, env=full, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=240)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _both(factory):
    db = factory()
    try:
        return _snapshot(db, TENANT), _snapshot(db, OTHER)
    finally:
        db.close()


# --------------------------------------------------------------------------- #

def test_importing_the_module_deletes_nothing():
    with _file_database() as (url, factory):
        before = _both(factory)
        rc, out = _cli(url, code="import reseed_inventory")
        assert rc == 0, f"importing reseed_inventory must succeed and do nothing:\n{out[-2000:]}"
        assert _both(factory) == before, "importing reseed_inventory changed the database"
    print("PASS importing reseed_inventory deletes nothing, for any tenant")


def test_the_cli_refuses_without_a_tenant():
    with _file_database() as (url, factory):
        before = _both(factory)
        rc, out = _cli(url, args=["--yes"])
        assert rc != 0, f"no --tenant must refuse:\n{out[-2000:]}"
        assert "--tenant" in out, out[-2000:]
        assert _both(factory) == before, "a run with no --tenant deleted rows"
    print("PASS no --tenant: refused, nothing deleted")


def test_a_blank_tenant_is_refused():
    db = _memory_session()
    _plant(db)
    before = (_snapshot(db, TENANT), _snapshot(db, OTHER))
    for blank in ("", "   ", None):
        rc, out = _run(db, blank, True)
        assert rc != 0, f"tenant {blank!r} must be refused"
        assert "--tenant" in out, out
    assert (_snapshot(db, TENANT), _snapshot(db, OTHER)) == before
    print("PASS a blank tenant is refused, nothing deleted")


def test_without_yes_nothing_is_deleted():
    db = _memory_session()
    _plant(db)
    before = (_snapshot(db, TENANT), _snapshot(db, OTHER))
    rc, out = _run(db, TENANT, False)
    assert rc != 0, "an unconfirmed run must not report success"
    assert "--yes" in out and TENANT in out, out
    assert (_snapshot(db, TENANT), _snapshot(db, OTHER)) == before
    print("PASS without --yes: says what it would delete, deletes nothing")


def test_production_is_refused():
    with _file_database() as (url, factory):
        before = _both(factory)
        rc, out = _cli(url, args=["--tenant", TENANT, "--yes"], RAILWAY_ENVIRONMENT="production")
        assert rc != 0, f"a Railway environment must refuse:\n{out[-2000:]}"
        assert "production" in out.lower(), out[-2000:]
        assert _both(factory) == before, "the reseed ran in a Railway environment"

    db = _memory_session()
    _plant(db)
    before = (_snapshot(db, TENANT), _snapshot(db, OTHER))
    rc, out = _run(db, TENANT, True, PRODUCTION="1")
    assert rc != 0 and "production" in out.lower(), out
    # Production is the first refusal, not a note behind "add --yes": a person on
    # a production shell must not be told the one missing step is confirmation.
    rc, out = _run(db, TENANT, False, PRODUCTION="1")
    assert rc != 0 and "production" in out.lower() and "--yes" not in out, out
    assert (_snapshot(db, TENANT), _snapshot(db, OTHER)) == before
    print("PASS production (RAILWAY_ENVIRONMENT or PRODUCTION=1) is refused first, nothing deleted")


def test_reseeding_one_tenant_leaves_the_other_whole():
    import factory_simulator
    with _file_database() as (url, factory):
        acme_before, beta_before = _both(factory)
        rc, out = _cli(url, args=["--tenant", TENANT, "--yes"])
        assert rc == 0, f"the reseed of {TENANT} must succeed:\n{out[-2000:]}"
        acme, beta = _both(factory)

        # BETA: every row exactly as it was — including its PO and both proposals.
        assert beta == beta_before, f"reseeding {TENANT} changed {OTHER}:\n{beta_before}\n->\n{beta}"

        # ACME: the planted rows are gone...
        old_po_ids = {r[0] for r in acme_before["purchase_orders"]}
        assert not {r[0] for r in acme["purchase_orders"]} & old_po_ids
        assert f"{TENANT}-OLD-001" not in {r[1] for r in acme["items"]}
        assert not {r[0] for r in acme["transactions"]} & {r[0] for r in acme_before["transactions"]}
        # ...and so are the proposals for its POs, while the maintenance proposal
        # that merely shares a ref_id survives.
        assert [a[1] for a in acme["agent_actions"]] == ["maintenance_task"], acme["agent_actions"]

        # ACME was actually rebuilt, from the seed, into ACME.
        seed_codes = {s["code"] for s in factory_simulator.INVENTORY_SEED}
        assert {r[1] for r in acme["items"]} == seed_codes
        steel = next(s for s in factory_simulator.INVENTORY_SEED if s["code"] == "RM-STEEL-001")
        assert dict((r[1], r[2]) for r in acme["items"])["RM-STEEL-001"] == steel["stock"]
        assert acme["transactions"], "the reseed created no stock movements"

        db = factory()
        try:
            # Nothing seeded landed in any other tenant (the model default is DEFAULT).
            assert db.query(models.InventoryItem).filter(
                models.InventoryItem.tenant_code.notin_([TENANT, OTHER])).count() == 0
            assert db.query(models.InventoryTransaction).filter(
                models.InventoryTransaction.tenant_code.notin_([TENANT, OTHER])).count() == 0
            # No purchasing proposal anywhere points at a PO that does not exist.
            po_ids = {i for (i,) in db.query(models.PurchaseOrder.id).all()}
            dangling = [a.id for a in db.query(models.AgentAction)
                        .filter(models.AgentAction.ref_kind == "purchase_order").all()
                        if a.ref_id not in po_ids]
            assert not dangling, f"proposals left pointing at deleted POs: {dangling}"
        finally:
            db.close()
    print(f"PASS reseeding {TENANT} rebuilt {TENANT} alone; {OTHER}'s stock, POs and proposals untouched")


def test_a_row_that_would_be_orphaned_stops_the_reseed():
    # Foreign keys OFF, as on a developer's SQLite: that is where this check is
    # the only thing standing between the delete and an orphaned row, so the
    # refusal has to be the script's own and not a constraint error.
    #
    # A goods-receipt line is a record of a real delivery, not demo seed.
    db = _memory_session(foreign_keys=False)
    _plant(db)
    steel = db.query(models.InventoryItem).filter(models.InventoryItem.tenant_code == TENANT,
                                                  models.InventoryItem.item_code == "RM-STEEL-001").one()
    grn = models.GoodsReceiptNote(tenant_code=TENANT, grn_no="GRN-1", supplier_name="ACME Metals",
                                  received_by="stores")
    db.add(grn)
    db.flush()
    db.add(models.GRNItem(tenant_code=TENANT, grn_id=grn.id, item_id=steel.id,
                          received_qty=5, accepted_qty=5))
    db.commit()
    before = (_snapshot(db, TENANT), _snapshot(db, OTHER))
    rc, out = _run(db, TENANT, True)
    assert rc != 0 and "grn_items" in out, out
    assert (_snapshot(db, TENANT), _snapshot(db, OTHER)) == before, "rows were deleted before the refusal"

    # Another tenant's movement pointing at ACME's stock is not ACME's to delete,
    # and deleting the item under it would orphan it.
    db = _memory_session(foreign_keys=False)
    _plant(db)
    steel = db.query(models.InventoryItem).filter(models.InventoryItem.tenant_code == TENANT,
                                                  models.InventoryItem.item_code == "RM-STEEL-001").one()
    db.add(models.InventoryTransaction(tenant_code=OTHER, item_id=steel.id, transaction_type="OUT",
                                       quantity=1, reference="CROSSED"))
    db.commit()
    before = (_snapshot(db, TENANT), _snapshot(db, OTHER))
    rc, out = _run(db, TENANT, True)
    assert rc != 0 and "inventory_transactions" in out, out
    assert (_snapshot(db, TENANT), _snapshot(db, OTHER)) == before, "rows were deleted before the refusal"
    print("PASS a row the reseed would orphan (a GRN line, another tenant's movement) stops it first")


def test_a_second_reseed_rebuilds_the_same_shape():
    db = _memory_session()
    _plant(db)
    beta = _snapshot(db, OTHER)
    rc, out = _run(db, TENANT, True)
    assert rc == 0, out
    first = _snapshot(db, TENANT)
    rc, out = _run(db, TENANT, True)
    assert rc == 0, out
    second = _snapshot(db, TENANT)
    assert len(second["items"]) == len(first["items"]) > 0
    assert len(second["transactions"]) == len(first["transactions"]) > 0
    assert second["purchase_orders"] == [] and [a[1] for a in second["agent_actions"]] == ["maintenance_task"]
    assert _snapshot(db, OTHER) == beta
    print("PASS a second reseed rebuilds the same shape, and still only for that tenant")


if __name__ == "__main__":
    test_importing_the_module_deletes_nothing()
    test_the_cli_refuses_without_a_tenant()
    test_a_blank_tenant_is_refused()
    test_without_yes_nothing_is_deleted()
    test_production_is_refused()
    test_reseeding_one_tenant_leaves_the_other_whole()
    test_a_row_that_would_be_orphaned_stops_the_reseed()
    test_a_second_reseed_rebuilds_the_same_shape()
    print("RESEED INVENTORY OK: inert on import; one named tenant; --yes required; production refused; "
          "other tenants and their proposals untouched; orphaning rows stop it")
