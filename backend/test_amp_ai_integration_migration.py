"""Migration 0009_native_ai_consent: the per-tenant learning-consent table (ADR-0020).

WHAT THIS PINS
--------------
AMP-native AI may learn from a tenant's own data only with that tenant's explicit,
stored, revocable consent. The consent lives in ``ai_learning_consents``. This
suite proves the table arrives the two ways a real database meets it:

  1. FRESH   an empty database is built by migrate.run(), stamped at head, and
             has the table with its unique (tenant_code, capability) constraint.
  2. UPGRADE a database that is ALREADY at 0008 with live rows (a machine, an
             audit record) upgrades to head without touching those rows; a
             downgrade drops only the consent table; upgrading again restores it.

Plus: the migrated table matches models.py (an autogenerate diff limited to this
table is empty, so a model/migration mismatch fails here and not only on the
PostgreSQL gate), the database itself refuses a second consent row for the same
(tenant, capability), and the revision id fits alembic_version's VARCHAR(32).

Each scenario runs in its own subprocess with its own DATABASE_URL, for the
reason test_migrate.py gives: database.py binds its engine at import.

PostgreSQL: verify_pg_native_ai.py runs the same upgrade against a live
PostgreSQL database; CI's migration gate runs the drift check there.

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_integration_migration.py
"""
import io
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REVISION = "0009_native_ai_consent"
PARENT = "0008_machine_claim"
TABLE = "ai_learning_consents"

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


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


# Shared by the subprocess scripts: describe the consent table as the database sees it.
_DESCRIBE = r'''
def describe(engine):
    from sqlalchemy import inspect
    insp = inspect(engine)
    names = set(insp.get_table_names())
    if "ai_learning_consents" not in names:
        print("TABLE False")
        return
    print("TABLE True")
    cols = {c["name"]: c for c in insp.get_columns("ai_learning_consents")}
    print("COLUMNS", ",".join(sorted(cols)))
    print("NOTNULL", ",".join(sorted(n for n, c in cols.items() if not c["nullable"])))
    print("UNIQUES", ",".join(sorted("%s(%s)" % (u["name"], "+".join(u["column_names"]))
                                     for u in insp.get_unique_constraints("ai_learning_consents"))))
    print("INDEXES", ",".join(sorted(i["name"] for i in insp.get_indexes("ai_learning_consents"))))

def drift(engine):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    import models  # noqa: F401
    from database import Base
    def include_object(obj, name, type_, reflected, compare_to):
        if type_ == "table" and name == "alembic_version":
            return False
        if type_ == "index" and reflected and compare_to is None:
            return False
        return True
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"include_object": include_object,
                                                     "compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    mine = [d for d in diff if "ai_learning_consents" in repr(d)]
    print("DRIFT", len(mine), repr(mine)[:600])

def duplicate_refused(engine):
    from sqlalchemy import text
    ins = text("INSERT INTO ai_learning_consents (tenant_code, capability, granted) "
               "VALUES ('TA', 'telemetry_baseline', 0)")
    first = None
    try:
        with engine.begin() as c:
            c.execute(ins)
    except Exception as e:
        first = str(e)[:120]
    print("FIRST_INSERT_OK", first is None, first)
    refused = False
    try:
        with engine.begin() as c:
            c.execute(ins)
    except Exception:
        refused = True
    print("DUPLICATE_REFUSED", refused)
    other = None
    try:
        with engine.begin() as c:
            c.execute(text("INSERT INTO ai_learning_consents (tenant_code, capability, granted) "
                           "VALUES ('TB', 'telemetry_baseline', 0)"))
    except Exception as e:
        other = str(e)[:120]
    print("OTHER_TENANT_OK", other is None, other)
'''

# The table as 0009 creates it. A later revision may add a column (0012 adds
# `scope`, ADR-0038), so a database at HEAD carries the second set: this suite
# pins 0009's own work at 0009 and the model's whole table at head.
EXPECTED_COLUMNS = "capability,granted,granted_at,granted_by,id,revoked_at,revoked_by,tenant_code,updated_at"
EXPECTED_COLUMNS_AT_HEAD = "capability,granted,granted_at,granted_by,id,revoked_at,revoked_by,scope,tenant_code,updated_at"
EXPECTED_NOTNULL = "capability,granted,id,tenant_code"
EXPECTED_UNIQUE = "uq_ai_learning_consent(tenant_code+capability)"
EXPECTED_INDEXES = {"ix_ai_learning_consents_id", "ix_ai_learning_consents_tenant_code"}


def _line(out, key):
    m = re.search(rf"^{key} (.*)$", out, re.M)
    return m.group(1).strip() if m else None


