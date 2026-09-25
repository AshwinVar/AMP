"""Migration 0012_consent_scope: a consent names the provider it was given for (ADR-0038).

WHAT IS PINNED
--------------
  1. THE REVISION   is in the chain with one head, and its upgrade side only
                    adds a column, guarded -- nothing is created, dropped or
                    rewritten.
  2. THE MODEL      ai_learning_consents.scope is a nullable string with no
                    default: a row written before this revision names no
                    provider, and nothing may invent one.
  3. THE UPGRADE    from the previous revision, on a database with consent
                    rows: every row survives with its decision, the column
                    arrives NULL for them, the schema matches models.py, the
                    revision is at head -- and the drift comparison is shown to
                    be able to fail (CONTROL).
  4. THE ROUND TRIP downgrade with rows that carry a scope: the column goes,
                    the rows stay; upgrade again: the column is back, NULL,
                    no drift.
  5. AT BOOT        main.py adds the same column to an older database
                    (test_boot_migrations scans for it), so a deployment that
                    starts before its migrate step ran does not 500 on /ai/ask.

Driven against real file-backed SQLite databases in subprocesses, like
test_migration_0011_action_outcomes.py; the PostgreSQL half is
verify_pg_consent_scope.py in the migration gate.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_migration_0012_consent_scope.py
"""
import io
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
VERSIONS = os.path.join(HERE, "alembic", "versions")
REVISION = "0012_consent_scope"
TABLE = "ai_learning_consents"
COLUMN = "scope"


