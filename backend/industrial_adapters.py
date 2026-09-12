"""
Industrial connectivity — protocol adapter framework + simulator.

AMP itself opens NO connection to a PLC, in any protocol. This module is the
SHAPE a driver would take plus a simulator for AMP's own demo fleet: none of
asyncua / pymodbus / python-snap7 / pycomm3 / pyads is a dependency,
`ProtocolAdapter.read` raises NotImplementedError, and `get_adapter()` returns
`SimulatorAdapter` for every device, always. PROTOCOLS is a catalogue of what an
on-site edge agent WOULD use — it is not a driver list, and must never be
presented as one.

Real drivers run on that edge agent, which this repository does not ship. AMP's
own boundary for machine data is MQTT (`mqtt_service`). To go live on a
customer's floor, something on site speaks the machine's protocol and publishes
to AMP; everything downstream (signals, mappings, dashboards) is unchanged.

WHO MAY BE SIMULATED. Only AMP's own demo fleet (`industrial_demo`). A device a
human registered is never simulated — see test_no_invented_plc_readings.py, and
the sixty invented readings that used to be attributed to a real compressor PLC
at a real IP over a protocol AMP cannot speak.
"""
import random
import re

from logging_config import get_logger

# Structured logger. These modules run INSIDE the web process — the seed helpers
# on the startup hook, the tick helpers on the simulation loop — so anything they
# emit belongs in the JSON stream with a level, not on raw stdout.
log = get_logger(__name__)

import industrial_demo
import models
from industrial_demo import DEMO_DEVICES, DEMO_DEVICE_CODES
from fastapi import APIRouter

# The supported protocols. `library` is the Python package an edge agent would
# use to implement the real driver; `port` is the protocol's standard TCP port.
PROTOCOLS = [
    {"key": "opcua",    "name": "OPC UA",        "port": 4840,  "library": "asyncua",       "transport": "TCP/binary",
     "desc": "Vendor-neutral industrial standard. Reads nodes from an OPC UA server."},
    {"key": "modbus",   "name": "Modbus TCP",    "port": 502,   "library": "pymodbus",      "transport": "TCP",
     "desc": "Ubiquitous register-based protocol. Reads holding/input registers."},
    {"key": "s7",       "name": "Siemens S7",    "port": 102,   "library": "python-snap7",  "transport": "ISO-on-TCP",
     "desc": "Siemens S7-300/400/1200/1500 PLCs. Reads data blocks (DB)."},
    {"key": "ab",       "name": "Allen-Bradley", "port": 44818, "library": "pycomm3",       "transport": "EtherNet/IP",
     "desc": "Rockwell ControlLogix / CompactLogix. Reads tags over EtherNet/IP."},
    {"key": "beckhoff", "name": "Beckhoff ADS",  "port": 48898, "library": "pyads",         "transport": "ADS/AMS",
     "desc": "Beckhoff TwinCAT controllers. Reads variables via ADS."},
    {"key": "omron",    "name": "Omron FINS",    "port": 9600,  "library": "aphyt / fins",  "transport": "FINS/TCP",
     "desc": "Omron CJ/CS/NJ PLCs. Reads memory areas via FINS."},
]

# Representative signals each protocol's devices expose (name, unit, min, max).
_SIGNAL_TEMPLATES = {
    "opcua":    [("temperature", "°C", 28, 90), ("pressure", "bar", 4, 10), ("spindle_speed", "RPM", 800, 3200)],
    "modbus":   [("flow_rate", "L/min", 10, 120), ("tank_level", "%", 20, 98), ("valve_position", "%", 0, 100)],
    "s7":       [("motor_current", "A", 3, 45), ("oven_temp", "°C", 120, 240), ("cycle_count", "pcs", 0, 5000)],
    "ab":       [("conveyor_speed", "m/s", 0, 3), ("part_count", "pcs", 0, 8000), ("vibration", "mm/s", 1, 12)],
    "beckhoff": [("axis_position", "mm", 0, 500), ("torque", "Nm", 5, 80), ("servo_temp", "°C", 30, 75)],
    "omron":    [("line_pressure", "bar", 3, 9), ("cycle_time", "s", 8, 40), ("reject_count", "pcs", 0, 200)],
}

