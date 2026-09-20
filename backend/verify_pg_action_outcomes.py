"""Migration 0011_action_outcomes on real PostgreSQL, from a populated database.

WHY POSTGRESQL AND NOT ONLY SQLITE
----------------------------------
SQLite ignores VARCHAR lengths, does not enforce foreign keys by default, and is
relaxed about NOT NULL on a column with no default. Every property this table
depends on is one of those:

  * `metric`, `scope_kind` and `verdict` are bounded, so a vocabulary that grows
    past its column fails loudly here instead of truncating silently in
    production;
  * `action_id` is a foreign key onto agent_actions, and unique — "did it help?"
    may not have two answers;
  * `tenant_code` is NOT NULL with no default, so an outcome written without its
    tenant is refused rather than handed to the founder workspace;
  * `baseline_value` and `measured_value` are NULLABLE, because "no reading" and
    "a reading of 0" are different claims (ADR-0014). A NOT NULL there is how
    this table would start manufacturing improvements out of absences, and
    PostgreSQL is where that can be proven by insertion.

test_migration_0011_action_outcomes pins the shape on SQLite; this proves it on
the engine production runs.

WHAT IT PROVES, IN PRODUCTION'S OWN ORDER
-----------------------------------------
  1. build from the FROZEN baseline snapshot, stamp 0001, migrate to the
     revision before 0011 — the database a live deployment has the moment before
     it deploys this change — and fill it with factory rows and an approved
     agent action;
  2. `alembic upgrade head` keeps every one of those rows and creates the table;
  3. the migrated schema matches models.py (autogenerate diff empty, with the CI
     gate's rules), and the comparison is shown able to FAIL;
  4. PostgreSQL itself refuses: a second outcome for one action, an outcome for
     an action that does not exist, an outcome with no tenant, and a metric or
     verdict longer than its column — each beside a control insert that succeeds;
  5. a NULL reading is accepted on both sides, because that is the design;
  6. downgrade with outcome rows present drops the table and leaves the previous
     schema's rows; re-upgrade restores an undrifted schema.

Run: python backend/verify_pg_action_outcomes.py [port]      (PostgreSQL only)
"""
import io
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pg_scratch  # noqa: E402

DB = "amp_scratch_action_outcomes"
TABLE = "action_outcomes"
COUNTED = ("machines", "downtime_logs", "agent_actions", "users")

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def previous_revision():
    src = io.open(os.path.join(HERE, "alembic", "versions", "0011_action_outcomes.py"),
                  encoding="utf-8").read()
    return re.search(r'^down_revision = "([^"]+)"', src, re.M).group(1)


def alembic(env, *args):
    r = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=HERE, env=env,
                       capture_output=True, text=True, errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