def _check_table(prefix, out, columns=EXPECTED_COLUMNS):
    check(f"{prefix}: the consent table exists", _line(out, "TABLE") == "True", out[-600:])
    check(f"{prefix}: its columns are exactly the expected ones", _line(out, "COLUMNS") == columns,
          str(_line(out, "COLUMNS")))
    check(f"{prefix}: tenant_code, capability and granted are NOT NULL",
          _line(out, "NOTNULL") == EXPECTED_NOTNULL, str(_line(out, "NOTNULL")))
    check(f"{prefix}: UNIQUE (tenant_code, capability) is named uq_ai_learning_consent",
          _line(out, "UNIQUES") == EXPECTED_UNIQUE, str(_line(out, "UNIQUES")))
    idx = set((_line(out, "INDEXES") or "").split(","))
    check(f"{prefix}: ix_*_id and the tenant_code index are present", EXPECTED_INDEXES <= idx, str(idx))


def section_fresh_database_reaches_head_with_the_table():
    print("=" * 74)
    print("1. A FRESH DATABASE")
    print("=" * 74)
    with _TempDb() as url:
        rc, out = _run(_DESCRIBE + r'''
import migrate
from database import engine
migrate.run(verbose=False)
print("HEAD", migrate.head_revision())
print("CURRENT", migrate.current_revision(engine))
from alembic.config import Config
from alembic.script import ScriptDirectory
import os as _os
_sd = ScriptDirectory.from_config(Config(_os.path.join(_os.getcwd(), "alembic.ini")))
print("IN_CHAIN", any(r.revision == "0009_native_ai_consent" for r in _sd.walk_revisions()))
describe(engine)
drift(engine)
duplicate_refused(engine)
''', url)
        check("migrate.run() succeeds on an empty database", rc == 0, out[-800:])
        # Not "head IS this revision": a later migration (0010_outcome_contracts)
        # builds on it, and this check must not fail every time one lands.
        check(f"{REVISION} is on the chain to head", _line(out, "IN_CHAIN") == "True",
              str(_line(out, "IN_CHAIN")))
        check("...and the database is stamped at head", _line(out, "CURRENT") == _line(out, "HEAD"),
              f"{_line(out, 'CURRENT')} vs {_line(out, 'HEAD')}")
        _check_table("fresh", out, columns=EXPECTED_COLUMNS_AT_HEAD)
        check("fresh: no model/migration drift for the consent table",
              (_line(out, "DRIFT") or "").startswith("0 "), str(_line(out, "DRIFT")))
        check("CONTROL: the first consent row inserts cleanly",
              (_line(out, "FIRST_INSERT_OK") or "").startswith("True"), str(_line(out, "FIRST_INSERT_OK")))
        check("the database refuses a second row for the same (tenant, capability)",
              _line(out, "DUPLICATE_REFUSED") == "True", str(_line(out, "DUPLICATE_REFUSED")))
        check("...but another tenant may hold the same capability",
              (_line(out, "OTHER_TENANT_OK") or "").startswith("True"), str(_line(out, "OTHER_TENANT_OK")))


