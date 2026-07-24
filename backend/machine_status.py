"""Canonical machine-status vocabulary + inbound-telemetry normalisation.

Machine status drives every status-based rollup — build_management_summary's
breakdown_count, build_smart_alerts, the Machine-Health twin's bands — and they
all match on these EXACT strings. The edge-facing ingest paths (IoT telemetry,
industrial signals) accept a raw status from a device, so an unrecognised string
("RUNNING", "faulted", a typo) written straight onto Machine.status silently
removes the machine from all of those reports. Utilization is a percentage but is
likewise written raw, so a glitching sensor can push it past 100 or negative.

One place to normalise both, shared by every ingest path.
"""

# The statuses a Machine may hold (what the seed, simulators and analytics use).
VALID_MACHINE_STATUSES = ("Running", "Idle", "Breakdown", "Maintenance", "Offline")

_CANONICAL = {s.lower(): s for s in VALID_MACHINE_STATUSES}


def normalize_machine_status(value):
    """Map an inbound status to its canonical form (case-insensitive), or None if
    it is not a recognised machine status. Callers should leave the machine's
    status untouched on None rather than write an unknown string that would drop
    the machine from every status-based report."""
    if value is None:
        return None
    return _CANONICAL.get(str(value).strip().lower())


def clamp_utilization(value):
    """Utilization is a percentage: clamp a raw sensor reading into [0, 100]
    (rounded to a whole percent), or None if it isn't a number. A guard on
    INGEST, not on display — an impossible reading is rejected at the door rather
    than shown as a >100% or negative utilization."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return max(0, min(100, round(v)))
