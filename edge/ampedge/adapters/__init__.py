"""Industrial adapters. Two, both real, both proven against a live server.

An adapter appears here ONLY once it has been run against a real server or a
real simulator of the protocol. A class that exists is not a supported protocol;
docs/integration/PLC-PILOT-READINESS.md says which is which, and this package
must never be the reason that document overclaims.
"""
from . import base  # noqa: F401
