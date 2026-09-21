"""Verify the AMP-native AI consent table (migration 0009) on real PostgreSQL, not on SQLite.

SQLite has hidden defect classes in this repository before (see verify_pg_oem.py).
Two of them matter for consent directly: a UNIQUE constraint that SQLite and
PostgreSQL enforce differently in edge cases, and transactional behaviour - the
consent row and its audit row must commit or roll back TOGETHER, and "together"
is the database's promise, not Python's.

What this proves, on the PostgreSQL the DATABASE_URL points at:

  1. migration 0009 applies to a database that ALREADY HAS FACTORY DATA, stamped
     at 0008 - the state a live deployment is in the moment before it runs;
  2. existing rows (machines, the audit trail) are untouched;
  3. the table matches models.py (an autogenerate diff limited to it is empty),
     `granted` is NOT NULL and defaults to false on the server, and
     UNIQUE (tenant_code, capability) is enforced BY THE DATABASE - the first
     insert is asserted to succeed, so a refusal for another reason cannot pass;
  4. amp_ai.consent.set_consent writes the consent and its audit row in one
     commit, and when the audit row cannot be written NEITHER survives;
  5. downgrade() drops only the consent table (the audit trail stays), and
     upgrading again restores it.

Not run by CI (like verify_pg_oem.py); CI's migration gate runs the drift check
for every model, including this one, on PostgreSQL.

Run: python backend/verify_pg_native_ai.py [port]
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pg_scratch  # noqa: E402

DB = "amp_scratch_native_ai"
HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = "ai_learning_consents"

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def alembic(env, *args):
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=HERE, env=env,
                          capture_output=True, text=True, errors="replace")


# Run in a subprocess: database.py binds its engine at import, from DATABASE_URL.
DRIFT = r'''
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import models  # noqa: F401
from database import Base, engine

def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and name == "alembic_version":
        return False
    return True

with engine.connect() as conn:
    ctx = MigrationContext.configure(conn, opts={"include_object": include_object, "compare_type": True})
    diff = compare_metadata(ctx, Base.metadata)
mine = [d for d in diff if "ai_learning_consents" in repr(d)]
print("DRIFT", len(mine), repr(mine)[:400])
'''

ATOMIC = r'''
import json
import models
import platform_routes
from amp_ai import consent
from database import SessionLocal

db = SessionLocal()
row = consent.set_consent(db, "PG_A", "telemetry_baseline", True, "pg-admin")
print("GRANTED", row.granted is True)
audits = db.query(models.AuditLog).filter(models.AuditLog.tenant_code == "PG_A",
                                          models.AuditLog.entity_type == "ai_learning_consent").all()
print("AUDITS", len(audits), audits[0].action if audits else None)
db.close()

original = platform_routes.build_audit_row
def broken(*a, **k):
    r = original(*a, **k)
    r.action = None          # NOT NULL: the one commit must fail
    return r
platform_routes.build_audit_row = broken
db = SessionLocal()
raised = False
try:
    consent.set_consent(db, "PG_B", "telemetry_baseline", True, "pg-admin")
except Exception:
    raised = True
db.close()
platform_routes.build_audit_row = original
db = SessionLocal()
print("RAISED", raised)
print("ROWS_B", db.query(models.AiLearningConsent).filter(models.AiLearningConsent.tenant_code == "PG_B").count())
print("AUDITS_B", db.query(models.AuditLog).filter(models.AuditLog.tenant_code == "PG_B").count())
print("GATE_B", consent.DbConsentGate().check(db, "PG_B", "telemetry_baseline").granted)
db.close()
'''


def _line(out, key):
    for line in out.splitlines():
        if line.startswith(key + " "):
            return line[len(key) + 1:].strip()
    return None


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "5432"
    version = pg_scratch.ensure(port, DB)
    url = pg_scratch.scratch_url(port, DB)
    env = {**os.environ, "DATABASE_URL": url, "PYTHONIOENCODING": "utf-8"}
    print(version.split(",")[0])
    print(f"scratch database: {DB}\n")

    from sqlalchemy import create_engine, text
    engine = create_engine(url)

    # --- 1. a populated database at the PREVIOUS revision -------------------------
    print("1. MIGRATING A DATABASE THAT ALREADY HAS FACTORY DATA")
    r = subprocess.run([sys.executable, "-c",
                        "import models; from database import Base, engine; Base.metadata.create_all(bind=engine)"],
                       cwd=HERE, env=env, capture_output=True, text=True, errors="replace")
    check("the full schema builds", r.returncode == 0, r.stderr[-400:])
    with engine.begin() as c:
        c.execute(text(f"DROP TABLE IF EXISTS {TABLE} CASCADE"))
        c.execute(text("INSERT INTO machines (tenant_code, site, name, status, utilization) "
                       "VALUES ('FACTORY_A', 'Plant 1', 'PRESS-01', 'Running', 70), "
                       "       ('FACTORY_B', 'Plant 1', 'PRESS-01', 'Running', 60)"))
        c.execute(text("INSERT INTO audit_logs (tenant_code, actor, action) "
                       "VALUES ('FACTORY_A', 'a-admin', 'login')"))
        machines_before = c.execute(text("SELECT count(*) FROM machines")).scalar()
        audits_before = c.execute(text("SELECT count(*) FROM audit_logs")).scalar()
    check("factory rows exist BEFORE the migration", machines_before == 2 and audits_before == 1,
          f"{machines_before} {audits_before}")
    r = alembic(env, "stamp", "0008_machine_claim")
    check("stamped at 0008 (the previous head)", r.returncode == 0, r.stderr[-300:])
    # The revision under test, by name. This said "head", which was 0009 when it
    # was written. #614's 0010 moved head, and the stamp check below then failed
    # on a migration that was still correct. A verification of one migration
    # upgrades to that migration.
    r = alembic(env, "upgrade", "0009_native_ai_consent")
    check("alembic upgrade to 0009_native_ai_consent", r.returncode == 0, r.stderr[-400:])

    # --- 2. additive -----------------------------------------------------------------
    print("\n2. THE CONSENT TABLE IS ADDITIVE")
    with engine.begin() as c:
        check("every machine survived", c.execute(text("SELECT count(*) FROM machines")).scalar()
              == machines_before)
        check("the audit trail survived", c.execute(text("SELECT count(*) FROM audit_logs")).scalar()
              == audits_before)
        current = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
        check("stamped at 0009_native_ai_consent", current == "0009_native_ai_consent", str(current))

    # 0009's own work is pinned above, at 0009. Everything below compares the
    # table with models.py or drives it through the ORM, and models.py describes
    # the table at HEAD -- 0012 (ADR-0038) adds `scope` -- so the rest runs at head.
    # (The same shape as the 0010 lesson: a verification of ONE migration names
    # its revision for that migration's checks, and nothing else.)
    r = alembic(env, "upgrade", "head")
    check("alembic upgrade to head (later revisions add columns the model declares)", r.returncode == 0,
          r.stderr[-400:])

    # --- 3. shape and constraints, enforced by the database ----------------------------------
    print("\n3. THE DATABASE ENFORCES ONE ANSWER PER TENANT PER CAPABILITY")
    r = subprocess.run([sys.executable, "-c", DRIFT], cwd=HERE, env=env, capture_output=True, text=True,
                       errors="replace")
    check("no model/migration drift for the consent table",
          r.returncode == 0 and (_line(r.stdout, "DRIFT") or "").startswith("0 "),
          (r.stdout + r.stderr)[-400:])
    with engine.begin() as c:
        cols = {row[0]: (row[1], row[2], row[3]) for row in c.execute(text(
            "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
            f"WHERE table_name='{TABLE}'"))}
        check("tenant_code, capability, granted are NOT NULL",
              all(cols.get(n, ("", "YES"))[1] == "NO" for n in ("tenant_code", "capability", "granted")),
              str(cols))
        check("granted is boolean with a server default of false",
              cols.get("granted", ("",))[0] == "boolean" and "false" in str(cols.get("granted", ("", "", ""))[2]),
              str(cols.get("granted")))
        idx = {row[0] for row in c.execute(text(f"SELECT indexname FROM pg_indexes WHERE tablename='{TABLE}'"))}
        check("ix_ai_learning_consents_id and the tenant_code index exist",
              {"ix_ai_learning_consents_id", "ix_ai_learning_consents_tenant_code"} <= idx, str(sorted(idx)))
        uq = {row[0] for row in c.execute(text(
            "SELECT constraint_name FROM information_schema.table_constraints "
            f"WHERE table_name='{TABLE}' AND constraint_type='UNIQUE'"))}
        check("uq_ai_learning_consent is a real UNIQUE constraint", "uq_ai_learning_consent" in uq, str(uq))

    insert = text(f"INSERT INTO {TABLE} (tenant_code, capability) VALUES (:t, 'telemetry_baseline')")
    first_error = None
    try:
        with engine.begin() as c:
            c.execute(insert, {"t": "FACTORY_A"})
    except Exception as e:  # noqa: BLE001 - reported, not swallowed
        first_error = str(e)[:200]
    check("CONTROL: the first consent row inserts cleanly", first_error is None, str(first_error))
    with engine.begin() as c:
        g = c.execute(text(f"SELECT granted FROM {TABLE} WHERE tenant_code='FACTORY_A'")).scalar()
    check("a row inserted without `granted` is a refusal (false), never a grant", g is False, repr(g))
    collided = False
    try:
        with engine.begin() as c:
            c.execute(insert, {"t": "FACTORY_A"})
    except Exception:  # noqa: BLE001 - the expected refusal
        collided = True
    check("PostgreSQL refuses a SECOND row for the same (tenant, capability)", collided)
    other_error = None
    try:
        with engine.begin() as c:
            c.execute(insert, {"t": "FACTORY_B"})
    except Exception as e:  # noqa: BLE001
        other_error = str(e)[:200]
    check("...while another tenant may hold the same capability", other_error is None, str(other_error))

    # --- 4. consent and its audit commit together ------------------------------------------
    print("\n4. CONSENT NEVER CHANGES WITHOUT ITS AUDIT ROW")
    r = subprocess.run([sys.executable, "-c", ATOMIC], cwd=HERE, env=env, capture_output=True, text=True,
                       errors="replace")
    out = r.stdout + r.stderr
    check("the consent script ran", r.returncode == 0, out[-600:])
    check("a grant is stored", _line(r.stdout, "GRANTED") == "True", str(_line(r.stdout, "GRANTED")))
    check("...with exactly one audit row, action ai.learning_consent.granted",
          _line(r.stdout, "AUDITS") == "1 ai.learning_consent.granted", str(_line(r.stdout, "AUDITS")))
    check("a grant whose audit row cannot be written raises", _line(r.stdout, "RAISED") == "True")
    check("...and leaves NO consent row", _line(r.stdout, "ROWS_B") == "0", str(_line(r.stdout, "ROWS_B")))
    check("...and no audit row", _line(r.stdout, "AUDITS_B") == "0", str(_line(r.stdout, "AUDITS_B")))
    check("...so the gate still refuses", _line(r.stdout, "GATE_B") == "False", str(_line(r.stdout, "GATE_B")))

    # --- 5. it reverses ----------------------------------------------------------------------
    print("\n5. THE MIGRATION REVERSES")
    r = alembic(env, "downgrade", "0008_machine_claim")
    check("alembic downgrade 0008", r.returncode == 0, r.stderr[-400:])
    with engine.begin() as c:
        gone = c.execute(text(f"SELECT count(*) FROM information_schema.tables WHERE table_name='{TABLE}'")).scalar()
        check("the consent table is gone", gone == 0, str(gone))
        check("...the machines are still there", c.execute(text("SELECT count(*) FROM machines")).scalar()
              == machines_before)
        kept = c.execute(text("SELECT count(*) FROM audit_logs WHERE entity_type='ai_learning_consent'")).scalar()
        check("...and the record of who granted what is kept", kept == 1, str(kept))
    r = alembic(env, "upgrade", "0009_native_ai_consent")
    check("alembic upgrade to 0009 (re-applied)", r.returncode == 0, r.stderr[-400:])
    with engine.begin() as c:
        back = c.execute(text(f"SELECT count(*) FROM information_schema.tables WHERE table_name='{TABLE}'")).scalar()
        check("the consent table is back", back == 1, str(back))

    engine.dispose()
    print()
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        return 1
    print("THE CONSENT TABLE HOLDS ON POSTGRESQL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
