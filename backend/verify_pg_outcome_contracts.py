"""Migration 0009_outcome_contracts on real PostgreSQL, from a populated database.

WHY POSTGRESQL AND NOT ONLY SQLITE
----------------------------------
SQLite ignores VARCHAR lengths, does not enforce foreign keys by default, and
stores NUMERIC as REAL. The agreed-downtime-attribution tables (ADR-0020) lean on
exactly those: a 64-character hash column, foreign keys that decide offboarding
order, and a NOT NULL tenant with no default. test_migration_0009_outcome_contracts
pins the shape on SQLite; this proves it on the engine production runs.

WHAT IT PROVES, IN PRODUCTION'S OWN ORDER
-----------------------------------------
  1. build from the FROZEN baseline snapshot, stamp 0001, migrate to the revision
     before 0009 — the database a live deployment has the moment before it
     deploys this change — and fill it with factory and OEM rows;
  2. `alembic upgrade head` keeps every one of those rows and creates the eight
     tables;
  3. the migrated schema matches models.py (autogenerate diff empty, with the CI
     gate's rules), and the comparison is shown able to FAIL;
  4. PostgreSQL itself refuses: a second statement for one contract period, a
     second acceptance of one revision by one party, a duplicate term version,
     contract reference, covered installation or record sequence, a hash longer
     than 64 characters, a span with no tenant, and a statement for a contract
     that does not exist — each beside a control insert that succeeds;
  5. attribution seconds hold more than 2**31;
  6. downgrade with contract rows present drops the eight tables and leaves the
     previous schema's rows; re-upgrade restores an undrifted schema;
  7. offboarding a factory on PostgreSQL purges its spans (FK onto machines)
     without being blocked, and does not touch the contract tables.

Run: python backend/verify_pg_outcome_contracts.py [port]     (PostgreSQL only)
"""
import io
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pg_scratch  # noqa: E402

DB = "amp_scratch_outcome_contracts"
NEW_TABLES = ["service_contracts", "service_contract_term_versions",
              "service_contract_machines", "contract_statements",
              "contract_attribution_records", "contract_statement_acceptances",
              "contract_disputes", "machine_telemetry_spans"]
COUNTED = ("machines", "downtime_logs", "oem_organizations", "machine_models",
           "machine_installations", "machine_claims", "users")

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def previous_revision():
    src = io.open(os.path.join(HERE, "alembic", "versions", "0009_outcome_contracts.py"),
                  encoding="utf-8").read()
    return re.search(r'^down_revision = "([^"]+)"', src, re.M).group(1)


