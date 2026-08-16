"""The demo reset survives a demo having been run (M5 of the pilot brief).

THE DEFECT THIS PINS
--------------------
`demo_aeron.py --reset` worked exactly once — on a clean database, before any
telemetry. Run a demo and reset again, which is the only time anybody resets,
and it died on a foreign key:

    update or delete on table "machines" violates foreign key constraint
    "industrial_devices_linked_machine_id_fkey"

The module's own docstring said "WHY IT IS SAFE TO RUN TWICE". It was not, and
nothing checked, because every existing test seeded a fresh database.

THREE ROWS BLOCK THE WIPE, AND THEY ARE NOT ALL THE DEMO'S TO DELETE
--------------------------------------------------------------------
`IndustrialDevice.linked_machine_id` and `IndustrialSignal.machine_id` both
point at `machines`. A row holding one of those may belong to the demo tenant —
or to somebody else, which is exactly what the row that first broke this was: a
DEFAULT-tenant device pointing at a demo machine.

So the wipe treats them differently, and this suite exists to keep that
distinction:

    demo-owned    ->  deleted, it is demo scope
    other tenant  ->  UNLINKED, never deleted

Deleting the second kind would be a demo script reaching outside the demo, which
is the one thing `_assert_demo_scope` and the whole file are built to prevent.

WHY THIS ASSERTS ON ROWS AND NOT ON THE EXCEPTION
-------------------------------------------------
SQLite does not enforce foreign keys unless asked, so on CI the broken wipe did
not raise at all — it "succeeded" and left the orphans behind. Only PostgreSQL
threw. A suite that checked for the exception would therefore have been green on
CI and red in the room, which is the same environment blind spot the repo has
been caught by before (#405, #407, #412). So the checks below count rows, and
they fail identically on both engines: removing the fix turns four of them red
on SQLite.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_demo_reset_repeatable.py
"""
import os

os.environ.setdefault("DEMO_PASSWORD", "reset-suite-password")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import database
import demo_aeron
import models
import tenancy
from database import Base

_URL = os.environ.get("DATABASE_URL", "")
engine = (create_engine(_URL) if _URL.startswith("postgresql") else
          create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool))
SessionLocal = sessionmaker(bind=engine)

failures = []

OTHER = "SOMEBODY_ELSE"


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def session():
    db = SessionLocal()
    return db, tenancy.set_current_tenant(None)


def plant_blockers(round_no=1):
    """Everything that referenced a demo machine and broke the second reset.

    The other tenant's device is RE-POINTED rather than re-inserted on later
    rounds, because it survives the wipe — which is the property under test. A
    second insert of the same `device_code` trips the unique constraint, and
    that collision is itself evidence the row was left alone.
    """
    db, tok = session()
    demo = [m.id for m in db.query(models.Machine)
            .filter(models.Machine.tenant_code == demo_aeron.DEMO_TENANT).all()]

    # 1. A device the DEMO owns, with a signal hanging off it. The demo scope is
    #    wiped each round, so this one is always new.
    mine = models.IndustrialDevice(
        tenant_code=demo_aeron.DEMO_TENANT, device_code=f"DEMO-GW-{round_no:02d}",
        device_name="Demo gateway", device_type="PLC", protocol="MQTT",
        linked_machine_id=demo[0])
    db.add(mine)
    db.flush()
    db.add(models.IndustrialSignal(
        tenant_code=demo_aeron.DEMO_TENANT, device_id=mine.id,
        machine_id=demo[0], signal_name="HrsRun", signal_value="3960"))

    # 2. A device ANOTHER tenant owns, pointing at a demo machine. This is the
    #    row that actually broke the reset, and it must survive.
    theirs = (db.query(models.IndustrialDevice)
                .filter(models.IndustrialDevice.tenant_code == OTHER,
                        models.IndustrialDevice.device_code == "THEIR-PLC-01")
                .first())
    if theirs is None:
        theirs = models.IndustrialDevice(
            tenant_code=OTHER, device_code="THEIR-PLC-01",
            device_name="Their PLC", device_type="PLC", protocol="Modbus")
        db.add(theirs)
        db.flush()
    theirs.linked_machine_id = demo[1]

    # 3. ...and one of their signals pointing at a demo machine too.
    signal = (db.query(models.IndustrialSignal)
                .filter(models.IndustrialSignal.tenant_code == OTHER).first())
    if signal is None:
        signal = models.IndustrialSignal(
            tenant_code=OTHER, device_id=theirs.id, signal_name="Theirs",
            signal_value="1")
        db.add(signal)
    signal.machine_id = demo[2]
    db.commit()
    ids = (mine.id, theirs.id)
    tenancy.reset_current_tenant(tok)
    db.close()
    return demo, ids


