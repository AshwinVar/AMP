"""A relationship is not consent (ADR-0017).

Selling a factory a compressor tells you nothing about its order book. So two
independent things must BOTH hold before an OEM sees a field:

    1. the machine is one the OEM installed   (an installation row)
    2. that factory granted that class of data (a sharing policy)

This suite proves neither implies the other, that the default is nothing, and
that revoking a grant takes effect on the next read rather than whenever some
projection next runs.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_oem_sharing.py
"""
import os
import sys
from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import oem_sharing
import tenancy
from database import Base

_URL = os.environ.get("DATABASE_URL", "")
engine = (create_engine(_URL) if _URL.startswith("postgresql") else
          create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool))
SessionLocal = sessionmaker(bind=engine)

failures = []
_open = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def fresh_db():
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


def world(db):
    """Two OEMs, three factories, overlapping local machine names.

    OEM_ALPHA sells into FACTORY_A and FACTORY_B.
    OEM_BETA  sells into FACTORY_B and FACTORY_C.
    Every factory runs a machine called COMP-001.
    """
    tok = tenancy.set_current_tenant(None)
    for code, name in (("OEM_ALPHA", "Alpha"), ("OEM_BETA", "Beta")):
        db.add(models.OemOrganization(oem_code=code, name=name))
    db.flush()
    models_by_oem = {}
    for code in ("OEM_ALPHA", "OEM_BETA"):
        m = models.MachineModel(oem_code=code, family="Compressor",
                                model_code="X200", name=f"{code} X200",
                                service_interval_hours=2000)
        db.add(m)
        db.flush()
        models_by_oem[code] = m

    machines = {}
    for t in ("FACTORY_A", "FACTORY_B", "FACTORY_C"):
        mc = models.Machine(tenant_code=t, site="Plant 1", name="COMP-001",
                            status="Running", utilization=71)
        db.add(mc)
        db.flush()
        machines[t] = mc
        # Confidential operational data the OEM must never reach.
        db.add(models.WorkOrder(tenant_code=t, work_order_no="WO-001",
                                part_number="SECRET", batch_number="B1",
                                target_quantity=10, status="Planned"))

    installs = {}
    plan = [("OEM_ALPHA", "FACTORY_A", "SN-A1"), ("OEM_ALPHA", "FACTORY_B", "SN-A2"),
            ("OEM_BETA", "FACTORY_B", "SN-B1"), ("OEM_BETA", "FACTORY_C", "SN-B2")]
    for oem, tenant, serial in plan:
        row = models.MachineInstallation(
            oem_code=oem, serial_number=serial, model_id=models_by_oem[oem].id,
            factory_tenant_code=tenant, site="Plant 1",
            machine_id=machines[tenant].id, status="Active",
            operating_hours=1850.0, last_seen_at=datetime.utcnow(),
            warranty_start=date(2026, 1, 1), warranty_end=date(2028, 1, 1))
        db.add(row)
        db.flush()
        installs[(oem, tenant)] = row
    db.commit()
    tenancy.reset_current_tenant(tok)
    return installs, models_by_oem


def grant(db, oem, tenant, grants):
    tok = tenancy.set_current_tenant(None)
    row = (db.query(models.OemDataSharingPolicy)
             .filter(models.OemDataSharingPolicy.oem_code == oem,
                     models.OemDataSharingPolicy.tenant_code == tenant).first())
    if row is None:
        row = models.OemDataSharingPolicy(oem_code=oem, tenant_code=tenant, grants="")
        db.add(row)
    row.grants = ",".join(grants)
    db.commit()
    tenancy.reset_current_tenant(tok)


# =====================================================================
def test_a_relationship_is_not_consent():
    print("=" * 74)
    print("1. HAVING SOLD THEM A MACHINE GRANTS NOTHING")
    print("=" * 74)
    db = fresh_db()
    installs, mods = world(db)

    g = oem_sharing.grants_for(db, "OEM_ALPHA", "FACTORY_A")
    check("with no policy row, a factory shares NOTHING", g == set(), str(g))

    inst = installs[("OEM_ALPHA", "FACTORY_A")]
    row = oem_sharing.fleet_row(db, inst, g, mods["OEM_ALPHA"])
    check("...the OEM still sees its OWN records (serial, model, customer)",
          row["serial_number"] == "SN-A1" and row["model_code"] == "X200"
          and row["customer"] == "FACTORY_A", str(row)[:160])
    check("...but no operating hours", row["operating_hours"] is None,
          str(row["operating_hours"]))
    check("...no machine status", row["machine_status"] is None,
          str(row["machine_status"]))
    check("...no utilization", row["utilization"] is None, str(row["utilization"]))
    check("...and no last-seen", row["last_seen_at"] is None, str(row["last_seen_at"]))

    # An EMPTY policy row is a considered "no", and must behave like no row.
    grant(db, "OEM_ALPHA", "FACTORY_A", [])
    g = oem_sharing.grants_for(db, "OEM_ALPHA", "FACTORY_A")
    check("an empty policy row shares nothing either", g == set(), str(g))


