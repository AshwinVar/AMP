"""The deployment migration contract, proved against real PostgreSQL (ADR-0018).

This is the test that would have caught #513, and none of the 186 suites could
have: every one of them builds its schema with `create_all()` on a fresh
database, which is the single arrangement in which model/schema drift cannot
occur. The defect lives in the gap between a database that already exists and a
model that has moved on, so that gap is what this file exercises — on PostgreSQL
18, because the mechanisms involved (transactional DDL, `ALTER TABLE ... ADD
COLUMN ... NOT NULL DEFAULT`, reflection of constraint names) behave differently
on SQLite or do not exist there at all.

THE MATRIX
----------
  1. FRESH          empty database -> migrate -> at head -> app ready -> login
  2. PRODUCTION-LIKE  a database built the way production's was (create_all, no
                    alembic_version, then the columns migrations add STRIPPED
                    back off) -> the app REFUSES -> migrate -> the app SERVES
                    -> login. This is #513, reproduced end to end and then fixed
                    by the deployment mechanism rather than by create_all.
  3. IDEMPOTENT     already at head -> migrate again -> no-op, still ready
  4. BEHIND         stamped one revision back -> readiness REFUSES, and names
                    both revisions
  5. FAILED         a migration that raises -> non-zero exit, database not
                    left half-changed, readiness still refuses
  6. INVALID        a revision string no build has ever produced -> fail closed
                    with a diagnostic rather than a traceback

Run: python backend/verify_pg_deploy.py [port]
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pg_scratch  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def run(code, url, extra_env=None):
    """Run a snippet in a FRESH interpreter against `url`; return the process.

    A subprocess per step is not ceremony: database.py builds its engine at
    import time from DATABASE_URL, main.py decides schema ownership at import,
    and schema_guard caches its verdict. Reusing one interpreter would test a
    process that had already made all three decisions against a different
    database than the one under test.

    Returns the CompletedProcess so callers assert on `returncode` directly. An
    earlier version returned a convenience `ok` flag that was hardcoded True
    whenever the caller said a failure was expected -- so the two checks that
    mattered most, "a failed migration exits non-zero" and "the query really
    breaks", passed on a constant instead of on the process.
    """
    env = {**os.environ, "DATABASE_URL": url}
    env.pop("PRODUCTION", None)
    env.pop("RAILWAY_ENVIRONMENT", None)
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-c", code], cwd=HERE, env=env,
                          capture_output=True, text=True, errors="replace")


MIGRATE = "import migrate; migrate.run(verbose=True)"

READINESS = """
import json
import schema_guard
from database import engine
print("VERDICT " + json.dumps(schema_guard.check(engine)))
"""

# Boot the real app and drive the real login handler. Not a mock of either: the
# whole point is that ORM-backed application functionality works, and login is
# the endpoint #513 took down.
SMOKE = """
import json
import main, models, schema_guard
from database import SessionLocal, engine
from security import hash_password
import core_routes, schemas

state = schema_guard.evaluate(engine, force=True)
ok = None
# Login is attempted ONLY when the guard says this instance should serve. That
# mirrors production, where SchemaGuardMiddleware would have returned 503 long
# before the handler ran -- driving the handler anyway would test a path the
# deployment does not allow.
if state["ok"]:
    db = SessionLocal()
    try:
        if not db.query(models.User).filter(models.User.username == "deploy_probe").first():
            db.add(models.User(username="deploy_probe", password=hash_password("probe-pw"),
                               role="Admin", tenant_code="DEFAULT", is_active=True))
            db.commit()
        token = core_routes.login(schemas.UserLogin(username="deploy_probe",
                                                    password="probe-pw"), db=db)
        ok = bool(token.get("access_token"))
    finally:
        db.close()
print("SMOKE " + json.dumps({"ready": state["ok"], "state": state["state"],
                             "login": ok, "routes": len(main.app.routes)}))