def section_upgrade_from_0008_with_live_rows():
    print()
    print("=" * 74)
    print("2. A DATABASE ALREADY AT 0008, WITH DATA")
    print("=" * 74)
    with _TempDb() as url:
        rc, out = _run(_DESCRIBE + r'''
import migrate
from alembic import command
from sqlalchemy import inspect, text
import models  # noqa: F401
from database import Base, engine
Base.metadata.create_all(bind=engine)
with engine.begin() as c:
    c.execute(text("DROP TABLE IF EXISTS ai_learning_consents"))
    c.execute(text("INSERT INTO machines (tenant_code, site, name, status, utilization) "
                   "VALUES ('TA', 'Plant 1', 'PRESS-01', 'Running', 80)"))
    c.execute(text("INSERT INTO audit_logs (tenant_code, actor, action) VALUES ('TA', 'ta-admin', 'login')"))
cfg = migrate._config()
command.stamp(cfg, "0008_machine_claim")
print("BEFORE_CURRENT", migrate.current_revision(engine))
print("BEFORE_TABLE", "ai_learning_consents" in set(inspect(engine).get_table_names()))
command.upgrade(cfg, "0009_native_ai_consent")
print("AFTER_CURRENT", migrate.current_revision(engine))
describe(engine)
# The drift comparison is against models.py, which describes the table at HEAD
# (0012 adds `scope`, ADR-0038); 0009's own work is pinned by describe() above.
command.upgrade(cfg, "head")
drift(engine)
with engine.begin() as c:
    print("MACHINES", c.execute(text("SELECT count(*) FROM machines WHERE name='PRESS-01'")).scalar())
    print("AUDIT", c.execute(text("SELECT count(*) FROM audit_logs WHERE actor='ta-admin'")).scalar())
    c.execute(text("INSERT INTO ai_learning_consents (tenant_code, capability, granted, granted_by) "
                   "VALUES ('TA', 'telemetry_baseline', 1, 'ta-admin')"))
command.downgrade(cfg, "0008_machine_claim")
print("DOWN_CURRENT", migrate.current_revision(engine))
print("DOWN_TABLE", "ai_learning_consents" in set(inspect(engine).get_table_names()))
with engine.begin() as c:
    print("DOWN_MACHINES", c.execute(text("SELECT count(*) FROM machines WHERE name='PRESS-01'")).scalar())
    print("DOWN_AUDIT", c.execute(text("SELECT count(*) FROM audit_logs WHERE actor='ta-admin'")).scalar())
command.upgrade(cfg, "0009_native_ai_consent")
print("REUP_CURRENT", migrate.current_revision(engine))
print("REUP_TABLE", "ai_learning_consents" in set(inspect(engine).get_table_names()))
command.upgrade(cfg, "0009_native_ai_consent")
print("IDEMPOTENT", migrate.current_revision(engine))
''', url)
        check("the upgrade script ran", rc == 0, out[-1200:])
        check("CONTROL: the database really started at 0008", _line(out, "BEFORE_CURRENT") == PARENT,
              str(_line(out, "BEFORE_CURRENT")))
        check("CONTROL: ...without the consent table", _line(out, "BEFORE_TABLE") == "False",
              str(_line(out, "BEFORE_TABLE")))
        check(f"upgrade lands on {REVISION}", _line(out, "AFTER_CURRENT") == REVISION,
              str(_line(out, "AFTER_CURRENT")))
        _check_table("upgraded", out)
        check("upgraded: no model/migration drift for the consent table",
              (_line(out, "DRIFT") or "").startswith("0 "), str(_line(out, "DRIFT")))
        check("the machine written before the upgrade is untouched", _line(out, "MACHINES") == "1",
              str(_line(out, "MACHINES")))
        check("...and so is the audit record", _line(out, "AUDIT") == "1", str(_line(out, "AUDIT")))
        check("downgrade returns to 0008", _line(out, "DOWN_CURRENT") == PARENT,
              str(_line(out, "DOWN_CURRENT")))
        check("...dropping the consent table", _line(out, "DOWN_TABLE") == "False",
              str(_line(out, "DOWN_TABLE")))
        check("...and nothing else: machines survive", _line(out, "DOWN_MACHINES") == "1",
              str(_line(out, "DOWN_MACHINES")))
        check("...and the audit trail survives (who granted what is kept)",
              _line(out, "DOWN_AUDIT") == "1", str(_line(out, "DOWN_AUDIT")))
        check("upgrading again restores the table", _line(out, "REUP_TABLE") == "True"
              and _line(out, "REUP_CURRENT") == REVISION, f"{_line(out, 'REUP_CURRENT')} {_line(out, 'REUP_TABLE')}")
        check("a second upgrade to it is a no-op", _line(out, "IDEMPOTENT") == REVISION,
              str(_line(out, "IDEMPOTENT")))


def section_revision_file_shape():
    print()
    print("=" * 74)
    print("3. THE REVISION FILE")
    print("=" * 74)
    versions = os.path.join(HERE, "alembic", "versions")
    found = [n for n in sorted(os.listdir(versions)) if n.startswith("0009_native_ai_") and n.endswith(".py")]
    check("exactly one 0009_native_ai_* revision file", len(found) == 1, str(found))
    if not found:
        return
    src = io.open(os.path.join(versions, found[0]), encoding="utf-8").read()
    rev = re.search(r'^revision = "([^"]+)"', src, re.M)
    down = re.search(r'^down_revision = "([^"]+)"', src, re.M)
    check(f"revision id is {REVISION}", bool(rev) and rev.group(1) == REVISION, str(rev and rev.group(1)))
    check("revision id fits alembic_version VARCHAR(32)", bool(rev) and len(rev.group(1)) <= 32,
          str(rev and len(rev.group(1))))
    check(f"its parent is {PARENT}", bool(down) and down.group(1) == PARENT, str(down and down.group(1)))
    others = []
    for n in os.listdir(versions):
        if n.endswith(".py") and n != found[0]:
            text = io.open(os.path.join(versions, n), encoding="utf-8").read()
            if f'down_revision = "{PARENT}"' in text:
                others.append(n)
    check("no other revision also branches from 0008 (a single head)", not others, str(others))


def main():
    section_fresh_database_reaches_head_with_the_table()
    section_upgrade_from_0008_with_live_rows()
    section_revision_file_shape()
    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def test_amp_ai_integration_migration():
    assert main() == 0, failures


if __name__ == "__main__":
    sys.exit(main())
