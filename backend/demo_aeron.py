"""The AERON sales demo: a self-contained, resettable OEM story.

WHAT THIS IS. A fictional machine manufacturer — AERON COMPRESSOR SYSTEMS —
and a clearly-labelled demo factory, seeded so the whole OEM journey can be
walked through in ten minutes in front of a prospect. Everything it creates is
built from EXISTING AMP mechanisms: a tenant, an OEM organisation, a machine
model with a telemetry profile, logins, and a shop-floor machine. No product
behaviour is special-cased for compressors, and nothing here is imported by
application code.

WHY IT IS SAFE TO RUN ON PRODUCTION. A sales demo has to be reachable over the
internet, which means it lives beside real customers. Three independent things
keep it away from them:

  1. THE TENANT IS A CONSTANT, NOT A PARAMETER. There is no argument that
     points this script at another workspace.
  2. THE NAME IS ASSERTED. `_assert_demo_scope()` refuses to run unless the
     tenant code begins with "DEMO_" and the OEM code is the demo one. A future
     edit that repoints the constant at a customer fails loudly instead of
     wiping them.
  3. EVERY DELETE IS FILTERED. The reset removes rows by
     `tenant_code == DEMO_TENANT` or `oem_code == DEMO_OEM` and nothing else.
     It has no unscoped statement to get wrong.

WHY IT IS SAFE TO RUN TWICE. Reset is the only mode: it clears the demo scope
and rebuilds it, so the starting state is the same every time and a half-walked
demo from the last meeting cannot leak into the next one.

This sentence was here before it was true. `wipe` deletes the demo's machines,
and both `IndustrialDevice.linked_machine_id` and `IndustrialSignal.machine_id`
hold foreign keys to that table — so the second reset died on a constraint, and
the second reset is the only one anybody runs. Nothing caught it because every
test seeded a fresh database, and SQLite does not enforce those keys anyway.
`test_demo_reset_repeatable.py` now runs four resets with a demo's worth of rows
planted between each, and asserts on the rows rather than on the exception so it
means the same thing on both engines.

WHAT IT DELIBERATELY DOES NOT CREATE. The machine at the centre of the demo,
SN-ACX-0001, is NOT seeded — registering it is minute 2 of the script, and a
demo that begins with the interesting row already present proves nothing. The
seeder creates only what must exist beforehand: the manufacturer, its
catalogue, the customer, their logins, and a machine on the customer's floor to
link the delivery to.

    python demo_aeron.py --reset          rebuild the demo to its start state
    python demo_aeron.py --telemetry      publish reports through the real
                                          MQTT handler (see below)
    python demo_aeron.py --status         what exists right now

Production resets reuse the proven RESEED_FACTORY pattern: set
`RESEED_DEMO_OEM=<a new value>` on Railway and redeploy. Each distinct value is
consumed exactly once (recorded in the append-only event_log), so a variable
left behind cannot rebuild the demo on every boot — the failure that wiped
production ~41 times before that guard existed.
"""
import json
import os
import sys
from datetime import date, datetime, timedelta

from sqlalchemy import or_

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import models
import tenancy
from security import hash_password

# ---------------------------------------------------------------- constants --
DEMO_TENANT = "DEMO_AERON"          # the customer, clearly labelled as a demo
DEMO_OEM = "AERON"                  # the manufacturer
DEMO_SITE = ""                      # machines made through POST /machines have
                                    # no site; MQTT addresses that with "-"
MODEL_CODE = "ACX-75"
FACTORY_ADMIN = "demo_factory_admin"
FACTORY_OPERATOR = "demo_factory_operator"
OEM_ADMIN = "aeron_admin"
OEM_ENGINEER = "aeron_engineer"

# The machine on the customer's floor that the delivered compressor is linked
# to in minute 6. Named as the customer would name it, not as AERON would.
FLOOR_MACHINE = "COMP-PLANT-A"

# Two more so the customer's floor is not a single row. Deliberately few:
# "enough realistic information to make the OEM journey clear", no more.
OTHER_MACHINES = ("PRESS-01", "CNC-03")

