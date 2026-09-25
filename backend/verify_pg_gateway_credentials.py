"""Migration 0013 on real PostgreSQL, from a populated database (ADR-0040).

WHY POSTGRESQL AND NOT ONLY SQLITE
----------------------------------
Two of the three things this migration adds are things SQLite cannot judge.

A UNIQUE constraint over a nullable column is the first. Production has
thousands of production records written by CSV import, the HTTP ingest and
people typing figures in; every one of them gets NULL in the new
`source_record_id`. The constraint is only addable to that table at all because
NULL is distinct from NULL — and "is it, on the engine we actually run?" is not
a question to answer from the documentation. If it collapsed them, the migration
would fail partway through a customer's deployment.

The second is `ALTER TABLE ... ADD CONSTRAINT` itself, which SQLite cannot do at
all: it needs a table rebuild, so the migration uses a batch operation, and a
batch operation behaving correctly on SQLite says nothing about what PostgreSQL
does with the same instruction against a populated table.

WHAT THIS PROVES, in production's own order:

  1. build from the FROZEN baseline snapshot, stamp 0001, migrate to the
     revision before 0013 — the database a live deployment has the moment
     before it deploys this — and write production records the way every
     existing path writes them;
  2. `alembic upgrade head` keeps every one of those rows, and the new column
     arrives NULL for all of them: no backfill invents a gateway that never
     published them;
  3. the constraint is REAL — a retried gateway message cannot write a second
     production record — while NULL rows are untouched and two workspaces may
     use the same record id;
  4. a credential binds to exactly one workspace and one site, and `gateway_id`
     is unique across the whole installation;
  5. the migrated schema matches models.py (autogenerate diff empty, with the
     CI gate's own rules), and the comparison is shown able to FAIL;
  6. downgrade with credentials and gateway-written records present reverses
     cleanly; re-upgrade restores an undrifted schema.

Run: python backend/verify_pg_gateway_credentials.py [port]      (PostgreSQL only)
"""
import io
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pg_scratch  # noqa: E402