def main():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    database.SessionLocal = SessionLocal

    print("=" * 74)
    print("1. THE FIRST RESET — the only one that ever worked")
    print("=" * 74)
    db, tok = session()
    demo_aeron.seed(db)
    tenancy.reset_current_tenant(tok)
    db.close()
    db, tok = session()
    machines = (db.query(models.Machine)
                  .filter(models.Machine.tenant_code == demo_aeron.DEMO_TENANT)
                  .count())
    check("the demo scope is built", machines == 3, str(machines))
    tenancy.reset_current_tenant(tok)
    db.close()

    print()
    print("=" * 74)
    print("2. A DEMO IS RUN — rows appear that reference the demo's machines")
    print("=" * 74)
    demo_ids, (mine_id, theirs_id) = plant_blockers()
    check("something now points at every demo machine", len(demo_ids) == 3,
          str(demo_ids))

    print()
    print("=" * 74)
    print("3. THE SECOND RESET — this is what used to die")
    print("=" * 74)
    db, tok = session()
    try:
        demo_aeron.seed(db)
        crashed = None
    except Exception as e:                       # noqa: BLE001 - reporting it
        crashed = f"{type(e).__name__}: {e}"
    tenancy.reset_current_tenant(tok)
    db.close()
    check("RESETTING AFTER A DEMO DOES NOT RAISE", crashed is None, str(crashed))

    print()
    print("=" * 74)
    print("4. IT CLEARED THE DEMO, AND ONLY THE DEMO")
    print("=" * 74)
    db, tok = session()
    check("the demo's own device is gone",
          db.query(models.IndustrialDevice).filter_by(id=mine_id).count() == 0,
          "still there")
    check("...and its signal with it",
          db.query(models.IndustrialSignal)
            .filter(models.IndustrialSignal.tenant_code
                    == demo_aeron.DEMO_TENANT).count() == 0,
          "still there")

    # The half that matters. A demo script that deletes another tenant's rows is
    # a far worse bug than the one it was fixing.
    theirs = db.query(models.IndustrialDevice).filter_by(id=theirs_id).first()
    check("ANOTHER TENANT'S DEVICE STILL EXISTS", theirs is not None, "deleted!")
    check("...and merely lost its reference to a machine that is gone",
          theirs is not None and theirs.linked_machine_id is None,
          str(getattr(theirs, "linked_machine_id", "row missing")))
    their_signals = (db.query(models.IndustrialSignal)
                       .filter(models.IndustrialSignal.tenant_code == OTHER).all())
    check("...and their signal still exists too", len(their_signals) == 1,
          str(len(their_signals)))
    check("...also unlinked rather than destroyed",
          all(s.machine_id is None for s in their_signals),
          str([s.machine_id for s in their_signals]))

    print()
    print("=" * 74)
    print("5. AND AGAIN, TWICE MORE — a demo week, not a demo")
    print("=" * 74)
    trouble = None
    for round_no in (2, 3):
        plant_blockers(round_no)
        db2, tok2 = session()
        try:
            demo_aeron.seed(db2)
        except Exception as e:                   # noqa: BLE001
            trouble = f"{type(e).__name__}: {e}"
        tenancy.reset_current_tenant(tok2)
        db2.close()
    check("four resets in a row, each after a demo", trouble is None, str(trouble))
    tenancy.reset_current_tenant(tok)
    db.close()

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
