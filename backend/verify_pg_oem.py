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
  6. migration 0007 adds the service clock to a table that ALREADY HAS ROWS,
     leaves those rows NULL rather than inventing a service that never happened,
     and reverses without destroying an installation;
  7. downgrade() removes the OEM layer and leaves the factory schema intact.

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

    # --- 5. migration 0007, against rows that already exist ------------------
    #
    # 0006 creates machine_installations and 0007 adds two columns to it, so a
    # plain `upgrade head` never exercises the case that matters: adding those
    # columns to a table that is ALREADY FULL. Section 3 filled it, so step back
    # one revision and forward again — that is the real deployment order.
    print("\n5. THE SERVICE CLOCK (0007) LANDS ON A POPULATED TABLE")
    with engine.begin() as c:
        rows_before = c.execute(text("SELECT count(*) FROM machine_installations")).scalar()
    check("installations exist before 0007 is re-applied", rows_before >= 2, str(rows_before))

    r = subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0006_oem_foundation"],
                       cwd=HERE, env=env, capture_output=True, text=True, errors="replace")
    check("alembic downgrade 0006", r.returncode == 0, r.stderr[-400:])
    with engine.begin() as c:
        cols = {r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='machine_installations'"))}
        check("downgrade removed BOTH service-clock columns",
              "last_service_hours" not in cols and "last_service_at" not in cols,
              str(sorted(cols)))
        kept = c.execute(text("SELECT count(*) FROM machine_installations")).scalar()
        check("...and dropping them destroyed no installation", kept == rows_before,
              f"{rows_before} -> {kept}")

    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                       cwd=HERE, env=env, capture_output=True, text=True, errors="replace")
    check("alembic upgrade head (0007 re-applied)", r.returncode == 0, r.stderr[-400:])
    with engine.begin() as c:
        meta = {r[0]: r[1] for r in c.execute(text(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name='machine_installations' "
            "AND column_name IN ('last_service_hours','last_service_at')"))}
        check("both service-clock columns are back", len(meta) == 2, str(meta))
        # NULLABLE is the whole design: NOT NULL would need a default, and every
        # candidate default is a lie -- 0 claims the machine was serviced at zero
        # hours, `operating_hours` claims it was serviced just now.
        check("both are NULLABLE",
              set(meta.values()) == {"YES"}, str(meta))
        nulls = c.execute(text(
            "SELECT count(*) FROM machine_installations "
            "WHERE last_service_hours IS NULL AND last_service_at IS NULL")).scalar()
        check("NO BACKFILL: every pre-existing row reads NULL, not an invented service",
              nulls == rows_before, f"{nulls} of {rows_before}")
        # CONTROL: prove the column can actually HOLD a value, so the NULLs above
        # are the migration's choice and not a column nothing can be written to.
        c.execute(text("UPDATE machine_installations SET last_service_hours = 1500 "
                       "WHERE serial_number = 'SN-0001' AND oem_code = 'OEM_ALPHA'"))
        wrote = c.execute(text(
            "SELECT last_service_hours FROM machine_installations "
            "WHERE serial_number='SN-0001' AND oem_code='OEM_ALPHA'")).scalar()
        check("CONTROL: a service reading can be recorded", float(wrote) == 1500.0, str(wrote))

    # --- 5b. migration 0008, against a database that already holds a fleet ----
    #
    # The claim table is NEW, so `upgrade head` on an empty database proves
    # nothing about the case production is actually in: an AMP with live OEM and
    # factory rows. Step back over 0008 and forward again, and check that the
    # fleet is untouched and the table is usable at the end.
    print("\n5b. THE CLAIM TABLE (0008) LANDS ON A LIVE FLEET")
    with engine.begin() as c:
        fleet_before = c.execute(text("SELECT count(*) FROM machine_installations")).scalar()
        machines_before = c.execute(text("SELECT count(*) FROM machines")).scalar()

    r = subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0007_oem_service_clock"],
                       cwd=HERE, env=env, capture_output=True, text=True, errors="replace")
    check("alembic downgrade 0007 (drops machine_claims)", r.returncode == 0,
          r.stderr[-400:])
    with engine.begin() as c:
        gone = c.execute(text("SELECT count(*) FROM information_schema.tables "
                              "WHERE table_name='machine_claims'")).scalar()
        check("the claim table is gone", gone == 0, str(gone))
        # The point of the downgrade note in 0008: the FLEET survives, because
        # factory_tenant_code lives on machine_installations and was set by the
        # acceptance rather than derived from the claim.
        kept = c.execute(text("SELECT count(*) FROM machine_installations")).scalar()
        check("...and every installation survived losing its provenance",
              kept == fleet_before, f"{fleet_before} -> {kept}")

    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                       cwd=HERE, env=env, capture_output=True, text=True, errors="replace")
    check("alembic upgrade head (0008 re-applied)", r.returncode == 0, r.stderr[-400:])
    with engine.begin() as c:
        cols = {r[0]: (r[1], r[2]) for r in c.execute(text(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name='machine_claims'"))}
        check("the claim table is back with its columns", len(cols) >= 14, str(len(cols)))
        # NOT NULL is the design: an invitation that never expires is a permanent
        # bearer credential printed on the side of a box.
        check("expires_at is NOT NULL", cols.get("expires_at", ("", "YES"))[1] == "NO",
              str(cols.get("expires_at")))
        check("the fleet is still whole after the round trip",
              c.execute(text("SELECT count(*) FROM machine_installations")).scalar()
              == fleet_before, str(fleet_before))
        check("...and so are the factory's own machines",
              c.execute(text("SELECT count(*) FROM machines")).scalar() == machines_before,
              str(machines_before))

        idx = {r[0] for r in c.execute(text(
            "SELECT indexname FROM pg_indexes WHERE tablename='machine_claims'"))}
        check("token_hash carries a UNIQUE index",
              "ix_machine_claims_token_hash" in idx, str(sorted(idx)))

        inst = c.execute(text("SELECT id, oem_code FROM machine_installations "
                              "LIMIT 1")).first()

    # THE INVARIANT IS THE DATABASE'S, not Python's. Prove PostgreSQL itself
    # refuses a second claim on one code.
    #
    # Two separate transactions, and the FIRST insert is asserted to succeed. A
    # single loop that treats "an exception happened" as proof would pass just as
    # happily if the first insert failed on the foreign key — a green line
    # reporting a constraint that was never reached.
    INSERT = text("INSERT INTO machine_claims (oem_code, installation_id, "
                  "token_hash, code_hint, status, expires_at) VALUES "
                  "(:o, :i, 'DUPLICATE-HASH', 'XXXX', 'Pending', now())")
    first_error = None
    try:
        with engine.begin() as c:
            c.execute(INSERT, {"o": inst[1], "i": inst[0]})
    except Exception as e:            # noqa: BLE001 - reported, not swallowed
        first_error = str(e)[:200]
    check("CONTROL: one claim row inserts cleanly", first_error is None,
          str(first_error))

    collided = False
    try:
        with engine.begin() as c:
            c.execute(INSERT, {"o": inst[1], "i": inst[0]})
    except Exception:                 # noqa: BLE001 - the expected refusal
        collided = True
    check("PostgreSQL refuses a SECOND claim on the same code hash", collided,
          "two rows share one token_hash")

    # --- 6. it reverses ------------------------------------------------------
    print("\n6. THE MIGRATION REVERSES")
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
