"""Migration 0010_outcome_contracts and the agreed-downtime-attribution schema.

WHAT THIS PINS
--------------
Eight new tables (ADR-0021). Seven hold service contracts, their statements and
the parties' acceptances; the eighth, machine_telemetry_spans, is a per-source
status history of a factory's machines.

  1. THE SHAPE the plan specifies: exact columns, the unique keys the whole
     acceptance design rests on, String-only money and hash columns, and which
     tables are tenant-scoped.
  2. THE UPGRADE from the previous revision, on a database that already holds
     factory and OEM rows — migrating an empty database proves nothing.
  3. THE MIGRATION BUILDS WHAT THE MODELS DECLARE (an autogenerate diff with the
     CI gate's own rules is empty), both after the upgrade and after a
     downgrade/re-upgrade round trip.
  4. IDEMPOTENCE: boot's create_all may already have made the tables before the
     migration runs; the migration must then be a no-op, not a crash.
  5. THE DATABASE ENFORCES THE KEYS (SQLite here; PostgreSQL in
     verify_pg_outcome_contracts.py, which CI runs).
  6. OFFBOARDING: the generic purge sweeps the tenant's spans and cannot see the
     contract tables, which carry `factory_tenant_code`, never `tenant_code`.

WHY THE PREVIOUS REVISION IS READ, NOT HARD-CODED
-------------------------------------------------
A sibling branch may also add a migration after 0008, and the founder re-parents
at merge. Reading `down_revision` from the script keeps every scenario here
testing "the revision before this one" whatever that turns out to be.

Each database scenario runs in its own subprocess with its own DATABASE_URL, for
the reason test_migrate.py gives: database.py binds the engine at import.

Run: DATABASE_URL="sqlite:///./ci.db" python test_migration_0010_outcome_contracts.py
"""
import io
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
VERSIONS = os.path.join(HERE, "alembic", "versions")
REVISION = "0010_outcome_contracts"

CONTRACT_TABLES = [
    "service_contracts", "service_contract_term_versions",
    "service_contract_machines", "contract_statements",
    "contract_attribution_records", "contract_statement_acceptances",
    "contract_disputes",
]
SPAN_TABLE = "machine_telemetry_spans"
NEW_TABLES = CONTRACT_TABLES + [SPAN_TABLE]

EXPECTED_COLUMNS = {
    "service_contracts": {
        "id", "oem_code", "factory_tenant_code", "contract_ref", "title",
        "contract_type", "status", "starts_at", "ends_at", "created_by",
        "created_at", "proposed_at", "factory_accepted_by", "factory_accepted_at",
        "termination_effective_at", "terminated_by_party", "terminated_by",
        "termination_reason", "updated_at"},
    "service_contract_term_versions": {
        "id", "contract_id", "version", "terms_json", "terms_hash",
        "effective_from", "status", "proposed_by_party", "proposed_by",
        "proposed_at", "oem_accepted_by", "oem_accepted_at", "oem_accepted_hash",
        "factory_accepted_by", "factory_accepted_at", "factory_accepted_hash",
        "decision_note"},
    "service_contract_machines": {
        "id", "term_version_id", "installation_id", "machine_id_at_acceptance",
        "factory_tenant_at_acceptance", "serial_number", "coverage_ended_at",
        "coverage_end_reason"},
    # No summary columns: every figure lives in canonical_json and the records.
    "contract_statements": {
        "id", "contract_id", "term_version_id", "period_start", "period_end",
        "revision", "content_hash", "canonical_json", "computed_at",
        "computed_by_party", "computed_by", "updated_at"},
    "contract_attribution_records": {
        "id", "statement_id", "seq", "installation_id", "start_at", "end_at",
        "seconds", "bucket", "cause", "evidence_json"},
    "contract_statement_acceptances": {
        "id", "statement_id", "party", "actor", "accepted_at", "content_hash",
        "revision"},
    "contract_disputes": {
        "id", "contract_id", "statement_id", "installation_id", "window_start",
        "window_end", "raised_by_party", "raised_by", "raised_at", "reason",
        "proposed_bucket", "status", "resolution_bucket", "resolution_note",
        "resolution_proposed_by_party", "resolution_proposed_by",
        "resolution_proposed_at", "resolution_accepted_by",
        "resolution_accepted_at", "closed_at"},
    "machine_telemetry_spans": {
        "id", "tenant_code", "machine_id", "source", "status", "span_start",
        "span_end", "message_count"},
}

