"""Per-model telemetry profiles: what a machine of THIS model reports.

WHY THIS IS CONFIGURATION AND NOT CODE
--------------------------------------
A compressor reports `pressure`, `discharge_temperature`, `loaded_hours`. A CNC
reports `spindle_speed`, `feed_rate`, `cycle_count`. Neither vocabulary belongs
in AMP core: the moment `discharge_temperature` appears in a shared module, AMP
is a compressor platform with a CNC bolted on, and every new machine family costs
a release.

So a profile is DATA on the machine model (`MachineModel.telemetry_profile`,
stored as JSON in a Text column — the EventLog precedent, since this codebase
imports no JSON column type). AMP core knows only the SHAPE of a profile, never
the names inside it.

WHAT A PROFILE DECIDES
----------------------
For each canonical signal:

    name         what AMP calls it            "discharge_temperature"
    source       where it arrives from        "DischargeTemp" / "40001" / topic
    datatype     number | bool | string
    unit         "degC", "bar", "h"           reported, never converted
    min / max    the plausible range          out-of-range is FLAGGED, not clamped
    aggregation  last | sum | max | avg       how a period collapses to a figure
    state        does this signal decide the machine's running state

OUT OF RANGE IS NOT INVALID
---------------------------
A discharge temperature of 400 degC is either a real emergency or a broken
sensor, and this module cannot tell which. It records the reading and marks it
out-of-range; it never silently clamps to the maximum. Clamping would turn an
overheating machine into a healthy-looking one, which is the same class of lie as
the fabricated OEE this platform already removed (ADR-0014).
"""
import json

VALID_TYPES = ("number", "bool", "string")
VALID_AGGREGATIONS = ("last", "sum", "max", "min", "avg")

# The shape AMP core understands. The NAMES are the OEM's business.
REQUIRED_KEYS = ("name", "source", "datatype")


class ProfileError(ValueError):
    """A profile that cannot be trusted to interpret readings."""


def parse(raw):
    """A stored profile -> [signal dicts]. Raises ProfileError on nonsense.

    An unparseable profile is an ERROR, not an empty profile. Returning [] would
    make a corrupted configuration look like a machine that reports nothing,
    and an OEM would spend a day chasing a gateway that is working perfectly.
    """
    if raw is None or raw == "":
        return []
    if isinstance(raw, (list, dict)):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as e:
            raise ProfileError(f"telemetry profile is not valid JSON: {e}")
    if isinstance(data, dict):
        data = data.get("signals", [])
    if not isinstance(data, list):
        raise ProfileError("telemetry profile must be a list of signals")

    seen = set()
    out = []
    for i, sig in enumerate(data):
        if not isinstance(sig, dict):
            raise ProfileError(f"signal {i} is not an object")
        missing = [k for k in REQUIRED_KEYS if not sig.get(k)]
        if missing:
            raise ProfileError(f"signal {i} is missing {', '.join(missing)}")
        name = str(sig["name"])
        if name in seen:
            # Two signals with one name means a reading has two meanings, and
            # whichever is applied second silently wins.
            raise ProfileError(f"signal {name!r} is defined twice")
        seen.add(name)
        if sig["datatype"] not in VALID_TYPES:
            raise ProfileError(
                f"signal {name!r} has unknown datatype {sig['datatype']!r}")
        agg = sig.get("aggregation", "last")
        if agg not in VALID_AGGREGATIONS:
            raise ProfileError(f"signal {name!r} has unknown aggregation {agg!r}")
        out.append({
            "name": name,
            "source": str(sig["source"]),
            "datatype": sig["datatype"],
            "unit": sig.get("unit") or "",
            "min": sig.get("min"),
            "max": sig.get("max"),
            "aggregation": agg,
            "state_signal": bool(sig.get("state_signal", False)),
            "description": sig.get("description") or "",
        })
    return out


def signal_map(profile):
    """{source -> signal}. The map ingest uses to name an incoming reading."""
    return {s["source"]: s for s in profile}


def interpret(profile, readings):
    """Raw {source: value} -> canonical readings, with range flags.

    Returns (named, unknown, out_of_range):
        named        {canonical_name: {"value":…, "unit":…, "in_range":bool}}
        unknown      sources the profile does not describe — REPORTED, not
                     dropped: a gateway sending a tag nobody configured is a
                     commissioning defect, and silence hides it
        out_of_range [names] whose value fell outside the configured band
    """
    by_source = signal_map(profile)
    named, unknown, out_of_range = {}, [], []
    for source, value in (readings or {}).items():
        sig = by_source.get(source)
        if sig is None:
            unknown.append(source)
            continue
        in_range = True
        if sig["datatype"] == "number":
            try:
                number = float(value)
            except (TypeError, ValueError):
                named[sig["name"]] = {"value": None, "unit": sig["unit"],
                                      "in_range": False, "raw": value}
                out_of_range.append(sig["name"])
                continue
            lo, hi = sig.get("min"), sig.get("max")
            if lo is not None and number < float(lo):
                in_range = False
            if hi is not None and number > float(hi):
                in_range = False
            value = number
        if not in_range:
            out_of_range.append(sig["name"])
        # The value is kept AS MEASURED even when out of range. Clamping would
        # turn an overheating machine into a healthy-looking one.
        named[sig["name"]] = {"value": value, "unit": sig["unit"],
                              "in_range": in_range}
    return named, sorted(unknown), out_of_range


def state_signals(profile):
    """The signals that decide whether the machine is running. A profile with
    none is not broken — AMP simply cannot infer state for that model, and must
    say so rather than guessing."""
    return [s["name"] for s in profile if s["state_signal"]]


# Reference profiles. NOT built in: they are examples an OEM can copy, and live
# here rather than in core so that nothing imports them by accident.
EXAMPLE_COMPRESSOR = [
    {"name": "running", "source": "Run", "datatype": "bool", "state_signal": True},
    {"name": "loaded", "source": "Load", "datatype": "bool", "state_signal": True},
    {"name": "pressure", "source": "P1", "datatype": "number", "unit": "bar",
     "min": 0, "max": 16, "aggregation": "avg"},
    {"name": "discharge_temperature", "source": "T_dis", "datatype": "number",
     "unit": "degC", "min": -20, "max": 120, "aggregation": "max"},
    {"name": "operating_hours", "source": "HrsRun", "datatype": "number",
     "unit": "h", "aggregation": "max"},
    {"name": "alarm_code", "source": "Alm", "datatype": "string"},
]

EXAMPLE_CNC = [
    {"name": "running", "source": "CycleActive", "datatype": "bool",
     "state_signal": True},
    {"name": "cycle_count", "source": "PartCount", "datatype": "number",
     "aggregation": "sum"},
    {"name": "spindle_speed", "source": "SpindleRPM", "datatype": "number",
     "unit": "rpm", "min": 0, "max": 24000, "aggregation": "avg"},
    {"name": "operating_hours", "source": "PowerOnHrs", "datatype": "number",
     "unit": "h", "aggregation": "max"},
]
