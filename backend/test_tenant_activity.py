"""SaaS tenant-adoption panel tests (founder-only /saas/tenant-activity).

Pins: per-tenant counts are isolated (the ADR-0002 scoping applies to every
count via set_current_tenant — no cross-tenant leak), the 7-day window bounds
production/orders, the active flag and quietest-first ordering, founder-only
access, and empty-registry safety.

Run:  python backend/test_tenant_activity.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models
import saas_routes
import tenancy
from database import Base

NOW = datetime.utcnow()


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    return sessionmaker(bind=engine)()


def _seed_tenant(db, code, users, machines, prod_recent, prod_old, orders_recent, esc_open):
    token = tenancy.set_current_tenant(code)
    try:
        for i in range(users):
            # User is not auto-stamped (not in SCOPED_MODELS) — set the tenant
            # explicitly, exactly as the real registration/provisioning paths do.
            db.add(models.User(username=f"{code.lower()}-u{i}", password="x",
                               role="Operator", tenant_code=code))
        mids = []
        for i in range(machines):
            m = models.Machine(name=f"{code}-M{i}", status="Running", utilization=50)
            db.add(m)
            db.flush()
            mids.append(m.id)
        for i in range(prod_recent):
            r = models.ProductionRecord(machine_id=mids[0], planned_minutes=60, runtime_minutes=60,
                                        ideal_cycle_time_seconds=60, total_count=10, good_count=10,
                                        rejected_count=0)
            r.created_at = NOW - timedelta(days=1)
            db.add(r)
        for i in range(prod_old):
            r = models.ProductionRecord(machine_id=mids[0], planned_minutes=60, runtime_minutes=60,
                                        ideal_cycle_time_seconds=60, total_count=10, good_count=10,
                                        rejected_count=0)
            r.created_at = NOW - timedelta(days=30)
            db.add(r)
        for i in range(orders_recent):
            o = models.CustomerOrder(order_no=f"{code}-O{i}", customer_name="C", product_name="P",
                                     order_quantity=10, dispatched_quantity=0, status="Pending",
                                     due_date=(NOW.date() + timedelta(days=5)))
            o.created_at = NOW - timedelta(days=2)
            db.add(o)
        for i in range(esc_open):
            db.add(models.Escalation(title=f"{code}-E{i}", severity="High", owner="Ops",
                                     department="Maintenance", status="Open"))
        db.commit()
    finally:
        tenancy.reset_current_tenant(token)


def test_counts_are_isolated_windowed_and_ordered():
    db = _fresh_session()
    db.add(models.CompanyTenant(company_code="ACME", company_name="Acme",
                                plan_name="Growth", subscription_status="Active"))
    db.add(models.CompanyTenant(company_code="BOLT", company_name="Bolt",
                                plan_name="Starter", subscription_status="Trial"))
    db.commit()
    # ACME is busy; BOLT is quiet (old production only); DEFAULT has a machine, no activity.
    _seed_tenant(db, "ACME", users=3, machines=2, prod_recent=5, prod_old=2,
                 orders_recent=1, esc_open=2)
    _seed_tenant(db, "BOLT", users=1, machines=1, prod_recent=0, prod_old=4,
                 orders_recent=0, esc_open=0)
    _seed_tenant(db, "DEFAULT", users=1, machines=1, prod_recent=0, prod_old=0,
                 orders_recent=0, esc_open=0)

    r = saas_routes.get_tenant_activity(db=db, current_user={"tenant": "DEFAULT"})
    assert r["days"] == 7
    by = {t["tenant_code"]: t for t in r["tenants"]}
    assert set(by) == {"DEFAULT", "ACME", "BOLT"}

    acme = by["ACME"]
    assert acme["users"] == 3 and acme["machines"] == 2          # isolated, no leak
    assert acme["production_7d"] == 5                            # the 2 old rows are out
    assert acme["orders_7d"] == 1 and acme["open_escalations"] == 2
    assert acme["active"] is True and acme["last_production_at"] is not None

    bolt = by["BOLT"]
    assert bolt["users"] == 1 and bolt["machines"] == 1
    assert bolt["production_7d"] == 0 and bolt["active"] is False
    assert bolt["last_production_at"] is not None                # old production still stamps it

    default = by["DEFAULT"]
    assert default["users"] == 1 and default["last_production_at"] is None

    # Quietest first: the two inactive tenants precede busy ACME.
    assert r["tenants"][-1]["tenant_code"] == "ACME"
    assert r["active_count"] == 1 and r["quiet_count"] == 2


def test_open_escalation_count_includes_null_status_excludes_terminal():
    """open_escalations = every escalation NOT in a terminal state, with a NULL
    status counted as open. Escalation.status is nullable (default "Open"), so a
    raw-SQL / migration / cleared-field row can hold a NULL; the old
    `status NOT IN (...)` filter evaluated to NULL — not TRUE — for it and
    silently dropped an open NULL-status escalation, undercounting the panel
    (the #403 bug class). Independently-derived expected: of Open + NULL +
    Resolved + Cancelled + Closed, exactly the first two are open → 2."""
    db = _fresh_session()
    db.add(models.CompanyTenant(company_code="ACME", company_name="Acme",
                                plan_name="Growth", subscription_status="Active"))
    db.commit()

    token = tenancy.set_current_tenant("ACME")
    try:
        for title, status in [("A-open", "Open"), ("A-null", "Open"),
                              ("A-resolved", "Resolved"), ("A-cancelled", "Cancelled"),
                              ("A-closed", "Closed")]:
            db.add(models.Escalation(title=title, severity="High", owner="Ops",
                                     department="Maintenance", status=status))
        db.commit()
        # The ORM default fills an explicit None on insert, so a genuine NULL only
        # arises out-of-band — reproduce it with a raw UPDATE (a migration / raw
        # write / an update that cleared the field), exactly as in production.
        db.execute(text("UPDATE escalations SET status=NULL WHERE title='A-null'"))
        db.commit()
    finally:
        tenancy.reset_current_tenant(token)

    r = saas_routes.get_tenant_activity(db=db, current_user={"tenant": "DEFAULT"})
    acme = {t["tenant_code"]: t for t in r["tenants"]}["ACME"]
    # Open + NULL count; Resolved / Cancelled / Closed do not.
    assert acme["open_escalations"] == 2, acme["open_escalations"]


def test_founder_only_and_empty_registry_safe():
    db = _fresh_session()
    try:
        saas_routes.get_tenant_activity(db=db, current_user={"tenant": "ACME"})
        assert False, "a client Admin reading cross-tenant adoption should 403"
    except Exception as e:
        assert getattr(e, "status_code", None) == 403, e

    r = saas_routes.get_tenant_activity(db=db, current_user={"tenant": "DEFAULT"})
    assert [t["tenant_code"] for t in r["tenants"]] == ["DEFAULT"]
    assert r["active_count"] == 0 and r["quiet_count"] == 1


if __name__ == "__main__":
    test_counts_are_isolated_windowed_and_ordered()
    print("ok  counts isolated per tenant, 7d-windowed, quietest-first, active flag")
    test_open_escalation_count_includes_null_status_excludes_terminal()
    print("ok  open-escalation count includes NULL status, excludes terminal states")
    test_founder_only_and_empty_registry_safe()
    print("ok  founder-only + empty registry safe")
    print("\nALL TENANT-ACTIVITY TESTS PASSED")