_PROTOCOL_BY_KEY = {p["key"]: p for p in PROTOCOLS}


# Protocol-classification aliases. A stored device.protocol is free text — a user
# onboarding a device types it — so map it to a known protocol key by its aliases.
# Keys are tried IN THIS ORDER; the first with a matching alias wins, else Modbus.
#
# Matching is TOKEN-based, not bare-substring. The previous version tested
# `key in normalised_protocol` after stripping spaces/hyphens, so the two-letter
# key "ab" (Allen-Bradley) matched ANY protocol whose name merely CONTAINED the
# letters a-b — "Fabricated", "Grabber", "Crab", "Modbus lab" all classified as
# Allen-Bradley, were read through the Rockwell signal templates and stamped with
# the wrong source_protocol on every tick_industrial poll. Whole-token matching
# (plus a substring fallback for the long, distinctive aliases, and an "s7"-prefix
# rule for the Siemens family) keeps every real protocol name resolving exactly as
# before while dropping the accidental hits.
#
# The alias set also covers the protocol's OWN identifiers as they appear in the
# PROTOCOLS table above — not just the display name. Allen-Bradley IS the ODVA
# "EtherNet/IP" protocol (PROTOCOLS[ab]["transport"]), and an OT engineer commonly
# types "EtherNet/IP" (or "EtherNetIP") rather than the vendor name; Siemens S7 IS
# the snap7/python-snap7 stack (PROTOCOLS[s7]["library"]). Without these two, that
# free text fell through to the Modbus default — feeding an Allen-Bradley or
# Siemens device the wrong protocol's signal templates and stamping the wrong
# source_protocol on every poll (and, on a real edge agent, picking the wrong
# driver entirely). Both aliases are long and distinctive (>= _MIN_SUBSTRING_ALIAS),
# so they only ever match "ethernet/ip"/"snap7"-family strings — never a generic
# name like "Ethernet Powerlink" or "GreenIP", which stay Modbus. "enip" is
# deliberately NOT an alias: at 4 chars it would substring-match unrelated names
# like "GreenIP".
_PROTOCOL_ALIASES = (
    ("opcua",    ("opcua", "opc")),
    ("s7",       ("s7", "siemens", "simatic", "snap7")),
    ("ab",       ("ab", "allen", "bradley", "rockwell",
                  "allenbradley", "controllogix", "compactlogix",
                  "ethernetip")),
    ("beckhoff", ("beckhoff", "twincat")),
    ("omron",    ("omron", "fins")),
    ("modbus",   ("modbus",)),
)

# Longest alias — a substring fallback only ever fires for aliases this length or
# more, so a short/ambiguous alias ("ab", "s7", "opc") can only match a whole token.
_MIN_SUBSTRING_ALIAS = 4

_TOKEN = re.compile(r"[a-z0-9]+")


def protocol_for(device) -> str:
    """Map a stored device.protocol string to a known protocol key (default modbus).

    Splits the protocol on any non-alphanumeric run into tokens, so "OPC UA",
    "OPC-UA" and "opcua" all resolve to opcua, and "Siemens S7"/"S7comm"/"S7-1200"
    to s7. A short alias only matches a WHOLE token (so "Fabricated" is NOT
    Allen-Bradley); a distinctive long alias (>= 4 chars) may also match inside a
    run-together name like "OPCUAServer". Anything unrecognised falls back to
    Modbus, the ubiquitous default."""
    raw = (device.protocol or "").lower()
    tokens = _TOKEN.findall(raw)
    squashed = "".join(tokens)  # separators removed: "opc-ua" -> "opcua"
    for key, aliases in _PROTOCOL_ALIASES:
        # Siemens S7 family: any token that STARTS with "s7" (s7, s7comm, s71200).
        # "s7" is a distinctive prefix (letter + digit), unlike the ambiguous "ab".
        if key == "s7" and any(token.startswith("s7") for token in tokens):
            return key
        for alias in aliases:
            if alias in tokens:
                return key
            if len(alias) >= _MIN_SUBSTRING_ALIAS and alias in squashed:
                return key
    return "modbus"


