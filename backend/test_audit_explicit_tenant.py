"""The audit row inside the caller's transaction, with an explicit tenant.

WHY THIS EXISTS (ADR-0021, critic finding C6)
---------------------------------------------
A service-contract transition is written by one party and must be recorded for
BOTH: one audit row in the factory's tenant and one in the manufacturer's
sentinel tenant, in the same transaction as the change itself. log_audit cannot
do that: it commits on its own and SWALLOWS every error, rolling back — inside a
business transaction that rollback would silently throw away the business change
the audit row was describing, and the caller would carry on and report success.

Three branches built this at once (the approval lock, AMP-native AI's consent,
this one), under three names. They are one layered API in platform_routes:

    build_audit_row(...)   the row's shape, decided once; not added, not committed
    add_audit(db, ...)     add ONLY, into the caller's transaction; a failure raises
    log_audit(db, ...)     add_audit + commit, never raises (a record on its own)

tenant_code=None keeps the historic behaviour (the ADR-0002 before_flush hook
stamps the request's tenant); an explicit tenant is KEPT by that hook.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_audit_explicit_tenant.py
"""
import inspect
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import platform_routes
import tenancy
from database import Base

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")
    return condition


def _engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _rows(engine):
    s = sessionmaker(bind=engine)()
    token = tenancy.set_current_tenant(None)
    try:
        return [(r.tenant_code, r.actor, r.action, r.entity_type, r.entity_id, r.details)
                for r in s.query(models.AuditLog).order_by(models.AuditLog.id.asc()).all()]
    finally:
        tenancy.reset_current_tenant(token)
        s.close()


class _SpySession:
    """Wraps a real session and records commit/rollback calls; `add` can be
    made to raise, which is the one failure log_audit itself can observe."""

    def __init__(self, real, add_raises=False):
        self.real = real
        self.add_raises = add_raises
        self.calls = []

    def add(self, obj):
        self.calls.append("add")
        if self.add_raises:
            raise RuntimeError("the database refused the audit row")
        self.real.add(obj)

    def commit(self):
        self.calls.append("commit")
        self.real.commit()

    def rollback(self):
        self.calls.append("rollback")
        self.real.rollback()


def test_signature():
    print("\n1. THE SIGNATURES: one parameter list, tenant_code defaulting to None")
    expected = ["db", "actor", "action", "entity_type", "entity_id", "details", "tenant_code"]
    for fn in (platform_routes.log_audit, platform_routes.add_audit):
        params = inspect.signature(fn).parameters
        check(f"{fn.__name__} takes {expected}", list(params) == expected, str(list(params)))
        check(f"...and its tenant_code defaults to None", params["tenant_code"].default is None)
    check("build_audit_row is the shape both use",
          list(inspect.signature(platform_routes.build_audit_row).parameters) == expected[1:])


def test_default_path_unchanged():
    print("\n2. commit=True (the default) behaves exactly as before")
    tenancy.install_scoping()
    engine = _engine()
    db = sessionmaker(bind=engine)()
    token = tenancy.set_current_tenant("FACTORY_A")
    try:
        platform_routes.log_audit(db, "alice", "thing_done", "widget", 7, "d")
    finally:
        tenancy.reset_current_tenant(token)
    rows = _rows(engine)
    check("the row is committed and stamped with the bound tenant",
          rows == [("FACTORY_A", "alice", "thing_done", "widget", 7, "d")], str(rows))

    platform_routes.log_audit(db, None, "system_thing")
    rows = _rows(engine)
    check("a missing actor is recorded as 'system'; an unbound system row stays NULL",
          rows[-1] == (None, "system", "system_thing", None, None, None), str(rows[-1]))

    spy = _SpySession(sessionmaker(bind=engine)(), add_raises=True)
    try:
        platform_routes.log_audit(spy, "bob", "fails")
        swallowed = True
    except Exception:
        swallowed = False
    check("a failure is still swallowed on the default path", swallowed)
    check("... and rolled back, never committed", spy.calls == ["add", "rollback"],
          str(spy.calls))
    db.close()


