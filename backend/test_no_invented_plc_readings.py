"""AMP must not invent a reading for a device it has never contacted.

THE DEFECT
----------
`tick_industrial` polls every device whose status is "Online" and writes
`random.randint()` values through `SimulatorAdapter` — and "every device"
included the ones a HUMAN registered.

The path an operator actually takes:

    Industrial Connectivity -> "Connect device"
      device_code  COMP-01
      protocol     Modbus TCP
      IP : port    192.168.10.22:502        <- a REAL compressor PLC
      -> POST /industrial/devices with status="Online" (the schema default)
      -> "OK Device added - signals will start flowing."
      -> the row shows a GREEN "Online" badge
      -> within one 45-second tick the card shows pressure, temperature and
         rpm, in green, marked quality="Good", tagged "Modbus TCP"

Not one of those numbers came from the machine. AMP opened no socket: none of
asyncua / pymodbus / python-snap7 / pycomm3 / pyads is a dependency,
`ProtocolAdapter.read` raises NotImplementedError, and `get_adapter()` returns
`SimulatorAdapter` for every device, always. The numbers are
`random.randint(lo, hi)` from a per-protocol template.

So the screen presented FABRICATED DATA AS MEASUREMENT, attributed to a named
device at a real IP address, over a protocol AMP cannot speak. That is the one
thing a plant system must never do, and it is worse than showing nothing: an
engineer who trusts a discharge temperature reads a number that was rolled by a
random number generator.

THE RULE THIS FILE PINS
-----------------------
A device AMP created as a demo may be simulated. A device a human registered may
NEVER be simulated — it reports only what something real sends, and until then it
honestly has no data.

The repo had already written down the reasoning; it just applied it one case too
narrowly. test_adapter_resilience.test_offline_devices_are_not_polled says the
failure it guards against is

    "tick_industrial polling every row regardless of status, which would
     fabricate live signals for a PLC that is known to be down and make the
     connectivity dashboard claim a dead device is reporting."

"Known to be down" and "never contacted at all" are the same lie. Only the first
was guarded.

NOT IN SCOPE, deliberately:
  * `IndustrialSignal.quality`. In OT, quality is the DEVICE's word for whether a
    reading is trustworthy (Good/Bad/Uncertain). Overloading it with "simulated"
    would give one word two meanings across `ai/connectivity._is_good`, which is
    the defect class this codebase keeps paying for. Simulated-ness is a property
    of the DEVICE and is reported there.
  * Writing a real protocol driver. AMP's boundary is MQTT; the PLC side belongs
    to an on-site edge agent that this repository does not ship.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_no_invented_plc_readings.py
"""
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

import industrial_adapters as ia
import models
import schemas
from database import Base

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _register(db, code="COMP-01", protocol="Modbus TCP", status=None):
    """A device registered the way the Connectivity screen registers one."""
    payload = schemas.IndustrialDeviceCreate(
        device_code=code, device_name="Customer compressor",
        protocol=protocol, ip_address="192.168.10.22:502",
        **({"status": status} if status is not None else {}),
    )
    device = models.IndustrialDevice(**payload.model_dump())
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


def _demo(db):
    """The seeded demo fleet — the six rows AMP creates for itself."""
    ia.seed_industrial(db)
    return db.query(models.IndustrialDevice).all()


