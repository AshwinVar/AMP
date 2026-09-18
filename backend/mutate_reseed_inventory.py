"""Mutation harness for the tenant-scoped inventory reseed.

Every mutation restores part of the defect (work on import, an unfiltered
delete, an orphaned proposal, production or a missing tenant let through) or
weakens a guard added with the fix. Each MUST be caught by
test_reseed_inventory_scoped.py.

A pattern that does not apply reports as a survivor, not a pass: a disabled
mutation measures nothing and looks identical to a guard that works. Files may
be CRLF in a Windows checkout, so patterns are written with \n and converted.

Not mutated, on purpose: `required=True` on --tenant. With it removed argparse
passes None through and run()'s blank-tenant check refuses the same run, so the
survivor would measure redundancy, not a missing guard.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_reseed_inventory.py
"""
import io
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST = os.path.join(HERE, "test_reseed_inventory_scoped.py")
F = "reseed_inventory.py"

PROD = (
    '        if schema_guard.is_production():\n'
    '            raise Refused(\n'
    '                "this is a production environment (RAILWAY_ENVIRONMENT or PRODUCTION "\n'
    '                "is set). reseed_inventory deletes a tenant\'s stock and purchase "\n'
    '                "orders; it is a development tool and does not run here. Nothing "\n'
    '                "was deleted.")\n')
BLANK = (
    '        if not tenant:\n'
    '            raise Refused(\n'
    '                "--tenant is required and may not be blank: name the one workspace "\n'
    '                "to reseed, e.g. --tenant DEFAULT. Nothing was deleted.")\n')
CONFIRM = (
    '        if not confirmed:\n'
    '            raise Refused(\n'
    '                f"nothing was deleted. This would delete every inventory item, stock "\n'
    '                f"movement and purchase order of tenant {tenant!r}, and the agent "\n'
    '                f"proposals for those purchase orders, then reseed demo stock into "\n'
    '                f"it. Re-run with --yes to do it.")\n')
ACTIONS = (
    '        # Proposals first, while the POs they name still exist to be matched.\n'
    '        deleted["agent_actions"] = db.query(models.AgentAction).filter(\n'
    '            models.AgentAction.ref_kind == "purchase_order",\n'
    '            models.AgentAction.ref_id.in_(\n'
    '                select(models.PurchaseOrder.id).where(models.PurchaseOrder.tenant_code == tenant)),\n'
    '        ).delete(synchronize_session=False)\n')
TXNS_AND_POS = (
    '        deleted["inventory_transactions"] = db.query(models.InventoryTransaction).filter(\n'
    '            models.InventoryTransaction.tenant_code == tenant).delete(synchronize_session=False)\n'
    '        deleted["purchase_orders"] = db.query(models.PurchaseOrder).filter(\n'
    '            models.PurchaseOrder.tenant_code == tenant).delete(synchronize_session=False)\n')

