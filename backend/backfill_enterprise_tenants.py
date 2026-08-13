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
  * Rollback — every applied run writes a MANIFEST of exactly which rows it
    touched, in the same transaction as the writes, so `--rollback` can put
    them back without restoring a backup.

    python backend/backfill_enterprise_tenants.py            # dry run (report only)
    python backend/backfill_enterprise_tenants.py --apply    # execute (after approval)
    python backend/backfill_enterprise_tenants.py --rollback # undo the last applied run

WHY THE MANIFEST EXISTS. This operation makes rows that were hidden from
everybody VISIBLE to one tenant. Data damage is reversible; DISCLOSURE IS NOT,
and it may not be reported for days. Without a record of which rows were
touched, an assigned row is indistinguishable from one the application wrote
normally, so the only undo would be restoring a backup — losing every write
since. The manifest is filed in `event_log`, which is append-only and which the
factory reset never touches, and it is written INSIDE the same transaction:
there is no path that changes rows without recording them.
"""
import json
import sys
from collections import Counter
from datetime import datetime

import models
from database import SessionLocal

MANIFEST_EVENT = "EnterpriseTenantBackfilled"

# Only the tables this script is allowed to touch. `--rollback` refuses a
# manifest naming anything else rather than trusting a stored string to name a
# model — a corrupted or hand-edited payload must not become a way to NULL an
# arbitrary table's tenant column.
BACKFILLABLE = {
    M.__name__: M for M in (
        models.Remnant, models.MaterialIssueSlip, models.GRNItem,
        models.CycleCountItem, models.GoodsReceiptNote, models.CycleCount,
        models.AuditLog,
    )
}


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


def rollback(db):
    """Put the last applied run's rows back to NULL, and only those rows.

    Each revert is CONDITIONAL on the row still holding the value this script
    assigned. If somebody has since moved it deliberately, that decision wins
    and the row is reported as skipped rather than silently overwritten — the
    same reason the machine-claim acceptance is a conditional UPDATE (ADR-0019).
    """
    manifest = (db.query(models.EventLog)
                  .filter(models.EventLog.event_type == MANIFEST_EVENT)
                  .order_by(models.EventLog.id.desc()).first())
    if manifest is None:
        print("No applied run found — nothing to roll back.")
        return 1

    payload = json.loads(manifest.payload or "{}")
    rows = payload.get("rows", [])
    print(f"=== rolling back run of {payload.get('applied_at')} "
          f"({len(rows)} rows) ===")

    unknown = sorted({name for name, _, _ in rows} - set(BACKFILLABLE))
    if unknown:
        print(f"REFUSED: manifest names tables this script never writes: {unknown}")
        return 2

    reverted = skipped = 0
    for name, row_id, tenant in rows:
        M = BACKFILLABLE[name]
        n = (db.query(M).filter(M.id == row_id, M.tenant_code == tenant)
               .update({"tenant_code": None}, synchronize_session=False))
        reverted += n
        skipped += (1 - n)
    db.add(models.EventLog(
        tenant_code="DEFAULT", event_type=f"{MANIFEST_EVENT}RolledBack",
        event_version=1,
        payload=json.dumps({"manifest_event_id": manifest.id,
                            "reverted": reverted, "skipped": skipped})))
    db.commit()
    print(f"ROLLED BACK — {reverted} rows hidden again; {skipped} left alone "
          f"(changed by somebody since).")
    return 0


def main(apply, undo=False):
    db = SessionLocal()
    try:
        if undo:
            return rollback(db)
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
            return 0
        if not mapped:
            print("Nothing to assign — no manifest written.")
            return 0

        touched = []
        for row, tenant in assignments:
            if tenant:
                row.tenant_code = tenant
                touched.append([type(row).__name__, row.id, tenant])
        # The manifest goes in the SAME transaction as the writes. A commit that
        # changed rows without recording them is not reachable.
        db.add(models.EventLog(
            tenant_code="DEFAULT", event_type=MANIFEST_EVENT, event_version=1,
            payload=json.dumps({"applied_at": datetime.utcnow().isoformat(),
                                "rows": touched})))
        db.commit()
        print(f"APPLIED — {mapped} rows assigned; {left} left NULL (hidden).")
        print(f"Manifest recorded in event_log as {MANIFEST_EVENT}. "
              f"Undo with:  python backfill_enterprise_tenants.py --rollback")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv, undo="--rollback" in sys.argv))
