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
          "test_unscoped_model_reads.py"]

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
    ("username stops being globally unique", "models.py",
     "    username = Column(String, unique=True, nullable=False)",
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
            originals[path] = io.open(os.path.join(here, path),
                                      encoding="utf-8").read()

    baseline = run_suites()
    if baseline:
        print(f"ABORT: suites already failing before any mutation: {baseline}")
        return 2
    print(f"baseline: all {len(SUITES)} suites green\n")
    print(f"{'mutation':<58} {'verdict':<10} caught by")
    print("-" * 104)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
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
        io.open(os.path.join(here, path), "w", encoding="utf-8",
                newline="\n").write(mutated)
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8",
                    newline="\n").write(source)
        verdict = "caught" if failing else "SURVIVED"
        print(f"{label:<58} {verdict:<10} "
              f"{', '.join(s.replace('test_', '').replace('.py', '')[:22] for s in failing) or '-- nothing --'}")
        if not failing:
            survived.append(label)

    dirty = [p for p, original in originals.items()
             if io.open(os.path.join(here, p), encoding="utf-8").read() != original]
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