def test_each_grant_opens_exactly_one_door():
    print()
    print("=" * 74)
    print("2. EACH GRANT OPENS EXACTLY ONE DOOR")
    print("=" * 74)
    db = fresh_db()
    installs, mods = world(db)
    inst = installs[("OEM_ALPHA", "FACTORY_A")]

    grant(db, "OEM_ALPHA", "FACTORY_A", [oem_sharing.SHARE_OPERATING_HOURS])
    g = oem_sharing.grants_for(db, "OEM_ALPHA", "FACTORY_A")
    row = oem_sharing.fleet_row(db, inst, g, mods["OEM_ALPHA"])
    check("SHARE_OPERATING_HOURS reveals hours", row["operating_hours"] == 1850.0,
          str(row["operating_hours"]))
    check("...and STILL not the machine status",
          row["machine_status"] is None, str(row["machine_status"]))

    grant(db, "OEM_ALPHA", "FACTORY_A", [oem_sharing.SHARE_MACHINE_HEALTH])
    g = oem_sharing.grants_for(db, "OEM_ALPHA", "FACTORY_A")
    row = oem_sharing.fleet_row(db, inst, g, mods["OEM_ALPHA"])
    check("SHARE_MACHINE_HEALTH reveals status", row["machine_status"] == "Running",
          str(row["machine_status"]))
    check("...and hours went back to hidden", row["operating_hours"] is None,
          str(row["operating_hours"]))

    # An unknown grant token must never widen anything.
    grant(db, "OEM_ALPHA", "FACTORY_A", ["SHARE_EVERYTHING", "SHARE_ORDERS"])
    g = oem_sharing.grants_for(db, "OEM_ALPHA", "FACTORY_A")
    check("unknown grant tokens are dropped, not honoured", g == set(), str(g))
    row = oem_sharing.fleet_row(db, inst, g, mods["OEM_ALPHA"])
    check("...so an invented grant reveals nothing",
          row["operating_hours"] is None and row["machine_status"] is None)


def test_a_grant_from_one_factory_does_not_travel():
    print()
    print("=" * 74)
    print("3. A GRANT FROM ONE FACTORY DOES NOT TRAVEL TO ANOTHER")
    print("=" * 74)
    db = fresh_db()
    installs, mods = world(db)

    # FACTORY_A is generous. FACTORY_B has granted nothing.
    grant(db, "OEM_ALPHA", "FACTORY_A", list(oem_sharing.ALL_GRANTS))

    a = oem_sharing.grants_for(db, "OEM_ALPHA", "FACTORY_A")
    b = oem_sharing.grants_for(db, "OEM_ALPHA", "FACTORY_B")
    check("FACTORY_A's generous grant is visible", len(a) == len(oem_sharing.ALL_GRANTS))
    check("FACTORY_B still shares nothing with the same OEM", b == set(), str(b))

    row_b = oem_sharing.fleet_row(db, installs[("OEM_ALPHA", "FACTORY_B")], b,
                                  mods["OEM_ALPHA"])
    check("...so FACTORY_B's machine shows no hours and no status",
          row_b["operating_hours"] is None and row_b["machine_status"] is None,
          str(row_b)[:160])

    # A grant to one OEM is not a grant to the other AT THE SAME FACTORY.
    # FACTORY_B must have a REAL policy row for OEM_ALPHA here: with no row at
    # all, dropping the oem_code filter from the lookup still finds nothing, and
    # the mutation "look the policy up by factory only" survives. The row is
    # what makes this assertion load-bearing.
    grant(db, "OEM_ALPHA", "FACTORY_B", [oem_sharing.SHARE_OPERATING_HOURS,
                                         oem_sharing.SHARE_ALARMS])
    mine = oem_sharing.grants_for(db, "OEM_ALPHA", "FACTORY_B")
    other = oem_sharing.grants_for(db, "OEM_BETA", "FACTORY_B")
    check("OEM_ALPHA's grant at FACTORY_B is visible to OEM_ALPHA",
          mine == {oem_sharing.SHARE_OPERATING_HOURS, oem_sharing.SHARE_ALARMS},
          str(mine))
    check("...and is NOT visible to OEM_BETA at the same factory",
          other == set(), str(other))
    row_beta = oem_sharing.fleet_row(db, installs[("OEM_BETA", "FACTORY_B")],
                                     other, mods["OEM_BETA"])
    check("...so OEM_BETA's machine there still shows no hours",
          row_beta["operating_hours"] is None, str(row_beta["operating_hours"]))

    # An installation not yet assigned to a customer has no factory to grant.
    unassigned = oem_sharing.grants_for(db, "OEM_ALPHA", None)
    check("an installation with no customer shares nothing", unassigned == set(),
          str(unassigned))
    check("...and a blank customer shares nothing either",
          oem_sharing.grants_for(db, "OEM_ALPHA", "") == set())
    check("...and a blank OEM shares nothing",
          oem_sharing.grants_for(db, "", "FACTORY_A") == set())