EXPECTED_UNIQUE = {
    "service_contracts": {("oem_code", "contract_ref")},
    "service_contract_term_versions": {("contract_id", "version")},
    "service_contract_machines": {("term_version_id", "installation_id")},
    "contract_statements": {("contract_id", "period_start")},
    "contract_attribution_records": {("statement_id", "seq")},
    # REVISION is part of the key (critic finding C1): the same party may accept
    # revision 1 and, after a change, revision 3 of one statement.
    "contract_statement_acceptances": {("statement_id", "party", "revision")},
    "contract_disputes": set(),
    "machine_telemetry_spans": set(),
}

EXPECTED_FKS = {
    ("service_contract_term_versions", "contract_id"): "service_contracts.id",
    ("service_contract_machines", "term_version_id"): "service_contract_term_versions.id",
    ("service_contract_machines", "installation_id"): "machine_installations.id",
    ("contract_statements", "contract_id"): "service_contracts.id",
    ("contract_statements", "term_version_id"): "service_contract_term_versions.id",
    ("contract_attribution_records", "statement_id"): "contract_statements.id",
    ("contract_statement_acceptances", "statement_id"): "contract_statements.id",
    ("contract_disputes", "contract_id"): "service_contracts.id",
    ("contract_disputes", "statement_id"): "contract_statements.id",
    ("machine_telemetry_spans", "machine_id"): "machines.id",
}

SPAN_LOOKUP_INDEX = ("tenant_code", "machine_id", "source", "span_start")


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
    assert m, "0010 declares no down_revision"
    return m.group(1)


# --------------------------------------------------------------------------
# 1. the revision and the models
# --------------------------------------------------------------------------

def test_the_revision_is_in_the_chain_and_fits_the_version_column():
    src = _migration_source()
    m = re.search(r'^revision = "([^"]+)"', src, re.M)
    assert m and m.group(1) == REVISION, m and m.group(1)
    assert len(REVISION) <= 32, len(REVISION)
    prev = _previous_revision()
    assert prev == "0009_native_ai_consent", prev

    from alembic.script import ScriptDirectory
    import migrate
    script = ScriptDirectory.from_config(migrate._config())
    heads = script.get_heads()
    assert len(heads) == 1, f"the migration tree has {len(heads)} heads: {heads}"
    chain = [r.revision for r in script.walk_revisions()]
    assert REVISION in chain, chain
    assert prev in chain, chain
    # Additive only: no ALTER of an existing table, no data rewrite, anywhere
    # upgrade() can reach (its helpers are defined above it).
    assert src.count("def downgrade") == 1, "expected exactly one downgrade()"
    upgrade_side = src.split("def downgrade")[0]
    assert "def upgrade" in upgrade_side
    for forbidden in ("op.add_column", "op.alter_column", "op.drop_column",
                      "op.drop_table", "op.execute", "op.bulk_insert"):
        assert forbidden not in upgrade_side, f"0010 upgrade side uses {forbidden}"
    for table in NEW_TABLES:
        assert f'op.create_table(\n            "{table}"' in upgrade_side, \
            f"0010 does not create {table}"
    print(f"PASS {REVISION} follows {prev}, is the single head's ancestor, fits "
          "VARCHAR(32) and only creates tables")


def _table(name):
    import models  # noqa: F401
    from database import Base
    assert name in Base.metadata.tables, f"no model maps {name}"
    return Base.metadata.tables[name]


def test_each_table_has_exactly_the_planned_columns():
    for name, expected in EXPECTED_COLUMNS.items():
        got = {c.name for c in _table(name).columns}
        assert got == expected, (f"{name}: missing {sorted(expected - got)}, "
                                 f"unexpected {sorted(got - expected)}")
    print(f"PASS all {len(EXPECTED_COLUMNS)} tables carry exactly the planned columns")


def test_the_unique_keys_are_declared():
    from sqlalchemy import UniqueConstraint
    for name, expected in EXPECTED_UNIQUE.items():
        t = _table(name)
        got = {tuple(c.name for c in con.columns)
               for con in t.constraints if isinstance(con, UniqueConstraint)}
        got |= {tuple(c.name for c in ix.columns) for ix in t.indexes if ix.unique}
        assert got == expected, f"{name}: unique keys {got}, expected {expected}"
    print("PASS unique keys: contract ref, term version, machine coverage, "
          "statement period, record seq, acceptance (statement, party, revision)")


