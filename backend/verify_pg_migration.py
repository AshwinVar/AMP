"""Verify the machine-identity migration against a real PostgreSQL 18 server.

Runs the campaign's required cycle on a DISPOSABLE database:

  1. fresh database -> migrate to head          (the new-customer path)
  2. seed three tenants that share machine names
  3. re-run migrate                              (idempotency: a redeploy must
                                                  not fail or change anything)
  4. the pre-upgrade path: build the OLD schema by hand (no site column, no
     constraint), put COLLIDING rows in it, then migrate. This is the only
     path an existing customer takes, and it is the one that can lose data if
     the migration is careless.

Why this cannot be inferred from the SQLite runs: SQLite does not enforce
NOT NULL the same way, rebuilds tables wholesale under batch_alter_table
(hiding whether the real ALTER works), and treats a UNIQUE constraint over
NULLs identically only by coincidence. Production is PostgreSQL.

Run:  python backend/verify_pg_migration.py
"""
import os
import subprocess
import sys

import pg_scratch

HERE = os.path.dirname(os.path.abspath(__file__))
failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def migrate(url, label):
    """Run migrate.py in a subprocess so it gets a clean import of database.py
    bound to THIS url — database.py reads DATABASE_URL at import time."""
    env = dict(os.environ, DATABASE_URL=url)
    proc = subprocess.run([sys.executable, os.path.join(HERE, "migrate.py")],
                          capture_output=True, text=True, errors="replace",
                          env=env, cwd=HERE)
    print(f"    [{label}] exit={proc.returncode}")
    for line in (proc.stdout + proc.stderr).splitlines():
        if any(k in line for k in ("MIGRATE", "Running upgrade", "renaming",
                                   "already present", "Error", "error")):
            print(f"      {line.strip()[:150]}")
    return proc


def schema(url):
    """(has_site, site_nullable, constraint_columns) straight from the catalog."""
    import sqlalchemy as sa
    eng = sa.create_engine(url)
    with eng.connect() as c:
        insp = sa.inspect(c)
        cols = {x["name"]: x for x in insp.get_columns("machines")}
        uqs = {u["name"]: u["column_names"]
               for u in insp.get_unique_constraints("machines")}
    eng.dispose()
    return ("site" in cols,
            cols.get("site", {}).get("nullable"),
            uqs.get("uq_machine_identity"))


