"""Every column added to an EXISTING table must reach production.

THE OUTAGE THIS EXISTS BECAUSE OF
---------------------------------
`models.User.is_active` was added with an Alembic migration (0005) and nothing
else. Two mechanisms were supposed to carry it to production and both missed:

  * Alembic — production does not run it on deploy. docs/MIGRATIONS.md says so
    outright: "Railway currently runs migrate.py MANUALLY". Nobody ran it.
  * create_all() — runs on every boot, but only creates missing TABLES. It has
    never altered an existing one, and `users` has existed since the first
    deploy.

So the column existed in the model and not in the database. SQLAlchemy names
every mapped column in its SELECT list, so EVERY query for a User became

    psycopg2.errors.UndefinedColumn: column users.is_active does not exist

and `/login` returned 500. Nobody could sign in to the product. `/health` kept
answering 200 the whole time, because it does not go through the ORM.

WHY THE WHOLE TEST SUITE STAYED GREEN
-------------------------------------
Every suite builds its schema with create_all against a fresh database, so
`users` is created WITH `is_active` — the one arrangement in which this defect
cannot appear. The bug lives exclusively in the gap between an old table and a
new model, and nothing was testing that gap.

So this file tests the gap directly, in two independent ways:

  1. STATICALLY — every `op.add_column` in a migration must have a matching
     `_ensure_column` in main.py, because that is the mechanism the deploy
     actually runs. (Columns inside `op.create_table` are exempt: create_all
     makes new tables.)
  2. FUNCTIONALLY — build the schema, DROP the columns to simulate the older
     production database, re-run main.py's boot migrations, and assert the
     schema is whole and a User query actually executes. That is the production
     failure, reproduced and then fixed, rather than asserted about.

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_boot_migrations.py
"""
import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VERSIONS = os.path.join(HERE, "alembic", "versions")

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def _first_string(node):
    """The literal value of a call argument, or None if it is not a literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def migration_added_columns():
    """{(table, column)} added by `op.add_column` across every migration.

    Deliberately NOT `op.create_table` columns: a table that does not exist yet
    is created whole by create_all on the next boot, so those need no helper.
    """
    found = set()
    for name in sorted(os.listdir(VERSIONS)):
        if not name.endswith(".py"):
            continue
        tree = ast.parse(io.open(os.path.join(VERSIONS, name), encoding="utf-8").read())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_column" and node.args):
                table = _first_string(node.args[0])
                column = None
                if len(node.args) > 1 and isinstance(node.args[1], ast.Call):
                    column = _first_string(node.args[1].args[0]) if node.args[1].args else None
                if table and column:
                    found.add((table, column))
    return found


def boot_ensured_columns():
    """{(table, column)} main.py adds at boot via _ensure_column / the users helper."""
    tree = ast.parse(io.open(os.path.join(HERE, "main.py"), encoding="utf-8").read())
    found = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_ensure_column" and len(node.args) >= 2):
            table, column = _first_string(node.args[0]), _first_string(node.args[1])
            if table and column:
                found.add((table, column))
    # users.tenant_code predates the generic helper and has its own function.
    found.add(("users", "tenant_code"))
    return found


def test_every_migration_column_is_also_added_at_boot():
    print("=" * 74)
    print("1. A MIGRATION ALONE DOES NOT REACH PRODUCTION")
    print("=" * 74)
    migrated = migration_added_columns()
    ensured = boot_ensured_columns()
    check("the scan found the known migration columns", len(migrated) >= 4, str(migrated))

    missing = sorted(migrated - ensured)
    check("every migration-added column has a boot-time _ensure_column",
          not missing,
          "NOT applied on production (Railway runs alembic by hand, and "
          f"create_all never alters an existing table): {missing}")

    # CONTROL: the check must be capable of failing. If `ensured` accidentally
    # contained everything — a bug in the parser, say — the assertion above
    # would pass vacuously forever.
    fake = migrated | {("users", "a_column_nobody_added")}
    check("CONTROL: the comparison detects a column with no boot helper",
          bool(fake - ensured))


def test_the_boot_migration_repairs_an_older_database():
    print()
    print("=" * 74)
    print("2. THE PRODUCTION FAILURE, REPRODUCED AND THEN REPAIRED")
    print("=" * 74)
    from sqlalchemy import inspect, text

    import models  # noqa: F401  (registers the mappers)
    from database import Base, engine, SessionLocal

    Base.metadata.create_all(bind=engine)

    columns = lambda t: {c["name"] for c in inspect(engine).get_columns(t)}
    check("the model declares users.is_active", "is_active" in models.User.__table__.c)

    # Simulate the production database: the table predates the column.
    with engine.begin() as conn:
        if "is_active" in columns("users"):
            conn.execute(text("ALTER TABLE users DROP COLUMN is_active"))
    check("simulated the older production schema (column dropped)",
          "is_active" not in columns("users"), str(sorted(columns("users"))))

    # THE FAILURE. Every User query names is_active, so all of them break --
    # which is why this was a 500 on /login and not a degraded feature.
    db = SessionLocal()
    try:
        db.query(models.User).first()
        check("a User query FAILS against the older schema", False,
              "it succeeded — the reproduction is wrong, not the fix")
    except Exception as e:
        check("a User query FAILS against the older schema (the reported 500)",
              "is_active" in str(e), str(e)[:120])
    finally:
        db.rollback()
        db.close()

    # THE REPAIR: exactly what main.py runs at boot, called by name rather than
    # re-typed, so this test cannot drift from the code it is guarding.
    import main
    main._ensure_column("users", "is_active",
                        "ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE")
    check("the boot migration added the column back", "is_active" in columns("users"))

    db = SessionLocal()
    try:
        db.query(models.User).first()          # the query that was 500-ing
        check("a User query now executes", True)
    except Exception as e:
        check("a User query now executes", False, str(e)[:160])
    finally:
        db.close()

    # And the DEFAULT is the safe one: an existing login stays able to act.
    db = SessionLocal()
    try:
        db.execute(text("INSERT INTO users (username, password, role, tenant_code) "
                        "VALUES ('boot_migration_probe', 'x', 'Admin', 'DEFAULT')"))
        db.commit()
        row = (db.query(models.User)
                 .filter(models.User.username == "boot_migration_probe").first())
        check("a row inserted without naming is_active defaults to ACTIVE",
              row is not None and row.is_active is True,
              str(row and row.is_active))
        # Revocation is still expressible — the default is a default, not a lock.
        row.is_active = False
        db.commit()
        db.refresh(row)
        check("CONTROL: is_active can still be set FALSE", row.is_active is False)
        db.delete(row)
        db.commit()
    finally:
        db.close()

    # Idempotent: boot runs it on every deploy.
    main._ensure_column("users", "is_active",
                        "ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE")
    check("running it a second time is a no-op, not an error",
          "is_active" in columns("users"))


def run_all():
    test_every_migration_column_is_also_added_at_boot()
    test_the_boot_migration_repairs_an_older_database()


def test_boot_migrations():
    run_all()
    assert not failures, failures


if __name__ == "__main__":
    run_all()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        sys.exit(1)
    print("EVERY MIGRATION COLUMN ALSO REACHES A DATABASE THAT PREDATES IT")