def test_one_oem_cannot_see_another_fleet():
    print()
    print("=" * 74)
    print("4. ONE MANUFACTURER CANNOT SEE ANOTHER'S FLEET")
    print("=" * 74)
    db = fresh_db()
    installs, mods = world(db)

    alpha = oem_sharing.installations_for(db, "OEM_ALPHA")
    beta = oem_sharing.installations_for(db, "OEM_BETA")
    check("OEM_ALPHA sees exactly its own two machines",
          {r.serial_number for r in alpha} == {"SN-A1", "SN-A2"},
          str([r.serial_number for r in alpha]))
    check("OEM_BETA sees exactly its own two machines",
          {r.serial_number for r in beta} == {"SN-B1", "SN-B2"},
          str([r.serial_number for r in beta]))

    # Both OEMs have machines at FACTORY_B. Neither sees the other's.
    at_b_alpha = oem_sharing.installations_for(db, "OEM_ALPHA", tenant_code="FACTORY_B")
    check("at a SHARED customer, OEM_ALPHA sees only its own machine",
          [r.serial_number for r in at_b_alpha] == ["SN-A2"],
          str([r.serial_number for r in at_b_alpha]))

    # Guessing a competitor's installation id must be a miss, not a 403.
    beta_id = beta[0].id
    check("fetching a competitor's installation by id returns nothing",
          oem_sharing.get_installation(db, "OEM_ALPHA", beta_id) is None)
    # ...and guessing its serial must miss too.
    check("fetching a competitor's serial returns nothing",
          oem_sharing.installations_for(db, "OEM_ALPHA", serial="SN-B1") == [])
    # CONTROL: its own id and serial DO resolve.
    check("CONTROL: its own installation id resolves",
          oem_sharing.get_installation(db, "OEM_ALPHA", alpha[0].id) is not None)
    check("CONTROL: its own serial resolves",
          len(oem_sharing.installations_for(db, "OEM_ALPHA", serial="SN-A1")) == 1)

    # A blank or missing manufacturer must return NOTHING, not everything. This
    # is the fail-open shape: `if not oem_code: pass` would hand a caller with
    # no identity the union of every manufacturer's fleet.
    check("a blank oem_code returns no installations at all",
          oem_sharing.installations_for(db, "") == [])
    check("a None oem_code returns no installations at all",
          oem_sharing.installations_for(db, None) == [])


def test_revocation_takes_effect_on_the_next_read():
    print()
    print("=" * 74)
    print("5. REVOKING A GRANT TAKES EFFECT IMMEDIATELY")
    print("=" * 74)
    db = fresh_db()
    installs, mods = world(db)
    inst = installs[("OEM_ALPHA", "FACTORY_A")]

    grant(db, "OEM_ALPHA", "FACTORY_A",
          [oem_sharing.SHARE_OPERATING_HOURS, oem_sharing.SHARE_MACHINE_HEALTH])
    g = oem_sharing.grants_for(db, "OEM_ALPHA", "FACTORY_A")
    before = oem_sharing.fleet_row(db, inst, g, mods["OEM_ALPHA"])
    check("while granted, hours and status are visible",
          before["operating_hours"] == 1850.0 and before["machine_status"] == "Running")

    grant(db, "OEM_ALPHA", "FACTORY_A", [])
    g = oem_sharing.grants_for(db, "OEM_ALPHA", "FACTORY_A")
    after = oem_sharing.fleet_row(db, inst, g, mods["OEM_ALPHA"])
    check("the very next read shows nothing",
          after["operating_hours"] is None and after["machine_status"] is None,
          str(after)[:160])
    check("...and the OEM still sees its own serial (relationship intact)",
          after["serial_number"] == "SN-A1")