def test_foreign_keys_point_where_the_plan_says():
    for (table, column), target in EXPECTED_FKS.items():
        col = _table(table).columns[column]
        targets = {f"{fk.column.table.name}.{fk.column.name}" for fk in col.foreign_keys}
        assert targets == {target}, f"{table}.{column} -> {targets}, expected {target}"
    # Deliberately NOT foreign keys: the snapshot of which machine was covered
    # must survive the machine row being purged at offboarding.
    for table, column in (("service_contract_machines", "machine_id_at_acceptance"),
                          ("contract_attribution_records", "installation_id")):
        assert not _table(table).columns[column].foreign_keys, f"{table}.{column}"
    print(f"PASS {len(EXPECTED_FKS)} foreign keys, and the snapshot ids are not FKs")


def test_money_hashes_and_text_have_fixed_types():
    from sqlalchemy import Float, Numeric, String, Text, BigInteger
    for name in NEW_TABLES:
        for c in _table(name).columns:
            # Numeric is a Float subclass's sibling; both are refused. SQLite
            # stores NUMERIC as REAL, which is exactly the float money must avoid.
            assert not isinstance(c.type, (Float, Numeric)), f"{name}.{c.name} is {c.type}"
    hash_columns = [("service_contract_term_versions", "terms_hash"),
                    ("service_contract_term_versions", "oem_accepted_hash"),
                    ("service_contract_term_versions", "factory_accepted_hash"),
                    ("contract_statements", "content_hash"),
                    ("contract_statement_acceptances", "content_hash")]
    for table, column in hash_columns:
        t = _table(table).columns[column].type
        assert isinstance(t, String) and t.length == 64, f"{table}.{column} is {t!r}"
    for table, column in (("service_contract_term_versions", "terms_json"),
                          ("contract_statements", "canonical_json"),
                          ("contract_attribution_records", "evidence_json")):
        assert isinstance(_table(table).columns[column].type, Text), (table, column)
    assert isinstance(_table("contract_attribution_records").columns["seconds"].type,
                      BigInteger)
    assert _table(SPAN_TABLE).columns["status"].type.length == 32
    assert _table("contract_disputes").columns["reason"].type.length == 1000
    print("PASS no Float/Numeric anywhere; hashes VARCHAR(64); JSON as Text; "
          "seconds BIGINT; span status VARCHAR(32); dispute reason VARCHAR(1000)")


def test_nullability_that_the_design_depends_on():
    must_be_null = [("contract_statements", "canonical_json"),     # NULL only after offboarding
                    ("service_contract_machines", "coverage_ended_at"),
                    ("service_contract_machines", "coverage_end_reason"),
                    ("service_contracts", "termination_effective_at")]
    for table, column in must_be_null:
        assert _table(table).columns[column].nullable, f"{table}.{column}"
    must_not_be_null = (
        [("contract_statements", c) for c in
         ("contract_id", "term_version_id", "period_start", "period_end",
          "revision", "content_hash", "computed_at")]
        + [("contract_statement_acceptances", c) for c in
           EXPECTED_COLUMNS["contract_statement_acceptances"] - {"id"}]
        + [(SPAN_TABLE, c) for c in EXPECTED_COLUMNS[SPAN_TABLE] - {"id"}]
        + [("service_contracts", c) for c in
           ("oem_code", "factory_tenant_code", "contract_ref", "status",
            "starts_at", "ends_at")]
        + [("service_contract_term_versions", c) for c in
           ("contract_id", "version", "terms_json", "terms_hash",
            "effective_from", "status")]
        + [("contract_attribution_records", c) for c in
           EXPECTED_COLUMNS["contract_attribution_records"] - {"id"}]
        + [("contract_disputes", c) for c in
           ("contract_id", "statement_id", "installation_id", "window_start",
            "window_end", "raised_by_party", "raised_by", "raised_at", "reason",
            "proposed_bucket", "status")])
    for table, column in must_not_be_null:
        assert not _table(table).columns[column].nullable, f"{table}.{column}"
    print(f"PASS {len(must_not_be_null)} NOT NULL columns and {len(must_be_null)} "
          "deliberately nullable ones")


