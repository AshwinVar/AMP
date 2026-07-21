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


def protocol_for(device) -> str:
    """Map a stored device.protocol string to a known protocol key (default modbus).
    Normalise away spaces/hyphens so "OPC UA" -> "opcua", "Modbus TCP" -> "modbus"."""
    p = (device.protocol or "").lower().replace(" ", "").replace("-", "")
    for key in _SIGNAL_TEMPLATES:
        if key in p:
            return key
    if "siemens" in p:
        return "s7"
    if "allen" in p or "rockwell" in p:
        return "ab"
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