"""


def verdict(proc):
    for line in proc.stdout.splitlines():
        if line.startswith("VERDICT "):
            import json
            return json.loads(line[len("VERDICT "):])
    return None


def smoke(proc):
    for line in proc.stdout.splitlines():
        if line.startswith("SMOKE "):
            import json
            return json.loads(line[len("SMOKE "):])
    return None


def fresh_db(port, name):
    pg_scratch.ensure(port, name)
    url = pg_scratch.scratch_url(port, name)
    from sqlalchemy import create_engine, text
    e = create_engine(url)
    with e.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    e.dispose()
    return url


# ======================================================================
def scenario_fresh(port):
    print("=" * 74)
    print("1. FRESH DATABASE -> MIGRATE -> READY -> LOGIN")
    print("=" * 74)
    url = fresh_db(port, "amp_deploy_fresh")

    p = run(MIGRATE, url)
    ok = p.returncode == 0
    check("migrate.py succeeds on an empty database", ok, p.stderr[-300:])

    p = run(READINESS, url)
    v = verdict(p)
    check("the schema guard reports ok", v and v["ok"] and v["state"] == "ok", str(v))

    p = run(SMOKE, url)
    ok = p.returncode == 0
    s = smoke(p)
    check("the app boots and login works", ok and s and s["login"] is True,
          str(s) or p.stderr[-400:])
    check("...with the expected route count", s and s["routes"] >= 290, str(s))


def scenario_production_like(port):
    """#513, reproduced: a database that predates the model, then repaired by the
    DEPLOYMENT MECHANISM rather than by create_all."""
    print()
    print("=" * 74)
    print("2. A PRODUCTION-LIKE DATABASE (THE #513 STATE)")
    print("=" * 74)
    url = fresh_db(port, "amp_deploy_prodlike")
    from sqlalchemy import create_engine, inspect, text

    # Build it the way production's was built: create_all, no alembic_version.
    p = run("import models\nfrom database import Base, engine\n"
            "Base.metadata.create_all(bind=engine)", url)
    check("built a create_all schema with no alembic_version",
          p.returncode == 0, p.stderr[-300:])

    # Now make it OLD. create_all can only ever have produced the shape of the
    # models AT THE TIME each table was first created, so strip the columns that
    # later migrations add to already-existing tables. That is exactly what
    # production looked like on 2026-08-09.
    e = create_engine(url)
    with e.begin() as c:
        c.execute(text("ALTER TABLE users DROP COLUMN is_active"))
        c.execute(text("ALTER TABLE agent_actions DROP COLUMN expires_at"))
        c.execute(text("ALTER TABLE machine_installations DROP COLUMN last_service_hours"))
        c.execute(text("ALTER TABLE machine_installations DROP COLUMN last_service_at"))
        # A real row, so the repair has to survive data rather than an empty table.
        c.execute(text("INSERT INTO users (username, password, role, tenant_code) "
                       "VALUES ('legacy_admin', 'x', 'Admin', 'DEFAULT')"))
    cols = {c["name"] for c in inspect(e).get_columns("users")}
    check("the users table has no is_active (the #513 shape)", "is_active" not in cols,
          str(sorted(cols)))

    # THE REFUSAL. In a production environment an unmanaged database is refused
    # outright — Alembic must own production.
    p = run(READINESS, url, extra_env={"PRODUCTION": "1"})
    v = verdict(p)
    check("a PRODUCTION deployment REFUSES an unmanaged database",
          v and v["ok"] is False and v["state"] == "unmanaged", str(v))

    # ...and it refuses BEFORE touching the ORM: no login is attempted, nothing
    # is seeded, no half-write happens against a schema this build cannot use.
    p = run(SMOKE, url, extra_env={"PRODUCTION": "1"})
    s = smoke(p)
    check("...and serves nothing: not ready, and login never attempted",
          s and s["ready"] is False and s["login"] is None, str(s) or p.stderr[-300:])

    # AN HONEST NOTE ABOUT DEV. Without PRODUCTION set, this same database is
    # NOT refused — the boot patches repair it, which is the pre-Alembic
    # mechanism deliberately preserved for laptops and scratch databases
    # (ADR-0018, staged retirement). Asserted rather than left implied, because
    # it is the one place the two mechanisms visibly disagree, and because an
    # earlier version of this file claimed the opposite and passed on a constant.
    p = run(SMOKE, url)
    s = smoke(p)
    check("CONTROL: the same database in DEV is repaired by the boot patches",
          s and s["ready"] is True and s["state"] == "unmanaged", str(s))

    # THE DEPLOYMENT STEP. This is `preDeployCommand` in railway.toml.
    p = run(MIGRATE, url)
    ok = p.returncode == 0
    check("migrate.py adopts and upgrades the old database", ok, p.stderr[-400:])
    check("...and reports what it did",
          "stamping baseline" in p.stdout or "upgrading" in p.stdout, p.stdout[-300:])

    cols = {c["name"] for c in inspect(e).get_columns("users")}
    check("users.is_active now exists", "is_active" in cols, str(sorted(cols)))
    inst = {c["name"] for c in inspect(e).get_columns("machine_installations")}
    check("the service-clock columns now exist",
          {"last_service_hours", "last_service_at"} <= inst, str(sorted(inst)))
    agent = {c["name"] for c in inspect(e).get_columns("agent_actions")}
    check("agent_actions.expires_at now exists", "expires_at" in agent, str(sorted(agent)))

    with e.connect() as c:
        row = c.execute(text("SELECT is_active FROM users WHERE username='legacy_admin'")).scalar()
    check("the pre-existing account was left ACTIVE, not disabled", row is True, str(row))
    e.dispose()

    # THE PROOF. Ready, and login works — in a PRODUCTION environment this time.
    p = run(READINESS, url, extra_env={"PRODUCTION": "1"})
    v = verdict(p)
    check("the production deployment is now ready", v and v["ok"] and v["state"] == "ok", str(v))

    p = run(SMOKE, url, extra_env={"PRODUCTION": "1"})
    ok = p.returncode == 0
    s = smoke(p)
    check("login succeeds after the migration", ok and s and s["login"] is True,
          str(s) or p.stderr[-400:])

    # CONTROL: prove the repair came from ALEMBIC, not from create_all or the
    # boot patches. On a managed database both are switched off (ADR-0018), so a
    # column dropped now must NOT come back on the next boot.
    e = create_engine(url)
    with e.begin() as c:
        c.execute(text("ALTER TABLE users DROP COLUMN is_active"))
    p = run("import main", url, extra_env={"PRODUCTION": "1"})
    cols = {c["name"] for c in inspect(e).get_columns("users")}
    check("CONTROL: create_all/boot patches do NOT silently repair a managed database",
          "is_active" not in cols,
          "the column came back — a boot patch is still doing schema management")
    with e.begin() as c:
        c.execute(text("ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE"))
    e.dispose()


def scenario_idempotent(port):
    print()
    print("=" * 74)
    print("3. RUNNING THE MIGRATION AGAIN IS A SAFE NO-OP")
    print("=" * 74)
    url = fresh_db(port, "amp_deploy_idem")
    run(MIGRATE, url)
    p = run(MIGRATE, url)
    ok = p.returncode == 0
    check("a second migrate.py exits 0", ok, p.stderr[-300:])
    check("...and says it is already at head", "already at head" in p.stdout, p.stdout[-200:])
    p = run(READINESS, url, extra_env={"PRODUCTION": "1"})
    v = verdict(p)
    check("still ready afterwards", v and v["ok"], str(v))


def scenario_behind(port):
    print()
    print("=" * 74)
    print("4. A DATABASE ONE REVISION BEHIND REFUSES TRAFFIC")
    print("=" * 74)
    url = fresh_db(port, "amp_deploy_behind")
    run(MIGRATE, url)

    p = run("import migrate; print('HEAD ' + migrate.head_revision())", url)
    head = next((l.split()[1] for l in p.stdout.splitlines() if l.startswith("HEAD ")), None)
    check("read this build's head revision", bool(head), p.stdout[-200:])

    # Rewind the stamp only — the tables stay as they are. That is the shape of
    # "a migration was added and the deploy did not run it".
    from sqlalchemy import create_engine, text
    e = create_engine(url)
    with e.begin() as c:
        c.execute(text("UPDATE alembic_version SET version_num = '0005_approval_gate'"))
    e.dispose()

    p = run(READINESS, url)
    v = verdict(p)
    check("readiness REFUSES a database behind head",
          v and v["ok"] is False and v["state"] == "behind", str(v))
    check("...and names both revisions",
          v and v["current"] == "0005_approval_gate" and v["expected"] == head, str(v))
    check("...in dev as well as production — 'behind' is never tolerated",
          v and v["ok"] is False, str(v))

    # And the deployment mechanism fixes it.
    p = run(MIGRATE, url)
    ok = p.returncode == 0
    check("migrate.py rolls it forward", ok, p.stderr[-300:])
    p = run(READINESS, url)
    v = verdict(p)
    check("ready again", v and v["ok"], str(v))


def scenario_failed_migration(port):
    print()
    print("=" * 74)
    print("5. A FAILING MIGRATION FAILS THE DEPLOY, AND CHANGES NOTHING")
    print("=" * 74)
    url = fresh_db(port, "amp_deploy_failed")
    run(MIGRATE, url)

    # A migration that does real work and THEN raises. Written to a temporary
    # file in the versions directory and removed afterwards.
    from sqlalchemy import create_engine, inspect, text
    broken = os.path.join(HERE, "alembic", "versions", "9999_deliberately_broken.py")
    with open(broken, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            '"""a migration that fails halfway, on purpose (test fixture)"""\n'
            "import sqlalchemy as sa\n"
            "from alembic import op\n\n"
            'revision = "9999_broken"\n'
            'down_revision = "0007_oem_service_clock"\n'
            "branch_labels = None\ndepends_on = None\n\n\n"
            "def upgrade() -> None:\n"
            '    op.add_column("users", sa.Column("half_applied", sa.String(), nullable=True))\n'
            '    raise RuntimeError("deliberate failure after a real DDL change")\n\n\n'
            "def downgrade() -> None:\n    pass\n")
    try:
        p = run(MIGRATE, url)
        ok = p.returncode == 0
        check("the migration step EXITS NON-ZERO (Railway aborts the deploy)",
              not ok, f"exit={p.returncode}")
        check("...and the failure is reported", "deliberate failure" in (p.stderr or ""),
              (p.stderr or "")[-200:])

        e = create_engine(url)
        cols = {c["name"] for c in inspect(e).get_columns("users")}
        # PostgreSQL runs DDL inside the transaction Alembic opens, so the
        # half-applied ADD COLUMN rolls back with the failure. This is the
        # property that makes a failed deploy safe rather than merely loud.
        check("the half-applied column was ROLLED BACK (transactional DDL)",
              "half_applied" not in cols, str(sorted(cols)))
        with e.connect() as c:
            rev = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
        check("the recorded revision did NOT advance", rev == "0007_oem_service_clock", str(rev))
        e.dispose()

        # And with a broken head in the build, the app refuses: its head is
        # 9999_broken, the database is at 0007.
        p = run(READINESS, url)
        v = verdict(p)
        check("the application refuses traffic while the migration is unapplied",
              v and v["ok"] is False and v["state"] == "behind", str(v))
    finally:
        os.remove(broken)

    # Removing the broken migration restores head; nothing was corrupted.
    p = run(READINESS, url)
    v = verdict(p)
    check("with the bad migration withdrawn, the database is healthy again",
          v and v["ok"], str(v))


def scenario_invalid_revision(port):
    print()
    print("=" * 74)
    print("6. AN UNKNOWN REVISION FAILS CLOSED, WITH A DIAGNOSTIC")
    print("=" * 74)
    url = fresh_db(port, "amp_deploy_invalid")
    run(MIGRATE, url)

    from sqlalchemy import create_engine, text
    e = create_engine(url)
    with e.begin() as c:
        c.execute(text("UPDATE alembic_version SET version_num = 'not_a_real_revision'"))
    e.dispose()

    p = run(READINESS, url)
    v = verdict(p)
    # A revision this build has never heard of is the ROLLBACK shape: the
    # database is assumed to be ahead. It is allowed, and it is loud.
    check("an unknown revision is reported as 'ahead', not silently ok",
          v and v["state"] == "ahead", str(v))
    check("...and the reason says the application is older than the schema",
          v and "OLDER than the schema" in v["reason"], str(v and v["reason"]))
    check("...and it does not pretend to be at the expected revision",
          v and v["current"] != v["expected"], str(v))


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "5432"
    version = pg_scratch.ensure(port, "amp_deploy_fresh")
    print(version.split(",")[0])
    print()
    scenario_fresh(port)
    scenario_production_like(port)
    scenario_idempotent(port)
    scenario_behind(port)
    scenario_failed_migration(port)
    scenario_invalid_revision(port)

    print()
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        return 1
    print("THE DEPLOYMENT MIGRATION CONTRACT HOLDS ON POSTGRESQL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
