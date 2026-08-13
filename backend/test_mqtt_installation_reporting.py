"""Telemetry has to reach the OEM's installation record, not just the machine.

THE DEFECT THIS PINS. `MachineInstallation.last_seen_at` and `operating_hours`
were declared by ADR-0017 and read in four places — the gated fleet row, the
fleet's connected/offline/unknown split, the service clock that decides a
machine is overdue, and the `has_reported` commissioning check — and written by
NOTHING in the product. MQTT ingest updated `Machine`, `MachineEvent` and
`ProductionRecord` and never looked at the installation.

An OEM would have watched "no data" forever, no machine would ever have come due
for service, and no machine could ever have been commissioned. The whole service
proposition read a column nobody set.

WHAT IS DELIBERATELY *NOT* ASSERTED HERE: that AMP invents a reading. Operating
hours are recorded only where the model's own telemetry profile names a source
for them (ADR-0017 — the vocabulary belongs to the OEM), so a model that reports
no hours keeps NULL, and the portal renders that as "no data" rather than zero.

MUTATION RESULTS, so the assertions are not taken on trust. Six mutations of
`_record_installation_report`; five are caught. The survivor is "the tenant
filter is dropped", and it is SHADOWED rather than missed: `machine_id` is a
globally unique primary key and a machine belongs to exactly one tenant, so the
link alone already determines the installation. Removing BOTH terms IS caught —
which is what makes the tenant filter a layer rather than dead code, and why it
stays. Two earlier survivors were this file's own FIXTURE: the unlinked row was
created last (so `.first()` still found the right one) and no test ever sent a
profile-KNOWN signal that was not the hour meter. Both are fixed above and both
now catch their mutation.

Run:  python backend/test_mqtt_installation_reporting.py
"""
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import mqtt_service
import oem_telemetry
import tenancy
from database import Base

_URL = os.environ.get("DATABASE_URL", "")
engine = (create_engine(_URL) if _URL.startswith("postgresql") else
          create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool))
SessionLocal = sessionmaker(bind=engine)
tenancy.install_scoping()

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


class Msg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = json.dumps(payload).encode()


# The ACX profile names the OEM's own tag for hours. AMP core never learns the
# word "HrsRun" — it learns it from this profile, which is data on the model.
PROFILE = json.dumps([
    {"name": "operating_hours", "source": "HrsRun", "datatype": "number",
     "unit": "h", "min": 0, "max": 200000, "aggregation": "last"},
    {"name": "discharge_pressure", "source": "DischPress", "datatype": "number",
     "unit": "bar", "min": 0, "max": 16, "aggregation": "avg"},
])

S = {}


def seed():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)

    for t in ("PLANT_ONE", "PLANT_TWO"):
        db.add(models.TenantConfig(tenant_code=t))
    db.add(models.OemOrganization(oem_code="AERON", name="Aeron Compressor Systems"))
    db.flush()

    model = models.MachineModel(oem_code="AERON", family="Compressor",
                                model_code="ACX-75", name="Aeron ACX-75",
                                service_interval_hours=4000,
                                telemetry_profile=PROFILE)
    bare = models.MachineModel(oem_code="AERON", family="Compressor",
                               model_code="ACX-NOPROFILE", name="No profile",
                               service_interval_hours=4000)
    db.add_all([model, bare])
    db.flush()
    S["model"], S["bare"] = model.id, bare.id

    # Three machines. One linked, one linked at ANOTHER plant, one unlinked.
    for tenant, name in (("PLANT_ONE", "COMP-01"), ("PLANT_TWO", "COMP-01"),
                         ("PLANT_ONE", "COMP-UNLINKED")):
        m = models.Machine(tenant_code=tenant, site="", name=name,
                           status="Idle", utilization=0, downtime="0 min")
        db.add(m)
        db.flush()
        S[f"machine_{tenant}_{name}"] = m.id

    def install(serial, tenant, machine_id, model_id):
        i = models.MachineInstallation(
            oem_code="AERON", serial_number=serial, model_id=model_id,
            factory_tenant_code=tenant, site="Plant 1", machine_id=machine_id,
            status="Active")
        db.add(i)
        db.flush()
        return i.id

    # ORDER IS LOAD-BEARING. The unlinked row is created FIRST so it holds the
    # lowest id in PLANT_ONE: a lookup that forgets the machine_id filter falls
    # to `.first()` and lands on THIS row, which the assertions then catch. With
    # it created last the same broken lookup still returned the linked row and
    # the test passed while proving nothing.
    S["unlinked"] = install("SN-ACX-0003", "PLANT_ONE", None, S["model"])
    S["no_profile"] = install("SN-ACX-0004", "PLANT_ONE", None, S["bare"])
    S["linked"] = install("SN-ACX-0001", "PLANT_ONE",
                          S["machine_PLANT_ONE_COMP-01"], S["model"])
    S["other_plant"] = install("SN-ACX-0002", "PLANT_TWO",
                               S["machine_PLANT_TWO_COMP-01"], S["model"])
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()


def publish(tenant, site, payload):
    mqtt_service.on_message(None, None, Msg(f"flowmes/{tenant}/{site}/machines", payload))


def unscoped():
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    return db, tok


def installations():
    db, tok = unscoped()
    rows = {r.id: r for r in db.query(models.MachineInstallation).all()}
    out = {k: (rows[v].last_seen_at, rows[v].operating_hours)
           for k, v in S.items() if isinstance(v, int) and v in rows}
    tenancy.reset_current_tenant(tok)
    db.close()
    return out


