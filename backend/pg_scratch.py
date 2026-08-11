"""Helper: build a DATABASE_URL for a DISPOSABLE PostgreSQL database.

Reads the local dev credentials from backend/.env, then points at a database
whose name is not the dev one. Nothing here ever writes to flowmes_db.

Prints the URL for a caller to consume; the password is never logged by callers
because they pass it straight into an env var.
"""
import os
import re
import sys

from dotenv import dotenv_values

SCRATCH_DB = "amp_scratch_verify"


def scratch_url(port=None, db=SCRATCH_DB):
    # backend/.env first, so a developer's local run keeps borrowing their dev
    # credentials and never depends on an exported DATABASE_URL.
    base = dotenv_values(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      ".env")).get("DATABASE_URL", "")
    # Then the environment. CI has no .env file — the runner supplies a
    # PostgreSQL service container and DATABASE_URL — and before this fallback
    # every harness built on pg_scratch was local-only, which is exactly why the
    # #513 upgrade path had never been exercised by an automated run.
    if not base:
        base = os.environ.get("DATABASE_URL", "")
    m = re.match(r"^(postgresql(?:\+\w+)?://[^/]+)/(.+)$", base)
    if not m:
        raise SystemExit("no postgresql DATABASE_URL to borrow credentials from "
                         "(looked in backend/.env, then the environment)")
    authority, dev_db = m.group(1), m.group(2)
    if port:
        authority = re.sub(r":\d+$", f":{port}", authority)
        if not re.search(r":\d+$", authority):
            authority = f"{authority}:{port}"
    if db == dev_db:
        raise SystemExit("refusing to hand back the dev database name")
    return f"{authority}/{db}"


def ensure(port=None, db=SCRATCH_DB):
    """Drop and recreate the scratch database. Connects via 'postgres'."""
    import psycopg2
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

    admin = scratch_url(port, "postgres").replace("postgresql+psycopg2", "postgresql")
    conn = psycopg2.connect(admin)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute("SELECT version()")
    version = cur.fetchone()[0]
    cur.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
    cur.execute(f'CREATE DATABASE "{db}"')
    cur.close()
    conn.close()
    return version


if __name__ == "__main__":
    port = sys.argv[1] if len(sys.argv) > 1 else None
    print(ensure(port))
    print(scratch_url(port))
