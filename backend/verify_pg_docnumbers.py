"""Verify alembic 0003 against real PostgreSQL 18.

Migration 0003 drops nineteen constraints it did not create and whose names
PostgreSQL chose (``work_orders_work_order_no_key``), then adds composite ones.
SQLite cannot exercise that at all — it has no DROP CONSTRAINT, so the SQLite
path rebuilds the table instead and proves nothing about the path production
takes.

Run:  python backend/verify_pg_docnumbers.py
"""
import os
import subprocess
import sys

import sqlalchemy as sa

import pg_scratch

HERE = os.path.dirname(os.path.abspath(__file__))
DB = "amp_scratch_docnum"
failures = []

# (table, code column) — must match alembic 0003.
TARGETS = [
    ("work_orders", "work_order_no"), ("production_plans", "plan_no"),
    ("inventory_items", "item_code"), ("quality_inspections", "inspection_no"),
    ("customer_orders", "order_no"), ("suppliers", "supplier_code"),
    ("purchase_orders", "po_no"), ("compliance_documents", "document_no"),
    ("maintenance_tasks", "task_no"), ("production_schedules", "schedule_no"),
    ("cost_records", "cost_no"), ("operator_job_executions", "execution_no"),
    ("report_requests", "report_no"), ("remnants", "tag_no"),
    ("material_issue_slips", "slip_no"), ("goods_receipt_notes", "grn_no"),
    ("cycle_counts", "count_no"), ("industrial_devices", "device_code"),
    ("plc_signal_mappings", "mapping_code"),
]


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def migrate(url, label):
    proc = subprocess.run([sys.executable, os.path.join(HERE, "migrate.py")],
                          capture_output=True, text=True, errors="replace",
                          env=dict(os.environ, DATABASE_URL=url), cwd=HERE)
    print(f"    [{label}] exit={proc.returncode}")
    for line in (proc.stdout + proc.stderr).splitlines():
        if any(k in line for k in ("MIGRATE", "Running upgrade", "tenant-scoped",
                                   "Error", "error")):
            print(f"      {line.strip()[:150]}")
    return proc


def constraint_shape(url):
    """{table: sorted list of unique-constraint column tuples}."""
    eng = sa.create_engine(url)
    out = {}
    with eng.connect() as c:
        insp = sa.inspect(c)
        names = set(insp.get_table_names())
        for table, _ in TARGETS:
            if table not in names:
                continue
            out[table] = sorted(tuple(u["column_names"])
                                for u in insp.get_unique_constraints(table))
    eng.dispose()
    return out


