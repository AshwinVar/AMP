"""Tenant-scoping tests (ADR-0002 / PR #2).

Proves the mechanism in isolation, against an in-memory SQLite DB:
  * the scoped repository isolates tenants, stamps writes, and blocks
    cross-tenant reads;
  * ``tenant_of`` reads the JWT principal;
  * the startup migration adds tenant_code to a legacy table, backfills it,
    and is idempotent.

Run:  python backend/test_tenancy.py     (exit 0 = pass)
Also collectable by pytest.
"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from tenancy import TenantScopedRepository, tenant_of, ensure_tenant_columns


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_repository_isolates_and_stamps_tenants():
    db = _fresh_session()

    gmats = TenantScopedRepository(db, models.Machine, "GMATS")
    gmats.add(models.Machine(name="CNC-01", status="Running"))
    default = TenantScopedRepository(db, models.Machine, "DEFAULT")
    default.add(models.Machine(name="DEMO-01", status="Idle"))
    db.commit()

    # each tenant sees only its own rows
    assert [m.name for m in gmats.all()] == ["CNC-01"]
    assert [m.name for m in default.all()] == ["DEMO-01"]
    # writes were stamped with the caller's tenant
    assert gmats.all()[0].tenant_code == "GMATS"
    # a cross-tenant get() cannot reach the other tenant's row
    other_id = default.all()[0].id
    assert gmats.get(other_id) is None


def test_tenant_of_reads_jwt_principal():
    assert tenant_of({"tenant": "GMATS"}) == "GMATS"
    assert tenant_of({}) == "DEFAULT"
    assert tenant_of(None) == "DEFAULT"


def test_migration_adds_and_backfills_tenant_code_idempotently():
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE machines (id INTEGER PRIMARY KEY, name VARCHAR)"))
        c.execute(text("INSERT INTO machines (name) VALUES ('legacy')"))
    ensure_tenant_columns(engine, tables=["machines"])
    ensure_tenant_columns(engine, tables=["machines"])  # idempotent: no error, no dupes
    with engine.begin() as c:
        rows = list(c.execute(text("SELECT name, tenant_code FROM machines")))
    assert rows == [("legacy", "DEFAULT")]


def test_automatic_scoping_filters_reads_and_stamps_writes():
    import tenancy as T
    T.install_scoping()
    db = _fresh_session()

    # system context (no tenant): the row falls back to the column default
    db.add(models.Machine(name="SEED-01", status="Idle"))
    db.commit()

    # request as GMATS: create + read
    tok = T.set_current_tenant("GMATS")
    db.add(models.Machine(name="GM-01", status="Running"))
    db.commit()
    gmats_seen = [m.name for m in db.query(models.Machine).all()]
    T.reset_current_tenant(tok)

    # request as DEFAULT: read
    tok2 = T.set_current_tenant("DEFAULT")
    default_seen = [m.name for m in db.query(models.Machine).all()]
    T.reset_current_tenant(tok2)

    assert gmats_seen == ["GM-01"], gmats_seen         # GMATS sees only its row; write auto-stamped
    assert default_seen == ["SEED-01"], default_seen   # DEFAULT sees only the seed; filter not baked


def test_scoped_models_and_migration_tables_stay_in_lockstep():
    from tenancy import SCOPED_MODELS, CORE_TENANT_TABLES, FAIL_SAFE_TENANT_TABLES
    # Every scoped model has a migration entry: 28 core (backfilled to DEFAULT) +
    # 7 fail-safe (audit + enterprise inventory, added NULL, not backfilled).
    assert len(SCOPED_MODELS) == len(CORE_TENANT_TABLES) + len(FAIL_SAFE_TENANT_TABLES) == 35
    assert len(FAIL_SAFE_TENANT_TABLES) == 7
    # the fail-safe (audit + enterprise-inventory) models are scoped
    for m in (models.AuditLog, models.Remnant, models.MaterialIssueSlip,
              models.GoodsReceiptNote, models.GRNItem, models.CycleCount, models.CycleCountItem):
        assert m in SCOPED_MODELS, m.__name__
    # a few core ones still covered
    for m in (models.DowntimeLog, models.Escalation, models.QualityInspection,
              models.OperatorJobExecution, models.MaintenanceTask):
        assert m in SCOPED_MODELS, m.__name__
    # every scoped model actually carries the tenant_code column
    for m in SCOPED_MODELS:
        assert hasattr(m, "tenant_code"), m.__name__


def test_audit_and_enterprise_inventory_are_tenant_isolated():
    # The core of this PR: two tenants' audit + enterprise-inventory rows must not
    # leak across tenants at the ORM level, and a NULL-tenant (system/ambiguous)
    # row must be hidden from everyone rather than assigned to a tenant.
    import tenancy as T
    T.install_scoping()
    db = _fresh_session()

    def seed_as(tenant, objs):
        tok = T.set_current_tenant(tenant)
        for o in objs:
            db.add(o)
        db.commit()
        T.reset_current_tenant(tok)

    seed_as("TA", [
        models.AuditLog(actor="a_admin", action="login"),
        models.Remnant(tag_no="REM-A", item_id=1, original_qty=10, remaining_qty=10, unit="pc"),
        models.GRNItem(grn_id=1, item_id=1, received_qty=5, accepted_qty=5),
        models.CycleCount(count_no="CC-A", counted_by="a"),
    ])
    seed_as("TB", [
        models.AuditLog(actor="b_admin", action="login"),
        models.Remnant(tag_no="REM-B", item_id=2, original_qty=20, remaining_qty=20, unit="pc"),
        models.GRNItem(grn_id=2, item_id=2, received_qty=7, accepted_qty=7),
        models.CycleCount(count_no="CC-B", counted_by="b"),
    ])
    # a system-context row (no current tenant) — stays NULL, hidden from all
    db.add(models.AuditLog(actor="system", action="boot"))
    db.commit()

    def seen(tenant, model, attr):
        tok = T.set_current_tenant(tenant)
        vals = [getattr(r, attr) for r in db.query(model).all()]
        T.reset_current_tenant(tok)
        return vals

    assert seen("TA", models.AuditLog, "actor") == ["a_admin"]
    assert seen("TA", models.Remnant, "tag_no") == ["REM-A"]
    assert seen("TA", models.GRNItem, "received_qty") == [5]
    assert seen("TA", models.CycleCount, "count_no") == ["CC-A"]
    assert seen("TB", models.AuditLog, "actor") == ["b_admin"]
    assert seen("TB", models.Remnant, "tag_no") == ["REM-B"]
    assert seen("TB", models.GRNItem, "received_qty") == [7]
    # the NULL-tenant system audit row is invisible to both tenants (fail-safe)
    assert "system" not in seen("TA", models.AuditLog, "actor")
    assert "system" not in seen("TB", models.AuditLog, "actor")
    # writes were auto-stamped with the caller's tenant
    tok = T.set_current_tenant("TA")
    assert db.query(models.Remnant).all()[0].tenant_code == "TA"
    T.reset_current_tenant(tok)
    print("PASS audit trail + enterprise inventory are tenant-isolated (repo/ORM); NULL rows hidden")


def test_fail_safe_migration_adds_nullable_column_without_backfill():
    # The fail-safe migration must NOT assign legacy rows to DEFAULT: it adds the
    # column nullable and leaves existing rows NULL (hidden) until an approved,
    # source-based backfill. Contrast test_migration_..._backfills above (-> DEFAULT).
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE audit_logs (id INTEGER PRIMARY KEY, actor VARCHAR)"))
        c.execute(text("INSERT INTO audit_logs (actor) VALUES ('legacy')"))
    ensure_tenant_columns(engine, tables=["audit_logs"], backfill=False)
    ensure_tenant_columns(engine, tables=["audit_logs"], backfill=False)  # idempotent
    with engine.begin() as c:
        rows = list(c.execute(text("SELECT actor, tenant_code FROM audit_logs")))
    assert rows == [("legacy", None)], rows   # LEFT NULL, not backfilled to DEFAULT


if __name__ == "__main__":
    test_repository_isolates_and_stamps_tenants()
    test_tenant_of_reads_jwt_principal()
    test_migration_adds_and_backfills_tenant_code_idempotently()
    test_automatic_scoping_filters_reads_and_stamps_writes()
    test_scoped_models_and_migration_tables_stay_in_lockstep()
    test_audit_and_enterprise_inventory_are_tenant_isolated()
    test_fail_safe_migration_adds_nullable_column_without_backfill()
    print("TENANCY OK: repository + automatic scoping isolate tenants and stamp writes; 35 tables scoped "
          "(28 core + 7 fail-safe); audit + enterprise inventory isolated; NULL rows hidden; migration idempotent")
