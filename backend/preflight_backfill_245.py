"""PREFLIGHT for the #245 tenant backfill. Proves its safety properties; writes
nothing to anything real.

`backfill_enterprise_tenants.py` assigns a tenant to rows that are currently
NULL and therefore hidden from everybody (ADR-0002: the scoping hook's
`tenant_code == tenant` never matches NULL). That is the whole risk in one
sentence:

    THIS OPERATION MAKES HIDDEN ROWS VISIBLE. A wrong assignment is not a bad
    number in a report — it is one customer's goods receipts, stock movements
    and audit trail appearing inside another customer's workspace.

Data damage from a wrong assignment is reversible (set the rows back to NULL).
DISCLOSURE IS NOT. So the properties below are checked by running the real
`plan()` against fixtures built to be adversarial, rather than by reading it.

    python preflight_backfill_245.py            # SQLite, fast
    python preflight_backfill_245.py 5432        # against scratch PostgreSQL

Exit 0 means: on this build, the backfill assigns only what it can prove, never
guesses, never touches a row that already has an owner, and can be run twice.
It does NOT mean the production data is safe to backfill — only a dry run
against production can say that, and it is the operator who must read it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAILURES = []
CHECKS = [0]


def check(label, ok, detail=""):
    CHECKS[0] += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{label}: {detail}")


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else None
    if port:
        import pg_scratch
        print(pg_scratch.ensure(port, "amp_preflight_245").split(",")[0])
        os.environ["DATABASE_URL"] = pg_scratch.scratch_url(port, "amp_preflight_245")
    else:
        os.environ["DATABASE_URL"] = "sqlite:///./preflight_245.db"
        for stale in ("preflight_245.db",):
            if os.path.exists(stale):
                os.remove(stale)

    import models
    import tenancy
    from database import Base, engine, SessionLocal

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()

    import backfill_enterprise_tenants as bf

    # ---------------------------------------------------------------- seed --
    # The session is deliberately UNBOUND: the backfill itself runs unbound
    # (it is a cross-tenant repair), so the fixture is built the same way.
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)

    for t in ("ALPHA_MFG", "BETA_WORKS"):
        db.add(models.User(username=f"{t.lower()}_ops", password="x", role="Admin",
                           tenant_code=t, is_active=True))
    db.flush()

    items = {}
    for t in ("ALPHA_MFG", "BETA_WORKS"):
        it = models.InventoryItem(tenant_code=t, item_code=f"{t}-1",
                                  item_name=f"{t} stock", category="Raw",
                                  unit="kg", current_stock=10, reorder_level=1)
        db.add(it)
        db.flush()
        items[t] = it.id
    ORPHAN_ITEM = 999_999          # an item_id pointing at nothing

    ids = {}

    def remnant(item_id, tenant, tag):
        r = models.Remnant(item_id=item_id, tenant_code=tenant, tag_no=tag,
                           original_qty=10, remaining_qty=4, unit="kg")
        db.add(r); db.flush()
        return r.id

    def grn(no, received_by):
        g = models.GoodsReceiptNote(grn_no=no, received_by=received_by,
                                    supplier_name="Acme Supply", tenant_code=None)
        db.add(g); db.flush()
        return g

    def grn_line(grn_id, item_id):
        db.add(models.GRNItem(grn_id=grn_id, item_id=item_id, received_qty=1,
                              accepted_qty=1, tenant_code=None))

    # 1. The direct cases: the row names an item, and the item names the tenant.
    ids["remnant_ok"] = remnant(items["ALPHA_MFG"], None, "RM-1")

    # 2. AMBIGUOUS: an orphaned item_id. Must be LEFT NULL, never guessed.
    ids["remnant_orphan"] = remnant(ORPHAN_ITEM, None, "RM-2")

    # 3. ALREADY OWNED: a row that is not NULL must never be re-tenanted, or a
    #    routine repair silently moves live data between customers.
    ids["remnant_owned"] = remnant(items["ALPHA_MFG"], "BETA_WORKS", "RM-3")

    # 4. A GRN whose line items are UNANIMOUS -> that tenant.
    g1 = grn("GRN-1", "alpha_mfg_ops")
    grn_line(g1.id, items["ALPHA_MFG"])
    ids["grn_unanimous"] = g1.id

    # 5. THE CROSS-TENANT GRN. Its lines span two customers, so no line-derived
    #    answer is defensible. This is the fixture that matters most: a wrong
    #    rule here discloses one customer's receipts to another.
    g2 = grn("GRN-2", "beta_works_ops")
    for t in ("ALPHA_MFG", "BETA_WORKS"):
        grn_line(g2.id, items[t])
    ids["grn_split"] = g2.id

    # 6. A GRN with NO lines and an UNKNOWN receiver -> nothing to go on.
    g3 = grn("GRN-3", "somebody_who_left")
    ids["grn_unknowable"] = g3.id

    # 7. Audit rows: a real actor, and the 'system' actor that owns nothing.
    a1 = models.AuditLog(actor="beta_works_ops", action="login", tenant_code=None)
    a2 = models.AuditLog(actor="system", action="retention_prune", tenant_code=None)
    db.add_all([a1, a2]); db.flush()
    ids["audit_user"], ids["audit_system"] = a1.id, a2.id

    db.commit()

    # ------------------------------------------------------------- the DRY --
    print()
    print("=" * 74)
    print("1. THE DRY RUN WRITES NOTHING")
    print("=" * 74)
    before = {m.__name__: {r.id: r.tenant_code for r in db.query(m).all()}
              for m in (models.Remnant, models.GoodsReceiptNote, models.AuditLog)}
    bf.main(apply=False)
    db.expire_all()
    after = {m.__name__: {r.id: r.tenant_code for r in db.query(m).all()}
             for m in (models.Remnant, models.GoodsReceiptNote, models.AuditLog)}
    check("a dry run changes not one row", before == after,
          f"{before} -> {after}")

    # ------------------------------------------------------------ the plan --
    print()
    print("=" * 74)
    print("2. WHAT IT WOULD ASSIGN, AND WHAT IT REFUSES TO GUESS")
    print("=" * 74)
    assignments, stats = bf.plan(db)
    decided = {(type(row).__name__, row.id): tenant for row, tenant in assignments}

    check("a row whose item names its owner is assigned that owner",
          decided.get(("Remnant", ids["remnant_ok"])) == "ALPHA_MFG",
          str(decided.get(("Remnant", ids["remnant_ok"]))))
    check("an ORPHANED item_id is left NULL, not guessed",
          decided.get(("Remnant", ids["remnant_orphan"])) is None,
          str(decided.get(("Remnant", ids["remnant_orphan"]))))
    check("a GRN whose lines agree is assigned that tenant",
          decided.get(("GoodsReceiptNote", ids["grn_unanimous"])) == "ALPHA_MFG",
          str(decided.get(("GoodsReceiptNote", ids["grn_unanimous"]))))

    # THE DISCLOSURE CASE. A split GRN must not be handed to either customer on
    # the strength of its line items. It falls back to the receiving USER, which
    # is a fact about who keyed it rather than an inference from mixed data.
    split = decided.get(("GoodsReceiptNote", ids["grn_split"]))
    check("a GRN spanning TWO customers is not assigned from its lines",
          split == "BETA_WORKS", f"assigned {split!r} — expected the receiver's tenant")
    check("...and it is certainly not given to the other customer",
          split != "ALPHA_MFG", "one customer's receipt handed to another")

    check("a GRN with no lines and an unknown receiver is left NULL",
          decided.get(("GoodsReceiptNote", ids["grn_unknowable"])) is None,
          str(decided.get(("GoodsReceiptNote", ids["grn_unknowable"]))))
    check("an audit row is assigned its actor's tenant",
          decided.get(("AuditLog", ids["audit_user"])) == "BETA_WORKS",
          str(decided.get(("AuditLog", ids["audit_user"]))))
    check("a 'system' audit row is left NULL",
          decided.get(("AuditLog", ids["audit_system"])) is None,
          str(decided.get(("AuditLog", ids["audit_system"]))))

    # NEVER DEFAULT. The tempting fallback is the one that would quietly hand
    # every unattributable row to the founder workspace.
    guessed_default = [k for k, v in decided.items() if v == "DEFAULT"]
    check("nothing anywhere is defaulted to DEFAULT", not guessed_default,
          str(guessed_default))

    # A row that already has an owner must not even be considered.
    check("a row that already has a tenant is not in the plan at all",
          ("Remnant", ids["remnant_owned"]) not in decided,
          "an owned row was scheduled for reassignment")

    # ----------------------------------------------------- apply, and again --
    print()
    print("=" * 74)
    print("3. APPLYING, AND APPLYING A SECOND TIME")
    print("=" * 74)
    bf.main(apply=True)
    db.expire_all()

    got = {r.id: r.tenant_code for r in db.query(models.Remnant).all()}
    check("the assignable row is now owned", got[ids["remnant_ok"]] == "ALPHA_MFG",
          str(got[ids["remnant_ok"]]))
    check("the orphan is STILL hidden", got[ids["remnant_orphan"]] is None,
          str(got[ids["remnant_orphan"]]))
    check("the already-owned row was not moved",
          got[ids["remnant_owned"]] == "BETA_WORKS", str(got[ids["remnant_owned"]]))

    snapshot = {m.__name__: {r.id: r.tenant_code for r in db.query(m).all()}
                for m in (models.Remnant, models.GoodsReceiptNote, models.GRNItem,
                          models.AuditLog, models.CycleCount, models.CycleCountItem,
                          models.MaterialIssueSlip)}
    second, _ = bf.plan(db)
    check("a second run finds nothing left to assign",
          all(t is None for _, t in second),
          f"{sum(1 for _, t in second if t)} rows would be written again")
    bf.main(apply=True)
    db.expire_all()
    again = {m.__name__: {r.id: r.tenant_code for r in db.query(m).all()}
             for m in (models.Remnant, models.GoodsReceiptNote, models.GRNItem,
                       models.AuditLog, models.CycleCount, models.CycleCountItem,
                       models.MaterialIssueSlip)}
    check("IDEMPOTENT: running it twice leaves the database identical",
          snapshot == again, "the second run changed rows")

    # ------------------------------------------------------------ rollback --
    print()
    print("=" * 74)
    print("4. CAN IT BE UNDONE?")
    print("=" * 74)
    # An assigned row is indistinguishable from one the application wrote
    # normally, so "set them back to NULL" is only actionable with a record of
    # WHICH rows. Prove the manifest exists, is complete, and reverts.
    manifest = (db.query(models.EventLog)
                  .filter(models.EventLog.event_type == bf.MANIFEST_EVENT)
                  .order_by(models.EventLog.id.desc()).first())
    check("an applied run records a manifest of what it touched",
          manifest is not None, "no manifest in event_log")

    import json as _json
    recorded = _json.loads(manifest.payload)["rows"] if manifest else []
    check("...naming every row it assigned, and no others",
          len(recorded) == 7 and all(t for _, _, t in recorded),
          f"{len(recorded)} rows recorded, expected 7")

    # SOMEBODY ELSE'S DECISION WINS. Move one row deliberately; the rollback
    # must leave it alone rather than reverting a change it did not make.
    db.query(models.Remnant).filter(models.Remnant.id == ids["remnant_ok"]).update(
        {"tenant_code": "BETA_WORKS"}, synchronize_session=False)
    db.commit()

    rc = bf.main(apply=False, undo=True)
    db.expire_all()
    check("the rollback runs cleanly", rc == 0, f"exit {rc}")

    after_undo = {r.id: r.tenant_code for r in db.query(models.Remnant).all()}
    check("a row deliberately moved since is NOT reverted",
          after_undo[ids["remnant_ok"]] == "BETA_WORKS",
          str(after_undo[ids["remnant_ok"]]))
    audits = {a.id: a.tenant_code for a in db.query(models.AuditLog).all()}
    check("every untouched assignment is hidden again",
          audits[ids["audit_user"]] is None, str(audits[ids["audit_user"]]))
    check("...and the row that was always ambiguous is still NULL",
          after_undo[ids["remnant_orphan"]] is None,
          str(after_undo[ids["remnant_orphan"]]))
    # CONTROL: the already-owned row was never in the manifest, so a rollback
    # must not touch it either.
    check("CONTROL: a row this script never wrote is untouched by the undo",
          after_undo[ids["remnant_owned"]] == "BETA_WORKS",
          str(after_undo[ids["remnant_owned"]]))

    # And a manifest naming a table this script never writes is REFUSED rather
    # than trusted to name a model.
    db.add(models.EventLog(tenant_code="DEFAULT", event_type=bf.MANIFEST_EVENT,
                           event_version=1,
                           payload=_json.dumps({"applied_at": "forged",
                                                "rows": [["Machine", 1, "ALPHA_MFG"]]})))
    db.commit()
    rc = bf.main(apply=False, undo=True)
    check("a manifest naming an unexpected table is REFUSED", rc == 2, f"exit {rc}")

    tenancy.reset_current_tenant(tok)
    db.close()
    engine.dispose()

    print()
    print("=" * 74)
    print(f"{CHECKS[0]} preflight checks")
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}):")
        for f in FAILURES:
            print("   *", f)
        return 1
    print("THE BACKFILL ASSIGNS ONLY WHAT IT CAN PROVE, AND REFUSES THE REST")
    return 0


if __name__ == "__main__":
    sys.exit(main())
