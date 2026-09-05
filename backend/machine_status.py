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

# Which of those mean HARD DOWN — stopped, and nobody planned it.
#
# Deliberately NOT "everything that is not Running": Maintenance is planned, and
# a plant reporting scheduled servicing as a fault is crying wolf. Idle is a
# machine waiting for work, which is a scheduling matter, not a breakdown.
#
# ONE DEFINITION, HERE, because there were two. `ai/assistant.py` and
# `ai/briefing.py` each carried their own identical copy, and `ai/agents.py`
# imported the briefing's. Nothing kept them equal: editing either one would
# have made the copilot's "which machines are down?" and the briefing's "what
# needs attention" disagree about the same plant — the exact class of split-brain
# the machine-status vocabulary exists to prevent (#549, #552).
#
# "Down" is retained and is NOT in VALID_MACHINE_STATUSES. That is not an
# oversight: normalize_machine_status rejects it today, so nothing can WRITE it,
# but Machine.status has no database constraint and a row predating normalisation
# could still hold it. Keeping it costs one string comparison and stops such a
# machine reading as healthy. test_down_statuses_single_source.py pins both
# halves of that so it cannot be "tidied up" into either the vocabulary or the
# bin without a deliberate decision.
DOWN_STATUSES = ("Breakdown", "Down", "Offline")

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
