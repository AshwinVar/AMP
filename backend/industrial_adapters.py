"""
Industrial connectivity — protocol adapter framework + simulator.

AMP talks to shop-floor PLCs through a small adapter layer. Each industrial
protocol (OPC UA, Modbus TCP, Siemens S7, Allen-Bradley, Beckhoff, Omron) has an
adapter that knows how to read tags/registers from that protocol and normalise
them into AMP signals.

Real drivers run on an on-site edge agent (they need the PLC hardware + the
vendor library noted in PROTOCOLS below). In the cloud demo we can't reach a
physical PLC, so `SimulatorAdapter` produces realistic values for each protocol —
the architecture is identical, only the `read()` implementation differs. To go
live on a customer's floor you implement `read()` per protocol on the edge agent
using the listed library; everything downstream (signals, mappings, dashboards)
is unchanged.
"""
import random
import re

import models
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

_DEMO_DEVICES = [
    ("PLC-OPCUA-01", "Line A OPC UA Server",   "opcua",    "192.168.10.21"),
    ("PLC-MODBUS-01", "Compressor Modbus PLC",  "modbus",   "192.168.10.22"),
    ("PLC-S7-01",    "Siemens S7-1200 Press",   "s7",       "192.168.10.23"),
    ("PLC-AB-01",    "Allen-Bradley Conveyor",  "ab",       "192.168.10.24"),
    ("PLC-BECK-01",  "Beckhoff CNC Axis",       "beckhoff", "192.168.10.25"),
    ("PLC-OMRON-01", "Omron Packaging PLC",     "omron",    "192.168.10.26"),
]


def seed_industrial(db):
    """Seed one demo device per protocol (idempotent), linked to machines if any."""
    if db.query(models.IndustrialDevice).count() > 0:
        return
    machines = db.query(models.Machine).all()
    for i, (code, name, proto, ip) in enumerate(_DEMO_DEVICES):
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
    print("[SEED] Industrial devices (6 protocols)")


def tick_industrial(db):
    """Poll each online device through its adapter and store the signals.
    This is what keeps the connectivity dashboard live."""
    devices = db.query(models.IndustrialDevice).filter(models.IndustrialDevice.status == "Online").all()
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
    """The supported protocol adapters — the connectivity surface AMP speaks."""
    return PROTOCOLS