class ProtocolAdapter:
    """Base adapter. A real edge-agent driver overrides read() to talk to the PLC
    using the protocol's library; the simulator subclass generates values instead."""

    def __init__(self, protocol_key: str):
        self.protocol_key = protocol_key

    def read(self, device):
        raise NotImplementedError


class SimulatorAdapter(ProtocolAdapter):
    """Generates plausible signal values for a protocol without any hardware."""

    def read(self, device):
        out = []
        for name, unit, lo, hi in _SIGNAL_TEMPLATES.get(self.protocol_key, _SIGNAL_TEMPLATES["modbus"]):
            value = random.randint(lo, hi)
            out.append((name, value, unit))
        return out


def get_adapter(device) -> ProtocolAdapter:
    """Adapter factory. Today every device uses the simulator; on an edge agent
    this returns the real driver for the device's protocol."""
    return SimulatorAdapter(protocol_for(device))


# ── Seed + live tick ─────────────────────────────────────────────

# The demo fleet lives in industrial_demo so that models.IndustrialDevice can
# import it too (models cannot import this module — this module imports models).


def seed_industrial(db):
    """Seed one demo device per protocol (idempotent), linked to machines if any."""
    if db.query(models.IndustrialDevice).count() > 0:
        return
    machines = db.query(models.Machine).all()
    for i, (code, name, proto, ip) in enumerate(DEMO_DEVICES):
        meta = _PROTOCOL_BY_KEY[proto]
        db.add(models.IndustrialDevice(
            device_code=code, device_name=name,
            device_type="PLC", protocol=meta["name"],
            ip_address=f"{ip}:{meta['port']}",
            topic=None,
            linked_machine_id=machines[i % len(machines)].id if machines else None,
            status="Online",
        ))
    db.commit()
    log.info("[SEED] Industrial devices (6 protocols)")


def tick_industrial(db):
    """Advance AMP's own DEMO fleet, and nothing else.

    Every value this writes is `random.randint()` — no socket is opened, because
    AMP has no PLC driver. So the one rule that matters is WHOSE rows may receive
    an invented number, and the answer is only the devices AMP seeded for itself.

    It used to poll every row whose status was "Online", and
    `IndustrialDeviceCreate.status` defaulted to "Online". Registering a real
    compressor PLC at a real IP therefore produced fabricated pressure and
    temperature readings within one tick, stamped `quality="Good"` and tagged
    with that device's real protocol — data presented as measurement from a
    machine AMP had never contacted. test_adapter_resilience already called that
    failure by its name for a device known to be DOWN ("would fabricate live
    signals ... and make the connectivity dashboard claim a dead device is
    reporting"); a device never contacted at all is the same lie.

    The status filter stays: an operator who marks a demo device Offline still
    expects it to go quiet.
    """
    devices = (db.query(models.IndustrialDevice)
               .filter(models.IndustrialDevice.status == "Online",
                       models.IndustrialDevice.device_code.in_(DEMO_DEVICE_CODES))
               .all())
    if not devices:
        return
    device = random.choice(devices)
    adapter = get_adapter(device)
    for name, value, unit in adapter.read(device):
        db.add(models.IndustrialSignal(
            device_id=device.id, machine_id=device.linked_machine_id,
            signal_name=name, signal_value=str(value), numeric_value=value,
            unit=unit, quality="Good", source_protocol=device.protocol,
        ))
    db.commit()
    # keep the signal table bounded
    count = db.query(models.IndustrialSignal).count()
    if count > 1200:
        old = db.query(models.IndustrialSignal).order_by(models.IndustrialSignal.id.asc()).limit(count - 1000).all()
        for s in old:
            db.delete(s)
        db.commit()


router = APIRouter(prefix="/industrial", tags=["Industrial Adapters"])


@router.get("/protocols")
def industrial_protocols():
    """The protocol catalogue: what an on-site EDGE AGENT would need, per protocol.

    Not a driver list and not a capability claim. AMP speaks none of these — the
    `library` field names the package the edge agent would install. Presenting
    this as "protocols AMP supports" is what made a registered device look
    connected.
    """
    return PROTOCOLS