DEMO_SERIAL = "SN-ACX-0001"         # created live during the demo, not seeded

# ---------------------------------------------------------- telemetry profile --
#
# THIS IS DATA, NOT CODE. Every name on the left is AMP's canonical vocabulary;
# every `source` on the right is AERON's own controller tag. AMP core never
# learns the word "DischPress" — it reads it from here (ADR-0017). A different
# manufacturer ships a different profile and no AMP module changes.
#
# `min`/`max` are the plausible band. A reading outside it is FLAGGED and kept
# as measured, never clamped: clamping turns an overheating machine into a
# healthy-looking one.
ACX_PROFILE = [
    {"name": "running", "source": "Run", "datatype": "bool",
     "state_signal": True, "description": "Motor energised"},
    {"name": "loaded", "source": "Load", "datatype": "bool",
     "description": "Compressing rather than idling"},
    {"name": "unloaded", "source": "Unload", "datatype": "bool",
     "description": "Running but not compressing"},
    {"name": "discharge_pressure", "source": "DischPress", "datatype": "number",
     "unit": "bar", "min": 0, "max": 16, "aggregation": "avg"},
    {"name": "discharge_temperature", "source": "DischTemp", "datatype": "number",
     "unit": "degC", "min": -20, "max": 130, "aggregation": "avg"},
    {"name": "motor_temperature", "source": "MotorTemp", "datatype": "number",
     "unit": "degC", "min": -20, "max": 155, "aggregation": "max"},
    {"name": "operating_hours", "source": "HrsRun", "datatype": "number",
     "unit": "h", "min": 0, "max": 200000, "aggregation": "last"},
    {"name": "loaded_hours", "source": "HrsLoad", "datatype": "number",
     "unit": "h", "min": 0, "max": 200000, "aggregation": "last"},
    {"name": "power_kw", "source": "MotorkW", "datatype": "number",
     "unit": "kW", "min": 0, "max": 90, "aggregation": "avg"},
    {"name": "energy_kwh", "source": "EnergyTotal", "datatype": "number",
     "unit": "kWh", "min": 0, "max": 100000000, "aggregation": "last"},
    {"name": "alarm_code", "source": "AlarmCode", "datatype": "string",
     "description": "AERON controller fault code; 0 = healthy"},
    {"name": "dryer_status", "source": "DryerState", "datatype": "string",
     "description": "Integrated refrigerant dryer state"},
]

SERVICE_INTERVAL_HOURS = 4000       # AERON's stated interval for the ACX range

# Where the demo's hour meter starts. Just under the interval, so ONE telemetry
# burst during the meeting carries it over and the service queue lights up while
# the prospect is watching — rather than needing a machine that is already
# overdue before the story begins.
START_HOURS = 3960.0


class DemoRefused(Exception):
    """A refusal to seed. Deliberately NOT SystemExit: this module is called
    from the application's startup hook, and SystemExit is not caught by
    `except Exception` — a missing password would have taken the whole
    deployment down instead of skipping the demo rebuild."""


def _assert_demo_scope():
    """The guard that makes this file safe to keep in a production image."""
    if not DEMO_TENANT.startswith("DEMO_"):
        raise DemoRefused(
            f"demo tenant {DEMO_TENANT!r} is not named as a demo. This script "
            f"only ever touches a DEMO_ workspace; if you are trying to seed a "
            f"real customer, you want a different tool.")
    if DEMO_OEM != "AERON":
        raise DemoRefused(f"{DEMO_OEM!r} is not the demo manufacturer.")


def _password():
    pw = os.environ.get("DEMO_PASSWORD", "").strip()
    if len(pw) < 8:
        raise DemoRefused(
            "set DEMO_PASSWORD (8+ characters) before seeding. This script will "
            "not ship a default password — a demo login on the public internet "
            "with a guessable password is a real account.")
    return pw