MUTATIONS = [
    ("a module-level reseed creeps back in: importing the file runs it",
     F,
     'if __name__ == "__main__":\n    sys.exit(main())',
     'run(SessionLocal(), "ACME", True)\nif __name__ == "__main__":\n    sys.exit(main())'),
    ("production is no longer refused",
     F, '        if schema_guard.is_production():\n', '        if False:\n'),
    ("production is checked only after --yes and the tenant",
     F, PROD + BLANK + CONFIRM, BLANK + CONFIRM + PROD),
    ("a blank tenant is let through",
     F, '        if not tenant:\n', '        if False:\n'),
    ("a whitespace tenant is no longer stripped to blank",
     F, '    tenant = (tenant or "").strip()\n', '    tenant = tenant or ""\n'),
    ("--yes is no longer required",
     F, '        if not confirmed:\n', '        if False:\n'),
    ("the item delete loses its tenant filter",
     F,
     'db.query(models.InventoryItem).filter(\n'
     '            models.InventoryItem.tenant_code == tenant).delete(',
     'db.query(models.InventoryItem).filter(\n            ).delete('),
    ("the purchase-order delete loses its tenant filter",
     F,
     'db.query(models.PurchaseOrder).filter(\n'
     '            models.PurchaseOrder.tenant_code == tenant).delete(',
     'db.query(models.PurchaseOrder).filter(\n            ).delete('),
    ("the stock-movement delete loses its tenant filter",
     F,
     'db.query(models.InventoryTransaction).filter(\n'
     '            models.InventoryTransaction.tenant_code == tenant).delete(',
     'db.query(models.InventoryTransaction).filter(\n            ).delete('),
    ("proposals for deleted POs are left behind",
     F, ACTIONS, '        deleted["agent_actions"] = 0\n'),
    ("the proposal cleanup ignores ref_kind and matches on the number alone",
     F, '            models.AgentAction.ref_kind == "purchase_order",\n', ''),
    ("the proposal cleanup is not limited to this tenant's POs",
     F,
     'select(models.PurchaseOrder.id).where(models.PurchaseOrder.tenant_code == tenant)),',
     'select(models.PurchaseOrder.id)),'),
    ("the proposal cleanup runs after the POs are gone",
     F, ACTIONS + TXNS_AND_POS, TXNS_AND_POS + ACTIONS),
    ("the seed runs with no tenant bound",
     F, 'token = tenancy.set_current_tenant(tenant)', 'token = tenancy.set_current_tenant(None)'),
    ("the scoping hook is never installed",
     F, '    tenancy.install_scoping()\n', ''),
    ("the orphan check is skipped",
     F, '    if orphaned:\n', '    if False:\n'),
    ("the orphan check ignores other tenants' rows in the tables it empties",
     F,
     '            if table.name in _DELETED_WITH_ITEMS:\n                q = q.where(',
     '            if table.name in _DELETED_WITH_ITEMS:\n                continue\n                q = q.where('),
    ("the orphan check counts the tenant's own movements and POs as orphans",
     F,
     '            if table.name in _DELETED_WITH_ITEMS:\n                q = q.where(',
     '            if False:\n                q = q.where('),
    ("the orphan check follows foreign keys to the wrong table",
     F,
     'if fk.column.table.name != models.InventoryItem.__tablename__:',
     'if fk.column.table.name != models.PurchaseOrder.__tablename__:'),
]


def run_test():
    # No bytecode: a mutation and its restore can land within one mtime tick,
    # and a stale .pyc would silently run the wrong version of the file.
    env = dict(os.environ, DATABASE_URL="sqlite:///./ci_mut_reseed.db", PYTHONDONTWRITEBYTECODE="1",
               PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, TEST], cwd=HERE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600, env=env)
    return r.returncode, (r.stdout + r.stderr)


def main():
    print("Baseline (unmutated) must PASS:")
    rc, out = run_test()
    if rc != 0:
        print(out[-2500:])
        print("BASELINE FAILS — fix the code before mutation testing.")
        return 1
    print("  PASS\n")

    caught, survived = 0, []
    for i, (label, rel, find, repl) in enumerate(MUTATIONS, 1):
        path = os.path.join(HERE, rel)
        # newline="" on read AND write, so a CRLF file round-trips byte-exact.
        original = io.open(path, encoding="utf-8", newline="").read()
        crlf = "\r\n" in original
        f = find.replace("\n", "\r\n") if crlf else find
        r_ = repl.replace("\n", "\r\n") if crlf else repl
        if f not in original:
            survived.append(f"{label}  [PATTERN DID NOT APPLY — unmeasured]")
            print(f"{i:2}. SURVIVED (pattern missing)  {label}")
            continue
        try:
            io.open(path, "w", encoding="utf-8", newline="").write(original.replace(f, r_, 1))
            rc, out = run_test()
        finally:
            io.open(path, "w", encoding="utf-8", newline="").write(original)
        if rc != 0:
            caught += 1
            lines = [ln.strip() for ln in out.splitlines()
                     if ln.strip().startswith(("AssertionError", "Error", "sqlalchemy.exc"))
                     or "Error:" in ln]
            print(f"{i:2}. caught     {label}")
            print(f"      -> {lines[-1][:100] if lines else '(non-zero exit)'}")
        else:
            survived.append(label)
            print(f"{i:2}. SURVIVED   {label}")

    print()
    print("=" * 74)
    print(f"{caught}/{len(MUTATIONS)} mutations caught")
    for s in survived:
        print(f"  SURVIVED: {s}")
    print("=" * 74)
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