def test_tenant_scoping_is_where_the_plan_puts_it():
    import models
    import tenancy
    from database import Base

    by_table = {m.class_.__tablename__: m.class_ for m in Base.registry.mappers}
    scoped = set(tenancy.SCOPED_MODELS)
    for name in CONTRACT_TABLES:
        cls = by_table[name]
        # offboard_tenant.purge_tenant_data hard-deletes every mapper with a
        # `tenant_code` attribute. A contract is the OEM's record as much as the
        # factory's; the sweep must not be able to see it.
        assert not hasattr(cls, "tenant_code"), f"{name} carries tenant_code"
        assert cls not in scoped, f"{name} is in SCOPED_MODELS"
    span = by_table[SPAN_TABLE]
    assert span is models.MachineTelemetrySpan
    assert span in scoped, "machine_telemetry_spans is not tenant-scoped"
    assert SPAN_TABLE in tenancy.CORE_TENANT_TABLES
    col = span.__table__.columns["tenant_code"]
    assert not col.nullable
    # FAIL CLOSED: no silent DEFAULT. A span written without its tenant is a
    # bug to surface, not a row to hand to the founder workspace.
    assert col.default is None and col.server_default is None, col.default
    lookup = [ix for ix in span.__table__.indexes
              if tuple(c.name for c in ix.columns) == SPAN_LOOKUP_INDEX]
    assert len(lookup) == 1, [tuple(c.name for c in ix.columns)
                              for ix in span.__table__.indexes]
    for name in NEW_TABLES:
        names = {ix.name for ix in by_table[name].__table__.indexes}
        assert f"ix_{name}_id" in names, f"{name} lacks ix_{name}_id: {names}"
    print("PASS 7 contract tables unscoped and without tenant_code; spans scoped, "
          "tenant NOT NULL with no default, composite lookup index present")


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

NEW = %(new)r
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

def drop_new_tables():
    with engine.begin() as c:
        for t in reversed(NEW):
            c.execute(text("DROP TABLE IF EXISTS " + t))

def seed_previous_revision_rows():
    with engine.begin() as c:
        c.execute(text("INSERT INTO machines (id, tenant_code, site, name, status, utilization) "
                       "VALUES (1,'FACTORY_A','P1','COMP-001','Running',70), "
                       "(2,'FACTORY_B','P1','COMP-001','Breakdown',0)"))
        c.execute(text("INSERT INTO downtime_logs (tenant_code, machine_id, reason, duration) "
                       "VALUES ('FACTORY_A',1,'No material','15 min')"))
        c.execute(text("INSERT INTO oem_organizations (id, oem_code, name, is_active) "
                       "VALUES (1,'OEM_A','Alpha',1)"))
        c.execute(text("INSERT INTO machine_models (id, oem_code, family, model_code, name, status) "
                       "VALUES (1,'OEM_A','Compressor','X','X','Active')"))
        c.execute(text("INSERT INTO machine_installations "
                       "(id, oem_code, serial_number, model_id, factory_tenant_code, site, machine_id, status) "
                       "VALUES (1,'OEM_A','SN-1',1,'FACTORY_A','P1',1,'Active')"))
        c.execute(text("INSERT INTO machine_claims (oem_code, installation_id, token_hash, code_hint, "
                       "status, expires_at) VALUES ('OEM_A',1,'h','ABCD','Claimed','2027-01-01 00:00:00')"))

COUNTED = ("machines", "downtime_logs", "oem_organizations", "machine_models",
           "machine_installations", "machine_claims")

def counts():
    with engine.begin() as c:
        return {t: c.execute(text("SELECT count(*) FROM " + t)).scalar() for t in COUNTED}

cfg = migrate._config()
'''


def _script(body):
    return (_COMMON % {"new": NEW_TABLES, "prev": _previous_revision()}) + body


def test_upgrade_from_the_previous_revision_keeps_every_row_and_matches_the_models():
    body = r'''
Base.metadata.create_all(bind=engine)
drop_new_tables()
seed_previous_revision_rows()
before = counts()
command.stamp(cfg, PREV)
print("PRE_TABLES", sorted(NEW) == sorted(set(NEW) - tables()) and "none" or "some")
print("BEFORE", before)
command.upgrade(cfg, "head")
print("AFTER", counts())
print("ROWS_INTACT", counts() == before)
print("ALL_CREATED", set(NEW) <= tables())
print("AT_HEAD", migrate.current_revision(engine) == migrate.head_revision())
d = drift()
print("DRIFT", len(d))
for item in d:
    print("  DIFF", item)