# --------------------------------------------------------------------- wipe --
def wipe(db):
    """Remove the demo scope, and only the demo scope.

    Ordered children-before-parents. Every statement carries either
    `tenant_code == DEMO_TENANT` or `oem_code == DEMO_OEM`; there is no
    unfiltered delete in this function.
    """
    _assert_demo_scope()

    # The OEM side. Claims reference installations, so they go first.
    inst_ids = [i.id for i in db.query(models.MachineInstallation)
                .filter(models.MachineInstallation.oem_code == DEMO_OEM).all()]
    if inst_ids:
        (db.query(models.MachineClaim)
           .filter(models.MachineClaim.installation_id.in_(inst_ids))
           .delete(synchronize_session=False))
    (db.query(models.MachineClaim)
       .filter(models.MachineClaim.oem_code == DEMO_OEM)
       .delete(synchronize_session=False))
    (db.query(models.MachineInstallation)
       .filter(models.MachineInstallation.oem_code == DEMO_OEM)
       .delete(synchronize_session=False))
    (db.query(models.MachineModel)
       .filter(models.MachineModel.oem_code == DEMO_OEM)
       .delete(synchronize_session=False))
    (db.query(models.OemDataSharingPolicy)
       .filter(models.OemDataSharingPolicy.oem_code == DEMO_OEM)
       .delete(synchronize_session=False))
    (db.query(models.OemUser)
       .filter(models.OemUser.oem_code == DEMO_OEM)
       .delete(synchronize_session=False))
    (db.query(models.OemOrganization)
       .filter(models.OemOrganization.oem_code == DEMO_OEM)
       .delete(synchronize_session=False))

    # The customer side.
    machine_ids = [m.id for m in db.query(models.Machine)
                   .filter(models.Machine.tenant_code == DEMO_TENANT).all()]
    if machine_ids:
        (db.query(models.MachineEvent)
           .filter(models.MachineEvent.machine_id.in_(machine_ids))
           .delete(synchronize_session=False))
        (db.query(models.ProductionRecord)
           .filter(models.ProductionRecord.machine_id.in_(machine_ids))
           .delete(synchronize_session=False))
        # AN INDUSTRIAL DEVICE HOLDS A FOREIGN KEY TO `machines`, so a device
        # pointing at a demo machine blocks the delete below and the whole reset
        # fails — which is what a second `--reset` did, every time, once a demo
        # had actually been run. TWO DIFFERENT ROWS, AND THEY ARE NOT TREATED
        # THE SAME:
        #
        #   a device belonging to the DEMO tenant   -> deleted, it is demo scope
        #   a device belonging to ANOTHER tenant    -> merely UNLINKED
        #
        # The second case is real: the row that first blocked this reset was a
        # DEFAULT-tenant device whose `linked_machine_id` pointed at a demo
        # machine. Deleting it would break this function's one promise — that it
        # removes the demo scope and nothing else — so it keeps its row and
        # loses only the reference to a machine that is about to stop existing.
        demo_device_ids = [d.id for d in db.query(models.IndustrialDevice)
                           .filter(models.IndustrialDevice.tenant_code
                                   == DEMO_TENANT).all()]
        # Signals first — they reference a device AND a machine, so they block
        # both deletes below.
        (db.query(models.IndustrialSignal)
           .filter(or_(models.IndustrialSignal.tenant_code == DEMO_TENANT,
                       models.IndustrialSignal.device_id.in_(
                           demo_device_ids or [-1])))
           .delete(synchronize_session=False))
        (db.query(models.IndustrialSignal)
           .filter(models.IndustrialSignal.machine_id.in_(machine_ids),
                   models.IndustrialSignal.tenant_code != DEMO_TENANT)
           .update({"machine_id": None}, synchronize_session=False))
        if demo_device_ids:
            (db.query(models.IndustrialDevice)
               .filter(models.IndustrialDevice.id.in_(demo_device_ids))
               .delete(synchronize_session=False))
        (db.query(models.IndustrialDevice)
           .filter(models.IndustrialDevice.linked_machine_id.in_(machine_ids),
                   models.IndustrialDevice.tenant_code != DEMO_TENANT)
           .update({"linked_machine_id": None}, synchronize_session=False))
    for M in (models.Notification, models.Alert, models.Machine,
              models.EventLog, models.TenantConfig):
        (db.query(M).filter(M.tenant_code == DEMO_TENANT)
           .delete(synchronize_session=False))
    (db.query(models.User).filter(models.User.tenant_code == DEMO_TENANT)
       .delete(synchronize_session=False))
    (db.query(models.CompanyTenant)
       .filter(models.CompanyTenant.company_code == DEMO_TENANT)
       .delete(synchronize_session=False))
    (db.query(models.AuditLog)
       .filter(models.AuditLog.tenant_code == DEMO_TENANT)
       .delete(synchronize_session=False))
    db.commit()