def test_add_audit_writes_inside_the_callers_transaction():
    print("\n3. add_audit adds only: the caller's commit decides")
    engine = _engine()
    db = sessionmaker(bind=engine)()
    spy = _SpySession(db)
    platform_routes.add_audit(spy, "carol", "contract_accepted", "service_contract", 3,
                              "ref=AMC-1", tenant_code="FACTORY_A")
    check("add_audit neither commits nor rolls back", spy.calls == ["add"], str(spy.calls))
    check("CONTROL: nothing is visible to another session before the caller commits",
          _rows(engine) == [], str(_rows(engine)))
    db.commit()
    check("the caller's commit writes the row, with its explicit tenant",
          _rows(engine) == [("FACTORY_A", "carol", "contract_accepted", "service_contract",
                             3, "ref=AMC-1")], str(_rows(engine)))

    platform_routes.add_audit(db, "carol", "rolled_back", tenant_code="FACTORY_A")
    db.rollback()
    check("the caller's rollback removes the row with the business change",
          [r[2] for r in _rows(engine)] == ["contract_accepted"], str(_rows(engine)))
    db.close()


def test_add_audit_raises_into_the_caller():
    print("\n4. add_audit never swallows: a failure raises into the caller")
    engine = _engine()
    spy = _SpySession(sessionmaker(bind=engine)(), add_raises=True)
    raised = None
    try:
        platform_routes.add_audit(spy, "dave", "contract_rejected", tenant_code="FACTORY_A")
    except RuntimeError as e:
        raised = e
    check("the failure propagates to the caller", raised is not None)
    check("... and add_audit did not roll the caller's transaction back",
          "rollback" not in spy.calls and "commit" not in spy.calls, str(spy.calls))


def test_business_change_and_audit_are_atomic():
    print("\n5. A failed business commit leaves no audit row behind")
    engine = _engine()
    db = sessionmaker(bind=engine)()
    db.add(models.OemOrganization(oem_code="OEM_ALPHA", name="Alpha"))
    db.commit()
    db.add(models.OemOrganization(oem_code="OEM_ALPHA", name="Alpha again"))  # duplicate key
    platform_routes.add_audit(db, "erin", "oem_created", tenant_code="OEM:OEM_ALPHA")
    failed = False
    try:
        db.commit()
    except Exception:
        failed = True
        db.rollback()
    check("CONTROL: the business commit really failed", failed)
    check("no audit row survives the failed commit", _rows(engine) == [], str(_rows(engine)))
    db.close()


def test_explicit_tenant_survives_the_stamp_hook():
    print("\n6. The ADR-0002 stamp hook keeps an explicit tenant")
    tenancy.install_scoping()
    engine = _engine()
    db = sessionmaker(bind=engine)()
    token = tenancy.set_current_tenant("FACTORY_A")
    try:
        platform_routes.add_audit(db, "oem:OEM_ALPHA:x", "contract_accepted",
                                  tenant_code="OEM:OEM_ALPHA")
        platform_routes.add_audit(db, "fa", "contract_accepted")
        db.commit()
        platform_routes.log_audit(db, "fa", "committed_with_tenant", tenant_code="FACTORY_B")
    finally:
        tenancy.reset_current_tenant(token)
    rows = _rows(engine)
    check("an explicit sentinel tenant is kept while FACTORY_A is bound",
          rows[0][0] == "OEM:OEM_ALPHA", str(rows))
    check("CONTROL: without tenant_code the bound tenant is stamped",
          rows[1][0] == "FACTORY_A", str(rows))
    check("an explicit tenant is honoured on the commit=True path too",
          rows[2][0] == "FACTORY_B", str(rows))
    db.close()


if __name__ == "__main__":
    test_signature()
    test_default_path_unchanged()
    test_add_audit_writes_inside_the_callers_transaction()
    test_add_audit_raises_into_the_caller()
    test_business_change_and_audit_are_atomic()
    test_explicit_tenant_survives_the_stamp_hook()
    print()
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        sys.exit(1)
    print("ALL EXPLICIT-TENANT AUDIT TESTS PASSED")
