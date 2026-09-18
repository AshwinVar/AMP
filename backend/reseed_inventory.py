"""Clear and reseed ONE tenant's demo inventory. A development tool.

WHAT THIS FILE USED TO DO
-------------------------
Its work ran at MODULE LEVEL, with no tenant bound and nothing asked:

    db.query(models.InventoryTransaction).delete()
    db.query(models.PurchaseOrder).delete()
    db.query(models.InventoryItem).delete()
    db.commit()

`import reseed_inventory` was enough to run it. The ADR-0002 hook scopes
SELECTs, never a bulk DELETE, and nothing was bound anyway, so those statements
removed EVERY tenant's stock, stock history and purchase orders. Agent-drafted
POs went with them, and each AgentAction that proposed one stayed 'Proposed' in
the approval queue with a ref_id pointing at a row that no longer existed. The
only thing that made it a "local dev reseed" was backend/.env pointing at
localhost: from a Railway shell, or with a production DATABASE_URL exported, the
same command wiped every customer's inventory.

WHAT KEEPS IT OFF A CUSTOMER NOW
--------------------------------
  1. NOTHING HAPPENS ON IMPORT. The work is behind `__main__`, and
     factory_simulator (whose import runs create_all() on an unmanaged
     database) is imported only once a reseed has been allowed.
  2. PRODUCTION IS REFUSED FIRST. "Production" is schema_guard.is_production():
     RAILWAY_ENVIRONMENT or PRODUCTION=1, the rule auth.py applies to the JWT
     signing key. One definition, not a third copy.
  3. ONE NAMED TENANT. `--tenant` is required and has no default. Every delete
     filters on that tenant's `tenant_code` in the statement itself, and the
     reseed runs with the tenant bound so each seeded row is stamped with it.
  4. IT ASKS. Without `--yes` it says what it would delete and deletes nothing.

WHAT IT DELETES, AND WHAT STOPS IT
----------------------------------
The tenant's stock movements, purchase orders and items, plus every AgentAction
proposing one of those POs, in ONE transaction, so the approval queue is never
left holding a proposal for a PO that is gone. A proposal is matched by what it
points at, not by its own tenant_code: once the PO is deleted, any row naming it
is dangling whoever it belongs to.

A row that is NOT deleted but points at one of the items stops the reseed before
anything is removed: a goods-receipt line, an issue slip, a remnant, a cycle
count, or another tenant's movement or PO that should never have pointed here.
Those record real stock, not demo seed. The check reads the schema's foreign
keys, so a table added later that references inventory_items is covered too. On
SQLite, where this tool runs, no foreign key would have stopped the delete; the
rows would simply have been orphaned.

Run:  python backend/reseed_inventory.py --tenant DEFAULT --yes
"""
import argparse
import sys

from sqlalchemy import func, or_, select

import models
import schema_guard
import tenancy
from database import SessionLocal

# Tables this reseed empties for the tenant along with its items. Their rows for
# OTHER tenants still count as references that would be orphaned.
_DELETED_WITH_ITEMS = ("inventory_transactions", "purchase_orders")


class Refused(Exception):
    """A refusal, raised before any row is deleted."""


def run(db, tenant, confirmed):
    """The whole command against an open session. Returns a process exit code."""
    tenant = (tenant or "").strip()
    try:
        if schema_guard.is_production():
            raise Refused(
                "this is a production environment (RAILWAY_ENVIRONMENT or PRODUCTION "
                "is set). reseed_inventory deletes a tenant's stock and purchase "
                "orders; it is a development tool and does not run here. Nothing "
                "was deleted.")
        if not tenant:
            raise Refused(
                "--tenant is required and may not be blank: name the one workspace "
                "to reseed, e.g. --tenant DEFAULT. Nothing was deleted.")
        if not confirmed:
            raise Refused(
                f"nothing was deleted. This would delete every inventory item, stock "
                f"movement and purchase order of tenant {tenant!r}, and the agent "
                f"proposals for those purchase orders, then reseed demo stock into "
                f"it. Re-run with --yes to do it.")
        result = _reseed(db, tenant)
    except Refused as e:
        print(f"REFUSED: {e}")
        return 2
    d, s = result["deleted"], result["seeded"]
    print(f"RESEEDED tenant {tenant}")
    print(f"  deleted : {d['agent_actions']} agent proposals, {d['inventory_transactions']} stock "
          f"movements, {d['purchase_orders']} purchase orders, {d['inventory_items']} items")
    print(f"  seeded  : {s['inventory_items']} items, {s['inventory_transactions']} stock movements")
    return 0


