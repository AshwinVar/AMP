"""Migration 0012_consent_scope on real PostgreSQL, from a populated database (ADR-0038).

WHY POSTGRESQL AND NOT ONLY SQLITE
----------------------------------
The SQLite half (test_migration_0012_consent_scope.py) stands in for the
previous revision by dropping the column from a create_all table. Production is
a PostgreSQL database built from the FROZEN baseline snapshot and every
migration since, with consent rows an Admin wrote before this change. This
proves the upgrade on that database, in production's own order:

  1. build from the frozen snapshot, stamp 0001, migrate to the revision before
     0012 -- the database a live deployment has the moment before it deploys
     this change -- and write consent rows the way ADR-0020 wrote them;
  2. `alembic upgrade head` keeps every one of those rows, with its decision,
     and the new column arrives NULL for all of them: no backfill invents the
     provider an Admin never named;
  3. the migrated schema matches models.py (autogenerate diff empty, with the CI
     gate's rules), and the comparison is shown able to FAIL;
  4. a grant written through amp_ai.consent.set_consent AFTER the upgrade
     records its provider, and the gate honours it for that provider only;
  5. downgrade with scoped rows present drops only the column and keeps every
     decision; re-upgrade restores an undrifted schema.

Run: python backend/verify_pg_consent_scope.py [port]      (PostgreSQL only)
"""
import io
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pg_scratch  # noqa: E402

DB = "amp_consent_scope"
TABLE = "ai_learning_consents"
COLUMN = "scope"
failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def previous_revision():
    src = io.open(os.path.join(HERE, "alembic", "versions", "0012_consent_scope.py"), encoding="utf-8").read()
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

    from sqlalchemy import create_engine, inspect, text
    engine = create_engine(url)

    def columns():
        return {c["name"]: c for c in inspect(engine).get_columns(TABLE)}

    def rows():
        with engine.connect() as c:
            return [tuple(r) for r in c.execute(text(
                "SELECT tenant_code, capability, granted, granted_by, revoked_by FROM ai_learning_consents "
                "ORDER BY id"))]

    # --- 1. the database a deployment has before this change ------------------
    print("1. BASELINE SNAPSHOT -> PREVIOUS REVISION -> CONSENT ROWS")
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
    check("the consent table exists (0009) without the column", TABLE in inspect(engine).get_table_names()
          and COLUMN not in columns())
    with engine.begin() as c:
        c.execute(text("INSERT INTO ai_learning_consents (tenant_code, capability, granted, granted_by, granted_at, "
                       "updated_at) VALUES ('FACTORY_A','telemetry_baseline',true,'a-admin',now(),now())"))
        c.execute(text("INSERT INTO ai_learning_consents (tenant_code, capability, granted, granted_by, granted_at, "
                       "updated_at) VALUES ('FACTORY_B','external_model',true,'b-admin',now(),now())"))
        c.execute(text("INSERT INTO ai_learning_consents (tenant_code, capability, granted, revoked_by, revoked_at, "
                       "updated_at) VALUES ('FACTORY_C','external_model',false,'c-admin',now(),now())"))
    before = rows()
    check("three consent rows exist before the upgrade", len(before) == 3, str(before))

    # --- 2. the upgrade -------------------------------------------------------
    print("\n2. UPGRADE TO HEAD")
    rc, out = alembic(env, "upgrade", "head")
    check("alembic upgrade head", rc == 0, out[-400:])
    check("every consent row survived with its decision", rows() == before, f"{before} -> {rows()}")
    cols = columns()
    check("the column exists, nullable, with no default", COLUMN in cols and cols[COLUMN]["nullable"]
          and cols[COLUMN].get("default") is None, str(cols.get(COLUMN)))
    with engine.connect() as c:
        scopes = [r[0] for r in c.execute(text("SELECT scope FROM ai_learning_consents ORDER BY id"))]
    check("...and is NULL for every pre-existing row (no backfill invents a provider)", scopes == [None] * 3,
          str(scopes))

    # --- 3. the migrated schema matches the models ----------------------------
    print("\n3. THE MIGRATED SCHEMA IS WHAT models.py DECLARES")
    import models  # noqa: F401
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
    check("no drift against models.py", not d, str(d[:3]))
    with engine.begin() as c:
        c.execute(text("ALTER TABLE ai_learning_consents DROP COLUMN scope"))
    check("CONTROL: the drift comparison notices the missing column", any("scope" in str(x) for x in drift()))
    with engine.begin() as c:
        c.execute(text("ALTER TABLE ai_learning_consents ADD COLUMN scope VARCHAR"))
    check("...and the column is restored", not drift())

    # --- 4. a grant after the upgrade names its provider ---------------------
    print("\n4. A GRANT WRITTEN AFTER THE UPGRADE NAMES ITS PROVIDER, AND HOLDS FOR THAT ONE ONLY")
    from sqlalchemy.orm import sessionmaker
    from amp_ai import consent as C
    from amp_ai.core.contracts import CAPABILITY_EXTERNAL_MODEL as EXT
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        C.set_consent(db, "FACTORY_D", EXT, True, "d-admin", scope="anthropic")
        with engine.connect() as c:
            stored = c.execute(text("SELECT scope FROM ai_learning_consents WHERE tenant_code='FACTORY_D'")).scalar()
        check("the row records the provider", stored == "anthropic", str(stored))
        check("the gate honours it for that provider", C.DbConsentGate().check(db, "FACTORY_D", EXT, scope="anthropic").granted)
        d2 = C.DbConsentGate().check(db, "FACTORY_D", EXT, scope="gemini")
        check("...and refuses it for another, saying so", d2.granted is False and "for anthropic" in d2.reason
              and "gemini is configured now" in d2.reason, d2.reason)
        d3 = C.DbConsentGate().check(db, "FACTORY_B", EXT, scope="anthropic")
        check("a pre-existing grant with no provider named is refused until an Admin decides again",
              d3.granted is False and "before AMP recorded which provider" in d3.reason, d3.reason)
        try:
            C.set_consent(db, "FACTORY_E", EXT, True, "e-admin")
            check("a scoped grant with no provider is refused by the writer", False, "no ValueError")
        except ValueError as exc:
            check("a scoped grant with no provider is refused by the writer", "nothing to consent to" in str(exc), str(exc))
        db.rollback()
    finally:
        db.close()

    # --- 5. the downgrade -----------------------------------------------------
    print("\n5. DOWNGRADE WITH SCOPED ROWS, THEN UPGRADE AGAIN")
    before_down = rows()
    rc, out = alembic(env, "downgrade", prev)
    check("alembic downgrade", rc == 0, out[-400:])
    check("the column is gone", COLUMN not in columns())
    check("every decision is untouched", rows() == before_down, f"{before_down} -> {rows()}")
    rc, out = alembic(env, "upgrade", "head")
    check("re-upgrade", rc == 0, out[-400:])
    check("the column is back", COLUMN in columns())
    check("no drift after the round trip", not drift(), str(drift()[:3]))
    with engine.connect() as c:
        d_scope = c.execute(text("SELECT scope FROM ai_learning_consents WHERE tenant_code='FACTORY_D'")).scalar()
    check("...and the provider a grant named is gone with the column: the Admin decides again", d_scope is None,
          str(d_scope))

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL POSTGRESQL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