def main():
    print("=" * 74)
    print("1. A DEVICE A HUMAN REGISTERED IS NEVER SIMULATED")
    print("=" * 74)
    db = _session()
    device = _register(db)
    for _ in range(20):                    # tick_industrial picks ONE device at random
        ia.tick_industrial(db)
    invented = db.query(models.IndustrialSignal).filter(
        models.IndustrialSignal.device_id == device.id).all()
    for signal in invented[:4]:
        print(f"      INVENTED  {signal.signal_name}={signal.numeric_value}{signal.unit or ''} "
              f"quality={signal.quality!r} protocol={signal.source_protocol!r}")
    check(f"20 ticks wrote 0 readings for a registered PLC ({len(invented)} written)",
          not invented,
          f"{len(invented)} fabricated readings attributed to {device.device_code} "
          f"at {device.ip_address}")
    db.close()

    # ...and the same device explicitly marked Online, which is exactly what the
    # old screen posted. Without this the check above passes on the STATUS
    # default alone: mutation testing showed that reverting the fleet filter by
    # itself left section 1 green, because a "Registered" device is skipped by
    # the status filter anyway. Two fixes, two independent assertions.
    db = _session()
    online = _register(db, code="COMP-ONLINE", status="Online")
    for _ in range(20):
        ia.tick_industrial(db)
    invented_online = db.query(models.IndustrialSignal).filter(
        models.IndustrialSignal.device_id == online.id).count()
    check(f"...and 0 even when it is explicitly marked Online ({invented_online} written)",
          invented_online == 0,
          f"{invented_online} fabricated readings for an Online customer device")
    db.close()

    print()
    print("=" * 74)
    print("2. THE DEMO FLEET STILL MOVES — SO SECTION 1 IS A RULE, NOT A BREAKAGE")
    print("=" * 74)
    # Without this control, deleting tick_industrial's body would pass section 1.
    db = _session()
    demo = _demo(db)
    check(f"seeding creates the {len(ia.DEMO_DEVICE_CODES)}-device demo fleet",
          len(demo) == len(ia.DEMO_DEVICE_CODES), f"{len(demo)} devices")
    for _ in range(10):
        ia.tick_industrial(db)
    produced = db.query(models.IndustrialSignal).count()
    check(f"...and ticking it still produces signals ({produced})", produced > 0,
          "the demo fleet went silent")
    check("...every one of them belongs to a demo device",
          all(db.query(models.IndustrialDevice).get(s.device_id).device_code
              in ia.DEMO_DEVICE_CODES
              for s in db.query(models.IndustrialSignal).all()),
          "a signal was written for a device outside the demo fleet")
    db.close()

    print()
    print("=" * 74)
    print("3. THE TWO FLEETS CANNOT DRIFT APART")
    print("=" * 74)
    # The codes the seeder writes and the codes the tick trusts must be the SAME
    # list, derived, not typed twice. Two copies is how this class of defect
    # starts.
    seeded = {code for code, *_ in ia.DEMO_DEVICES}
    check(f"DEMO_DEVICE_CODES is exactly what seed_industrial writes ({len(seeded)})",
          set(ia.DEMO_DEVICE_CODES) == seeded and len(seeded) >= 6,
          f"seeded={sorted(seeded)} trusted={sorted(ia.DEMO_DEVICE_CODES)}")
    db = _session()
    _demo(db)
    stored = {d.device_code for d in db.query(models.IndustrialDevice).all()}
    check("...and exactly what ends up in the table", stored == set(ia.DEMO_DEVICE_CODES),
          f"table={sorted(stored)}")
    db.close()

    print()
    print("=" * 74)
    print("4. A REGISTERED DEVICE IS NOT ANNOUNCED AS ONLINE")
    print("=" * 74)
    # "Online" is a claim about a connection AMP has never made. The green badge
    # was the other half of the lie: even with no readings, the row asserted the
    # PLC was connected.
    check(f"the create schema does not default to Online "
          f"(default={schemas.IndustrialDeviceCreate.model_fields['status'].default!r})",
          schemas.IndustrialDeviceCreate.model_fields["status"].default != "Online",
          "POST /industrial/devices still asserts Online for an uncontacted device")
    db = _session()
    device = _register(db)
    check(f"...so a freshly registered device reads {device.status!r}",
          device.status != "Online", device.status)
    # An explicit request is still honoured — this is a default, not a veto.
    explicit = _register(db, code="COMP-02", status="Offline")
    check("...and an explicitly supplied status is still honoured",
          explicit.status == "Offline", explicit.status)
    db.close()

    print()
    print("=" * 74)
    print("5. THE API SAYS WHICH DEVICES ARE SIMULATED")
    print("=" * 74)
    # The screen cannot label what the API does not tell it. A page-level
    # disclaimer under the protocol grid is not attached to the number.
    db = _session()
    _demo(db)
    registered = _register(db)
    rows = {d.device_code: schemas.IndustrialDeviceResponse.model_validate(d)
            for d in db.query(models.IndustrialDevice).all()}
    check("every demo device is flagged simulated",
          all(rows[c].simulated for c in ia.DEMO_DEVICE_CODES),
          str([c for c in ia.DEMO_DEVICE_CODES if not rows[c].simulated]))
    check("...and a registered device is not",
          rows[registered.device_code].simulated is False,
          str(rows[registered.device_code].simulated))
    db.close()

    print()
    print("=" * 74)
    print("6. THE PROTOCOL CATALOGUE DOES NOT CLAIM AMP SPEAKS THEM")
    print("=" * 74)
    # PROTOCOLS names six industrial protocols and the library each WOULD need.
    # None is installed; get_adapter returns the simulator for all of them. The
    # catalogue must describe an edge agent's toolkit, not AMP's capability.
    catalogue = ia.industrial_protocols()
    check(f"the catalogue still lists {len(catalogue)} protocols", len(catalogue) >= 6,
          str(len(catalogue)))
    doc = (ia.industrial_protocols.__doc__ or "").lower()
    # Asserted as what the docstring MUST say, not as a phrase it must avoid. The
    # first version of this check looked for the absence of "amp speaks" and then
    # failed the corrected docstring, which reads "AMP speaks NONE of these" — a
    # substring cannot tell a claim from its denial.
    check("...and its docstring denies being a capability claim",
          "not a driver list" in doc and "capability claim" in doc,
          repr(ia.industrial_protocols.__doc__))
    check("...and it says where the driver actually runs",
          "edge agent" in doc, repr(ia.industrial_protocols.__doc__))

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for failure in failures:
            print(f"  - {failure}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


def test_no_invented_plc_readings():
    """The pytest entry point — a suite exposing only main() contributes nothing
    to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
