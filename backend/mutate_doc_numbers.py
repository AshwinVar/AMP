"""Mutation harness for the tenant-scoped document-number controls.

Each entry removes exactly one control and asserts a suite goes red. A SURVIVED
line means the assertion protecting it is missing or vacuous.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_doc_numbers.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_tenant_document_numbers.py", "test_migrate.py",
          "test_unscoped_model_reads.py", "test_gmats_document_numbers_never_reused.py",
          "test_sim_numbers_never_reused.py", "test_document_numbers_one_rule.py"]

MUTATIONS = [
    # --- the constraint itself -------------------------------------------
    ("work_order_no goes back to platform-wide unique", "models.py",
     ['        UniqueConstraint("tenant_code", "work_order_no", name="uq_work_orders_tenant_work_order_no"),\n',
      "    work_order_no = Column(String, nullable=False)"],
     ["", "    work_order_no = Column(String, unique=True, nullable=False)"]),
    ("item_code goes back to platform-wide unique", "models.py",
     ['        UniqueConstraint("tenant_code", "item_code", name="uq_inventory_items_tenant_item_code"),\n',
      # GmatsItem also has an item_code column, so the bare line matches twice
      # and the harness reports SKIP. Anchored on the two lines that follow it
      # in InventoryItem, which GmatsItem does not share.
      "    item_code = Column(String, nullable=False)\n"
      "    item_name = Column(String, nullable=False)\n"
      "    category = Column(String, nullable=False)"],
     ["", "    item_code = Column(String, unique=True, nullable=False)\n"
      "    item_name = Column(String, nullable=False)\n"
      "    category = Column(String, nullable=False)"]),
    ("the constraint is dropped rather than scoped (slip_no unconstrained)",
     "models.py",
     '        UniqueConstraint("tenant_code", "slip_no", name="uq_material_issue_slips_tenant_slip_no"),\n',
     ""),
    # OemUser (#509) added a second identical `username` line, so the bare line
    # matched twice and this entry has reported SKIP -- and the harness exited 1 --
    # ever since. Anchored on the User class header, which OemUser does not share.
    ("username stops being globally unique", "models.py",
     'class User(Base):\n    __tablename__ = "users"\n\n    id = Column(Integer, primary_key=True, index=True)\n'
     "    username = Column(String, unique=True, nullable=False)",
     'class User(Base):\n    __tablename__ = "users"\n\n    id = Column(Integer, primary_key=True, index=True)\n'
     "    username = Column(String, nullable=False)"),

    # --- the allocator ----------------------------------------------------
    ("the sequence rewinds to a row count (the original bug)", "doc_numbers.py",
     "    value = row.next_value\n    row.next_value = value + 1",
     "    value = row.next_value\n    row.next_value = value"),
    ("the first allocation ignores numbers already issued", "doc_numbers.py",
     "        seed = max(start, _highest_existing(db, model, column, prefix, tenant))",
     "        seed = start"),
    ("the seed scan is not filtered by tenant", "doc_numbers.py",
     "    rows = db.query(col).filter(\n        model.tenant_code == tenant, col.like(f\"{prefix}-%\")).all()",
     "    rows = db.query(col).filter(col.like(f\"{prefix}-%\")).all()"),
    ("the sequence is shared across tenants", "doc_numbers.py",
     "             .filter(models.DocumentSequence.tenant_code == tenant,\n"
     "                     models.DocumentSequence.doc_type == doc_type)\n"
     "             .with_for_update()\n"
     "             .first())",
     "             .filter(models.DocumentSequence.doc_type == doc_type)\n"
     "             .with_for_update()\n"
     "             .first())"),
    # `... if True else None` was a no-op dressed as a mutation and of course
    # survived. The real one drops the doc_type predicate, so every document
    # type in a tenant would draw from one shared counter.
    ("the sequence key drops the document type", "doc_numbers.py",
     "             .filter(models.DocumentSequence.tenant_code == tenant,\n"
     "                     models.DocumentSequence.doc_type == doc_type)\n"
     "             .with_for_update()\n"
     "             .first())",
     "             .filter(models.DocumentSequence.tenant_code == tenant)\n"
     "             .with_for_update()\n"
     "             .first())"),

    # --- the wiring -------------------------------------------------------
    ("issue slips go back to count()+1", "enterprise_inventory_routes.py",
     '        slip_no=doc_numbers.allocate(\n'
     '            db, tenancy.current_tenant() or "DEFAULT", "MIS", models.MaterialIssueSlip,\n'
     '            "slip_no", "MIS", start=5000),',
     '        slip_no=f"MIS-{5000 + db.query(models.MaterialIssueSlip).count() + 1}",'),
    ("GMATS tax invoices go back to count()+1", "gmats_inventory_routes.py",
     '        invoice_no=doc_numbers.allocate(db, p.tenant_code, "INV", models.GmatsInvoice, "invoice_no", "INV", start=7000),',
     '        invoice_no=f"INV-{7000 + db.query(models.GmatsInvoice).filter(models.GmatsInvoice.tenant_code == p.tenant_code).count() + 1}",'),
    ("GMATS MINs go back to count()+1", "gmats_inventory_routes.py",
     '        min_no=doc_numbers.allocate(db, tenant, "MIN", models.GmatsMIN, "min_no", "MIN", start=4000),',
     '        min_no=f"MIN-{4000 + db.query(models.GmatsMIN).filter(models.GmatsMIN.tenant_code == tenant).count() + 1}",'),
    ("GMATS proformas go back to count()+1", "gmats_inventory_routes.py",
     '        proforma_no=doc_numbers.allocate(db, tenant, "PI", models.GmatsProforma, "proforma_no", "PI", start=1000),',
     '        proforma_no=f"PI-{1000 + db.query(models.GmatsProforma).filter(models.GmatsProforma.tenant_code == tenant).count() + 1}",'),
    ("GMATS invoices draw from the MIN series", "gmats_inventory_routes.py",
     '"INV", models.GmatsInvoice, "invoice_no", "INV", start=7000)',
     '"MIN", models.GmatsInvoice, "invoice_no", "INV", start=7000)'),
    ("GMATS invoice numbering keyed on the caller, not the document", "gmats_inventory_routes.py",
     'doc_numbers.allocate(db, p.tenant_code, "INV",',
     'doc_numbers.allocate(db, current_user.get("tenant"), "INV",'),
    ("GMATS MIN number allocated before the stock check", "gmats_inventory_routes.py",
     ['    for item_id, qty in needed.items():\n'
      '        item = db.query(models.GmatsItem).filter(\n'
      '            models.GmatsItem.id == item_id, models.GmatsItem.tenant_code == tenant).first()\n'
      '        if not item:\n'
      '            raise HTTPException(status_code=404, detail="Item not found")\n'
      '        _heal_stock(item)\n'
      '        if qty > item.physical_stock:',
      '        min_no=doc_numbers.allocate(db, tenant, "MIN", models.GmatsMIN, "min_no", "MIN", start=4000),'],
     ['    early_no = doc_numbers.allocate(db, tenant, "MIN", models.GmatsMIN, "min_no", "MIN", start=4000)\n'
      '    for item_id, qty in needed.items():\n'
      '        item = db.query(models.GmatsItem).filter(\n'
      '            models.GmatsItem.id == item_id, models.GmatsItem.tenant_code == tenant).first()\n'
      '        if not item:\n'
      '            raise HTTPException(status_code=404, detail="Item not found")\n'
      '        _heal_stock(item)\n'
      '        if qty > item.physical_stock:',
      '        min_no=early_no,']),
    ("simulator inspections go back to count()+1", "factory_simulator.py",
     '        inspection_no=_next_number(db, "QI", models.QualityInspection, "inspection_no", 7000),',
     '        inspection_no=f"QI-{7000 + db.query(models.QualityInspection).count() + 1}",'),
    ("simulator operator jobs go back to count()+1", "factory_simulator.py",
     '                execution_no=_next_number(db, "EXE", models.OperatorJobExecution, "execution_no", 9000),',
     '                execution_no=f"EXE-{9000 + db.query(models.OperatorJobExecution).count() + 1}",'),
    ("simulator numbers drawn from DEFAULT, not the ticked tenant", "factory_simulator.py",
     '    return doc_numbers.allocate(db, tenancy.current_tenant() or "DEFAULT", prefix, model, column,',
     '    return doc_numbers.allocate(db, "DEFAULT", prefix, model, column,'),

    # --- the repo-wide guard ------------------------------------------------
    ("the count guard stops looking inside additions", "test_document_numbers_one_rule.py",
     "            if not (isinstance(add, ast.BinOp) and isinstance(add.op, ast.Add)):",
     "            if not isinstance(add, ast.Constant):"),
    ("the count guard ignores `x_no = ...` assignments", "test_document_numbers_one_rule.py",
     "            if any(n.endswith(\"_no\") for n in names) and _adds_to_a_count(node.value):",
     "            if False:"),
    ("the count guard only reads route modules", "test_document_numbers_one_rule.py",
     '    for pattern in ("*.py", "ai/*.py", "amp_ai/*.py", "amp_ai/**/*.py"):',
     '    for pattern in ("*_routes.py",):'),
    ("the count guard flags every f-string, labels included", "test_document_numbers_one_rule.py",
     '        if isinstance(node, ast.keyword) and (node.arg or "").endswith("_no"):',
     '        if isinstance(node, ast.keyword):'),

    # --- the migration guard ---------------------------------------------
    ("a revision id longer than VARCHAR(32) is accepted",
     "alembic/versions/0003_tenant_doc_numbers.py",
     'revision = "0003_tenant_doc_numbers"',
     'revision = "0003_tenant_scoped_document_numbers"'),
]


def run_suites():
    failed = []
    for suite in SUITES:
        proc = subprocess.run([sys.executable, suite], capture_output=True,
                              text=True, errors="replace",
                              cwd=os.path.dirname(os.path.abspath(__file__)))
        if proc.returncode != 0:
            failed.append(suite)
    return failed


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    originals = {}
    for _, path, _, _ in MUTATIONS:
        if path not in originals:
            # Raw bytes: the tree is CRLF, and a text-mode round trip rewrote every
            # mutated file as LF (no content diff under autocrlf, but a dirty tree).
            originals[path] = io.open(os.path.join(here, path), "rb").read()

    baseline = run_suites()
    if baseline:
        print(f"ABORT: suites already failing before any mutation: {baseline}")
        return 2
    print(f"baseline: all {len(SUITES)} suites green\n")
    print(f"{'mutation':<58} {'verdict':<10} caught by")
    print("-" * 104)

    survived = []
    for label, path, old, new in MUTATIONS:
        raw = originals[path]
        crlf = b"\r\n" in raw
        source = raw.decode("utf-8").replace("\r\n", "\n")
        olds = old if isinstance(old, list) else [old]
        news = new if isinstance(new, list) else [new]
        misses = [o for o in olds if source.count(o) != 1]
        if misses:
            print(f"{label:<58} {'SKIP':<10} {len(misses)} pattern(s) did not "
                  f"match exactly once in {path}")
            survived.append(f"{label} (pattern did not apply)")
            continue
        mutated = source
        for o, n in zip(olds, news):
            mutated = mutated.replace(o, n, 1)
        io.open(os.path.join(here, path), "wb").write(
            (mutated.replace("\n", "\r\n") if crlf else mutated).encode("utf-8"))
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "wb").write(raw)
        verdict = "caught" if failing else "SURVIVED"
        print(f"{label:<58} {verdict:<10} "
              f"{', '.join(s.replace('test_', '').replace('.py', '')[:22] for s in failing) or '-- nothing --'}")
        if not failing:
            survived.append(label)

    dirty = [p for p, original in originals.items()
             if io.open(os.path.join(here, p), "rb").read() != original]
    print()
    print(f"source files restored: {'yes' if not dirty else 'NO - DIRTY: ' + str(dirty)}")
    if dirty:
        return 3
    if survived:
        print(f"{len(survived)} MUTATION(S) SURVIVED - investigate each:")
        for s in survived:
            print("   *", s)
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