# --------------------------------------------------------------------- seed --
def seed(db):
    """Build the demo to its known starting state."""
    _assert_demo_scope()
    pw = _password()
    wipe(db)

    # The customer. A real tenant, named so nobody mistakes it for one.
    db.add(models.CompanyTenant(
        company_code=DEMO_TENANT, company_name="Northgate Precision (DEMO)",
        industry="Precision engineering", plan_name="Professional",
        subscription_status="Trial", seats=10, monthly_fee=0))
    db.add(models.TenantConfig(tenant_code=DEMO_TENANT))
    db.add(models.User(username=FACTORY_ADMIN, password=hash_password(pw),
                       role="Admin", tenant_code=DEMO_TENANT, is_active=True))
    db.add(models.User(username=FACTORY_OPERATOR, password=hash_password(pw),
                       role="Operator", tenant_code=DEMO_TENANT, is_active=True))

    # The manufacturer.
    db.add(models.OemOrganization(
        oem_code=DEMO_OEM, name="Aeron Compressor Systems",
        brand_name="Aeron", brand_color="#0F62FE",
        support_email="service@aeron.example", support_phone="+44 20 7946 0000"))
    db.add(models.OemUser(oem_code=DEMO_OEM, username=OEM_ADMIN,
                          password=hash_password(pw), role="OEM_ADMIN"))
    db.add(models.OemUser(oem_code=DEMO_OEM, username=OEM_ENGINEER,
                          password=hash_password(pw),
                          role="OEM_SERVICE_ENGINEER"))
    db.flush()

    # The catalogue. There is no HTTP route that creates a model, so this is
    # the one thing the demo cannot do live — and a manufacturer's catalogue is
    # onboarding data anyway, not something entered during a sales call.
    db.add(models.MachineModel(
        oem_code=DEMO_OEM, family="AERON ACX Series", model_code=MODEL_CODE,
        name="Aeron ACX-75 rotary screw compressor",
        description="75 kW oil-injected rotary screw air compressor with "
                    "integrated refrigerant dryer.",
        rated_capacity=12.5, rated_capacity_unit="m3/min",
        service_interval_hours=SERVICE_INTERVAL_HOURS, warranty_months=24,
        status="Active",
        telemetry_profile=json.dumps(ACX_PROFILE)))

    # The customer's floor. FLOOR_MACHINE is what the delivered compressor gets
    # linked to; the other two are there so the screen is not a single row.
    for name in (FLOOR_MACHINE,) + OTHER_MACHINES:
        db.add(models.Machine(tenant_code=DEMO_TENANT, site=DEMO_SITE, name=name,
                              status="Running", utilization=68, downtime="0 min"))
    db.commit()


