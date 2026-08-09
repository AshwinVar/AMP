"""The OEM ownership dimension (ADR-0017).

AMP's every existing question is "what belongs to this tenant". The OEM question
is "what belongs to this manufacturer, across tenants, to the extent each of
those tenants has agreed". This suite pins the foundation that makes the second
question answerable without weakening the first.

THE MEASUREMENT THAT SHAPES THE DESIGN
--------------------------------------
The ADR-0002 hook is a no-op when nothing is bound. Against the real hook, two
machines seeded in two tenants:

    bound tenant = 'FACTORY_A'          -> 1 machine   ['FACTORY_A']
    bound tenant = '__OEM_NO_FACTORY__' -> 0 machines  []
    bound tenant =  None                -> 2 machines  ['FACTORY_A','FACTORY_B']

So an OEM request must never run unbound. Section 4 pins the sentinel property
that the rest of the OEM layer is built on.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_oem_foundation.py
"""
import os
import sys
from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import offboard_tenant
import tenancy
from database import Base

_URL = os.environ.get("DATABASE_URL", "")
engine = (create_engine(_URL) if _URL.startswith("postgresql") else
          create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool))
SessionLocal = sessionmaker(bind=engine)

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


_open = []


def fresh_db():
    """A clean database, and NO session left holding locks.

    Closing the previous session is not tidiness. On PostgreSQL an unclosed
    session sits `idle in transaction` holding locks on the tables it touched,
    and the next `drop_all` then blocks forever — this suite hung for ten
    minutes with no output before the sessions were tracked.
    """
    while _open:
        try:
            _open.pop().close()
        except Exception:
            pass
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    db = SessionLocal()
    _open.append(db)
    return db


def seed_oem(db, oem_code, name):
    db.add(models.OemOrganization(oem_code=oem_code, name=name))
    db.flush()
    m = models.MachineModel(oem_code=oem_code, family="Compressor",
                            model_code="X200", name=f"{name} X200",
                            service_interval_hours=2000, warranty_months=24)
    db.add(m)
    db.flush()
    return m


def install(db, oem_code, model, serial, tenant, site="Plant 1", machine_id=None):
    row = models.MachineInstallation(
        oem_code=oem_code, serial_number=serial, model_id=model.id,
        factory_tenant_code=tenant, site=site, machine_id=machine_id,
        status="Active", warranty_start=date(2026, 1, 1),
        warranty_end=date(2028, 1, 1), operating_hours=1500.0)
    db.add(row)
    db.flush()
    return row


# =====================================================================
def test_serial_is_unique_per_oem_not_globally():
    print("=" * 74)
    print("1. SERIAL NUMBER IS THE DURABLE IDENTITY, UNIQUE PER OEM")
    print("=" * 74)
    db = fresh_db()
    tok = tenancy.set_current_tenant(None)
    alpha = seed_oem(db, "OEM_ALPHA", "Alpha Compressors")
    beta = seed_oem(db, "OEM_BETA", "Beta Systems")
    db.commit()

    install(db, "OEM_ALPHA", alpha, "SN-0001", "FACTORY_A")
    db.commit()

    # TWO manufacturers may both use SN-0001. Global uniqueness would let one
    # enumerate the other's fleet by probing for collisions.
    try:
        install(db, "OEM_BETA", beta, "SN-0001", "FACTORY_B")
        db.commit()
        check("two OEMs can both use serial SN-0001", True)
    except IntegrityError as e:
        db.rollback()
        check("two OEMs can both use serial SN-0001", False, str(e)[:120])

    # The SAME manufacturer may not reuse one twice — that is a real collision.
    try:
        install(db, "OEM_ALPHA", alpha, "SN-0001", "FACTORY_C")
        db.commit()
        check("one OEM cannot reuse its own serial", False, "the duplicate was accepted")
    except IntegrityError:
        db.rollback()
        check("one OEM cannot reuse its own serial", True)

    # Identity survives renaming: the serial is not derived from the factory's
    # machine name, so renaming the machine cannot change it.
    row = (db.query(models.MachineInstallation)
             .filter(models.MachineInstallation.oem_code == "OEM_ALPHA").first())
    before = row.serial_number
    machine = models.Machine(tenant_code="FACTORY_A", site="Plant 1",
                             name="COMP-001", status="Running", utilization=70)
    db.add(machine)
    db.flush()
    row.machine_id = machine.id
    db.commit()
    machine.name = "RENAMED-999"
    machine.site = "Plant 9"
    db.commit()
    db.refresh(row)
    check("renaming the factory machine does not change the serial",
          row.serial_number == before, f"{before} -> {row.serial_number}")
    check("...and the installation still points at the same machine",
          row.machine_id == machine.id)
    tenancy.reset_current_tenant(tok)