def main():
    version = pg_scratch.ensure(5432, DB)
    url = pg_scratch.scratch_url(5432, DB)
    print(version.split(",")[0])
    print()

    # ---- 1. the pre-migration schema ---------------------------------------
    # Built by create_all (so every table exists, as in a real deployment) and
    # then converted BACK to the old shape: drop the composite constraint, add
    # the platform-wide one. A hand-written three-table fixture looked fine and
    # was measuring almost nothing — sixteen of the nineteen tables simply did
    # not exist, so the migration "skipped" them and the check called it a pass.
    print("=" * 74)
    print("1. AN EXISTING DATABASE WITH PLATFORM-WIDE UNIQUE CODES")
    print("=" * 74)
    os.environ["DATABASE_URL"] = url
    subprocess.run(
        [sys.executable, "-c",
         "import models; from database import Base, engine; "
         "Base.metadata.create_all(bind=engine); print('schema built')"],
        env=dict(os.environ, DATABASE_URL=url), cwd=HERE, check=True,
        capture_output=True, text=True)

    eng = sa.create_engine(url)
    with eng.begin() as c:
        for table, col in TARGETS:
            c.execute(sa.text(
                f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS uq_{table}_tenant_{col}'))
            c.execute(sa.text(
                f'ALTER TABLE {table} ADD CONSTRAINT {table}_{col}_key UNIQUE ({col})'))
        c.execute(sa.text(
            "INSERT INTO work_orders (tenant_code, work_order_no, part_number,"
            " batch_number, target_quantity) VALUES ('ACME','WO-001','P','B',1)"))
    with eng.connect() as c:
        before = [u["column_names"] for u in
                  sa.inspect(c).get_unique_constraints("work_orders")]
    eng.dispose()
    check("work_orders starts with a platform-wide UNIQUE on work_order_no",
          ["work_order_no"] in before, str(before))

    eng = sa.create_engine(url)
    try:
        with eng.begin() as c:
            c.execute(sa.text(
                "INSERT INTO work_orders (tenant_code, work_order_no, part_number,"
                " batch_number, target_quantity) VALUES ('OTHERCO','WO-001','P','B',1)"))
        check("BEFORE: a second tenant is blocked from WO-001", False,
              "the insert succeeded, so there was nothing to fix")
    except Exception as exc:
        check("BEFORE: a second tenant is blocked from WO-001",
              "unique" in str(exc).lower(), repr(exc)[:100])
    eng.dispose()

    # ---- 2. migrate ---------------------------------------------------------
    print()
    print("=" * 74)
    print("2. MIGRATE")
    print("=" * 74)
    proc = migrate(url, "upgrade")
    check("migrate.py succeeded", proc.returncode == 0,
          (proc.stdout + proc.stderr)[-400:])

    shape = constraint_shape(url)
    wrong = []
    for table, col in TARGETS:
        got = shape.get(table)
        if got is None:
            wrong.append(f"{table} missing")
        elif ("tenant_code", col) not in got:
            wrong.append(f"{table}: {got}")
        elif (col,) in got:
            wrong.append(f"{table} STILL has a platform-wide unique on {col}")
    check(f"all {len(TARGETS)} tables now carry UNIQUE (tenant_code, code)",
          not wrong, "; ".join(wrong[:5]))

    # ---- 3. the blocker is gone, and the constraint still bites -------------
    print()
    print("=" * 74)
    print("3. AFTER: TWO TENANTS SHARE WO-001; ONE TENANT STILL CANNOT")
    print("=" * 74)
    eng = sa.create_engine(url)
    try:
        with eng.begin() as c:
            c.execute(sa.text(
                "INSERT INTO work_orders (tenant_code, work_order_no, part_number,"
                " batch_number, target_quantity) VALUES ('OTHERCO','WO-001','P','B',1)"))
        check("a second tenant can now own WO-001", True)
    except Exception as exc:
        check("a second tenant can now own WO-001", False, repr(exc)[:120])

    try:
        with eng.begin() as c:
            c.execute(sa.text(
                "INSERT INTO work_orders (tenant_code, work_order_no, part_number,"
                " batch_number, target_quantity) VALUES ('ACME','WO-001','P','B',1)"))
        check("the SAME tenant still cannot duplicate WO-001", False,
              "the duplicate was accepted — the constraint was removed, not scoped")
    except Exception as exc:
        check("the SAME tenant still cannot duplicate WO-001",
              "uq_work_orders_tenant_work_order_no" in str(exc), repr(exc)[:120])
    with eng.connect() as c:
        n = c.execute(sa.text("SELECT count(*) FROM work_orders")).scalar()
    eng.dispose()
    check("no row was lost in the conversion", n == 2, f"n={n}")

    # ---- 4. idempotency -----------------------------------------------------
    print()
    print("=" * 74)
    print("4. RE-RUN MIGRATE (a redeploy must be a no-op)")
    print("=" * 74)
    before_shape = constraint_shape(url)
    proc = migrate(url, "re-run")
    check("second run succeeded", proc.returncode == 0,
          (proc.stdout + proc.stderr)[-300:])
    check("schema unchanged", constraint_shape(url) == before_shape)

    # ---- 5. fresh database --------------------------------------------------
    print()
    print("=" * 74)
    print("5. A FRESH DATABASE REACHES THE SAME SHAPE")
    print("=" * 74)
    pg_scratch.ensure(5432, DB)
    proc = migrate(url, "fresh")
    check("migrate.py succeeded on an empty database", proc.returncode == 0,
          (proc.stdout + proc.stderr)[-400:])
    fresh = constraint_shape(url)
    missing = [t for t, col in TARGETS
               if ("tenant_code", col) not in fresh.get(t, [])]
    check("a fresh database has every per-tenant constraint", not missing,
          str(missing[:5]))
    check("document_sequences exists on a fresh database",
          "document_sequences" in sa.inspect(sa.create_engine(url)).get_table_names())

    print()
    print("=" * 74)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        return 1
    print("DOCUMENT-NUMBER MIGRATION VERIFIED ON POSTGRESQL 18")
    return 0


if __name__ == "__main__":
    sys.exit(main())
