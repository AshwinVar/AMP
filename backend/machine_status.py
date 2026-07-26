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
import math

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
    than shown as a >100% or negative utilization.

    NaN / +-inf are not real readings and are rejected as None too. They pass
    float() (they ARE floats), but round(nan) raises ValueError and round(inf)
    OverflowError — uncaught by the float() guard below. JSON permits NaN/Infinity
    (Python's json.loads decodes them), and a disconnected analog input commonly
    reads NaN, so the MQTT/PLC ingest path (mqtt_service reads raw json) could hand
    one straight in; without this check the exception aborted the whole inbound
    message. isfinite() maps a non-finite reading to "no usable value" -> None, so
    the caller leaves the machine's last good utilization untouched."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return max(0, min(100, round(v)))