# DECLARED LENGTHS, read back from the migrated schema itself. Alembic's type
# comparison on SQLite does not see VARCHAR vs VARCHAR(64) (mutation-tested:
# the migration dropping terms_hash's length survived DRIFT 0), but SQLite
# keeps the declared type text, so reflect it directly.
want = {("service_contract_term_versions", "terms_hash"): 64,
        ("service_contract_term_versions", "oem_accepted_hash"): 64,
        ("service_contract_term_versions", "factory_accepted_hash"): 64,
        ("contract_statements", "content_hash"): 64,
        ("contract_statement_acceptances", "content_hash"): 64,
        ("machine_telemetry_spans", "status"): 32,
        ("contract_disputes", "reason"): 1000,
        ("contract_disputes", "resolution_note"): 1000}
got = {}
for (t, col), n in want.items():
    types = {c["name"]: c["type"] for c in inspect(engine).get_columns(t)}
    got[(t, col)] = getattr(types[col], "length", None)
print("LENGTHS_OK", got == want)
if got != want:
    print("  LENGTHS", got)
# CONTROL: the comparison must be able to fail on this engine, or DRIFT 0 above
# is decoration. Remove the engine's lookup index and require the diff to name it.
with engine.begin() as c:
    c.execute(text("DROP INDEX ix_machine_telemetry_spans_lookup"))
print("CONTROL_DETECTS", any("ix_machine_telemetry_spans_lookup" in str(x) for x in drift()))
'''
    with _TempDb() as url:
        rc, out = _run(_script(body), url)
        assert rc == 0, out
        assert "CONTROL_DETECTS True" in out, \
            "the drift comparison cannot see a missing index; DRIFT 0 proves nothing:\n" + out
        assert "PRE_TABLES none" in out, out
        assert "ROWS_INTACT True" in out, out
        assert "ALL_CREATED True" in out, out
        assert "AT_HEAD True" in out, out
        assert "DRIFT 0" in out, "the migration does not build what models.py declares:\n" + out
        assert "LENGTHS_OK True" in out, "the migration declares the wrong lengths:\n" + out
    print("PASS upgrade from the previous revision: existing rows intact, 8 tables "
          "created, stamped at head, no drift against models.py, declared VARCHAR "
          "lengths as specified")


def test_downgrade_with_contract_rows_then_upgrade_again():
    body = r'''
Base.metadata.create_all(bind=engine)
drop_new_tables()
seed_previous_revision_rows()
before = counts()
command.stamp(cfg, PREV)
command.upgrade(cfg, "head")
with engine.begin() as c:
    c.execute(text("INSERT INTO service_contracts (id, oem_code, factory_tenant_code, contract_ref, "
                   "title, contract_type, status, starts_at, ends_at, created_by, created_at) VALUES "
                   "(1,'OEM_A','FACTORY_A','AMC-1','AMC','AMC','accepted','2026-08-31 18:30:00',"
                   "'2027-08-31 18:30:00','oem:OEM_A:admin','2026-08-01 00:00:00')"))
    c.execute(text("INSERT INTO machine_telemetry_spans (tenant_code, machine_id, source, status, "
                   "span_start, span_end, message_count) VALUES "
                   "('FACTORY_A',1,'mqtt','Running','2026-09-01 00:00:00','2026-09-01 00:05:00',11)"))
command.downgrade(cfg, PREV)
print("GONE", not (set(NEW) & tables()))
print("ROWS_INTACT_DOWN", counts() == before)
print("AT_PREV", migrate.current_revision(engine) == PREV)
command.upgrade(cfg, "head")
print("BACK", set(NEW) <= tables())
print("ROWS_INTACT_UP", counts() == before)
d = drift()
print("DRIFT", len(d))
for item in d:
    print("  DIFF", item)
'''
    with _TempDb() as url:
        rc, out = _run(_script(body), url)
        assert rc == 0, out
        for marker in ("GONE True", "ROWS_INTACT_DOWN True", "AT_PREV True",
                       "BACK True", "ROWS_INTACT_UP True", "DRIFT 0"):
            assert marker in out, f"{marker} missing:\n{out}"
    print("PASS downgrade drops all 8 tables (with rows in them) and leaves the "
          "previous schema's rows; re-upgrade restores an undrifted schema")


def test_the_migration_is_a_no_op_when_boot_already_created_the_tables():
    """main.py runs create_all at import; on a deploy the tables can exist
    before `alembic upgrade` reaches 0010."""
    body = r'''