def test_a_grant_never_opens_the_rest_of_the_factory():
    print()
    print("=" * 74)
    print("6. THE BROADEST POSSIBLE GRANT STILL HIDES THE FACTORY")
    print("=" * 74)
    db = fresh_db()
    installs, mods = world(db)
    # FACTORY_A grants EVERYTHING it can grant.
    grant(db, "OEM_ALPHA", "FACTORY_A", list(oem_sharing.ALL_GRANTS))
    g = oem_sharing.grants_for(db, "OEM_ALPHA", "FACTORY_A")

    import oem_auth
    tok = tenancy.set_current_tenant(oem_auth.sentinel_tenant("OEM_ALPHA"))
    leaked = {
        "work orders": db.query(models.WorkOrder).count(),
        "machines": db.query(models.Machine).count(),
    }
    tenancy.reset_current_tenant(tok)
    check("even with EVERY grant, an OEM request reads no factory table",
          all(v == 0 for v in leaked.values()), str(leaked))

    # The one door a grant does open is exactly one machine row, by id.
    machine = oem_sharing.visible_machine(db, installs[("OEM_ALPHA", "FACTORY_A")], g)
    check("SHARE_MACHINE_HEALTH opens exactly the machine it sold",
          machine is not None and machine.name == "COMP-001", str(machine))
    # ...and closing the grant closes that door again.
    closed = oem_sharing.visible_machine(db, installs[("OEM_ALPHA", "FACTORY_A")], set())
    check("...and without the grant, that door is shut", closed is None, str(closed))

    # The grant vocabulary contains nothing that could reach production data.
    for forbidden in ("ORDER", "RECIPE", "BOM", "INVENTORY", "COST", "CUSTOMER",
                      "PRODUCTION", "QUALITY", "OPERATOR"):
        check(f"no grant exposes {forbidden.lower()} data",
              not any(forbidden in gr for gr in oem_sharing.ALL_GRANTS))


def test_a_corrupted_installation_cannot_reach_another_factory():
    """visible_machine binds the tenant taken FROM THE INSTALLATION ROW, so it
    is only as trustworthy as whatever wrote that row. A row pairing one
    factory's tenant with another factory's machine id — a bad commissioning
    write, a botched migration, a hostile create — would otherwise bind the
    tenant that makes the target visible and hand it straight over."""
    print()
    print("=" * 74)
    print("7. A MISMATCHED INSTALLATION ROW REACHES NOTHING")
    print("=" * 74)
    db = fresh_db()
    installs, mods = world(db)
    grant(db, "OEM_ALPHA", "FACTORY_A", [oem_sharing.SHARE_MACHINE_HEALTH])
    g = oem_sharing.grants_for(db, "OEM_ALPHA", "FACTORY_A")

    tok = tenancy.set_current_tenant(None)
    victim = (db.query(models.Machine)
                .filter(models.Machine.tenant_code == "FACTORY_C").one())
    inst = installs[("OEM_ALPHA", "FACTORY_A")]
    # The attack: keep the tenant the OEM is allowed to read, point the machine
    # id at a DIFFERENT factory's machine.
    inst.machine_id = victim.id
    db.commit()
    tenancy.reset_current_tenant(tok)

    got = oem_sharing.visible_machine(db, inst, g)
    check("an installation pointing at another factory's machine resolves to None",
          got is None, f"leaked {getattr(got, 'tenant_code', None)}")

    # And the mirror: claim the victim's tenant while keeping your own machine.
    tok = tenancy.set_current_tenant(None)
    inst.factory_tenant_code = "FACTORY_C"
    db.commit()
    tenancy.reset_current_tenant(tok)
    stolen = oem_sharing.grants_for(db, "OEM_ALPHA", "FACTORY_C")
    check("...and claiming another factory's tenant grants nothing either",
          stolen == set(), str(stolen))


def run_all():
    test_a_relationship_is_not_consent()
    test_each_grant_opens_exactly_one_door()
    test_a_grant_from_one_factory_does_not_travel()
    test_one_oem_cannot_see_another_fleet()
    test_revocation_takes_effect_on_the_next_read()
    test_a_grant_never_opens_the_rest_of_the_factory()
    test_a_corrupted_installation_cannot_reach_another_factory()


def test_oem_sharing():
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
    print("A RELATIONSHIP IS NOT CONSENT, AND CONSENT IS NARROW")
