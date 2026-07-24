"""APPROVAL-GATED backfill of tenant_code for the audit trail + enterprise
inventory (ADR-0002 / security/fix-enterprise-tenant-isolation).

These tables were given tenant_code NULLABLE with NO blind backfill, so their
existing rows are NULL and hidden from every tenant. This script assigns each
NULL row to its OWNING tenant using a reliable in-data source, and LEAVES
AMBIGUOUS ROWS NULL (hidden) rather than guessing:

  Remnant / MaterialIssueSlip / GRNItem / CycleCountItem
      -> tenant of their InventoryItem (item_id -> inventory_items.tenant_code)
  GoodsReceiptNote  -> tenant of its GRN items' inventory_items (only if they all
                       agree on one tenant), else the received_by user's tenant,
                       else LEFT NULL
  CycleCount        -> tenant of its count items' inventory_items (if unanimous),
                       else the counted_by user's tenant, else LEFT NULL
  AuditLog          -> tenant of the `actor` user (username -> users.tenant_code);
                       'system' / unknown actor -> LEFT NULL

Safety:
  * DRY by default — prints a per-table plan and writes nothing.
  * Idempotent — only rows with tenant_code IS NULL are considered.
  * Never assigns DEFAULT as a fallback; ambiguous rows stay NULL (hidden).
  * Rollback — set the affected rows' tenant_code back to NULL; they re-hide.

    python backend/backfill_enterprise_tenants.py            # dry run (report only)
    python backend/backfill_enterprise_tenants.py --apply    # execute (after approval)
"""
import sys
from collections import Counter

import models
from database import SessionLocal


def _map(db, model, key, val):
    return {getattr(r, key): getattr(r, val) for r in db.query(model).all()}


def plan(db):
    items = _map(db, models.InventoryItem, "id", "tenant_code")
    users = _map(db, models.User, "username", "tenant_code")
    assignments = []          # (row, tenant_or_None)
    stats = {}

    def record(model_name, row, tenant):
        assignments.append((row, tenant))
        stats.setdefault(model_name, Counter())[tenant or "<ambiguous: left NULL>"] += 1

    # Direct: the row carries an item_id into the (already scoped) inventory_items.
    for M in (models.Remnant, models.MaterialIssueSlip, models.GRNItem, models.CycleCountItem):
        for r in db.query(M).filter(M.tenant_code.is_(None)).all():
            record(M.__name__, r, items.get(r.item_id))

    # GoodsReceiptNote: unanimous tenant across its line items, else received_by user.
    for g in db.query(models.GoodsReceiptNote).filter(models.GoodsReceiptNote.tenant_code.is_(None)).all():
        child = {items.get(li.item_id) for li in
                 db.query(models.GRNItem).filter(models.GRNItem.grn_id == g.id).all()}
        child.discard(None)
        tenant = next(iter(child)) if len(child) == 1 else users.get(g.received_by)
        record("GoodsReceiptNote", g, tenant)

    # CycleCount: unanimous tenant across its items, else counted_by user.
    for c in db.query(models.CycleCount).filter(models.CycleCount.tenant_code.is_(None)).all():
        child = {items.get(li.item_id) for li in
                 db.query(models.CycleCountItem).filter(models.CycleCountItem.count_id == c.id).all()}
        child.discard(None)
        tenant = next(iter(child)) if len(child) == 1 else users.get(c.counted_by)
        record("CycleCount", c, tenant)

    # AuditLog: the acting user's tenant; system/unknown actors stay NULL.
    for a in db.query(models.AuditLog).filter(models.AuditLog.tenant_code.is_(None)).all():
        record("AuditLog", a, users.get(a.actor))

    return assignments, stats


def main(apply):
    db = SessionLocal()
    try:
        assignments, stats = plan(db)
        print("=== enterprise-inventory + audit tenant backfill plan ===")
        for model_name, counts in sorted(stats.items()):
            print(f"  {model_name}:")
            for tenant, n in sorted(counts.items()):
                print(f"      -> {tenant}: {n}")
        mapped = sum(1 for _, t in assignments if t)
        left = sum(1 for _, t in assignments if not t)
        print(f"  TOTAL: {mapped} mappable, {left} ambiguous (left NULL / hidden)")
        if not apply:
            print("DRY RUN — nothing written. Re-run with --apply once approved.")
            return
        for row, tenant in assignments:
            if tenant:
                row.tenant_code = tenant
        db.commit()
        print(f"APPLIED — {mapped} rows assigned; {left} left NULL (hidden).")
    finally:
        db.close()


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