Base.metadata.create_all(bind=engine)
seed_previous_revision_rows()
with engine.begin() as c:
    c.execute(text("INSERT INTO machine_telemetry_spans (tenant_code, machine_id, source, status, "
                   "span_start, span_end, message_count) VALUES "
                   "('FACTORY_A',1,'mqtt','Running','2026-09-01 00:00:00','2026-09-01 00:05:00',3)"))
command.stamp(cfg, PREV)
command.upgrade(cfg, "head")
with engine.begin() as c:
    print("SPAN_KEPT", c.execute(text("SELECT count(*) FROM machine_telemetry_spans")).scalar() == 1)
print("AT_HEAD", migrate.current_revision(engine) == migrate.head_revision())
print("DRIFT", len(drift()))
'''
    with _TempDb() as url:
        rc, out = _run(_script(body), url)
        assert rc == 0, out
        for marker in ("SPAN_KEPT True", "AT_HEAD True", "DRIFT 0"):
            assert marker in out, f"{marker} missing:\n{out}"
    print("PASS tables already present: the migration changes nothing and stamps head")


# --------------------------------------------------------------------------
# 5. the keys are enforced by the database
# --------------------------------------------------------------------------

def _memory_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import models  # noqa: F401
    from database import Base
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


T0 = datetime(2026, 8, 31, 18, 30, 0)
T1 = datetime(2026, 9, 30, 18, 30, 0)
T2 = datetime(2026, 10, 31, 18, 30, 0)


def _graph(db, ref="AMC-1", oem="OEM_A"):
    import models
    c = models.ServiceContract(oem_code=oem, factory_tenant_code="FACTORY_A",
                               contract_ref=ref, title="AMC", contract_type="AMC",
                               status="accepted", starts_at=T0, ends_at=T2,
                               created_by="oem:OEM_A:admin", created_at=T0)
    db.add(c)
    db.flush()
    v = models.ServiceContractTermVersion(contract_id=c.id, version=1, terms_json="{}",
                                          terms_hash="a" * 64, effective_from=T0,
                                          status="accepted")
    db.add(v)
    db.flush()
    s = models.ContractStatement(contract_id=c.id, term_version_id=v.id,
                                 period_start=T0, period_end=T1, revision=1,
                                 content_hash="b" * 64, canonical_json="{}",
                                 computed_at=T1, computed_by_party="OEM",
                                 computed_by="oem:OEM_A:admin")
    db.add(s)
    db.flush()
    return c, v, s


def _refused(db, make):
    from sqlalchemy.exc import IntegrityError
    try:
        with db.begin_nested():
            db.add(make())
            db.flush()
    except IntegrityError:
        return True
    return False


def test_the_database_refuses_duplicate_keys():
    import models
    db = _memory_session()
    c, v, s = _graph(db)
    db.commit()

    def acceptance(party="FACTORY", revision=1):
        return models.ContractStatementAcceptance(
            statement_id=s.id, party=party, actor="factory_admin", accepted_at=T1,
            content_hash="b" * 64, revision=revision)

    # CONTROL first, so a refusal below is the key and not a broken row.
    assert not _refused(db, acceptance), "a well-formed acceptance was refused"
    assert _refused(db, acceptance), "the same party accepted one revision twice"
    assert not _refused(db, lambda: acceptance(party="OEM")), \
        "the other party could not accept the same revision"
    assert not _refused(db, lambda: acceptance(revision=3)), \
        "a party could not accept a LATER revision of the same statement"

    def statement(period_start=T0):
        return models.ContractStatement(
            contract_id=c.id, term_version_id=v.id, period_start=period_start,
            period_end=T1, revision=1, content_hash="c" * 64, canonical_json="{}",
            computed_at=T1, computed_by_party="FACTORY", computed_by="f")
    assert _refused(db, statement), "two statements exist for one contract period"
    assert not _refused(db, lambda: statement(T1)), "CONTROL: next period refused"

    def version(n=1):
        return models.ServiceContractTermVersion(
            contract_id=c.id, version=n, terms_json="{}", terms_hash="d" * 64,
            effective_from=T0, status="draft")
    assert _refused(db, version), "two term versions share a number"
    assert not _refused(db, lambda: version(2)), "CONTROL: version 2 refused"

    def contract(oem="OEM_A"):
        return models.ServiceContract(
            oem_code=oem, factory_tenant_code="FACTORY_B", contract_ref="AMC-1",
            title="t", contract_type="AMC", status="draft", starts_at=T0,
            ends_at=T2, created_by="x", created_at=T0)
    assert _refused(db, contract), "one OEM reused a contract reference"
    assert not _refused(db, lambda: contract("OEM_B")), \
        "CONTROL: a different OEM could not use the same reference"

    db.add(models.OemOrganization(oem_code="OEM_A", name="A"))
    db.add(models.MachineModel(id=1, oem_code="OEM_A", model_code="X", name="X"))
    db.add(models.Machine(id=1, tenant_code="FACTORY_A", site="P1", name="M", status="Running"))
    db.add(models.MachineInstallation(id=5, oem_code="OEM_A", serial_number="SN",
                                      model_id=1, factory_tenant_code="FACTORY_A",
                                      machine_id=1))
    db.commit()

    def covered(installation_id=5):
        return models.ServiceContractMachine(
            term_version_id=v.id, installation_id=installation_id,
            machine_id_at_acceptance=1, factory_tenant_at_acceptance="FACTORY_A",
            serial_number="SN")
    assert not _refused(db, covered), "CONTROL: covering a machine was refused"
    assert _refused(db, covered), "one term version covers the same installation twice"

    def record(seq=0):
        return models.ContractAttributionRecord(
            statement_id=s.id, seq=seq, installation_id=5, start_at=T0, end_at=T1,
            seconds=2592000, bucket="AVAILABLE", cause="telemetry", evidence_json="{}")
    assert not _refused(db, record), "CONTROL: a record was refused"
    assert _refused(db, record), "two records share one sequence number"
    assert not _refused(db, lambda: record(1)), "CONTROL: seq 1 refused"
    db.close()
    print("PASS SQLite refuses duplicate acceptance (statement, party, revision), "
          "statement period, term version, contract ref, covered installation and "
          "record seq — each beside a control that inserts")


def test_seconds_hold_more_than_a_32_bit_integer():
    import models
    db = _memory_session()
    _, _, s = _graph(db)
    big = 2 ** 40
    db.add(models.ContractAttributionRecord(
        statement_id=s.id, seq=0, installation_id=1, start_at=T0, end_at=T1,
        seconds=big, bucket="UNMEASURED", cause="no_telemetry", evidence_json="{}"))
    db.commit()
    got = db.query(models.ContractAttributionRecord).one().seconds
    assert got == big and type(got) is int, got
    db.close()
    print("PASS attribution seconds round-trip as an exact int")


# --------------------------------------------------------------------------
# 6. offboarding
# --------------------------------------------------------------------------

def test_offboarding_sweeps_spans_and_cannot_see_contracts():
    import models
    from offboard_tenant import purge_tenant_data
    db = _memory_session()
    db.add(models.Machine(id=1, tenant_code="FACTORY_A", site="", name="A1", status="Running"))
    db.add(models.Machine(id=2, tenant_code="FACTORY_B", site="", name="B1", status="Running"))
    db.flush()
    for tenant, machine_id in (("FACTORY_A", 1), ("FACTORY_A", 1), ("FACTORY_B", 2)):
        db.add(models.MachineTelemetrySpan(
            tenant_code=tenant, machine_id=machine_id, source="mqtt", status="Running",
            span_start=T0, span_end=T1, message_count=1))
    c, v, s = _graph(db)
    db.add(models.ContractStatementAcceptance(
        statement_id=s.id, party="OEM", actor="oem:OEM_A:admin", accepted_at=T1,
        content_hash="b" * 64, revision=1))
    db.commit()

    counts = purge_tenant_data(db, "FACTORY_A")
    assert counts.get(SPAN_TABLE) == 2, counts
    left = {r.tenant_code for r in db.query(models.MachineTelemetrySpan).all()}
    assert left == {"FACTORY_B"}, left
    for name in CONTRACT_TABLES:
        assert name not in counts, f"the generic sweep deleted from {name}: {counts}"
    assert db.query(models.ServiceContract).count() == 1
    assert db.query(models.ContractStatement).count() == 1
    assert db.query(models.ContractStatementAcceptance).count() == 1
    db.close()
    print("PASS purge_tenant_data removes the tenant's 2 spans, keeps the other "
          "tenant's, and leaves contract, statement and acceptance rows alone")


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"ALL {len(tests)} MIGRATION 0010 TESTS PASSED")