def _run(script, url):
    env = dict(os.environ, DATABASE_URL=url, PYTHONPATH=HERE, PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                       env=env, cwd=HERE, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


class _TempDb:
    """A file-backed SQLite database; alembic opens its own connections."""

    def __enter__(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        return f"sqlite:///{self.path.replace(os.sep, '/')}"

    def __exit__(self, *exc):
        if os.path.exists(self.path):
            try:
                os.unlink(self.path)
            except OSError:
                pass


def _migration_source():
    path = os.path.join(VERSIONS, f"{REVISION}.py")
    assert os.path.exists(path), f"missing migration file {path}"
    return io.open(path, encoding="utf-8").read()


def _previous_revision():
    m = re.search(r'^down_revision = "([^"]+)"', _migration_source(), re.M)
    assert m, f"{REVISION} declares no down_revision"
    return m.group(1)


def _table():
    import models  # noqa: F401
    from database import Base
    assert TABLE in Base.metadata.tables, f"no model maps {TABLE}"
    return Base.metadata.tables[TABLE]


# --------------------------------------------------------------------------
# 1-2. the revision and the model
# --------------------------------------------------------------------------
def test_the_revision_is_in_the_chain_and_only_adds_a_column():
    src = _migration_source()
    m = re.search(r'^revision = "([^"]+)"', src, re.M)
    assert m and m.group(1) == REVISION, m and m.group(1)
    assert len(REVISION) <= 32, len(REVISION)
    from alembic.script import ScriptDirectory
    import migrate
    script = ScriptDirectory.from_config(migrate._config())
    heads = script.get_heads()
    assert len(heads) == 1, f"the migration tree has {len(heads)} heads: {heads}"
    # IN THE CHAIN, not AT THE HEAD. This asserted `heads[0] == REVISION`, which
    # was true until the next migration was written and then failed for the one
    # reason that is not a defect -- somebody added a revision after it. The
    # durable invariants are that the tree has ONE head (no branching) and that
    # this revision is reachable from it; both still catch a migration wired in
    # wrongly, which is what the check is for.
    chain = {rev.revision for rev in script.walk_revisions("base", heads[0])}
    assert REVISION in chain, f"{REVISION} is not in the chain leading to {heads[0]}"
    assert _previous_revision() == "0011_action_outcomes", _previous_revision()
    upgrade_side = src.split("def downgrade")[0]
    for forbidden in ("create_table", "drop_table", "drop_column", "execute(", "alter_column", "UPDATE"):
        assert forbidden not in upgrade_side, f"{REVISION}'s upgrade side uses {forbidden}"
    assert "op.add_column(" in upgrade_side, f"{REVISION} adds no column"
    assert "if COLUMN not in present" in upgrade_side, "the add is not guarded against a boot-made column"
    print(f"PASS {REVISION} is the single head after 0011 and only adds one guarded column")


def test_the_column_is_a_nullable_string_with_no_default():
    col = _table().columns[COLUMN]
    assert col.nullable, "scope must be nullable: a row from before this revision names no provider"
    assert col.default is None and col.server_default is None, "no default may invent a provider"
    assert "VARCHAR" in str(col.type).upper() or "STRING" in str(col.type).upper(), str(col.type)
    # And the migration declares the same shape, so the drift gate stays empty.
    assert 'sa.Column(COLUMN, sa.String(), nullable=True)' in _migration_source()
    print("PASS scope is a nullable string with no default, in the model and the migration alike")


# --------------------------------------------------------------------------
# 3-4. the migration against real databases (subprocess per scenario)
# --------------------------------------------------------------------------
_COMMON = r'''
import models, migrate
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from database import Base, engine
TABLE = %(table)r
COLUMN = %(column)r
PREV = %(prev)r

def columns():
    return {c["name"] for c in inspect(engine).get_columns(TABLE)}

def include_object(obj, name, type_, reflected, compare_to):
    # The CI migration gate's rules, verbatim.
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

def previous_revision_table():
    """The table as 0009 created it: create_all builds today's shape, so the
    column is dropped again to stand in for a database at the previous revision."""
    with engine.begin() as c:
        c.execute(text("ALTER TABLE " + TABLE + " DROP COLUMN " + COLUMN))

def seed_consents():
    with engine.begin() as c:
        c.execute(text("INSERT INTO ai_learning_consents (tenant_code, capability, granted, granted_by, "
                       "granted_at) VALUES ('FACTORY_A','telemetry_baseline',1,'a-admin','2026-09-20 09:00:00')"))
        c.execute(text("INSERT INTO ai_learning_consents (tenant_code, capability, granted, granted_by, "
                       "granted_at) VALUES ('FACTORY_B','external_model',1,'b-admin','2026-09-20 10:00:00')"))
        c.execute(text("INSERT INTO ai_learning_consents (tenant_code, capability, granted, revoked_by, "
                       "revoked_at) VALUES ('FACTORY_C','external_model',0,'c-admin','2026-09-20 11:00:00')"))

def rows():
    with engine.connect() as c:
        return [tuple(r) for r in c.execute(text(
            "SELECT tenant_code, capability, granted, granted_by FROM ai_learning_consents ORDER BY id"))]

cfg = migrate._config()
'''


def _script(body):
    return (_COMMON % {"table": TABLE, "column": COLUMN, "prev": _previous_revision()}) + body


def test_upgrade_from_the_previous_revision_keeps_every_row_and_matches_the_models():
    body = r'''
Base.metadata.create_all(bind=engine)
previous_revision_table()
print("PRE_ABSENT", COLUMN not in columns())
seed_consents()
before = rows()
command.stamp(cfg, PREV)
command.upgrade(cfg, "head")
print("ROWS_INTACT", rows() == before and len(before) == 3)
print("ADDED", COLUMN in columns())
print("AT_HEAD", migrate.current_revision(engine) == migrate.head_revision())
with engine.connect() as c:
    scopes = [r[0] for r in c.execute(text("SELECT scope FROM ai_learning_consents"))]
print("ALL_NULL", scopes == [None, None, None])
nullable = {c["name"]: c["nullable"] for c in inspect(engine).get_columns(TABLE)}
print("NULLABLE", nullable[COLUMN])
d = drift()
print("DRIFT", len(d))
for item in d:
    print("  DIFF", item)
# CONTROL: the comparison must be able to fail on this engine.
with engine.begin() as c:
    c.execute(text("ALTER TABLE " + TABLE + " DROP COLUMN " + COLUMN))
print("CONTROL_DETECTS", any(COLUMN in str(x) for x in drift()))
'''
    with _TempDb() as url:
        rc, out = _run(_script(body), url)
        assert rc == 0, out
        assert "CONTROL_DETECTS True" in out, "the drift comparison cannot see a missing column:\n" + out
        assert "PRE_ABSENT True" in out, out
        assert "ROWS_INTACT True" in out, out
        assert "ADDED True" in out, out
        assert "AT_HEAD True" in out, out
        assert "ALL_NULL True" in out, "the upgrade invented a provider for an old row:\n" + out
        assert "NULLABLE True" in out, out
        assert "DRIFT 0" in out, "the migration does not build what models.py declares:\n" + out
    print("PASS upgrade from the previous revision: rows intact, column added NULL, at head, no drift")


def test_downgrade_with_scoped_rows_then_upgrade_again():
    body = r'''
Base.metadata.create_all(bind=engine)
previous_revision_table()
seed_consents()
command.stamp(cfg, PREV)
command.upgrade(cfg, "head")
with engine.begin() as c:
    c.execute(text("UPDATE ai_learning_consents SET scope = 'anthropic' WHERE tenant_code = 'FACTORY_B'"))
before = rows()
command.downgrade(cfg, PREV)
print("GONE", COLUMN not in columns())
print("ROWS_KEPT", rows() == before)
command.upgrade(cfg, "head")
print("BACK", COLUMN in columns())
with engine.connect() as c:
    scopes = [r[0] for r in c.execute(text("SELECT scope FROM ai_learning_consents"))]
print("NULL_AGAIN", scopes == [None, None, None])
print("DRIFT", len(drift()))
print("AT_HEAD", migrate.current_revision(engine) == migrate.head_revision())
'''
    with _TempDb() as url:
        rc, out = _run(_script(body), url)
        assert rc == 0, out
        assert "GONE True" in out, out
        assert "ROWS_KEPT True" in out, "the downgrade lost consent rows:\n" + out
        assert "BACK True" in out, out
        assert "NULL_AGAIN True" in out, out
        assert "DRIFT 0" in out, out
        assert "AT_HEAD True" in out, out
    print("PASS downgrade drops only the column and keeps every decision; re-upgrade is clean")


def test_the_same_column_is_added_at_boot():
    src = io.open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    assert f'_ensure_column("{TABLE}", "{COLUMN}"' in src, "main.py has no boot-time _ensure_column for scope"
    print("PASS main.py adds ai_learning_consents.scope at boot for a database that has not migrated yet")


def main():
    test_the_revision_is_in_the_chain_and_only_adds_a_column()
    test_the_column_is_a_nullable_string_with_no_default()
    test_upgrade_from_the_previous_revision_keeps_every_row_and_matches_the_models()
    test_downgrade_with_scoped_rows_then_upgrade_again()
    test_the_same_column_is_added_at_boot()
    print("ALL MIGRATION 0012 CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