def run_all():
    seed()
    mqtt_service.SessionLocal = SessionLocal

    print()
    print("=" * 74)
    print("1. A REPORTING MACHINE UPDATES ITS INSTALLATION")
    print("=" * 74)
    before = installations()
    check("CONTROL: nothing has reported yet",
          before[S and "linked"][0] is None and before["linked"][1] is None,
          str(before["linked"]))

    publish("PLANT_ONE", "-", {
        "machine": "COMP-01", "status": "Running", "utilization": 70,
        "readings": {"HrsRun": 3990.5, "DischPress": 7.4},
    })
    after = installations()
    check("last_seen_at is now set", after["linked"][0] is not None,
          str(after["linked"][0]))
    check("operating_hours came from the model's OWN tag name",
          after["linked"][1] == 3990.5, str(after["linked"][1]))

    print()
    print("=" * 74)
    print("2. WHAT IT MUST NOT TOUCH")
    print("=" * 74)
    check("an UNLINKED installation is untouched",
          after["unlinked"] == (None, None), str(after["unlinked"]))
    check("ANOTHER factory's installation is untouched",
          after["other_plant"] == (None, None), str(after["other_plant"]))

    # CONTROL: the other plant's machine is reachable — the isolation above is
    # about tenancy, not about that row being unreachable.
    #
    # This report carries a signal the profile DESCRIBES but which is not the
    # hour meter, and it arrives while that installation's hours are still NULL.
    # That combination is the only one that can catch "write whatever number
    # turned up": with hours already recorded, the monotonic guard refuses a
    # small pressure reading for the wrong reason and the bug hides behind it.
    publish("PLANT_TWO", "-", {
        "machine": "COMP-01", "status": "Running", "utilization": 55,
        "readings": {"DischPress": 7.4},
    })
    ctl = installations()
    check("CONTROL: that factory's own report DOES reach it",
          ctl["other_plant"][0] is not None, str(ctl["other_plant"]))
    check("a non-hours signal does NOT become hours on a machine with none yet",
          ctl["other_plant"][1] is None, str(ctl["other_plant"][1]))
    check("...and it did not disturb the first plant",
          ctl["linked"][1] == 3990.5, str(ctl["linked"][1]))

    publish("PLANT_TWO", "-", {
        "machine": "COMP-01", "status": "Running", "utilization": 55,
        "readings": {"HrsRun": 12.0},
    })
    ctl2 = installations()
    check("CONTROL: its real hour meter IS recorded", ctl2["other_plant"][1] == 12.0,
          str(ctl2["other_plant"][1]))

    print()
    print("=" * 74)
    print("3. WHAT IT REFUSES TO INVENT")
    print("=" * 74)
    publish("PLANT_ONE", "-", {
        "machine": "COMP-01", "status": "Running", "utilization": 70})
    seen_only = installations()
    check("a report with NO readings still proves the machine is alive",
          seen_only["linked"][0] is not None, str(seen_only["linked"][0]))
    check("...and does not invent an hours figure",
          seen_only["linked"][1] == 3990.5, str(seen_only["linked"][1]))

    # An hour meter counts up. A lower figure is a replaced controller or a
    # mis-mapped tag; accepting it would silently reset the service clock and
    # cancel a service that is due.
    publish("PLANT_ONE", "-", {
        "machine": "COMP-01", "status": "Running", "utilization": 70,
        "readings": {"HrsRun": 11.0}})
    back = installations()
    check("operating_hours going BACKWARDS is refused",
          back["linked"][1] == 3990.5, str(back["linked"][1]))
    publish("PLANT_ONE", "-", {
        "machine": "COMP-01", "status": "Running", "utilization": 70,
        "readings": {"HrsRun": 4100.0}})
    fwd = installations()
    check("CONTROL: a HIGHER figure is still accepted",
          fwd["linked"][1] == 4100.0, str(fwd["linked"][1]))

    # A signal the profile DOES describe, but which is not the hour meter. This
    # is the one that catches "take whatever number arrived": discharge pressure
    # is perfectly valid and must never land in operating_hours.
    publish("PLANT_ONE", "-", {
        "machine": "COMP-01", "status": "Running", "utilization": 70,
        "readings": {"DischPress": 7.4}})
    other_signal = installations()
    check("a KNOWN signal that is not the hour meter never becomes hours",
          other_signal["linked"][1] == 4100.0, str(other_signal["linked"][1]))

    # A tag the profile does not describe must not be guessed into a column.
    publish("PLANT_ONE", "-", {
        "machine": "COMP-01", "status": "Running", "utilization": 70,
        "readings": {"TotalRunTime": 99999.0}})
    unknown = installations()
    check("an UNCONFIGURED tag never becomes operating hours",
          unknown["linked"][1] == 4100.0, str(unknown["linked"][1]))

    print()
    print("=" * 74)
    print("4. THE MACHINE'S OWN RECORD STILL WORKS")
    print("=" * 74)
    db, tok = unscoped()
    m = db.query(models.Machine).filter(
        models.Machine.id == S["machine_PLANT_ONE_COMP-01"]).first()
    check("CONTROL: ingest still updates the shop-floor machine",
          m.status == "Running" and m.utilization == 70,
          f"{m.status} {m.utilization}")
    tenancy.reset_current_tenant(tok)
    db.close()


def test_mqtt_installation_reporting():
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
    print("A MACHINE THAT REPORTS TELLS ITS MANUFACTURER'S RECORD SO")