def main():
    version = pg_scratch.ensure(5432)
    url = pg_scratch.scratch_url(5432)
    print(version.split(",")[0])
    print(f"scratch database: {url.rsplit('/', 1)[1]}  (dev database untouched)")
    print()

    # ---- 1. fresh database ------------------------------------------------
    print("=" * 70)
    print("1. FRESH DATABASE -> MIGRATE TO HEAD")
    print("=" * 70)
    proc = migrate(url, "fresh")
    check("migrate.py succeeded on an empty database", proc.returncode == 0,
          (proc.stdout + proc.stderr)[-300:])
    has_site, nullable, uq = schema(url)
    check("machines.site exists", has_site)
    check("machines.site is NOT NULL", nullable is False, f"nullable={nullable}")
    check("uq_machine_identity covers (tenant_code, site, name)",
          uq == ["tenant_code", "site", "name"], str(uq))

    # ---- 2. seed and prove the constraint bites ---------------------------
    print()
    print("=" * 70)
    print("2. THREE TENANTS SHARING MACHINE NAMES, ON POSTGRESQL")
    print("=" * 70)
    import sqlalchemy as sa
    eng = sa.create_engine(url)
    with eng.begin() as c:
        for t in ("FACTORY_A", "FACTORY_B", "FACTORY_C"):
            c.execute(sa.text(
                "INSERT INTO machines (tenant_code, site, name, status, utilization,"
                " downtime, line) VALUES (:t, '', 'CNC-01', 'Running', 50, '0 min', '')"),
                {"t": t})
    with eng.connect() as c:
        n = c.execute(sa.text(
            "SELECT count(*) FROM machines WHERE name='CNC-01'")).scalar()
    check("three tenants each hold their own CNC-01", n == 3, f"n={n}")

    try:
        with eng.begin() as c:
            c.execute(sa.text(
                "INSERT INTO machines (tenant_code, site, name, status, utilization,"
                " downtime, line) VALUES ('FACTORY_A', '', 'CNC-01', 'Idle', 0, '0 min', '')"))
        check("PostgreSQL rejects a duplicate identity", False,
              "the duplicate insert succeeded")
    except Exception as exc:
        check("PostgreSQL rejects a duplicate identity",
              "uq_machine_identity" in str(exc), repr(exc)[:120])
    eng.dispose()

    # ---- 3. idempotency ---------------------------------------------------
    print()
    print("=" * 70)
    print("3. RE-RUN MIGRATE (a redeploy must be a no-op)")
    print("=" * 70)
    proc = migrate(url, "re-run")
    check("second migrate.py run succeeded", proc.returncode == 0,
          (proc.stdout + proc.stderr)[-300:])
    check("schema unchanged after the second run",
          schema(url) == (True, False, ["tenant_code", "site", "name"]),
          str(schema(url)))
    eng = sa.create_engine(url)
    with eng.connect() as c:
        n = c.execute(sa.text("SELECT count(*) FROM machines")).scalar()
    eng.dispose()
    check("the seeded rows survived the re-run", n == 3, f"n={n}")

    # ---- 4. the upgrade-from-old-schema path ------------------------------
    print()
    print("=" * 70)
    print("4. UPGRADE AN EXISTING DATABASE THAT HAS COLLIDING ROWS")
    print("=" * 70)
    pg_scratch.ensure(5432)
    eng = sa.create_engine(url)
    with eng.begin() as c:
        # migrate.py decides "existing database" by finding BOTH machines and
        # users. Creating only machines would send it down the fresh-database
        # branch (create_all + stamp), which is not the path a real customer
        # takes — and the whole point of this section is the path where the
        # ALTER has to run against data that is already there.
        c.execute(sa.text("""
            CREATE TABLE users (
                id SERIAL PRIMARY KEY,
                username VARCHAR UNIQUE NOT NULL,
                password VARCHAR NOT NULL,
                role VARCHAR NOT NULL,
                tenant_code VARCHAR DEFAULT 'DEFAULT'
            )"""))
        # The pre-migration shape: no site column, no identity constraint.
        c.execute(sa.text("""
            CREATE TABLE machines (
                id SERIAL PRIMARY KEY,
                tenant_code VARCHAR NOT NULL DEFAULT 'DEFAULT',
                name VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                utilization INTEGER DEFAULT 0,
                downtime VARCHAR DEFAULT '0 min',
                line VARCHAR DEFAULT ''
            )"""))
        # Two machines with the SAME name in the SAME tenant — a database the
        # constraint cannot be applied to without resolving them first.
        for status in ("Running", "Idle"):
            c.execute(sa.text(
                "INSERT INTO machines (tenant_code, name, status) "
                "VALUES ('ACME', 'CNC-01', :s)"), {"s": status})
        c.execute(sa.text(
            "INSERT INTO machines (tenant_code, name, status) "
            "VALUES ('OTHERCO', 'CNC-01', 'Running')"))
    eng.dispose()

    proc = migrate(url, "upgrade-with-collisions")
    check("migrate.py succeeded against colliding existing data",
          proc.returncode == 0, (proc.stdout + proc.stderr)[-400:])

    eng = sa.create_engine(url)
    with eng.connect() as c:
        rows = c.execute(sa.text(
            "SELECT id, tenant_code, site, name FROM machines ORDER BY id")).fetchall()
    eng.dispose()
    print("    resulting rows:")
    for r in rows:
        print(f"      id={r[0]} tenant={r[1]} site={r[2]!r} name={r[3]}")

    check("NO row was deleted to make the constraint fit", len(rows) == 3,
          f"{len(rows)} rows remain, expected 3")
    check("the colliding row was renamed, not dropped",
          sorted(r[3] for r in rows) == ["CNC-01", "CNC-01", "CNC-01#2"],
          str(sorted(r[3] for r in rows)))
    check("the other tenant's identically-named machine was left alone",
          any(r[1] == "OTHERCO" and r[3] == "CNC-01" for r in rows))
    has_site, nullable, uq = schema(url)
    check("the constraint is now in place on the upgraded database",
          uq == ["tenant_code", "site", "name"] and nullable is False,
          f"uq={uq} nullable={nullable}")

    print()
    print("=" * 70)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        return 1
    print("MIGRATION VERIFIED ON POSTGRESQL 18: fresh, idempotent, and safe on")
    print("an existing database that already contained duplicate identities.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
