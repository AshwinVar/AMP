"""platform_routes.log_audit with an explicit tenant, inside the caller's transaction.

WHY THIS EXISTS (ADR-0021, critic finding C6)
---------------------------------------------
A service-contract transition is written by one party and must be recorded for
BOTH: one audit row in the factory's tenant and one in the manufacturer's
sentinel tenant, in the same transaction as the change itself. The historic
log_audit cannot do that. It commits on its own, and it SWALLOWS every error
and rolls back — inside a business transaction that rollback would silently
throw away the business change the audit row was describing, and the caller
would carry on and report success.

So log_audit grows two keyword-only arguments:

    tenant_code=None   the tenant the row belongs to. None keeps the historic
                       behaviour (the ADR-0002 before_flush hook stamps the
                       request's tenant). An explicit tenant is KEPT by that hook.
    commit=True        True: unchanged (add, commit, swallow and roll back).
                       False: add only. No commit, no try/except, no rollback. A
                       failure raises into the caller's transaction, and the row
                       lives or dies with the caller's commit.

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
    print("\n1. THE SIGNATURE: tenant_code and commit are keyword-only, defaults unchanged")
    params = inspect.signature(platform_routes.log_audit).parameters
    check("tenant_code is keyword-only and defaults to None",
          "tenant_code" in params and params["tenant_code"].kind is inspect.Parameter.KEYWORD_ONLY
          and params["tenant_code"].default is None, str(params))
    check("commit is keyword-only and defaults to True",
          "commit" in params and params["commit"].kind is inspect.Parameter.KEYWORD_ONLY
          and params["commit"].default is True, str(params))
    check("the six historic positional parameters are unchanged",
          list(params)[:6] == ["db", "actor", "action", "entity_type", "entity_id", "details"],
          str(list(params)))


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


def test_commit_false_writes_inside_the_callers_transaction():
    print("\n3. commit=False adds only: the caller's commit decides")
    engine = _engine()
    db = sessionmaker(bind=engine)()
    spy = _SpySession(db)
    platform_routes.log_audit(spy, "carol", "contract_accepted", "service_contract", 3,
                              "ref=AMC-1", tenant_code="FACTORY_A", commit=False)
    check("commit=False neither commits nor rolls back", spy.calls == ["add"], str(spy.calls))
    check("CONTROL: nothing is visible to another session before the caller commits",
          _rows(engine) == [], str(_rows(engine)))
    db.commit()
    check("the caller's commit writes the row, with its explicit tenant",
          _rows(engine) == [("FACTORY_A", "carol", "contract_accepted", "service_contract",
                             3, "ref=AMC-1")], str(_rows(engine)))

    platform_routes.log_audit(db, "carol", "rolled_back", tenant_code="FACTORY_A",
                              commit=False)
    db.rollback()
    check("the caller's rollback removes the row with the business change",
          [r[2] for r in _rows(engine)] == ["contract_accepted"], str(_rows(engine)))
    db.close()


def test_commit_false_raises_into_the_caller():
    print("\n4. commit=False never swallows: a failure raises into the caller")
    engine = _engine()
    spy = _SpySession(sessionmaker(bind=engine)(), add_raises=True)
    raised = None
    try:
        platform_routes.log_audit(spy, "dave", "contract_rejected", tenant_code="FACTORY_A",
                                  commit=False)
    except RuntimeError as e:
        raised = e
    check("the failure propagates to the caller", raised is not None)
    check("... and log_audit did not roll the caller's transaction back",
          "rollback" not in spy.calls and "commit" not in spy.calls, str(spy.calls))


def test_business_change_and_audit_are_atomic():
    print("\n5. A failed business commit leaves no audit row behind")
    engine = _engine()
    db = sessionmaker(bind=engine)()
    db.add(models.OemOrganization(oem_code="OEM_ALPHA", name="Alpha"))
    db.commit()
    db.add(models.OemOrganization(oem_code="OEM_ALPHA", name="Alpha again"))  # duplicate key
    platform_routes.log_audit(db, "erin", "oem_created", tenant_code="OEM:OEM_ALPHA",
                              commit=False)
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
        platform_routes.log_audit(db, "oem:OEM_ALPHA:x", "contract_accepted",
                                  tenant_code="OEM:OEM_ALPHA", commit=False)
        platform_routes.log_audit(db, "fa", "contract_accepted", commit=False)
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
    test_commit_false_writes_inside_the_callers_transaction()
    test_commit_false_raises_into_the_caller()
    test_business_change_and_audit_are_atomic()
    test_explicit_tenant_survives_the_stamp_hook()
    print()
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        sys.exit(1)
    print("ALL EXPLICIT-TENANT AUDIT TESTS PASSED")
