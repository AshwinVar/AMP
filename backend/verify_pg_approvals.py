"""Verify the approval gate on a real PostgreSQL, not on SQLite.

SQLite has hidden three different defects in this campaign already: it returns
float where PostgreSQL returns Decimal, it ignores VARCHAR lengths, and it
treats NULL != NULL in a way that voids a UNIQUE constraint. A migration that
adds a NOT NULL column to a populated table is exactly the shape that passes on
SQLite and fails in production, so it is run here against the real engine.

What this proves, on PostgreSQL 18.3:

  1. migration 0005 applies to a database that ALREADY HAS USERS — the case
     that matters, because adding a NOT NULL column to an empty table proves
     nothing;
  2. existing users come out active, so nobody is locked out by the deploy;
  3. the NOT NULL constraint is real (the database refuses a NULL is_active);
  4. agent_actions.expires_at is genuinely nullable;
  5. the gate itself refuses every bypass against this engine;
  6. downgrade() reverses both columns.

Run: python backend/verify_pg_approvals.py [port]
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pg_scratch  # noqa: E402

DB = "amp_scratch_approvals"


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else None
    here = os.path.dirname(os.path.abspath(__file__))
    version = pg_scratch.ensure(port, DB)
    url = pg_scratch.scratch_url(port, DB)
    print(version.split(",")[0])
    print(f"scratch database: {DB}\n")

    env = {**os.environ, "DATABASE_URL": url}
    failures = []

    def check(label, ok, detail=""):
        print(f"  {'PASS' if ok else 'FAIL'}  {label}"
              + (f"  [{detail}]" if detail and not ok else ""))
        if not ok:
            failures.append(label)

    from sqlalchemy import create_engine, text
    engine = create_engine(url)

    # --- 1. a database that already has users, at the PREVIOUS revision ------
    # The full schema, then the two new columns dropped and the version stamped
    # back — the exact state a live deployment is in the moment before 0005
    # runs. Adding a NOT NULL column to an EMPTY table would prove nothing; the
    # question is what happens to rows that are already there.
    print("1. UPGRADING A POPULATED DATABASE")
    r = subprocess.run(
        [sys.executable, "-c",
         "import models; from database import Base, engine; "
         "Base.metadata.create_all(bind=engine)"],
        cwd=here, env=env, capture_output=True, text=True, errors="replace")
    check("the pre-0005 schema is in place", r.returncode == 0, r.stderr[-400:])

    with engine.begin() as c:
        c.execute(text("ALTER TABLE users DROP COLUMN IF EXISTS is_active"))
        c.execute(text("ALTER TABLE agent_actions DROP COLUMN IF EXISTS expires_at"))
        c.execute(text(
            "INSERT INTO users (username, password, role, tenant_code) VALUES "
            "('pg-admin', 'x', 'Admin', 'FACTORY_A'), "
            "('pg-op', 'x', 'Operator', 'FACTORY_A')"))
        before = c.execute(text("SELECT count(*) FROM users")).scalar()
    check("two users exist BEFORE the migration", before == 2, str(before))

    r = subprocess.run([sys.executable, "-m", "alembic", "stamp",
                        "0004_per_tenant_bom"], cwd=here, env=env,
                       capture_output=True, text=True, errors="replace")
    check("stamped at 0004 (the revision a live deploy is on)",
          r.returncode == 0, r.stderr[-400:])

    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                       cwd=here, env=env, capture_output=True, text=True,
                       errors="replace")
    check("alembic upgrade head over a populated users table",
          r.returncode == 0, r.stderr[-400:])

    # --- 2/3/4. the shape the migration actually produced --------------------
    print("\n2. WHAT THE MIGRATION LEFT BEHIND")
    with engine.begin() as c:
        rows = c.execute(text(
            "SELECT column_name, is_nullable, column_default FROM "
            "information_schema.columns WHERE table_name='users' "
            "AND column_name='is_active'")).fetchall()
        check("users.is_active exists", len(rows) == 1, str(rows))
        if rows:
            name, nullable, default = rows[0]
            check("...NOT NULL", nullable == "NO", nullable)
            check("...defaulting to true", "true" in (default or "").lower(),
                  str(default))
        active = c.execute(text(
            "SELECT count(*) FROM users WHERE is_active")).scalar()
        check("both pre-existing users are ACTIVE (nobody locked out)",
              active == 2, str(active))

        exp = c.execute(text(
            "SELECT is_nullable FROM information_schema.columns WHERE "
            "table_name='agent_actions' AND column_name='expires_at'")).scalar()
        check("agent_actions.expires_at exists and is NULLABLE",
              exp == "YES", str(exp))

    # The constraint has to be enforced by the DATABASE, not only by the ORM —
    # that is the whole reason approvals.py can treat NULL as unreachable.
    try:
        with engine.begin() as c:
            c.execute(text("UPDATE users SET is_active = NULL "
                           "WHERE username = 'pg-admin'"))
        check("the database REFUSES a NULL is_active", False, "it accepted one")
    except Exception as e:
        check("the database REFUSES a NULL is_active",
              "null" in str(e).lower(), str(e)[:120])

    # --- 5. the gate, against this engine ------------------------------------
    # On its OWN database. test_approval_gate's fresh_db() drops and recreates
    # every table between sections, so pointing it at the migrated database
    # would destroy the rows section 4 below is about to check — and the
    # downgrade assertion would then be measuring the test suite's teardown
    # rather than the migration.
    print("\n3. THE GATE ITSELF, ON POSTGRESQL")
    gate_url = pg_scratch.scratch_url(port, DB + "_gate")
    pg_scratch.ensure(port, DB + "_gate")
    r = subprocess.run([sys.executable, "test_approval_gate.py"], cwd=here,
                       env={**os.environ, "DATABASE_URL": gate_url},
                       capture_output=True, text=True, errors="replace")
    passed = r.stdout.count("PASS  ")
    check(f"test_approval_gate.py green on PostgreSQL ({passed} assertions)",
          r.returncode == 0, r.stdout[-800:] + r.stderr[-400:])

    # --- 6. downgrade ---------------------------------------------------------
    print("\n4. THE MIGRATION REVERSES")
    r = subprocess.run([sys.executable, "-m", "alembic", "downgrade",
                        "0004_per_tenant_bom"], cwd=here, env=env,
                       capture_output=True, text=True, errors="replace")
    check("alembic downgrade 0004", r.returncode == 0, r.stderr[-400:])
    with engine.begin() as c:
        gone = c.execute(text(
            "SELECT count(*) FROM information_schema.columns WHERE "
            "(table_name='users' AND column_name='is_active') OR "
            "(table_name='agent_actions' AND column_name='expires_at')")).scalar()
        check("both columns are gone", gone == 0, str(gone))
        still = c.execute(text("SELECT count(*) FROM users")).scalar()
        check("...and the users themselves survived", still == 2, str(still))

    engine.dispose()
    print()
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        return 1
    print("THE APPROVAL GATE HOLDS ON POSTGRESQL 18.3")
    return 0


if __name__ == "__main__":
    sys.exit(main())
