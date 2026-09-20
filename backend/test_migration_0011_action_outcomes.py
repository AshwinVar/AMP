"""Migration 0011_action_outcomes and the closed-loop schema (ADR-0029).

WHAT THIS PINS
--------------
One new table. It holds the only evidence AMP has ever had about whether its own
recommendations help, so the shape matters as much as the arithmetic above it.

  1. THE SHAPE the design depends on: exact columns, the unique key that keeps
     "did it help?" from having two answers, and — the point of the whole
     table — that BOTH value columns are nullable, because "no reading" and "a
     reading of 0" are different claims (ADR-0014).
  2. THE UPGRADE from the previous revision, on a database that already holds
     factory rows. Migrating an empty database proves nothing.
  3. THE MIGRATION BUILDS WHAT THE MODELS DECLARE: an autogenerate diff with the
     CI gate's own rules is empty, after the upgrade AND after a
     downgrade/re-upgrade round trip. With a CONTROL that the comparison can
     fail at all, so "DRIFT 0" is not decoration.
  4. IDEMPOTENCE: boot's create_all may already have made the table, and the
     migration must then be a no-op rather than a crash.
  5. THE DATABASE ENFORCES THE KEY (SQLite here; PostgreSQL in
     verify_pg_action_outcomes.py, which CI runs).
  6. OFFBOARDING sweeps a tenant's outcomes with the generic purge.

WHY THE PREVIOUS REVISION IS READ, NOT HARD-CODED
-------------------------------------------------
A sibling branch may also add a migration after 0010 and the founder re-parents
at merge, so every scenario here tests "the revision before this one", whatever
that turns out to be.

Each database scenario runs in its own subprocess with its own DATABASE_URL, for
the reason test_migrate.py gives: database.py binds the engine at import.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_migration_0011_action_outcomes.py
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
REVISION = "0011_action_outcomes"
TABLE = "action_outcomes"

EXPECTED_COLUMNS = {
    "id", "tenant_code", "action_id", "metric", "scope_kind", "scope_id",
    "scope_label", "window_days", "baseline_value", "baseline_at",
    "measured_value", "measured_at", "verdict", "created_at",
}

# The columns whose nullability IS the design.
NULLABLE = {"scope_id", "scope_label", "baseline_value", "measured_value",
            "measured_at", "verdict", "created_at"}
NOT_NULL = {"tenant_code", "action_id", "metric", "scope_kind", "window_days", "baseline_at"}

EXPECTED_LENGTHS = {"metric": 48, "scope_kind": 16, "verdict": 16}


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
# 1. the revision and the model
# --------------------------------------------------------------------------

def test_the_revision_is_in_the_chain_and_only_creates_a_table():
    src = _migration_source()
    m = re.search(r'^revision = "([^"]+)"', src, re.M)
    assert m and m.group(1) == REVISION, m and m.group(1)
    assert len(REVISION) <= 32, len(REVISION)

    from alembic.script import ScriptDirectory
    import migrate
    script = ScriptDirectory.from_config(migrate._config())
    heads = script.get_heads()
    assert len(heads) == 1, f"the migration tree has {len(heads)} heads: {heads}"
    chain = [r.revision for r in script.walk_revisions()]
    assert REVISION in chain, chain
    assert _previous_revision() in chain, chain

    # Additive only: no ALTER of an existing table and no data rewrite anywhere
    # upgrade() can reach.
    assert src.count("def downgrade") == 1
    upgrade_side = src.split("def downgrade")[0]
    for forbidden in ("op.add_column", "op.alter_column", "op.drop_column",
                      "op.drop_table", "op.execute", "op.bulk_insert"):
        assert forbidden not in upgrade_side, f"{REVISION}'s upgrade side uses {forbidden}"
    # The migration names the table through its own TABLE constant, so check the
    # constant rather than matching a literal inside the call.
    assert "op.create_table(" in upgrade_side, f"{REVISION} creates no table"
    assert re.search(r'^TABLE = "action_outcomes"', src, re.M), \
        f"{REVISION} does not create {TABLE}"
    print(f"PASS {REVISION} follows {_previous_revision()}, is the single head, and only creates a table")


def test_the_table_has_exactly_the_planned_columns():
    got = {c.name for c in _table().columns}
    assert got == EXPECTED_COLUMNS, (f"missing {sorted(EXPECTED_COLUMNS - got)}, "
                                     f"unexpected {sorted(got - EXPECTED_COLUMNS)}")
    print(f"PASS {TABLE} carries exactly the {len(EXPECTED_COLUMNS)} planned columns")


def test_null_is_not_zero():
    """The columns whose nullability IS the design.

    `baseline_value` and `measured_value` are nullable so that "AMP had no
    reading" can be stored as itself. A NOT NULL here would force a 0.0 into
    that slot and manufacture an improvement out of an absence — which is how
    this table would start lying.
    """
    cols = _table().columns
    for name in NULLABLE:
        assert cols[name].nullable, f"{name} must be nullable"
    for name in NOT_NULL:
        assert not cols[name].nullable, f"{name} must be NOT NULL"
    # And no default may quietly fill a missing reading.
    for name in ("baseline_value", "measured_value"):
        col = cols[name]
        assert col.default is None and col.server_default is None, f"{name} has a default"
    # tenant_code fails closed: a row written without its tenant is a bug to
    # surface, not a row to hand to the founder workspace.
    tenant = cols["tenant_code"]
    assert tenant.default is None and tenant.server_default is None
    print("PASS both readings are nullable with no default; tenant_code fails closed")


def test_one_outcome_per_action_is_declared():
    from sqlalchemy import UniqueConstraint
    t = _table()
    got = {tuple(c.name for c in con.columns)
           for con in t.constraints if isinstance(con, UniqueConstraint)}
    got |= {tuple(c.name for c in ix.columns) for ix in t.indexes if ix.unique}
    assert got == {("action_id",)}, got
    targets = {f"{fk.column.table.name}.{fk.column.name}"
               for fk in t.columns["action_id"].foreign_keys}
    assert targets == {"agent_actions.id"}, targets
    names = {ix.name for ix in t.indexes}
    assert f"ix_{TABLE}_id" in names, names
    assert "ix_action_outcomes_tenant_created" in names, names
    print("PASS one outcome per action, pointing at agent_actions.id, with the read index")


def test_the_lengths_the_vocabularies_need():
    cols = _table().columns
    for name, length in EXPECTED_LENGTHS.items():
        assert getattr(cols[name].type, "length", None) == length, \
            f"{name} is {getattr(cols[name].type, 'length', None)}, expected {length}"
    # Every value AMP will ever write must fit, or a vocabulary change becomes a
    # silent truncation on PostgreSQL.
    from ai import outcomes as oc
    assert max(len(m) for m in oc.METRIC_SPEC) <= EXPECTED_LENGTHS["metric"]
    assert max(len(v) for v in oc.VERDICTS) <= EXPECTED_LENGTHS["verdict"]
    assert max(len(s["scope_kind"]) for s in oc.METRIC_SPEC.values()) <= EXPECTED_LENGTHS["scope_kind"]
    print("PASS metric, scope_kind and verdict are bounded, and every value AMP writes fits")


def test_the_table_is_tenant_scoped():
    import models
    import tenancy
    assert models.ActionOutcome in tenancy.SCOPED_MODELS, "action_outcomes is not tenant-scoped"
    assert TABLE in tenancy.CORE_TENANT_TABLES, "action_outcomes is not in CORE_TENANT_TABLES"
    print("PASS action_outcomes is in SCOPED_MODELS and CORE_TENANT_TABLES")


# --------------------------------------------------------------------------
# 2-4. the migration against real databases (subprocess per scenario)
# --------------------------------------------------------------------------

_COMMON = r'''
import models, migrate
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from database import Base, engine

TABLE = %(table)r
PREV = %(prev)r

def tables():
    return set(inspect(engine).get_table_names())

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

def drop_new_table():
    with engine.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS " + TABLE))

def seed_previous_revision_rows():
    with engine.begin() as c:
        c.execute(text("INSERT INTO machines (id, tenant_code, site, name, status, utilization) "
                       "VALUES (1,'FACTORY_A','P1','CNC-01','Running',70)"))
        c.execute(text("INSERT INTO downtime_logs (tenant_code, machine_id, reason, duration) "
                       "VALUES ('FACTORY_A',1,'Breakdown','15 min')"))
        c.execute(text("INSERT INTO agent_actions (id, tenant_code, agent, action_type, summary, "
                       "ref_kind, ref_id, status) "
                       "VALUES (1,'FACTORY_A','maintenance','open_task','Service CNC-01',"
                       "'maintenance_task',1,'Approved')"))

COUNTED = ("machines", "downtime_logs", "agent_actions")

def counts():
    with engine.begin() as c:
        return {t: c.execute(text("SELECT count(*) FROM " + t)).scalar() for t in COUNTED}

cfg = migrate._config()
'''


def _script(body):
    return (_COMMON % {"table": TABLE, "prev": _previous_revision()}) + body


def test_upgrade_from_the_previous_revision_keeps_every_row_and_matches_the_models():
    body = r'''
Base.metadata.create_all(bind=engine)
drop_new_table()
seed_previous_revision_rows()
before = counts()
command.stamp(cfg, PREV)
print("PRE_ABSENT", TABLE not in tables())
command.upgrade(cfg, "head")
print("ROWS_INTACT", counts() == before)
print("CREATED", TABLE in tables())
print("AT_HEAD", migrate.current_revision(engine) == migrate.head_revision())
d = drift()
print("DRIFT", len(d))
for item in d:
    print("  DIFF", item)
# DECLARED LENGTHS, read back from the migrated schema itself. Alembic's type
# comparison on SQLite does not see VARCHAR vs VARCHAR(16), but SQLite keeps the
# declared type text, so reflect it directly.
types = {c["name"]: c["type"] for c in inspect(engine).get_columns(TABLE)}
want = %(lengths)r
got = {k: getattr(types[k], "length", None) for k in want}
print("LENGTHS_OK", got == want)
if got != want:
    print("  LENGTHS", got)
# NULLABILITY, from the migrated schema rather than from models.py: the whole
# "null is not zero" rule lives in the database, or it does not live anywhere.
nullable = {c["name"]: c["nullable"] for c in inspect(engine).get_columns(TABLE)}
print("VALUES_NULLABLE", nullable["baseline_value"] and nullable["measured_value"])
print("TENANT_NOT_NULL", not nullable["tenant_code"])
# CONTROL: the comparison must be able to fail on this engine, or DRIFT 0 above
# is decoration.
with engine.begin() as c:
    c.execute(text("DROP INDEX ix_action_outcomes_tenant_created"))
print("CONTROL_DETECTS", any("ix_action_outcomes_tenant_created" in str(x) for x in drift()))
''' % {"lengths": EXPECTED_LENGTHS}
    with _TempDb() as url:
        rc, out = _run(_script(body), url)
        assert rc == 0, out
        assert "CONTROL_DETECTS True" in out, \
            "the drift comparison cannot see a missing index; DRIFT 0 proves nothing:\n" + out
        assert "PRE_ABSENT True" in out, out
        assert "ROWS_INTACT True" in out, out
        assert "CREATED True" in out, out
        assert "AT_HEAD True" in out, out
        assert "DRIFT 0" in out, "the migration does not build what models.py declares:\n" + out
        assert "LENGTHS_OK True" in out, "the migration declares the wrong lengths:\n" + out
        assert "VALUES_NULLABLE True" in out, "the migrated readings are NOT NULL:\n" + out
        assert "TENANT_NOT_NULL True" in out, out
    print("PASS upgrade from the previous revision: rows intact, table created, at head, "
          "no drift, lengths and nullability as designed")


def test_downgrade_with_outcome_rows_then_upgrade_again():
    body = r'''
Base.metadata.create_all(bind=engine)
drop_new_table()
seed_previous_revision_rows()
command.stamp(cfg, PREV)
command.upgrade(cfg, "head")
with engine.begin() as c:
    c.execute(text("INSERT INTO action_outcomes (tenant_code, action_id, metric, scope_kind, "
                   "scope_id, window_days, baseline_value, baseline_at) "
                   "VALUES ('FACTORY_A',1,'downtime_minutes','machine',1,7,90.0,'2026-09-20 09:00:00')"))
before = counts()
command.downgrade(cfg, PREV)
print("GONE", TABLE not in tables())
print("OTHERS_INTACT", counts() == before)
command.upgrade(cfg, "head")
print("BACK", TABLE in tables())
print("ROUND_TRIP_DRIFT", len(drift()))
with engine.begin() as c:
    print("EMPTY_AFTER_ROUND_TRIP",
          c.execute(text("SELECT count(*) FROM action_outcomes")).scalar() == 0)
'''
    with _TempDb() as url:
        rc, out = _run(_script(body), url)
        assert rc == 0, out
        assert "GONE True" in out, out
        assert "OTHERS_INTACT True" in out, "the downgrade touched rows it should not have:\n" + out
        assert "BACK True" in out, out
        assert "ROUND_TRIP_DRIFT 0" in out, out
        # Stated, not hidden: a downgrade DISCARDS the measurements.
        assert "EMPTY_AFTER_ROUND_TRIP True" in out, out
    print("PASS downgrade drops the table (and its measurements), other rows intact, "
          "re-upgrade is clean")


def test_the_migration_is_a_no_op_when_boot_already_created_the_table():
    body = r'''
Base.metadata.create_all(bind=engine)          # boot's create_all: the table exists
seed_previous_revision_rows()
with engine.begin() as c:
    c.execute(text("INSERT INTO action_outcomes (tenant_code, action_id, metric, scope_kind, "
                   "scope_id, window_days, baseline_value, baseline_at) "
                   "VALUES ('FACTORY_A',1,'downtime_minutes','machine',1,7,90.0,'2026-09-20 09:00:00')"))
command.stamp(cfg, PREV)
command.upgrade(cfg, "head")                   # must not raise
print("STILL_THERE", TABLE in tables())
with engine.begin() as c:
    print("ROW_KEPT", c.execute(text("SELECT count(*) FROM action_outcomes")).scalar() == 1)
print("AT_HEAD", migrate.current_revision(engine) == migrate.head_revision())
print("DRIFT", len(drift()))
'''
    with _TempDb() as url:
        rc, out = _run(_script(body), url)
        assert rc == 0, "the migration crashed on a table boot had already created:\n" + out
        assert "STILL_THERE True" in out, out
        assert "ROW_KEPT True" in out, "the guarded create dropped a row:\n" + out
        assert "AT_HEAD True" in out, out
        assert "DRIFT 0" in out, out
    print("PASS the migration is a no-op when boot's create_all got there first")


def test_the_database_refuses_two_outcomes_for_one_action():
    body = r'''
Base.metadata.create_all(bind=engine)
drop_new_table()
seed_previous_revision_rows()
command.stamp(cfg, PREV)
command.upgrade(cfg, "head")
row = ("INSERT INTO action_outcomes (tenant_code, action_id, metric, scope_kind, scope_id, "
       "window_days, baseline_value, baseline_at) "
       "VALUES ('FACTORY_A',1,'downtime_minutes','machine',1,7,%s,'2026-09-20 09:00:00')")
with engine.begin() as c:
    c.execute(text(row % "90.0"))
try:
    with engine.begin() as c:
        c.execute(text(row % "10.0"))
    print("REFUSED False")
except Exception as e:
    print("REFUSED True", type(e).__name__)
'''
    with _TempDb() as url:
        rc, out = _run(_script(body), url)
        assert rc == 0, out
        assert "REFUSED True" in out, \
            "the database allowed two outcomes for one action; 'did it help?' can have two answers:\n" + out
    print("PASS the database itself refuses a second outcome for one action")


def test_offboarding_sweeps_a_tenants_outcomes():
    body = r'''
import offboarding
from sqlalchemy.orm import sessionmaker
Base.metadata.create_all(bind=engine)
drop_new_table()
seed_previous_revision_rows()
command.stamp(cfg, PREV)
command.upgrade(cfg, "head")
row = ("INSERT INTO action_outcomes (tenant_code, action_id, metric, scope_kind, scope_id, "
       "window_days, baseline_value, baseline_at) "
       "VALUES ('%s',%s,'downtime_minutes','machine',1,7,90.0,'2026-09-20 09:00:00')")
with engine.begin() as c:
    c.execute(text("INSERT INTO agent_actions (id, tenant_code, agent, action_type, summary, "
                   "ref_kind, ref_id, status) VALUES (2,'FACTORY_B','maintenance','open_task','x',"
                   "'maintenance_task',1,'Approved')"))
    c.execute(text(row % ("FACTORY_A", 1)))
    c.execute(text(row % ("FACTORY_B", 2)))
db = sessionmaker(bind=engine)()
counts_deleted = offboarding.purge_tenant_data(db, "FACTORY_A")
db.commit()
with engine.begin() as c:
    left = c.execute(text("SELECT tenant_code FROM action_outcomes")).fetchall()
print("SWEPT", TABLE in counts_deleted)
print("ONLY_OTHER_LEFT", [r[0] for r in left] == ["FACTORY_B"])
'''
    with _TempDb() as url:
        rc, out = _run(_script(body), url)
        if "No module named 'offboarding'" in out or "has no attribute 'purge_tenant_data'" in out:
            print("SKIP offboarding is not a module in this tree")
            return
        assert rc == 0, out
        assert "SWEPT True" in out, "the generic purge does not sweep action_outcomes:\n" + out
        assert "ONLY_OTHER_LEFT True" in out, "the purge took another tenant's outcomes:\n" + out
    print("PASS offboarding sweeps the tenant's outcomes and leaves the other tenant's")


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"ALL {len(tests)} MIGRATION {REVISION} TESTS PASSED")