# ---------------------------------------------------------------- telemetry --
def publish(db, hours=None, alarm="0", running=True):
    """Send one report through the REAL MQTT handler.

    NOT an HTTP shortcut. `mqtt_service.on_message` is the same function the
    broker calls, and this hands it the same (topic, payload) a gateway would —
    so the message is routed by `mqtt_identity.parse_topic`, checked against the
    provisioning gate, and written by the same code as production traffic. A
    broker is not required to exercise the path; if one is configured,
    publishing to it lands in this identical handler.
    """
    import mqtt_service

    inst = (db.query(models.MachineInstallation)
              .filter(models.MachineInstallation.oem_code == DEMO_OEM,
                      models.MachineInstallation.serial_number == DEMO_SERIAL)
              .first())
    if inst is None:
        raise DemoRefused(
            f"{DEMO_SERIAL} does not exist yet. Register it in the OEM portal "
            f"first — that is minute 2 of the demo.")

    hours = START_HOURS if hours is None else hours
    payload = {
        "machine": FLOOR_MACHINE,
        "status": "Running" if running else "Idle",
        "utilization": 72 if running else 0,
        "downtime": "0 min",
        # AERON's OWN tag names. AMP maps them through the model's profile.
        "readings": {
            "Run": running, "Load": running, "Unload": False,
            "DischPress": 7.4, "DischTemp": 78.5, "MotorTemp": 82.0,
            "HrsRun": hours, "HrsLoad": round(hours * 0.71, 1),
            "MotorkW": 61.2 if running else 0.0,
            "EnergyTotal": round(hours * 58.4, 1),
            "AlarmCode": alarm, "DryerState": "Running",
        },
    }
    site = DEMO_SITE or "-"
    topic = f"{mqtt_service.TOPIC_PREFIX}/{DEMO_TENANT}/{site}/machines"

    class _Msg:
        def __init__(self, t, p):
            self.topic = t
            self.payload = json.dumps(p).encode()

    mqtt_service.on_message(None, None, _Msg(topic, payload))
    return topic, payload


def status(db):
    org = (db.query(models.OemOrganization)
             .filter(models.OemOrganization.oem_code == DEMO_OEM).first())
    machines = (db.query(models.Machine)
                  .filter(models.Machine.tenant_code == DEMO_TENANT).count())
    insts = (db.query(models.MachineInstallation)
               .filter(models.MachineInstallation.oem_code == DEMO_OEM).all())
    claims = (db.query(models.MachineClaim)
                .filter(models.MachineClaim.oem_code == DEMO_OEM).all())
    print(f"manufacturer         : {'AERON present' if org else 'MISSING'}")
    print(f"demo factory machines: {machines}")
    print(f"registered machines  : {len(insts)}")
    for i in insts:
        print(f"   {i.serial_number}  {i.status}  factory={i.factory_tenant_code} "
              f"machine_id={i.machine_id} hours={i.operating_hours} "
              f"last_seen={i.last_seen_at}")
    print(f"claims               : {len(claims)}"
          + ("" if not claims else
             "  [" + ", ".join(f"{c.code_hint}:{c.status}" for c in claims) + "]"))
    print()
    print("READY FOR A DEMO" if (org and machines and not insts)
          else "NOT at the starting state — run --reset")


def main(argv):
    from database import SessionLocal
    tenancy.install_scoping()
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    try:
        return _run(db, argv)
    except DemoRefused as e:
        print(f"REFUSED: {e}")
        return 2
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def _run(db, argv):
    if "--reset" in argv:
        seed(db)
        print(f"DEMO REBUILT - tenant {DEMO_TENANT}, manufacturer {DEMO_OEM}")
        print(f"  factory admin : {FACTORY_ADMIN}")
        print(f"  OEM admin     : {OEM_ADMIN}")
        print(f"  model         : {MODEL_CODE} "
              f"({len(ACX_PROFILE)} telemetry signals)")
        print(f"  floor machine : {FLOOR_MACHINE}")
        print(f"  {DEMO_SERIAL} is deliberately NOT created - that is minute 2.")
        return 0
    if "--telemetry" in argv:
        hours = None
        for a in argv:
            if a.startswith("--hours="):
                hours = float(a.split("=", 1)[1])
        topic, payload = publish(db, hours=hours)
        print(f"published {topic}")
        print(f"  readings: {json.dumps(payload['readings'])}")
        return 0
    if "--status" in argv:
        status(db)
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