def alembic(env, *args):
    r = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=HERE, env=env,
                       capture_output=True, text=True, errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "5432"
    version = pg_scratch.ensure(port, DB)
    url = pg_scratch.scratch_url(port, DB)
    # database.py binds its engine at import: point it at the scratch database
    # BEFORE anything imports models.
    os.environ["DATABASE_URL"] = url
    env = {**os.environ, "DATABASE_URL": url}
    prev = previous_revision()
    print(version.split(",")[0])
    print(f"scratch database: {DB}; previous revision: {prev}\n")

    from sqlalchemy import create_engine, text
    engine = create_engine(url)

    def tables():
        with engine.connect() as c:
            return {r[0] for r in c.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public'"))}

    def counts():
        with engine.connect() as c:
            return {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in COUNTED}

    # --- 1. the database a deployment has before this change ------------------
    print("1. BASELINE SNAPSHOT -> PREVIOUS REVISION -> FACTORY AND OEM ROWS")
    sql = io.open(os.path.join(HERE, "alembic", "baseline_schema.sql"), encoding="utf-8").read()
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(sql)
        raw.commit()
    finally:
        raw.close()
    rc, out = alembic(env, "stamp", "0001_baseline")
    check("stamped 0001_baseline over the frozen snapshot", rc == 0, out[-300:])
    rc, out = alembic(env, "upgrade", prev)
    check(f"migrated to {prev}", rc == 0, out[-400:])
    check("none of the eight tables exist yet", not (set(NEW_TABLES) & tables()),
          str(sorted(set(NEW_TABLES) & tables())))

    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO machines (id, tenant_code, site, name, status, utilization) VALUES "
            "(1,'FACTORY_A','P1','COMP-001','Running',70),"
            "(2,'FACTORY_B','P1','COMP-001','Breakdown',0)"))
        c.execute(text(
            "INSERT INTO downtime_logs (tenant_code, machine_id, reason, duration, created_at) "
            "VALUES ('FACTORY_A',1,'No material','15 min', now())"))
        c.execute(text(
            "INSERT INTO users (username, password, role, tenant_code, is_active) VALUES "
            "('a_admin','x','Admin','FACTORY_A',true)"))
        c.execute(text(
            "INSERT INTO oem_organizations (id, oem_code, name, is_active) "
            "VALUES (1,'OEM_A','Alpha',true)"))
        c.execute(text(
            "INSERT INTO machine_models (id, oem_code, family, model_code, name, status) "
            "VALUES (1,'OEM_A','Compressor','X','X','Active')"))
        c.execute(text(
            "INSERT INTO machine_installations (id, oem_code, serial_number, model_id, "
            "factory_tenant_code, site, machine_id, status) VALUES "
            "(1,'OEM_A','SN-1',1,'FACTORY_A','P1',1,'Active'),"
            "(2,'OEM_A','SN-2',1,'FACTORY_B','P1',2,'Active')"))
        c.execute(text(
            "INSERT INTO machine_claims (oem_code, installation_id, token_hash, code_hint, "
            "status, expires_at) VALUES ('OEM_A',1,'h1','ABCD','Claimed', now())"))
        c.execute(text("SELECT setval('machines_id_seq', 10)"))
        c.execute(text("SELECT setval('machine_installations_id_seq', 10)"))
    before = counts()
    check("rows exist in every counted table before the upgrade",
          all(v >= 1 for v in before.values()), str(before))

    # --- 2. the upgrade -------------------------------------------------------
    print("\n2. UPGRADE TO HEAD")
    rc, out = alembic(env, "upgrade", "head")
    check("alembic upgrade head", rc == 0, out[-400:])
    check("every pre-existing row survived", counts() == before, f"{before} -> {counts()}")
    check("all eight tables exist", set(NEW_TABLES) <= tables(),
          str(sorted(set(NEW_TABLES) - tables())))
    rc, out = alembic(env, "current")
    check("the database reports the head revision", "0009_outcome_contracts" in out
          or "(head)" in out, out[-200:])

    # --- 3. models and migration agree ---------------------------------------
    print("\n3. THE MIGRATED SCHEMA MATCHES models.py")
    import models  # noqa: F401  (registers every table)
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from database import Base

    def include_object(obj, name, type_, reflected, compare_to):
        if type_ == "table" and name == "alembic_version":
            return False
        if type_ == "index" and reflected and compare_to is None:
            return False
        return True

    def drift():
        with engine.connect() as conn:
            ctx = MigrationContext.configure(
                conn, opts={"include_object": include_object, "compare_type": True})
            return compare_metadata(ctx, Base.metadata)

    d = drift()
    check("autogenerate diff is empty", not d, "; ".join(str(x) for x in d)[:600])
    # Declared lengths read straight from the catalogue. Mutation-tested: a
    # migration declaring terms_hash as bare VARCHAR left the autogenerate diff
    # EMPTY, so the diff alone does not guard lengths.
    want = {("service_contract_term_versions", "terms_hash"): 64,
            ("service_contract_term_versions", "oem_accepted_hash"): 64,
            ("service_contract_term_versions", "factory_accepted_hash"): 64,
            ("contract_statements", "content_hash"): 64,
            ("contract_statement_acceptances", "content_hash"): 64,
            ("machine_telemetry_spans", "status"): 32,
            ("contract_disputes", "reason"): 1000,
            ("contract_disputes", "resolution_note"): 1000}
    with engine.connect() as c:
        got = {(t, col): c.execute(text(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_name=:t AND column_name=:c"), {"t": t, "c": col}).scalar()
            for (t, col) in want}
    check("every hash, status and reason column has its declared length",
          got == want, str({k: v for k, v in got.items() if want[k] != v}))
    with engine.begin() as c:
        c.execute(text("DROP INDEX ix_machine_telemetry_spans_lookup"))
    check("CONTROL: the comparison notices a missing index",
          any("ix_machine_telemetry_spans_lookup" in str(x) for x in drift()))
    with engine.begin() as c:
        c.execute(text("CREATE INDEX ix_machine_telemetry_spans_lookup ON "
                       "machine_telemetry_spans (tenant_code, machine_id, source, span_start)"))
    check("...and is empty again once it is restored", not drift())

    # --- 4. PostgreSQL enforces the keys --------------------------------------
    print("\n4. POSTGRESQL ENFORCES THE KEYS")

    def attempt(sql_text, params=None):
        try:
            with engine.begin() as c:
                c.execute(text(sql_text), params or {})
            return None
        except Exception as e:          # noqa: BLE001 - reported, not swallowed
            return str(e).splitlines()[0][:160]

    def refused(label, control_sql, dup_sql, needle="unique"):
        err = attempt(control_sql)
        check(f"CONTROL: {label} — the first row inserts", err is None, str(err))
        err = attempt(dup_sql)
        check(f"{label} — PostgreSQL refuses", err is not None and needle in err.lower(),
              str(err) if err else "accepted")

    contract = ("INSERT INTO service_contracts (id, oem_code, factory_tenant_code, contract_ref, "
                "title, contract_type, status, starts_at, ends_at, created_by, created_at) VALUES "
                "({id},'{oem}','FACTORY_A','AMC-1','AMC','AMC','accepted','2026-08-31 18:30:00',"
                "'2027-08-31 18:30:00','oem:OEM_A:admin','2026-08-01 00:00:00')")
    refused("one OEM reuses a contract reference",
            contract.format(id=1, oem="OEM_A"), contract.format(id=2, oem="OEM_A"))
    check("CONTROL: another OEM may use the same reference",
          attempt(contract.format(id=3, oem="OEM_B")) is None)

    version = ("INSERT INTO service_contract_term_versions (id, contract_id, version, terms_json, "
               "terms_hash, effective_from, status) VALUES "
               "({id},1,{v},'{{}}','" + "a" * 64 + "','2026-08-31 18:30:00','accepted')")
    refused("two term versions share a number", version.format(id=1, v=1),
            version.format(id=2, v=1))

    covered = ("INSERT INTO service_contract_machines (term_version_id, installation_id, "
               "machine_id_at_acceptance, factory_tenant_at_acceptance, serial_number) VALUES "
               "(1,1,1,'FACTORY_A','SN-1')")
    refused("one version covers an installation twice", covered, covered)

    statement = ("INSERT INTO contract_statements (id, contract_id, term_version_id, period_start, "
                 "period_end, revision, content_hash, canonical_json, computed_at, "
                 "computed_by_party, computed_by) VALUES "
                 "({id},1,1,'2026-08-31 18:30:00','2026-09-30 18:30:00',1,'" + "b" * 64
                 + "','{{}}','2026-10-01 00:00:00','OEM','oem:OEM_A:admin')")
    refused("two statements for one contract period", statement.format(id=1),
            statement.format(id=2))

    acceptance = ("INSERT INTO contract_statement_acceptances (statement_id, party, actor, "
                  "accepted_at, content_hash, revision) VALUES "
                  "(1,'{party}','x','2026-10-01 00:00:00','" + "b" * 64 + "',{rev})")
    refused("one party accepts one revision twice", acceptance.format(party="FACTORY", rev=1),
            acceptance.format(party="FACTORY", rev=1))
    check("CONTROL: the same party may accept a LATER revision",
          attempt(acceptance.format(party="FACTORY", rev=3)) is None)
    check("CONTROL: the other party may accept the same revision",
          attempt(acceptance.format(party="OEM", rev=1)) is None)

    record = ("INSERT INTO contract_attribution_records (statement_id, seq, installation_id, "
              "start_at, end_at, seconds, bucket, cause, evidence_json) VALUES "
              "(1,{seq},1,'2026-08-31 18:30:00','2026-09-30 18:30:00',{sec},'AVAILABLE',"
              "'telemetry','{{}}')")
    refused("two records share a sequence number", record.format(seq=0, sec=2592000),
            record.format(seq=0, sec=1))
    err = attempt(record.format(seq=1, sec=2 ** 40))
    check("seconds above 2**31 are stored (BIGINT)", err is None, str(err))
    with engine.connect() as c:
        big = c.execute(text("SELECT seconds FROM contract_attribution_records "
                             "WHERE seq=1")).scalar()
    check("...and read back exactly", big == 2 ** 40, str(big))

    err = attempt(acceptance.format(party="OEM", rev=7).replace("b" * 64, "b" * 65))
    check("a 65-character content hash is refused (VARCHAR(64))",
          err is not None and "too long" in err.lower(), str(err))
    err = attempt("INSERT INTO machine_telemetry_spans (machine_id, source, status, span_start, "
                  "span_end, message_count) VALUES (1,'mqtt','Running','2026-09-01 00:00:00',"
                  "'2026-09-01 00:05:00',1)")
    check("a span without a tenant is refused (NOT NULL, no default)",
          err is not None and "null" in err.lower(), str(err))
    err = attempt(statement.format(id=9).replace("(9,1,1,", "(9,999,1,"))
    check("a statement for a contract that does not exist is refused (FK)",
          err is not None and "foreign key" in err.lower(), str(err))
    err = attempt("INSERT INTO contract_disputes (contract_id, statement_id, installation_id, "
                  "window_start, window_end, raised_by_party, raised_by, raised_at, reason, "
                  "proposed_bucket) VALUES (1,1,1,'2026-09-01 00:00:00','2026-09-01 01:00:00',"
                  "'FACTORY','a_admin','2026-10-02 00:00:00', :r, 'FACTORY')",
                  {"r": "x" * 1000})
    check("CONTROL: a dispute with a 1000-character reason inserts", err is None, str(err))
    with engine.connect() as c:
        status = c.execute(text("SELECT status FROM contract_disputes")).scalar()
    check("...and its status defaults to open in the database", status == "open", str(status))
    err = attempt("INSERT INTO contract_disputes (contract_id, statement_id, installation_id, "
                  "window_start, window_end, raised_by_party, raised_by, raised_at, reason, "
                  "proposed_bucket) VALUES (1,1,1,'2026-09-01 00:00:00','2026-09-01 01:00:00',"
                  "'FACTORY','a_admin','2026-10-02 00:00:00', :r, 'FACTORY')",
                  {"r": "x" * 1001})
    check("a dispute reason over 1000 characters is refused (VARCHAR(1000))",
          err is not None and "too long" in err.lower(), str(err))
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO machine_telemetry_spans (tenant_code, machine_id, source, status, "
            "span_start, span_end, message_count) VALUES "
            "('FACTORY_A',1,'mqtt','Running','2026-09-01 00:00:00','2026-09-01 00:05:00',11),"
            "('FACTORY_A',1,'mqtt','Breakdown','2026-09-01 00:05:10','2026-09-01 00:20:00',30),"
            "('FACTORY_B',2,'mqtt','Running','2026-09-01 00:00:00','2026-09-01 00:05:00',11)"))

    # --- 6. round trip ---------------------------------------------------------
    print("\n6. DOWNGRADE WITH CONTRACT ROWS, THEN UPGRADE AGAIN")
    engine.dispose()
    rc, out = alembic(env, "downgrade", prev)
    check(f"alembic downgrade {prev}", rc == 0, out[-400:])
    check("all eight tables are gone", not (set(NEW_TABLES) & tables()),
          str(sorted(set(NEW_TABLES) & tables())))
    check("every pre-existing row survived the downgrade", counts() == before,
          f"{before} -> {counts()}")
    rc, out = alembic(env, "upgrade", "head")
    check("alembic upgrade head (re-applied)", rc == 0, out[-400:])
    check("every pre-existing row survived the round trip", counts() == before,
          f"{before} -> {counts()}")
    d = drift()
    check("the re-applied schema still matches models.py", not d,
          "; ".join(str(x) for x in d)[:600])

    # --- 7. offboarding --------------------------------------------------------
    print("\n7. OFFBOARDING A FACTORY ON POSTGRESQL")
    with engine.begin() as c:
        c.execute(text(contract.format(id=1, oem="OEM_A")))
        c.execute(text(version.format(id=1, v=1)))
        c.execute(text(covered))
        c.execute(text(statement.format(id=1)))
        c.execute(text(acceptance.format(party="OEM", rev=1)))
        c.execute(text(
            "INSERT INTO machine_telemetry_spans (tenant_code, machine_id, source, status, "
            "span_start, span_end, message_count) VALUES "
            "('FACTORY_A',1,'mqtt','Running','2026-09-01 00:00:00','2026-09-01 00:05:00',11),"
            "('FACTORY_A',1,'iot','Running','2026-09-01 00:00:00','2026-09-01 00:05:00',2),"
            "('FACTORY_B',2,'mqtt','Running','2026-09-01 00:00:00','2026-09-01 00:05:00',11)"))

    import tenancy
    from database import SessionLocal
    from offboard_tenant import purge_tenant_data
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    try:
        purged = purge_tenant_data(db, "FACTORY_A")
        err = None
    except Exception as e:              # noqa: BLE001 - reported
        purged, err = {}, str(e)[:300]
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()
    check("purge_tenant_data completes (not blocked by the span FK onto machines)",
          err is None, str(err))
    check("it deleted FACTORY_A's 2 spans", purged.get("machine_telemetry_spans") == 2,
          str(purged))
    with engine.connect() as c:
        left = [r[0] for r in c.execute(text(
            "SELECT tenant_code FROM machine_telemetry_spans"))]
        kept = {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                for t in ("service_contracts", "service_contract_term_versions",
                          "service_contract_machines", "contract_statements",
                          "contract_statement_acceptances")}
    check("FACTORY_B's span is untouched", left == ["FACTORY_B"], str(left))
    check("the generic sweep did not touch any contract table",
          all(v == 1 for v in kept.values())
          and not any(t in purged for t in NEW_TABLES[:-1]), f"{kept} {purged}")

    engine.dispose()
    print()
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        return 1
    print("MIGRATION 0009 HOLDS ON POSTGRESQL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