def _orphaned_by_delete(db, tenant):
    """{table: rows} that would still exist and point at one of `tenant`'s items."""
    items = select(models.InventoryItem.id).where(models.InventoryItem.tenant_code == tenant)
    found = {}
    for table in models.Base.metadata.sorted_tables:
        for fk in table.foreign_keys:
            if fk.column.table.name != models.InventoryItem.__tablename__:
                continue
            q = select(func.count()).select_from(table).where(fk.parent.in_(items))
            if table.name in _DELETED_WITH_ITEMS:
                q = q.where(or_(table.c.tenant_code != tenant, table.c.tenant_code.is_(None)))
            n = db.execute(q).scalar() or 0
            if n:
                found[table.name] = found.get(table.name, 0) + n
    return found


def _reseed(db, tenant):
    orphaned = _orphaned_by_delete(db, tenant)
    if orphaned:
        listed = ", ".join(f"{t}={n}" for t, n in sorted(orphaned.items()))
        raise Refused(
            f"rows this reseed does not delete still point at tenant {tenant!r}'s "
            f"inventory items: {listed}. Deleting the items would orphan them. "
            f"Nothing was deleted.")

    deleted = {}
    try:
        # Proposals first, while the POs they name still exist to be matched.
        deleted["agent_actions"] = db.query(models.AgentAction).filter(
            models.AgentAction.ref_kind == "purchase_order",
            models.AgentAction.ref_id.in_(
                select(models.PurchaseOrder.id).where(models.PurchaseOrder.tenant_code == tenant)),
        ).delete(synchronize_session=False)
        deleted["inventory_transactions"] = db.query(models.InventoryTransaction).filter(
            models.InventoryTransaction.tenant_code == tenant).delete(synchronize_session=False)
        deleted["purchase_orders"] = db.query(models.PurchaseOrder).filter(
            models.PurchaseOrder.tenant_code == tenant).delete(synchronize_session=False)
        deleted["inventory_items"] = db.query(models.InventoryItem).filter(
            models.InventoryItem.tenant_code == tenant).delete(synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()
        raise

    # The seeders build rows with no tenant_code and skip when inventory already
    # exists. Bound, the ADR-0002 hook scopes that check to this tenant and stamps
    # the new rows with it; unbound, another tenant's stock would make them seed
    # nothing, and anything they did seed would take the column default, DEFAULT.
    import factory_simulator
    tenancy.install_scoping()
    token = tenancy.set_current_tenant(tenant)
    try:
        factory_simulator._inventory(db)
        factory_simulator._inventory_transactions(db)
    finally:
        tenancy.reset_current_tenant(token)

    seeded = {
        "inventory_items": db.query(models.InventoryItem).filter(
            models.InventoryItem.tenant_code == tenant).count(),
        "inventory_transactions": db.query(models.InventoryTransaction).filter(
            models.InventoryTransaction.tenant_code == tenant).count(),
    }
    return {"deleted": deleted, "seeded": seeded}


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="reseed_inventory.py",
        description="Clear and reseed ONE tenant's demo inventory. Development only; "
                    "refuses to run in production.")
    parser.add_argument("--tenant", required=True,
                        help="tenant_code of the one workspace to reseed, e.g. DEFAULT")
    parser.add_argument("--yes", action="store_true",
                        help="confirm the delete; without it nothing is deleted")
    args = parser.parse_args(argv)
    db = SessionLocal()
    try:
        return run(db, args.tenant, args.yes)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
