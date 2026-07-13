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


if __name__ == "__main__":
    test_repository_isolates_and_stamps_tenants()
    test_tenant_of_reads_jwt_principal()
    test_migration_adds_and_backfills_tenant_code_idempotently()
    test_automatic_scoping_filters_reads_and_stamps_writes()
    print("TENANCY OK: repository + automatic scoping isolate tenants and stamp writes; migration adds/backfills idempotently")
