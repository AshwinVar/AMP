"""The demo PLC fleet — the only devices AMP is allowed to simulate.

WHY THIS IS ITS OWN MODULE
--------------------------
Two places have to agree on which devices are AMP's own demo rows:

  * ``industrial_adapters`` seeds them and is the only caller permitted to feed
    them simulated values;
  * ``models.IndustrialDevice.simulated`` reports the fact to the API, so the
    screen can label an invented number as invented.

``models`` cannot import ``industrial_adapters`` (that module imports ``models``),
and ``schemas`` is deliberately dependency-free. A vocabulary both sides import
is the same shape as ``machine_status`` and ``work_order_status``: one rule, one
file, no second copy to drift.

WHAT THE RULE IS
----------------
A device AMP created for itself may be simulated. A device a HUMAN registered may
NEVER be simulated — it reports only what something real sends, and until then it
honestly has no data. See test_no_invented_plc_readings.py for why: before this
list existed, registering a real compressor PLC at a real IP produced sixty
``random.randint()`` readings, stamped ``quality="Good"`` and tagged with the
device's real protocol, over a protocol AMP cannot speak.
"""

# code, display name, protocol key, IP — the fleet seed_industrial writes.
# RFC 5737 / RFC 1918 addresses that resolve to nothing: these rows must never
# look like they belong to a real plant.
DEMO_DEVICES = (
    ("PLC-OPCUA-01",  "Line A OPC UA Server",   "opcua",    "192.168.10.21"),
    ("PLC-MODBUS-01", "Compressor Modbus PLC",  "modbus",   "192.168.10.22"),
    ("PLC-S7-01",     "Siemens S7-1200 Press",  "s7",       "192.168.10.23"),
    ("PLC-AB-01",     "Allen-Bradley Conveyor", "ab",       "192.168.10.24"),
    ("PLC-BECK-01",   "Beckhoff CNC Axis",      "beckhoff", "192.168.10.25"),
    ("PLC-OMRON-01",  "Omron Packaging PLC",    "omron",    "192.168.10.26"),
)

# Derived, never typed twice: the seeder and the simulate-permission check read
# the same list, so a renamed demo device cannot silently become un-simulatable
# (or, worse, leave a real device simulatable).
DEMO_DEVICE_CODES = tuple(code for code, *_ in DEMO_DEVICES)


def is_demo_device(device_code) -> bool:
    """True only for a device AMP seeded for its own demo."""
    return device_code in DEMO_DEVICE_CODES
