"""Industrial adapters. Three, all real, none of them aspirational.

An adapter appears here ONLY once it has been run against a real server or a
real simulator of the protocol. A class that exists is not a supported protocol;
docs/integration/PLC-PILOT-READINESS.md says which is which, and this package
must never be the reason that document overclaims.

Note the asymmetry, because it is the opposite of what anyone would guess: OPC
UA and Modbus are proven against SIMULATORS on loopback. FOCAS is the only one
proven against a PHYSICAL CONTROL — a FANUC 0i-MF Plus on a BFW machining
centre, whose parts counter it read back as the number on the operator's screen.
The readiness matrix says so in both directions.
"""
from . import base  # noqa: F401