def test_installation_is_invisible_to_the_purge_sweep():
    """The bug this design avoids, pinned as a property.

    offboard_tenant.purge_tenant_data sweeps EVERY mapper carrying a
    `tenant_code` attribute and hard-deletes rows matching the departing tenant.
    An OEM's record of a machine it BUILT belongs to the OEM. Had the column been
    called `tenant_code`, offboarding one customer would have erased another
    company's fleet history.
    """
    print()
    print("=" * 74)
    print("2. OFFBOARDING A FACTORY RELEASES ITS OEM EQUIPMENT, NEVER DELETES IT")
    print("=" * 74)
    db = fresh_db()
    tok = tenancy.set_current_tenant(None)
    alpha = seed_oem(db, "OEM_ALPHA", "Alpha")
    db.commit()
    machine = models.Machine(tenant_code="FACTORY_A", site="Plant 1",
                             name="COMP-001", status="Running", utilization=70)
    db.add(machine)
    db.flush()
    inst = install(db, "OEM_ALPHA", alpha, "SN-A-1", "FACTORY_A",
                   machine_id=machine.id)
    keep = install(db, "OEM_ALPHA", alpha, "SN-B-1", "FACTORY_B")
    db.commit()
    inst_id, keep_id = inst.id, keep.id

    check("the model does NOT expose a `tenant_code` attribute, so the sweep "
          "cannot see it",
          not hasattr(models.MachineInstallation, "tenant_code"))

    offboard_tenant.purge_tenant_data(db, "FACTORY_A")

    survivor = db.query(models.MachineInstallation).filter(
        models.MachineInstallation.id == inst_id).first()
    check("the OEM's installation record SURVIVED the purge", survivor is not None,
          "it was deleted — the OEM lost a machine it built")
    if survivor:
        check("...unlinked from the departed factory",
              survivor.factory_tenant_code is None, str(survivor.factory_tenant_code))
        check("...unlinked from the deleted machine row",
              survivor.machine_id is None, str(survivor.machine_id))
        check("...and marked decommissioned",
              survivor.status == "Decommissioned", survivor.status)
        check("...keeping its serial and model",
              survivor.serial_number == "SN-A-1" and survivor.model_id == alpha.id)
    # CONTROL: the factory's own machine IS gone, so the purge really ran.
    check("CONTROL: the factory's machine row was purged",
          db.query(models.Machine).filter(
              models.Machine.tenant_code == "FACTORY_A").count() == 0)
    other = db.query(models.MachineInstallation).filter(
        models.MachineInstallation.id == keep_id).first()
    check("CONTROL: an installation at ANOTHER factory is untouched",
          other is not None and other.factory_tenant_code == "FACTORY_B",
          str(other and other.factory_tenant_code))
    tenancy.reset_current_tenant(tok)


def test_sharing_policy_defaults_to_deny():
    print()
    print("=" * 74)
    print("3. SHARING IS EXPLICIT, AND DEFAULTS TO NOTHING")
    print("=" * 74)
    db = fresh_db()
    tok = tenancy.set_current_tenant(None)
    db.add(models.OemOrganization(oem_code="OEM_ALPHA", name="Alpha"))
    db.commit()

    found = (db.query(models.OemDataSharingPolicy)
               .filter(models.OemDataSharingPolicy.oem_code == "OEM_ALPHA",
                       models.OemDataSharingPolicy.tenant_code == "FACTORY_A")
               .first())
    check("no policy row exists until a factory grants one", found is None)

    db.add(models.OemDataSharingPolicy(oem_code="OEM_ALPHA",
                                       tenant_code="FACTORY_A", grants=""))
    db.commit()
    row = (db.query(models.OemDataSharingPolicy)
             .filter(models.OemDataSharingPolicy.oem_code == "OEM_ALPHA").one())
    check("an empty grant string is a real row sharing nothing", row.grants == "",
          repr(row.grants))

    # One policy per (oem, factory) — a second row would make "what is shared"
    # ambiguous, and ambiguity resolves in someone's favour.
    try:
        db.add(models.OemDataSharingPolicy(oem_code="OEM_ALPHA",
                                           tenant_code="FACTORY_A", grants="SHARE_ALARMS"))
        db.commit()
        check("a duplicate (oem, factory) policy is refused", False, "accepted")
    except IntegrityError:
        db.rollback()
        check("a duplicate (oem, factory) policy is refused", True)
    tenancy.reset_current_tenant(tok)