DB = "amp_gateway_credentials"
failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def previous_revision():
    src = io.open(os.path.join(HERE, "alembic", "versions", "0013_gateway_credentials.py"),
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
    print(f"scratch database: {DB}; previous revision: {prev}")

    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.exc import IntegrityError
    engine = create_engine(url)

    def columns(table):
        return {c["name"] for c in inspect(engine).get_columns(table)}

    def production_rows():
        with engine.connect() as c:
            return [tuple(r) for r in c.execute(text(
                "SELECT tenant_code, total_count, good_count, rejected_count "
                "FROM production_records ORDER BY id"))]

    # ── 1. the database a deployment has before this change ─────────
    section("1. FROZEN SNAPSHOT -> PREVIOUS REVISION -> REAL PRODUCTION ROWS")
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
    check("production_records exists without the new column",
          "source_record_id" not in columns("production_records"))
    check("...and gateway_credentials does not exist yet",
          "gateway_credentials" not in inspect(engine).get_table_names())

    with engine.begin() as c:
        c.execute(text("INSERT INTO machines (tenant_code, site, name, status, utilization, "
                       "downtime) VALUES ('FACTORY_A','','CNC-01','Running',80,'0 min')"))
        c.execute(text("INSERT INTO machines (tenant_code, site, name, status, utilization, "
                       "downtime) VALUES ('FACTORY_B','','CNC-01','Idle',0,'0 min')"))
        for tenant in ("FACTORY_A", "FACTORY_B", "FACTORY_A"):
            c.execute(text(
                "INSERT INTO production_records (tenant_code, machine_id, planned_minutes, "
                "runtime_minutes, ideal_cycle_time_seconds, total_count, good_count, "
                "rejected_count, created_at) SELECT :t, id, 480, 400, 45, 100, 96, 4, now() "
                "FROM machines WHERE tenant_code = :t LIMIT 1"), {"t": tenant})
    before = production_rows()
    check("three production records exist, written the way every path writes them today",
          len(before) == 3, str(len(before)))

    # ── 2. the upgrade keeps every row ──────────────────────────────
    section("2. THE UPGRADE KEEPS EVERY ROW AND INVENTS NOTHING")
    rc, out = alembic(env, "upgrade", "head")
    check("upgrade to head succeeded on a populated database", rc == 0, out[-600:])
    after = production_rows()
    check("every production record survived", after == before, f"{before} -> {after}")
    with engine.connect() as c:
        nulls = c.execute(text("SELECT count(*) FROM production_records "
                               "WHERE source_record_id IS NULL")).scalar()
    check("...and all of them carry NULL: no backfill invented a gateway", nulls == 3, str(nulls))
    check("gateway_credentials now exists", "gateway_credentials" in inspect(engine).get_table_names())

    # ── 3. the constraint is real ───────────────────────────────────
    section("3. A RETRIED GATEWAY MESSAGE CANNOT DOUBLE-COUNT A SHIFT")

    def write(tenant, record_id):
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO production_records (tenant_code, machine_id, planned_minutes, "
                "runtime_minutes, ideal_cycle_time_seconds, total_count, good_count, "
                "rejected_count, source_record_id, created_at) "
                "SELECT :t, id, 30, 28, 45, 7, 7, 0, :r, now() FROM machines "
                "WHERE tenant_code = :t LIMIT 1"), {"t": tenant, "r": record_id})

    write("FACTORY_A", "rec-abc-123")
    refused = False
    try:
        write("FACTORY_A", "rec-abc-123")
    except IntegrityError:
        refused = True
    check("the SAME record id is refused the second time", refused,
          "a retry wrote a second record and the shift doubled")

    write("FACTORY_A", None)
    write("FACTORY_A", None)
    check("two NULL rows are both written, so every existing path is unaffected",
          True)

    crossed = True
    try:
        write("FACTORY_B", "rec-abc-123")
    except IntegrityError:
        crossed = False
    check("another WORKSPACE may use the same record id: the key is per tenant", crossed,
          "one customer's record id blocked another customer's")

    # ── 4. a credential binds to one workspace and one site ─────────
    section("4. A CREDENTIAL BINDS TO ONE WORKSPACE AND ONE SITE")
    with engine.begin() as c:
        c.execute(text("INSERT INTO gateway_credentials (tenant_code, site, gateway_id, "
                       "secret, is_active, created_at) "
                       "VALUES ('FACTORY_A','plant-1','gw-a-1','s3cr3t',true,now())"))
    duplicate = False
    try:
        with engine.begin() as c:
            c.execute(text("INSERT INTO gateway_credentials (tenant_code, site, gateway_id, "
                           "secret, is_active, created_at) "
                           "VALUES ('FACTORY_B','plant-9','gw-a-1','other',true,now())"))
    except IntegrityError:
        duplicate = True
    check("a gateway id is unique across the whole installation, not per tenant", duplicate,
          "two workspaces registered the same gateway id")

    with engine.connect() as c:
        row = c.execute(text("SELECT tenant_code, site, is_active FROM gateway_credentials "
                             "WHERE gateway_id = 'gw-a-1'")).first()
    check("the credential names exactly one workspace and one site",
          tuple(row) == ("FACTORY_A", "plant-1", True), str(tuple(row)))

    with engine.begin() as c:
        c.execute(text("INSERT INTO gateway_credentials (tenant_code, gateway_id, secret, "
                       "created_at) VALUES ('SOLO','gw-solo-1','k',now())"))
    with engine.connect() as c:
        solo = c.execute(text("SELECT site, is_active FROM gateway_credentials "
                              "WHERE gateway_id = 'gw-solo-1'")).first()
    check("site defaults to '' (never NULL), so equality comparisons are reliable",
          solo[0] == "", repr(solo[0]))
    check("...and a new credential is active by default", solo[1] is True, repr(solo[1]))

    nullable = False
    try:
        with engine.begin() as c:
            c.execute(text("INSERT INTO gateway_credentials (tenant_code, gateway_id, secret, "
                           "site, created_at) VALUES ('X','gw-x','k',NULL,now())"))
        nullable = True
    except IntegrityError:
        pass
    check("a NULL site is refused by the database", not nullable,
          "site accepted NULL, so NULL != NULL defeats the comparison")

    # ── 5. no drift, and the comparison can fail ────────────────────
    section("5. THE MIGRATED SCHEMA MATCHES models.py")
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

    found = drift()
    check("autogenerate reports no drift", not found, str(found)[:400])

    # The comparison must be able to FAIL, or it proves nothing.
    with engine.begin() as c:
        c.execute(text("ALTER TABLE gateway_credentials DROP COLUMN label"))
    check("...and it DOES detect a column removed behind its back", bool(drift()),
          "the drift detector did not notice a dropped column")
    with engine.begin() as c:
        c.execute(text("ALTER TABLE gateway_credentials ADD COLUMN label VARCHAR"))
    check("...and is clean again once restored", not drift(), str(drift())[:300])

    # ── 6. downgrade and back ───────────────────────────────────────
    section("6. DOWNGRADE REVERSES CLEANLY, WITH DATA PRESENT")
    with engine.connect() as c:
        # Captured rather than hardcoded: the count depends on how many writes
        # above were REFUSED, and a magic number here tests my arithmetic
        # instead of the migration. (It was wrong the first time.)
        before_downgrade = c.execute(text("SELECT count(*) FROM production_records")).scalar()
    rc, out = alembic(env, "downgrade", "-1")
    check("downgrade succeeded with credentials and gateway records present", rc == 0,
          out[-600:])
    check("gateway_credentials is gone",
          "gateway_credentials" not in inspect(engine).get_table_names())
    check("source_record_id is gone", "source_record_id" not in columns("production_records"))
    with engine.connect() as c:
        kept = c.execute(text("SELECT count(*) FROM production_records")).scalar()
    check("...and NO production record was deleted to make that fit",
          kept == before_downgrade, f"{before_downgrade} -> {kept}")

    rc, out = alembic(env, "upgrade", "head")
    check("re-upgrade succeeded", rc == 0, out[-400:])
    check("...and the schema is undrifted again", not drift(), str(drift())[:300])

    print()
    print("=" * 74)
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        return 1
    print("MIGRATION 0013 VERIFIED ON POSTGRESQL: additive on a populated")
    print("database, the idempotency constraint is real, and reversible.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
