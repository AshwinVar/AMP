"""Schema migration entry point — the one command a deploy should run.

WHY THIS EXISTS. AMP's schema has always been built by
Base.metadata.create_all() plus ~30 idempotent boot-time DDL helpers in
main.py. That works, but it has three properties that stop being acceptable
once real customer data is in the database:

  * No history. Nothing records which schema changes have been applied, so
    there is no way to tell a half-migrated database from a correct one.
  * No rollback. Every helper is forward-only.
  * Failure is silent. Each helper is wrapped in try/except that prints and
    continues, because a migration hiccup must not stop the app booting.
    The cost is that a genuinely failed ALTER is a log line, and the app
    then serves traffic against a schema it believes is correct.

Alembic fixes all three for changes made from now on. This module handles the
awkward part — adopting it on a database that already exists.

THE TRANSITION, and why it is safe:

    no alembic_version table + tables present  -> stamp baseline, run nothing
    no alembic_version table + empty database  -> create_all, then stamp
    alembic_version present                     -> upgrade to head

The first case is the live production database. Stamping asserts "this schema
already matches revision 0001_baseline", which is true by construction —
0001_baseline is an empty anchor precisely so that this assertion costs
nothing and changes nothing.

WHAT THIS DOES NOT DO. It does not remove create_all or the boot helpers in
main.py. Those stay until a later change retires them deliberately, because
removing them in the same step as introducing Alembic would mean a single
deploy where a bug in either mechanism leaves the schema wrong with no
fallback. Belt and braces first; retire the belt once the braces are proven.

Usage:
    python backend/migrate.py            # bring the database to head
    python backend/migrate.py --status   # report current vs head, change nothing
    python backend/migrate.py --sql      # print the SQL instead of running it
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _config():
    from alembic.config import Config
    cfg = Config(os.path.join(HERE, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(HERE, "alembic"))
    return cfg


def _has_any_table(engine):
    """Does this database already contain the application's schema?

    Checked by looking for a table that has existed since the first commit
    rather than counting tables, so a partially-created database is not
    mistaken for a fully-migrated one.
    """
    from sqlalchemy import inspect
    names = set(inspect(engine).get_table_names())
    return "machines" in names and "users" in names


def _is_stamped(engine):
    from sqlalchemy import inspect
    return "alembic_version" in set(inspect(engine).get_table_names())


def current_revision(engine):
    from alembic.migration import MigrationContext
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def head_revision():
    from alembic.script import ScriptDirectory
    return ScriptDirectory.from_config(_config()).get_current_head()


def run(sql_only=False, verbose=True):
    """Bring the database to head, adopting Alembic first if needed."""
    from alembic import command
    from database import engine

    cfg = _config()

    if not _is_stamped(engine):
        if _has_any_table(engine):
            # The live case. The schema exists; record that it matches the
            # baseline rather than trying to build what is already there.
            if verbose:
                print("[MIGRATE] existing schema detected, stamping baseline")
            command.stamp(cfg, "0001_baseline")
        else:
            # Fresh database. main.py's create_all also handles this, but a
            # migration run must be able to stand alone (CI, a new
            # environment, a restored-from-backup database).
            if verbose:
                print("[MIGRATE] empty database, creating schema then stamping baseline")
            from database import Base
            import models  # noqa: F401  (registers the tables)
            Base.metadata.create_all(bind=engine)
            command.stamp(cfg, "0001_baseline")

    before = current_revision(engine)
    head = head_revision()
    if before == head:
        if verbose:
            print(f"[MIGRATE] already at head ({head})")
        return head

    if verbose:
        print(f"[MIGRATE] upgrading {before} -> {head}")
    command.upgrade(cfg, "head", sql=sql_only)
    return head_revision()


def status():
    from database import engine
    cur = current_revision(engine) if _is_stamped(engine) else None
    head = head_revision()
    print(f"current: {cur or '(not stamped)'}")
    print(f"head:    {head}")
    print("up to date" if cur == head else "PENDING MIGRATIONS")
    return 0 if cur == head else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Apply AMP schema migrations")
    p.add_argument("--status", action="store_true", help="report state, change nothing")
    p.add_argument("--sql", action="store_true", help="print SQL instead of executing")
    args = p.parse_args()
    if args.status:
        sys.exit(status())
    run(sql_only=args.sql)
