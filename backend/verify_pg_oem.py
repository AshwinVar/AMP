"""Verify the OEM foundation on real PostgreSQL, not on SQLite.

SQLite has hidden three defect classes in this repository already: Decimal vs
float from SUM, NULL != NULL voiding a UNIQUE over a nullable column, and
VARCHAR lengths being ignored. The second one matters here directly —
`machine_installations.factory_tenant_code` IS nullable, and the OEM design
depends on `uq_installation_serial` actually holding.

What this proves, on PostgreSQL 18.3:

  1. migration 0006 applies to a database that ALREADY HAS FACTORY DATA — the
     case that matters, because migrating an empty database proves nothing;
  2. existing factory rows are untouched (the OEM layer is additive);
  3. per-OEM serial uniqueness is enforced BY THE DATABASE, and two OEMs may
     still share a serial;
  4. the sharing-policy uniqueness is enforced by the database;
  5. the whole foundation suite is green on this engine;
  6. downgrade() removes the OEM layer and leaves the factory schema intact.

Run: python backend/verify_pg_oem.py [port]
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pg_scratch  # noqa: E402

DB = "amp_scratch_oem"
HERE = os.path.dirname(os.path.abspath(__file__))

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "5432"
    version = pg_scratch.ensure(port, DB)
    url = pg_scratch.scratch_url(port, DB)
    env = {**os.environ, "DATABASE_URL": url}
    print(version.split(",")[0])
    print(f"scratch database: {DB}\n")

    from sqlalchemy import create_engine, text
    engine = create_engine(url)

    # --- 1. a populated factory database, at the PREVIOUS revision ----------
    print("1. MIGRATING A DATABASE THAT ALREADY HAS FACTORY DATA")
    r = subprocess.run(
        [sys.executable, "-c",
         "import models; from database import Base, engine; "
         "Base.metadata.create_all(bind=engine)"],
        cwd=HERE, env=env, capture_output=True, text=True, errors="replace")
    check("the full schema builds", r.returncode == 0, r.stderr[-400:])

    with engine.begin() as c:
        # Drop the OEM tables so 0006 has to create them, and stamp back to 0005
        # — the exact state a live deployment is in the moment before it runs.
        for t in ("oem_data_sharing_policies", "machine_installations",
                  "machine_models", "oem_users", "oem_organizations"):
            c.execute(text(f"DROP TABLE IF EXISTS {t} CASCADE"))
        c.execute(text(
            "INSERT INTO machines (tenant_code, site, name, status, utilization) "
            "VALUES ('FACTORY_A', 'Plant 1', 'COMP-001', 'Running', 70), "
            "       ('FACTORY_B', 'Plant 1', 'COMP-001', 'Running', 60)"))
        before = c.execute(text("SELECT count(*) FROM machines")).scalar()
    check("two factories' machines exist BEFORE the migration", before == 2, str(before))

    r = subprocess.run([sys.executable, "-m", "alembic", "stamp", "0005_approval_gate"],
                       cwd=HERE, env=env, capture_output=True, text=True, errors="replace")
    check("stamped at 0005 (the live revision)", r.returncode == 0, r.stderr[-300:])
    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                       cwd=HERE, env=env, capture_output=True, text=True, errors="replace")
    check("alembic upgrade head", r.returncode == 0, r.stderr[-400:])

    # --- 2. additive: nothing about the factory changed ---------------------
    print("\n2. THE OEM LAYER IS ADDITIVE")
    with engine.begin() as c:
        after = c.execute(text("SELECT count(*) FROM machines")).scalar()
        check("every factory machine survived", after == before, f"{before} -> {after}")
        cols = {r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='machines'"))}
        check("the machines table gained no OEM column",
              "oem_code" not in cols and "serial_number" not in cols, str(sorted(cols)))
        made = {r[0] for r in c.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name LIKE 'oem%' OR table_name LIKE 'machine_%'"))}
        check("all five OEM tables exist",
              {"oem_organizations", "oem_users", "machine_models",
               "machine_installations", "oem_data_sharing_policies"} <= made, str(made))
        inst_cols = {r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='machine_installations'"))}
        check("the installation carries factory_tenant_code, NOT tenant_code",
              "factory_tenant_code" in inst_cols and "tenant_code" not in inst_cols,
              str(sorted(inst_cols)))

    # --- 3. the constraints are enforced by the DATABASE --------------------
    print("\n3. THE DATABASE ENFORCES THE IDENTITY RULES")
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO oem_organizations (oem_code, name, is_active) "
            "VALUES ('OEM_ALPHA','Alpha',true), ('OEM_BETA','Beta',true)"))
        c.execute(text(
            "INSERT INTO machine_models (oem_code, family, model_code, name, status) "
            "VALUES ('OEM_ALPHA','Compressor','X200','Alpha X200','Active'), "
            "       ('OEM_BETA','Compressor','X200','Beta X200','Active')"))
        a_model = c.execute(text(
            "SELECT id FROM machine_models WHERE oem_code='OEM_ALPHA'")).scalar()
        b_model = c.execute(text(
            "SELECT id FROM machine_models WHERE oem_code='OEM_BETA'")).scalar()
        c.execute(text(
            "INSERT INTO machine_installations "
            "(oem_code, serial_number, model_id, site, status) "
            f"VALUES ('OEM_ALPHA','SN-0001',{a_model},'','Active')"))

    # Two OEMs may share a serial.
    try:
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO machine_installations "
                "(oem_code, serial_number, model_id, site, status) "
                f"VALUES ('OEM_BETA','SN-0001',{b_model},'','Active')"))
        check("two OEMs may both use SN-0001", True)
    except Exception as e:
        check("two OEMs may both use SN-0001", False, str(e)[:140])

    # One OEM may not.
    try:
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO machine_installations "
                "(oem_code, serial_number, model_id, site, status) "
                f"VALUES ('OEM_ALPHA','SN-0001',{a_model},'','Active')"))
        check("one OEM cannot reuse its own serial", False, "the duplicate was accepted")
    except Exception as e:
        check("one OEM cannot reuse its own serial",
              "uq_installation_serial" in str(e) or "unique" in str(e).lower(),
              str(e)[:140])

    # The model catalogue is unique per OEM too.
    try:
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO machine_models (oem_code, family, model_code, name, status) "
                "VALUES ('OEM_ALPHA','Compressor','X200','Duplicate','Active')"))
        check("one OEM cannot reuse a model code", False, "accepted")
    except Exception as e:
        check("one OEM cannot reuse a model code", "unique" in str(e).lower(), str(e)[:140])

    # One sharing policy per (oem, factory).
    try:
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO oem_data_sharing_policies (oem_code, tenant_code, grants) "
                "VALUES ('OEM_ALPHA','FACTORY_A',''), ('OEM_ALPHA','FACTORY_A','SHARE_ALARMS')"))
        check("a duplicate (oem, factory) policy is refused", False, "accepted")
    except Exception as e:
        check("a duplicate (oem, factory) policy is refused",
              "unique" in str(e).lower(), str(e)[:140])

    # --- 4. the suite, on this engine ---------------------------------------
    print("\n4. THE FOUNDATION SUITE, ON POSTGRESQL")
    gate_url = pg_scratch.scratch_url(port, DB + "_suite")
    pg_scratch.ensure(port, DB + "_suite")
    r = subprocess.run([sys.executable, "test_oem_foundation.py"], cwd=HERE,
                       env={**os.environ, "DATABASE_URL": gate_url},
                       capture_output=True, text=True, errors="replace")
    passed = r.stdout.count("PASS  ")
    check(f"test_oem_foundation.py green on PostgreSQL ({passed} assertions)",
          r.returncode == 0, r.stdout[-900:])

    # --- 5. it reverses ------------------------------------------------------
    print("\n5. THE MIGRATION REVERSES")
    r = subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0005_approval_gate"],
                       cwd=HERE, env=env, capture_output=True, text=True, errors="replace")
    check("alembic downgrade 0005", r.returncode == 0, r.stderr[-400:])
    with engine.begin() as c:
        left = c.execute(text(
            "SELECT count(*) FROM information_schema.tables WHERE table_name IN "
            "('oem_organizations','oem_users','machine_models',"
            "'machine_installations','oem_data_sharing_policies')")).scalar()
        check("every OEM table is gone", left == 0, str(left))
        still = c.execute(text("SELECT count(*) FROM machines")).scalar()
        check("...and the factory's machines are still there", still == before, str(still))

    engine.dispose()
    print()
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        return 1
    print("THE OEM FOUNDATION HOLDS ON POSTGRESQL 18.3")
    return 0


if __name__ == "__main__":
    sys.exit(main())