def test_a_sentinel_tenant_hides_every_factory_table():
    """The security keystone (ADR-0017). An OEM principal binds a tenant that
    matches no factory, so factory operational tables return nothing BY
    CONSTRUCTION — before any OEM route exists, and whether or not its author
    remembers to filter."""
    print()
    print("=" * 74)
    print("4. A SENTINEL TENANT HIDES EVERY FACTORY TABLE (THE KEYSTONE)")
    print("=" * 74)
    db = fresh_db()
    tok = tenancy.set_current_tenant(None)
    for t in ("FACTORY_A", "FACTORY_B"):
        db.add(models.Machine(tenant_code=t, site="Plant 1", name="COMP-001",
                              status="Running", utilization=70))
        db.add(models.WorkOrder(tenant_code=t, work_order_no="WO-001",
                                part_number="P-1", batch_number="B1",
                                target_quantity=10, status="Planned"))
        db.add(models.InventoryItem(tenant_code=t, item_code="INV-001",
                                    item_name="Steel", category="Raw",
                                    unit="kg", current_stock=100,
                                    reorder_level=5))
    db.commit()
    tenancy.reset_current_tenant(tok)

    tables = {"machines": models.Machine, "work orders": models.WorkOrder,
              "inventory": models.InventoryItem}

    tok = tenancy.set_current_tenant("OEM:OEM_ALPHA")
    seen = {label: db.query(m).count() for label, m in tables.items()}
    tenancy.reset_current_tenant(tok)
    check("an OEM-sentinel request sees ZERO rows in every factory table",
          all(v == 0 for v in seen.values()), str(seen))

    # CONTROL 1: a real factory still sees its own rows, so the tables are not
    # simply empty.
    tok = tenancy.set_current_tenant("FACTORY_A")
    own = {label: db.query(m).count() for label, m in tables.items()}
    tenancy.reset_current_tenant(tok)
    check("CONTROL: a real factory still sees its own rows",
          all(v == 1 for v in own.values()), str(own))

    # CONTROL 2: unbound sees EVERYTHING — this is why the sentinel exists and
    # why an OEM request must never fall back to None.
    tok = tenancy.set_current_tenant(None)
    unbound = {label: db.query(m).count() for label, m in tables.items()}
    tenancy.reset_current_tenant(tok)
    check("CONTROL: an UNBOUND request sees every tenant's rows (the trap)",
          all(v == 2 for v in unbound.values()), str(unbound))


def test_oem_principals_are_a_separate_table():
    print()
    print("=" * 74)
    print("5. OEM PRINCIPALS ARE NOT FACTORY USERS")
    print("=" * 74)
    db = fresh_db()
    tok = tenancy.set_current_tenant(None)
    db.add(models.OemOrganization(oem_code="OEM_ALPHA", name="Alpha"))
    db.add(models.OemUser(oem_code="OEM_ALPHA", username="alpha-admin",
                          password="x", role="OEM_ADMIN"))
    db.add(models.User(username="factory-admin", password="x", role="Admin",
                       tenant_code="FACTORY_A"))
    db.commit()

    check("an OEM user is not in the factory users table",
          db.query(models.User).filter(
              models.User.username == "alpha-admin").first() is None)
    check("a factory user is not in the OEM users table",
          db.query(models.OemUser).filter(
              models.OemUser.username == "factory-admin").first() is None)
    check("OEM roles are a different vocabulary from factory roles",
          db.query(models.OemUser).one().role.startswith("OEM_"))
    check("an OEM user carries no tenant_code at all",
          not hasattr(models.OemUser, "tenant_code"))
    tenancy.reset_current_tenant(tok)


def run_all():
    test_serial_is_unique_per_oem_not_globally()
    test_installation_is_invisible_to_the_purge_sweep()
    test_sharing_policy_defaults_to_deny()
    test_a_sentinel_tenant_hides_every_factory_table()
    test_oem_principals_are_a_separate_table()


def test_oem_foundation():
    run_all()
    assert not failures, failures


if __name__ == "__main__":
    run_all()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        sys.exit(1)
    print("THE OEM DIMENSION EXISTS, AND CANNOT REACH FACTORY DATA")