OUTCOME = ("INSERT INTO action_outcomes (tenant_code, action_id, metric, scope_kind, scope_id, "
           "scope_label, window_days, baseline_value, baseline_at) VALUES "
           "({tenant}, {action}, {metric}, 'machine', 1, 'CNC-01', 7, {baseline}, now())")


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

    def tables():
        with engine.connect() as c:
            return {r[0] for r in c.execute(text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))}

    def counts():
        with engine.connect() as c:
            return {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in COUNTED}

    def refuses(sql):
        """(did PostgreSQL refuse it, the error class)."""
        try:
            with engine.begin() as c:
                c.execute(text(sql))
            return False, "accepted"
        except Exception as e:                        # noqa: BLE001 - the refusal is the result
            return True, type(e).__name__

    # --- 1. the database a deployment has before this change ------------------
    print("1. BASELINE SNAPSHOT -> PREVIOUS REVISION -> FACTORY ROWS")
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
    check("the table does not exist yet", TABLE not in tables())

    with engine.begin() as c:
        c.execute(text("INSERT INTO machines (id, tenant_code, site, name, status, utilization) "
                       "VALUES (1,'FACTORY_A','P1','CNC-01','Running',70)"))
        c.execute(text("INSERT INTO downtime_logs (tenant_code, machine_id, reason, duration, "
                       "created_at) VALUES ('FACTORY_A',1,'Breakdown','15 min', now())"))
        c.execute(text("INSERT INTO users (username, password, role, tenant_code, is_active) "
                       "VALUES ('a_admin','x','Admin','FACTORY_A',true)"))
        c.execute(text("INSERT INTO agent_actions (id, tenant_code, agent, action_type, summary, "
                       "ref_kind, ref_id, status) VALUES "
                       "(1,'FACTORY_A','maintenance','open_task','Service CNC-01','maintenance_task',1,"
                       "'Approved')"))
        c.execute(text("SELECT setval('machines_id_seq', 10)"))
        c.execute(text("SELECT setval('agent_actions_id_seq', 10)"))
    before = counts()
    check("rows exist in every counted table before the upgrade",
          all(v >= 1 for v in before.values()), str(before))

    # --- 2. the upgrade -------------------------------------------------------
    print("\n2. UPGRADE TO HEAD")
    rc, out = alembic(env, "upgrade", "head")
    check("alembic upgrade head", rc == 0, out[-400:])
    check("every pre-existing row survived", counts() == before, f"{before} -> {counts()}")
    check("the table exists", TABLE in tables())

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

    cols = {c["name"]: c for c in inspect(engine).get_columns(TABLE)}
    for name, length in (("metric", 48), ("scope_kind", 16), ("verdict", 16)):
        check(f"{name} is VARCHAR({length}) on PostgreSQL",
              getattr(cols[name]["type"], "length", None) == length,
              str(getattr(cols[name]["type"], "length", None)))
    check("baseline_value is nullable (no reading is not a reading of zero)",
          cols["baseline_value"]["nullable"])
    check("measured_value is nullable", cols["measured_value"]["nullable"])
    check("tenant_code is NOT NULL", not cols["tenant_code"]["nullable"])

    # CONTROL: the comparison must be able to fail, or "no drift" proves nothing.
    with engine.begin() as c:
        c.execute(text("DROP INDEX ix_action_outcomes_tenant_created"))
    check("CONTROL: the drift comparison notices a missing index",
          any("ix_action_outcomes_tenant_created" in str(x) for x in drift()))
    with engine.begin() as c:
        c.execute(text("CREATE INDEX ix_action_outcomes_tenant_created "
                       "ON action_outcomes (tenant_code, created_at)"))
    check("...and the index is restored", not drift())

    # --- 4. what PostgreSQL itself refuses ------------------------------------
    print("\n4. THE DATABASE ENFORCES THE DESIGN")
    ok, _ = refuses(OUTCOME.format(tenant="'FACTORY_A'", action="1",
                                   metric="'downtime_minutes'", baseline="90.0"))
    check("CONTROL: a well-formed outcome is accepted", not ok)

    ok, why = refuses(OUTCOME.format(tenant="'FACTORY_A'", action="1",
                                     metric="'downtime_minutes'", baseline="10.0"))
    check("a SECOND outcome for the same action is refused", ok, why)

    ok, why = refuses(OUTCOME.format(tenant="'FACTORY_A'", action="9999",
                                     metric="'downtime_minutes'", baseline="10.0"))
    check("an outcome for an action that does not exist is refused", ok, why)

    ok, why = refuses(OUTCOME.format(tenant="NULL", action="1",
                                     metric="'downtime_minutes'", baseline="10.0"))
    check("an outcome with no tenant is refused", ok, why)

    ok, why = refuses(OUTCOME.format(tenant="'FACTORY_A'", action="1",
                                     metric="'" + "m" * 49 + "'", baseline="10.0"))
    check("a metric longer than 48 characters is refused (not truncated)", ok, why)

    with engine.begin() as c:
        c.execute(text("INSERT INTO agent_actions (id, tenant_code, agent, action_type, summary, "
                       "ref_kind, ref_id, status) VALUES "
                       "(2,'FACTORY_A','maintenance','open_task','Second','maintenance_task',1,"
                       "'Approved')"))
    ok, why = refuses("UPDATE action_outcomes SET verdict = '" + "v" * 17 + "' WHERE action_id = 1")
    check("a verdict longer than 16 characters is refused", ok, why)

    # --- 5. a null reading is the point ---------------------------------------
    print("\n5. NULL IS NOT ZERO, AND POSTGRESQL ACCEPTS IT")
    ok, why = refuses(OUTCOME.format(tenant="'FACTORY_A'", action="2",
                                     metric="'stock_on_hand'", baseline="NULL"))
    check("an outcome with NO baseline reading is accepted", not ok, why)
    with engine.connect() as c:
        nulls = c.execute(text("SELECT count(*) FROM action_outcomes "
                               "WHERE baseline_value IS NULL")).scalar()
        zeros = c.execute(text("SELECT count(*) FROM action_outcomes "
                               "WHERE baseline_value = 0")).scalar()
    check("...and is stored as NULL, not as 0", nulls == 1 and zeros == 0, f"null={nulls} zero={zeros}")

    # --- 6. the downgrade -----------------------------------------------------
    print("\n6. DOWNGRADE WITH OUTCOME ROWS, THEN UPGRADE AGAIN")
    before_down = counts()
    rc, out = alembic(env, "downgrade", prev)
    check("alembic downgrade", rc == 0, out[-400:])
    check("the table is gone", TABLE not in tables())
    check("the previous schema's rows are untouched", counts() == before_down,
          f"{before_down} -> {counts()}")
    rc, out = alembic(env, "upgrade", "head")
    check("re-upgrade", rc == 0, out[-400:])
    check("the table is back", TABLE in tables())
    check("no drift after the round trip", not drift(), str(drift()[:3]))
    with engine.connect() as c:
        check("and it is empty, because a downgrade discards the measurements",
              c.execute(text("SELECT count(*) FROM action_outcomes")).scalar() == 0)

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
