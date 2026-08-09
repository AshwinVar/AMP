"""Verify alembic 0004 (per-tenant bills of materials) against PostgreSQL 18.

The part SQLite cannot exercise is the backfill's selectivity: it decides, per
tenant, whether a legacy recipe is demonstrably theirs. Getting that wrong in
either direction is a real failure — seed too widely and every customer inherits
somebody else's rates (the defect ADR-0013 removes), seed too narrowly and the
existing deployment silently stops moving stock.

Run:  python backend/verify_pg_bom.py
"""
import os
import subprocess
import sys

import sqlalchemy as sa

import pg_scratch

HERE = os.path.dirname(os.path.abspath(__file__))
DB = "amp_scratch_bom"
failures = []


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
        if any(k in line for k in ("MIGRATE", "Running upgrade", "legacy recipe",
                                   "Error", "error")):
            print(f"      {line.strip()[:150]}")
    return proc


def recipes(url):
    """{(tenant, part): [(component, qty)]} straight from the database."""
    eng = sa.create_engine(url)
    out = {}
    with eng.connect() as c:
        for bid, tenant, part, fg in c.execute(sa.text(
                "SELECT id, tenant_code, part_number, output_item_code "
                "FROM bills_of_materials ORDER BY tenant_code, part_number")):
            comps = [(r[0], float(r[1])) for r in c.execute(sa.text(
                "SELECT component_code, quantity_per_unit FROM bom_components "
                "WHERE bom_id = :b ORDER BY component_code"), {"b": bid})]
            out[(tenant, part)] = {"output": fg, "components": comps}
    eng.dispose()
    return out


def main():
    version = pg_scratch.ensure(5432, DB)
    url = pg_scratch.scratch_url(5432, DB)
    print(version.split(",")[0])
    print()

    print("=" * 74)
    print("1. AN EXISTING DEPLOYMENT, MID-UPGRADE")
    print("=" * 74)
    # Full schema, then the BOM tables dropped so the migration has to create
    # them — the state a real deployment is in before 0004 runs.
    subprocess.run(
        [sys.executable, "-c",
         "import models; from database import Base, engine; "
         "Base.metadata.create_all(bind=engine)"],
        env=dict(os.environ, DATABASE_URL=url), cwd=HERE, check=True,
        capture_output=True, text=True)
    eng = sa.create_engine(url)
    with eng.begin() as c:
        c.execute(sa.text("DROP TABLE IF EXISTS bom_components"))
        c.execute(sa.text("DROP TABLE IF EXISTS bills_of_materials"))
        # create_all does not make alembic_version; migrate.py will, and will
        # take its existing-schema branch because the tables are already here.
        # GMATS: the incumbent, stocking the items the built-in recipes name.
        for code, unit in (("RM-STEEL-001", "kg"), ("FG-SHAFT-001", "pc"),
                           ("RM-SHEET-002", "kg"), ("FG-PLATE-002", "pc")):
            c.execute(sa.text(
                "INSERT INTO inventory_items (tenant_code, item_code, item_name,"
                " category, unit, current_stock, reorder_level) "
                "VALUES ('GMATS', :c, :c, 'Raw', :u, 100, 5)"),
                {"c": code, "u": unit})
        # NEWCO: a customer whose products we know nothing about.
        for code in ("NC-ALLOY-1", "NC-FG-VALVE"):
            c.execute(sa.text(
                "INSERT INTO inventory_items (tenant_code, item_code, item_name,"
                " category, unit, current_stock, reorder_level) "
                "VALUES ('NEWCO', :c, :c, 'Raw', 'kg', 100, 5)"),
                {"c": code})
    eng.dispose()
    check("the BOM tables do not exist yet",
          "bills_of_materials" not in sa.inspect(sa.create_engine(url)).get_table_names())

    print()
    print("=" * 74)
    print("2. MIGRATE")
    print("=" * 74)
    proc = migrate(url, "upgrade")
    check("migrate.py succeeded", proc.returncode == 0,
          (proc.stdout + proc.stderr)[-400:])

    tables = set(sa.inspect(sa.create_engine(url)).get_table_names())
    check("both tables were created",
          {"bills_of_materials", "bom_components"} <= tables,
          str(sorted(tables & {"bills_of_materials", "bom_components"})))

    got = recipes(url)
    print("    resulting recipes:")
    for (tenant, part), v in sorted(got.items()):
        print(f"      {tenant:<8} {part:<12} -> {v['output']}  {v['components']}")

    print()
    print("=" * 74)
    print("3. THE BACKFILL SEEDED THE INCUMBENT, AND ONLY THE INCUMBENT")
    print("=" * 74)
    check("GMATS kept its SHAFT-001 recipe at the built-in rate",
          got.get(("GMATS", "SHAFT-001"), {}).get("components") == [("RM-STEEL-001", 2.0)],
          str(got.get(("GMATS", "SHAFT-001"))))
    check("GMATS kept PLATE-002 too",
          got.get(("GMATS", "PLATE-002"), {}).get("components") == [("RM-SHEET-002", 1.0)],
          str(got.get(("GMATS", "PLATE-002"))))
    check("GMATS was NOT given recipes for items it does not stock",
          ("GMATS", "GEAR-004") not in got and ("GMATS", "PKG-006") not in got,
          str([k for k in got if k[0] == "GMATS"]))

    newco = [k for k in got if k[0] == "NEWCO"]
    check("NEWCO inherited NOTHING — its recipe book starts empty",
          newco == [], str(newco))

    print()
    print("=" * 74)
    print("4. RE-RUN IS A NO-OP (no duplicates, no overwrite)")
    print("=" * 74)
    # Edit a carried-forward recipe the way a customer would, then re-migrate.
    eng = sa.create_engine(url)
    with eng.begin() as c:
        c.execute(sa.text(
            "UPDATE bom_components SET quantity_per_unit = 7.5 "
            "WHERE component_code = 'RM-STEEL-001'"))
    eng.dispose()
    proc = migrate(url, "re-run")
    check("second migrate.py run succeeded", proc.returncode == 0,
          (proc.stdout + proc.stderr)[-300:])
    after = recipes(url)
    check("the customer's edit was NOT overwritten by the backfill",
          after.get(("GMATS", "SHAFT-001"), {}).get("components") == [("RM-STEEL-001", 7.5)],
          str(after.get(("GMATS", "SHAFT-001"))))
    check("no duplicate recipe rows were created", len(after) == len(got),
          f"{len(got)} -> {len(after)}")

    print()
    print("=" * 74)
    print("5. A FRESH DATABASE")
    print("=" * 74)
    pg_scratch.ensure(5432, DB)
    proc = migrate(url, "fresh")
    check("migrate.py succeeded on an empty database", proc.returncode == 0,
          (proc.stdout + proc.stderr)[-400:])
    fresh_tables = set(sa.inspect(sa.create_engine(url)).get_table_names())
    check("both tables exist on a fresh database",
          {"bills_of_materials", "bom_components"} <= fresh_tables)
    check("a fresh database has no recipes at all (nothing to carry forward)",
          recipes(url) == {}, str(recipes(url)))

    print()
    print("=" * 74)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        return 1
    print("PER-TENANT BOM MIGRATION VERIFIED ON POSTGRESQL 18")
    return 0


if __name__ == "__main__":
    sys.exit(main())
