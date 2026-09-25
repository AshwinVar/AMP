"""Migration 0013 on SQLite: the shape, the chain, and the round trip (ADR-0040).

The PostgreSQL half is verify_pg_gateway_credentials.py in the migration gate,
and it is the half that matters for the unique-constraint-over-a-nullable-column
question. This half is the one that can run on every developer's machine and in
the ordinary backend job:

  1. THE CHAIN      the tree has ONE head, 0013 is reachable from it and
                    follows 0012, and its upgrade side adds without destroying;
  2. THE SHAPE      gateway_credentials binds to one workspace and one site;
                    `site` is NOT NULL (NULL != NULL defeats a comparison);
                    `secret` is NOT NULL; `gateway_id` is unique INSTALLATION
                    wide, not per tenant;
  3. NO BACKFILL    production_records.source_record_id is nullable with no
                    default, so nothing invents a gateway for a record that was
                    typed in by a person;
  4. FROM THE PREVIOUS REVISION, with rows: a database at 0012 carrying real
                    production records upgrades, keeps every one, and gives them
                    all NULL;
  5. THE ROUND TRIP downgrade drops the table and the column and keeps the rows;
                    upgrade again restores them.

Driven against real file-backed SQLite databases in subprocesses, like
test_migration_0012_consent_scope.py.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_migration_0013_gateway_credentials.py
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
REVISION = "0013_gateway_credentials"
TABLE = "gateway_credentials"
COLUMN = "source_record_id"


def _json(out):
    """The JSON line from a subprocess's combined output.

    NOT the last line: alembic logs to stderr, `_run` concatenates both, and the
    migration's own INFO lines land after the result. Found by shape instead.
    """
    import json
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            return json.loads(line)
    raise AssertionError("no JSON result in subprocess output: " + out[-1500:])


def _run(script, url):
    env = dict(os.environ, DATABASE_URL=url, PYTHONPATH=HERE, PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                       env=env, cwd=HERE, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


class _TempDb:
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


# --------------------------------------------------------------------------
# 1. the chain
# --------------------------------------------------------------------------
def test_the_revision_is_in_the_chain_after_0012():
    src = _migration_source()
    m = re.search(r'^revision = "([^"]+)"', src, re.M)
    assert m and m.group(1) == REVISION, m and m.group(1)
    from alembic.script import ScriptDirectory
    import migrate
    script = ScriptDirectory.from_config(migrate._config())
    heads = script.get_heads()
    assert len(heads) == 1, f"the migration tree has {len(heads)} heads: {heads}"
    # In the chain, NOT at the head -- 0012's copy of this check asserted
    # `heads[0] == REVISION` and broke the moment this revision was written,
    # for the one reason that is not a defect. One head and reachable from it
    # are the invariants worth keeping.
    chain = {rev.revision for rev in script.walk_revisions("base", heads[0])}
    assert REVISION in chain, f"{REVISION} is not in the chain leading to {heads[0]}"
    assert _previous_revision() == "0012_consent_scope", _previous_revision()

    upgrade_side = src.split("def downgrade")[0]
    # The upgrade side must not destroy anything. A migration that drops or
    # rewrites is how a deployment loses a customer's history at 3am.
    for forbidden in ("drop_table", "drop_column", "alter_column", "UPDATE ", "DELETE "):
        assert forbidden not in upgrade_side, f"{REVISION}'s upgrade side uses {forbidden}"
    assert "op.create_table(" in upgrade_side and "op.add_column(" in upgrade_side
    # Guarded, because boot's create_all may have made both already.
    assert 'if "gateway_credentials" not in inspector.get_table_names()' in upgrade_side, \
        "the create_table is not guarded against a boot-made table"
    assert 'if "source_record_id" not in columns' in upgrade_side, \
        "the add_column is not guarded against a boot-made column"
    print(f"PASS {REVISION} is in the chain after 0012 and only adds, guarded")


# --------------------------------------------------------------------------
# 2. the shape of the credential
# --------------------------------------------------------------------------
def test_a_credential_binds_to_one_workspace_and_one_site():
    import models  # noqa: F401
    from database import Base
    assert TABLE in Base.metadata.tables, f"no model maps {TABLE}"
    t = Base.metadata.tables[TABLE]

    for name in ("tenant_code", "site", "gateway_id", "secret", "is_active"):
        assert name in t.columns, f"{TABLE} has no {name}"

    # NOT NULL, for the reason machines.site is: in PostgreSQL NULL != NULL, so
    # a nullable column cannot be compared for equality reliably, and the whole
    # point of this row is an equality comparison against the topic.
    assert not t.columns["site"].nullable, "site must be NOT NULL"
    assert t.columns["site"].default is not None or t.columns["site"].server_default is not None, \
        "site must default to '' rather than requiring every caller to pass it"
    assert not t.columns["tenant_code"].nullable, "tenant_code must be NOT NULL"
    assert not t.columns["secret"].nullable, "a credential with no secret authenticates nothing"
    assert not t.columns["is_active"].nullable, "revocation must not be ambiguous"

    uniques = [tuple(c.name for c in u.columns) for u in t.constraints
               if u.__class__.__name__ == "UniqueConstraint"]
    # INSTALLATION-wide, not per tenant: the id arrives in the payload before
    # AMP knows whose it is, so it must resolve to exactly one credential.
    assert ("gateway_id",) in uniques, f"gateway_id is not unique on its own: {uniques}"
    print("PASS a credential is NOT NULL where it must be, and gateway_id is globally unique")


# --------------------------------------------------------------------------
# 3. no backfill on production records
# --------------------------------------------------------------------------
def test_the_idempotency_column_invents_nothing():
    import models  # noqa: F401
    from database import Base
    t = Base.metadata.tables["production_records"]
    col = t.columns[COLUMN]
    assert col.nullable, "every record written by a person or a CSV has no gateway id"
    assert col.default is None and col.server_default is None, \
        "a default here would invent a gateway id and make every legacy row collide"
    uniques = [tuple(c.name for c in u.columns) for u in t.constraints
               if u.__class__.__name__ == "UniqueConstraint"]
    assert ("tenant_code", COLUMN) in uniques, \
        f"the idempotency key must be per workspace, not global: {uniques}"
    print("PASS source_record_id is nullable, defaultless, and unique per workspace")


# --------------------------------------------------------------------------
# 4. upgrade from the previous revision, with rows
# --------------------------------------------------------------------------
UPGRADE_FROM_PREVIOUS = '''
import json
from sqlalchemy import create_engine, inspect, text
from alembic import command

import models  # noqa: F401
import migrate
from database import Base

cfg = migrate._config()
engine = create_engine("%(url)s")

# create_all builds TODAY's shape; removing what 0013 adds stands in for the
# previous revision. The chain cannot be replayed from an empty file -- the
# early migrations assume the tables boot creates -- so this is the same
# stand-in test_migration_0012_consent_scope.py uses.
Base.metadata.create_all(bind=engine)
# SQLite refuses to DROP a column a unique constraint references, so
# production_records is rebuilt at its pre-0013 shape. That is a more honest
# stand-in than a column drop anyway: it is the table as 0012 left it.
PRE_0013_PRODUCTION = """
CREATE TABLE production_records (
    id INTEGER NOT NULL PRIMARY KEY,
    tenant_code VARCHAR NOT NULL,
    machine_id INTEGER,
    planned_minutes INTEGER NOT NULL,
    runtime_minutes INTEGER NOT NULL,
    ideal_cycle_time_seconds INTEGER NOT NULL,
    total_count INTEGER NOT NULL,
    good_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    created_at DATETIME,
    FOREIGN KEY(machine_id) REFERENCES machines (id)
)
"""
with engine.begin() as c:
    c.execute(text("DROP TABLE IF EXISTS %(table)s"))
    c.execute(text("DROP TABLE production_records"))
    c.execute(text(PRE_0013_PRODUCTION))

insp = inspect(engine)
had_table = "%(table)s" in insp.get_table_names()
had_column = "%(col)s" in {c["name"] for c in insp.get_columns("production_records")}

with engine.begin() as c:
    c.execute(text("INSERT INTO machines (tenant_code, site, name, status, utilization, downtime) "
                   "VALUES ('T','','CNC-01','Running',80,'0 min')"))
    for _ in range(3):
        c.execute(text("INSERT INTO production_records (tenant_code, machine_id, planned_minutes, "
                       "runtime_minutes, ideal_cycle_time_seconds, total_count, good_count, "
                       "rejected_count) SELECT 'T', id, 480, 400, 45, 100, 96, 4 FROM machines LIMIT 1"))

before = [tuple(r) for r in engine.connect().execute(text(
    "SELECT tenant_code, total_count, good_count FROM production_records ORDER BY id"))]

command.stamp(cfg, "%(prev)s")
command.upgrade(cfg, "head")

insp = inspect(engine)
after = [tuple(r) for r in engine.connect().execute(text(
    "SELECT tenant_code, total_count, good_count FROM production_records ORDER BY id"))]
nulls = engine.connect().execute(text(
    "SELECT count(*) FROM production_records WHERE source_record_id IS NULL")).scalar()
print(json.dumps({
    "had_table_before": had_table,
    "had_column_before": had_column,
    "has_table_after": "%(table)s" in insp.get_table_names(),
    "rows_kept": before == after,
    "row_count": len(after),
    "all_null": nulls == len(after),
    "column_present": "%(col)s" in {c["name"] for c in insp.get_columns("production_records")},
    "at_head": migrate.current_revision(engine) == migrate.head_revision(),
}))
'''


def test_upgrading_a_populated_database_keeps_every_row():
    with _TempDb() as url:
        rc, out = _run(UPGRADE_FROM_PREVIOUS % {"prev": _previous_revision(), "url": url,
                                                "table": TABLE, "col": COLUMN}, url)
        assert rc == 0, out[-2000:]
        result = _json(out)
        assert result["had_table_before"] is False, "the table existed before its own migration"
        assert result["had_column_before"] is False, "the column existed before its own migration"
        assert result["at_head"] is True, "the database did not end at head"
        assert result["has_table_after"] is True, "the migration did not create the table"
        assert result["column_present"] is True, "the migration did not add the column"
        assert result["row_count"] == 3, result
        assert result["rows_kept"] is True, "a production record changed during the upgrade"
        assert result["all_null"] is True, "the migration backfilled a gateway id it invented"
    print("PASS a populated database at 0012 upgrades, keeps every row, and backfills nothing")


# --------------------------------------------------------------------------
# 5. the round trip
# --------------------------------------------------------------------------
ROUND_TRIP = '''
import json
from sqlalchemy import create_engine, inspect, text
from alembic import command

import models  # noqa: F401
import migrate
from database import Base

cfg = migrate._config()
engine = create_engine("%(url)s")
Base.metadata.create_all(bind=engine)
command.stamp(cfg, "head")

with engine.begin() as c:
    c.execute(text("INSERT INTO machines (tenant_code, site, name, status, utilization, downtime) "
                   "VALUES ('T','','CNC-01','Running',80,'0 min')"))
    c.execute(text("INSERT INTO production_records (tenant_code, machine_id, planned_minutes, "
                   "runtime_minutes, ideal_cycle_time_seconds, total_count, good_count, "
                   "rejected_count, source_record_id) SELECT 'T', id, 30, 28, 45, 7, 7, 0, 'rec-1' "
                   "FROM machines LIMIT 1"))
    c.execute(text("INSERT INTO %(table)s (tenant_code, site, gateway_id, secret, is_active) "
                   "VALUES ('T','plant-1','gw-1','k',1)"))

command.downgrade(cfg, "-1")
insp = inspect(engine)
after_down = {
    "table_gone": "%(table)s" not in insp.get_table_names(),
    "column_gone": "%(col)s" not in {c["name"] for c in insp.get_columns("production_records")},
    "records_kept": engine.connect().execute(text("SELECT count(*) FROM production_records")).scalar(),
}
command.upgrade(cfg, "head")
insp = inspect(engine)
after_up = {
    "table_back": "%(table)s" in insp.get_table_names(),
    "column_back": "%(col)s" in {c["name"] for c in insp.get_columns("production_records")},
    "credentials": engine.connect().execute(text("SELECT count(*) FROM %(table)s")).scalar(),
    "records": engine.connect().execute(text("SELECT count(*) FROM production_records")).scalar(),
}
print(json.dumps({"down": after_down, "up": after_up}))
'''


def test_downgrade_keeps_the_production_records():
    with _TempDb() as url:
        rc, out = _run(ROUND_TRIP % {"url": url, "table": TABLE, "col": COLUMN}, url)
        assert rc == 0, out[-2000:]
        result = _json(out)
        assert result["down"]["table_gone"], "downgrade left the table behind"
        assert result["down"]["column_gone"], "downgrade left the column behind"
        # THE POINT: dropping the column must not drop the row it was on.
        assert result["down"]["records_kept"] == 1, \
            f"downgrade deleted a production record: {result['down']}"
        assert result["up"]["table_back"] and result["up"]["column_back"], result["up"]
        # The credentials are GONE and cannot come back — the keys live on plant
        # PCs and AMP has no other copy. The migration's docstring says so; this
        # pins that it is true rather than aspirational.
        assert result["up"]["credentials"] == 0, \
            "credentials survived a downgrade, so the docstring is wrong about re-issuing"
        assert result["up"]["records"] == 1, result["up"]
    print("PASS downgrade drops the table and the column, keeps the records, and re-upgrades")


if __name__ == "__main__":
    test_the_revision_is_in_the_chain_after_0012()
    test_a_credential_binds_to_one_workspace_and_one_site()
    test_the_idempotency_column_invents_nothing()
    test_upgrading_a_populated_database_keeps_every_row()
    test_downgrade_keeps_the_production_records()
    print("ALL MIGRATION 0013 TESTS PASSED")
