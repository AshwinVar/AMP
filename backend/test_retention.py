"""Retention / pruning tests (backend/retention.py).

The tables retention.py touches are append-only and were never pruned — the
telemetry table grows every 45 seconds in production. A pruning job is the one
kind of code where "it ran and deleted some rows" is not evidence of anything:
deleting the whole table and deleting nothing at all both satisfy a naive
assertion. So the suite is built around CONTROL cases that distinguish a working
prune from a broken one:

  * the boundary itself — rows an hour outside the window go, rows an hour inside
    it stay (a prune that wipes the table fails this, and so does a no-op);
  * dry run leaves the row counts byte-for-byte unchanged;
  * batching really loops (batch count varies with batch size) rather than
    deleting one batch and stopping;
  * the compliance tables survive a --days 0 run that empties a prunable table in
    the same pass, so their survival cannot be explained by a generous window;
  * a NULL timestamp is kept — with the fixture inserted by raw SQL and asserted
    to be genuinely NULL, because an ORM insert would silently fill it from the
    column default and make the test vacuous.

Run:  python backend/test_retention.py     (exit 0 = pass)
Also collectable by pytest.
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models
import retention
from database import Base
from retention import KEEP_FOREVER, POLICIES, prune, table_names

# A fixed clock. Every age in this file is expressed relative to it and passed
# into prune(now=...), so a suite that runs at midnight cannot drift across a day
# boundary mid-test and change which rows are expired.
NOW = datetime(2026, 8, 4, 12, 0, 0)


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _telemetry(db, age, label, count=1, tenant="DEFAULT"):
    """Seed ``count`` iot_telemetry rows aged ``age`` before NOW."""
    rows = [models.IoTTelemetry(tenant_code=tenant, machine_id=1, signal_name=label,
                                signal_value="70", created_at=NOW - age)
            for _ in range(count)]
    db.add_all(rows)
    db.commit()
    return rows


def test_apply_deletes_only_rows_older_than_the_window():
    """CONTROL — the boundary itself.

    Pins the failure that a bare "some rows were deleted" assertion cannot see: a
    prune whose predicate is inverted, or whose cutoff is unset, deletes the live
    window too. iot_telemetry keeps 14 days, so a row an hour past the cutoff must
    go and a row an hour inside it must stay, and the survivors are checked by
    name — deleting everything and deleting nothing both fail.
    """
    db = _fresh_session()
    _telemetry(db, timedelta(days=20), "ancient", 3)
    _telemetry(db, timedelta(days=14, hours=1), "just-outside", 1)
    _telemetry(db, timedelta(days=13, hours=23), "just-inside", 1)
    _telemetry(db, timedelta(days=2), "fresh", 2)

    entry = prune(db, dry_run=False, now=NOW)["iot_telemetry"]

    assert entry["total"] == 7, entry
    assert entry["expired"] == 4, entry          # 3 ancient + 1 an hour past the cutoff
    assert entry["deleted"] == 4, entry
    survivors = sorted(r.signal_name for r in db.query(models.IoTTelemetry).all())
    assert survivors == ["fresh", "fresh", "just-inside"], survivors


def test_dry_run_reports_expiry_but_deletes_nothing():
    """Dry run is the whole safety story of this job: the operator reads the plan
    and only then passes --apply. If a dry run deleted so much as one row the
    approval gate would be theatre. Counts must be identical before and after.
    """
    db = _fresh_session()
    _telemetry(db, timedelta(days=90), "ancient", 5)
    before = db.query(models.IoTTelemetry).count()

    entry = prune(db, now=NOW)["iot_telemetry"]   # dry_run defaults to True

    assert entry["expired"] == 5, entry           # it still reports what WOULD go
    assert entry["deleted"] == 0 and entry["batches"] == 0, entry
    assert db.query(models.IoTTelemetry).count() == before == 5


def test_deletion_is_batched_across_more_rows_than_one_batch():
    """A first run against a table that has grown unpruned for a year must not be
    one enormous transaction. The failure this pins is the naive implementation
    that issues a single LIMITed delete and stops: with 250 expired rows and a
    batch size of 100 it would report one batch and leave 150 rows behind.

    CONTROL: the same 250 rows with a batch size above the row count must take
    exactly one batch — so the batch counter is measuring real loop iterations,
    not a constant.
    """
    db = _fresh_session()
    _telemetry(db, timedelta(days=60), "ancient", 250)

    entry = prune(db, dry_run=False, batch_size=100, now=NOW)["iot_telemetry"]

    assert entry["batches"] == 3, entry           # 100 + 100 + 50
    assert entry["deleted"] == 250, entry
    assert db.query(models.IoTTelemetry).count() == 0

    _telemetry(db, timedelta(days=60), "ancient", 250)
    single = prune(db, dry_run=False, batch_size=1000, now=NOW)["iot_telemetry"]
    assert single["batches"] == 1, single
    assert single["deleted"] == 250, single


def test_compliance_tables_are_exempt_even_under_a_days_override():
    """audit_logs (the security trail) and event_log (the ADR-0001 event history,
    which also gates the single-shot RESEED_FACTORY flag) must survive the job.

    The run uses --days 0, which is both the override test and the CONTROL: the
    same pass empties iot_telemetry of rows that the normal 14-day policy would
    have kept, so the compliance rows surviving cannot be explained away as
    "nothing was old enough". These rows are ten years old.
    """
    db = _fresh_session()
    db.add(models.AuditLog(tenant_code="DEFAULT", actor="admin", action="login",
                           created_at=NOW - timedelta(days=3650)))
    db.add(models.EventLog(tenant_code="DEFAULT", event_type="FactoryReseeded",
                           payload='{"flag": "2026-07-18"}',
                           occurred_at=NOW - timedelta(days=3650)))
    db.commit()
    _telemetry(db, timedelta(days=1), "fresh", 4)

    report = prune(db, dry_run=False, days=0, now=NOW)

    assert report["audit_logs"]["exempt"] and report["audit_logs"]["deleted"] == 0
    assert report["event_log"]["exempt"] and report["event_log"]["deleted"] == 0
    assert report["audit_logs"]["retention_days"] is KEEP_FOREVER
    assert db.query(models.AuditLog).count() == 1
    assert db.query(models.EventLog).count() == 1
    # CONTROL: --days 0 really did reach the prunable table in the same run.
    assert report["iot_telemetry"]["deleted"] == 4, report["iot_telemetry"]
    assert db.query(models.IoTTelemetry).count() == 0


def test_empty_tables_are_safe():
    """A brand-new deployment, or any table nothing has written to yet, must not
    error, not report phantom work, and not stop the sweep of the tables that do
    have rows. Run with --days 0 so every policy is maximally eager."""
    db = _fresh_session()

    report = prune(db, dry_run=False, days=0, now=NOW)

    assert set(report) == set(table_names())
    for name, entry in report.items():
        assert entry["error"] is None, (name, entry["error"])
        assert entry["refused"] is None, (name, entry["refused"])
        assert entry["total"] == 0 and entry["expired"] == 0, (name, entry)
        assert entry["deleted"] == 0 and entry["batches"] == 0, (name, entry)


def test_rows_with_a_null_timestamp_are_kept_and_counted():
    """A NULL timestamp means the row's age is UNKNOWN, not infinite, so it must
    never be deleted — and the pruner must not crash on it either. These rows can
    only come from raw SQL or an old migration (the models default the column in
    Python), which is exactly why their provenance deserves an operator's eyes
    rather than a silent delete.

    The fixture is inserted with raw SQL and then asserted to be genuinely NULL:
    constructing the row through the ORM would fill created_at from the column
    default and leave a test that proves nothing.
    """
    db = _fresh_session()
    db.execute(text("INSERT INTO iot_telemetry "
                    "(tenant_code, machine_id, signal_name, signal_value, created_at) "
                    "VALUES ('DEFAULT', 1, 'unknown-age', '70', NULL)"))
    db.commit()
    assert db.execute(text("SELECT created_at FROM iot_telemetry")).scalar() is None
    _telemetry(db, timedelta(days=60), "ancient", 2)

    entry = prune(db, dry_run=False, now=NOW)["iot_telemetry"]

    assert entry["null_timestamps"] == 1, entry
    assert entry["deleted"] == 2, entry           # only the datable old rows
    assert [r.signal_name for r in db.query(models.IoTTelemetry).all()] == ["unknown-age"]


def test_prune_ignores_an_inherited_tenant_scope():
    """Retention is an operational job that must bound the tables for EVERY
    tenant. The ADR-0002 hook filters SELECTs whenever a tenant is bound, so a
    prune that inherited a request's scope would count and delete only that
    tenant's expired rows and let every other tenant's telemetry grow forever —
    while its report cheerfully claimed the table was pruned.

    CONTROL: scoping is verifiably active in this test (bound to TA the session
    sees one of the two rows) before prune is called inside the same scope.
    """
    import tenancy as T
    T.install_scoping()
    db = _fresh_session()
    _telemetry(db, timedelta(days=60), "ta", 1, tenant="TA")
    _telemetry(db, timedelta(days=60), "tb", 1, tenant="TB")

    token = T.set_current_tenant("TA")
    try:
        # CONTROL: the hook is live — a scoped read sees only TA's row.
        assert db.query(models.IoTTelemetry).count() == 1
        entry = prune(db, dry_run=False, now=NOW)["iot_telemetry"]
    finally:
        T.reset_current_tenant(token)

    assert entry["total"] == 2, entry             # both tenants counted
    assert entry["deleted"] == 2, entry           # both tenants pruned
    assert db.query(models.IoTTelemetry).count() == 0


def test_a_prunable_policy_over_a_nullable_tenant_column_is_refused():
    """Defence in depth behind the exemption list. A NULL tenant_code row is
    hidden from every tenant scope by design (tenancy.py parks ambiguous legacy
    rows that way, pending the approved backfill), and this unscoped job is the
    only thing that could ever delete one. So if a future edit gave audit_logs —
    or any nullable-tenant table — a retention window, the pruner must refuse it
    rather than quietly destroy the evidence.
    """
    db = _fresh_session()
    db.add(models.AuditLog(tenant_code=None, actor="system", action="boot",
                           created_at=NOW - timedelta(days=3650)))
    db.commit()

    original = retention.POLICIES
    retention.POLICIES = (retention.RetentionPolicy(
        models.AuditLog, "created_at", 1, "hypothetical bad edit"),)
    try:
        entry = prune(db, dry_run=False, now=NOW)["audit_logs"]
    finally:
        retention.POLICIES = original

    assert entry["refused"], entry
    assert entry["deleted"] == 0, entry
    assert db.query(models.AuditLog).count() == 1


def test_policy_table_covers_the_audited_tables_and_holds_its_invariants():
    """The policy table is the review artefact — these are the invariants the
    safety argument in prune()'s docstring rests on. If any of them stops holding,
    the reasoning about what this job can reach stops holding with it."""
    covered = set(table_names())
    # every unbounded table named in the audit is accounted for, one way or other
    assert {"iot_telemetry", "event_log", "machine_events", "industrial_signals",
            "audit_logs", "notifications", "inventory_transactions"} <= covered

    exempt = {p.model.__tablename__ for p in POLICIES if p.days is KEEP_FOREVER}
    assert exempt == {"audit_logs", "event_log"}, exempt

    for policy in POLICIES:
        table = policy.model.__tablename__
        # the timestamp column is named per policy precisely because they differ
        assert hasattr(policy.model, policy.timestamp_column), table
        assert policy.why.strip(), table
        if policy.days is KEEP_FOREVER:
            continue
        assert policy.days > 0, table
        # a prunable table must not be able to hold rows hidden from every tenant
        column = policy.model.__table__.columns.get("tenant_code")
        assert column is not None and not column.nullable, table

    # event_log is pruned on the domain clock, not the write clock, if it ever is
    assert [p.timestamp_column for p in POLICIES
            if p.model is models.EventLog] == ["occurred_at"]


if __name__ == "__main__":
    test_apply_deletes_only_rows_older_than_the_window()
    test_dry_run_reports_expiry_but_deletes_nothing()
    test_deletion_is_batched_across_more_rows_than_one_batch()
    test_compliance_tables_are_exempt_even_under_a_days_override()
    test_empty_tables_are_safe()
    test_rows_with_a_null_timestamp_are_kept_and_counted()
    test_prune_ignores_an_inherited_tenant_scope()
    test_a_prunable_policy_over_a_nullable_tenant_column_is_refused()
    test_policy_table_covers_the_audited_tables_and_holds_its_invariants()
    print("RETENTION OK: expired rows pruned and in-window rows kept (boundary controlled); "
          "dry run deletes nothing; deletes batch across batch boundaries; audit_logs + event_log "
          "exempt under --days 0; empty tables safe; NULL timestamps kept; tenant scope ignored; "
          "nullable-tenant policies refused")
