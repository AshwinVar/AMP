"""Tag mapping: a PLC address becomes a canonical AMP signal, by CONFIGURATION.

THE RULE IS THAT AN ENGINEER NEVER EDITS PYTHON. A pilot's tag list arrives as a
spreadsheet from a controls engineer, and it is wrong the first three times:
a node id has a typo, a register is 16-bit not 32-bit, a counter turns out to
reset every shift. If any of that needs a code change, every correction is a
deploy, and commissioning takes a week instead of an afternoon.

So a mapping is data:

    source          protocol, address/node, datatype
    transformation  scale, offset, boolean mapping, enum mapping, units
    destination     the canonical AMP signal

VALIDATION HAPPENS BEFORE ANYTHING RUNS. `validate()` is the gate: it refuses a
mapping that names a signal AMP does not have, a datatype it cannot read, a
boolean map that cannot produce a bool, or a counter with no `counter_mode`.
Refusing at load is the whole point — a mapping that fails at 3am on the
twentieth packet is a mapping that has already written nonsense into a plant's
history.

WHAT THIS DELIBERATELY WILL NOT DO. It will not guess. A value that does not fit
its declared datatype, a boolean whose raw value is in neither the true-set nor
the false-set, an enum with no entry for what arrived — each is an ERROR with
the tag named, never a default. `UNKNOWN is not false` and `no data is not 0`
are the same rule AMP's read-models already keep, held one layer earlier.
"""
from . import signals

# Datatypes a mapping may declare. Deliberately small: these are what the two
# pilot protocols actually produce, and a type nobody has read from a real PLC
# is a type nobody has tested.
DATATYPES = ("bool", "int", "float", "string")

# How a counter behaves on the PLC. There is no safe default — see below.
COUNTER_MODES = ("cumulative", "per_cycle", "resets")


class MappingError(ValueError):
    """A mapping AMP Edge refuses to run. Names the tag; never vague."""


class ValueRefused(ValueError):
    """A reading the mapping will not convert. Named, never defaulted."""


def _as_bool(raw, spec, tag):
    """A boolean, or a refusal. Never a guess.

    `true_values` / `false_values` exist because a PLC's idea of true is not
    Python's: 1, "1", "ON", "RUN", -1 (Siemens), and a plain bool are all real.
    What is NOT acceptable is treating anything-not-in-true-set as false. A
    mis-typed mapping would then report every machine as stopped, and a stopped
    machine looks exactly like a quiet one — nobody would question it.
    """
    if isinstance(raw, bool):
        return raw
    true_set = spec.get("true_values")
    false_set = spec.get("false_values")
    if true_set is None and false_set is None:
        if isinstance(raw, (int, float)):
            return bool(raw)
        raise ValueRefused(
            f"{tag}: {raw!r} is not a boolean and the mapping gives no true_values/false_values "
            f"to read it by")
    norm = raw.strip().lower() if isinstance(raw, str) else raw
    def _in(values):
        if not values:
            return False
        return any((v.strip().lower() if isinstance(v, str) else v) == norm for v in values)
    if _in(true_set):
        return True
    if _in(false_set):
        return False
    raise ValueRefused(
        f"{tag}: {raw!r} is in neither true_values nor false_values, so AMP will not decide "
        f"whether it means running or stopped")


def _as_number(raw, tag, want_int):
    if isinstance(raw, bool):
        raw = int(raw)
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            raise ValueRefused(f"{tag}: an empty string is not a number")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueRefused(f"{tag}: {raw!r} is not a number")
    if value != value or value in (float("inf"), float("-inf")):
        # NaN and infinity print as figures and mean nothing — the same rule
        # ev.Fact keeps on the AMP side.
        raise ValueRefused(f"{tag}: {raw!r} is not a finite number")
    return int(round(value)) if want_int else value


class TagMapping:
    """One PLC tag and what it becomes."""

    __slots__ = ("tag", "signal", "address", "datatype", "scale", "offset", "unit",
                 "true_values", "false_values", "enum", "counter_mode", "raw")

    def __init__(self, spec: dict):
        self.raw = dict(spec)
        # `or` would swallow address 0, which is a legal Modbus coil -- and the
        # most common first address on a device. Explicit None checks throughout
        # this file for exactly that reason.
        tag = spec.get("tag")
        if tag is None or tag == "":
            tag = spec.get("address")
        self.tag = str(tag) if tag is not None and tag != "" else "<unnamed>"
        self.signal = spec.get("signal")
        self.address = spec.get("address")
        self.datatype = (spec.get("datatype") or "float").lower()
        self.scale = spec.get("scale", 1)
        self.offset = spec.get("offset", 0)
        self.unit = spec.get("unit") or ""
        self.true_values = spec.get("true_values")
        self.false_values = spec.get("false_values")
        self.enum = spec.get("enum")
        self.counter_mode = spec.get("counter_mode")

    def apply(self, raw):
        """The canonical value for a raw reading, or ValueRefused.

        Order matters: scale and offset are applied to the NUMBER, before any
        enum lookup, because an enum whose keys were scaled would key on a value
        the engineer never wrote down.
        """
        if raw is None:
            raise ValueRefused(f"{self.tag}: the PLC returned no value")
        if self.datatype == "bool":
            return _as_bool(raw, self.raw, self.tag)
        if self.datatype == "string":
            return str(raw)
        want_int = self.datatype == "int"
        value = _as_number(raw, self.tag, want_int=False)
        value = value * float(self.scale) + float(self.offset)
        if self.enum:
            key = int(round(value))
            # str() because config formats (JSON keys, YAML) disagree about
            # whether 3 and "3" are the same key, and an engineer writing a
            # mapping should not have to care.
            table = {str(k): v for k, v in self.enum.items()}
            if str(key) not in table:
                raise ValueRefused(
                    f"{self.tag}: {key} has no entry in the mapping's enum, so AMP will not "
                    f"invent a name for it")
            return str(table[str(key)])
        return int(round(value)) if want_int else value


def validate(specs) -> list:
    """Every mapping, checked before anything connects. Raises on the first fault.

    The errors are collected rather than raised one at a time, because a pilot
    engineer fixing a tag list wants all twelve problems at once, not twelve
    round trips.
    """
    problems = []
    mappings = []
    seen = set()
    for i, spec in enumerate(specs or []):
        if not isinstance(spec, dict):
            problems.append(f"mapping #{i + 1} is not an object")
            continue
        m = TagMapping(spec)
        where = f"{m.tag!r}"
        if m.address is None or m.address == "":
            # NOT `if not m.address`: coil 0 and holding register 0 are real
            # addresses, and rejecting them told a commissioning engineer their
            # correct mapping was missing an address.
            problems.append(f"{where}: no address/node to read")
        if not m.signal:
            problems.append(f"{where}: no AMP signal to write to")
        elif not signals.is_canonical(m.signal) and m.signal not in signals.KNOWN_TELEMETRY:
            # Not a refusal: a plant measures things AMP cannot enumerate. But
            # it IS worth saying, because a typo'd canonical name ("runing")
            # would otherwise be silently accepted as telemetry and never drive
            # machine state — the failure would look like "AMP ignores my
            # running signal".
            problems.append(
                f"{where}: {m.signal!r} is not an AMP signal. Canonical signals are "
                f"{', '.join(signals.CANONICAL)}. Process telemetry (e.g. "
                f"{', '.join(signals.KNOWN_TELEMETRY[:5])}) is published under `readings`, but "
                f"BE AWARE: AMP currently interprets `readings` only for a machine registered as "
                f"an OEM installation with a telemetry profile. On an ordinary machine they are "
                f"delivered and then ignored. Map it if you want it on the wire; do not promise "
                f"a customer a chart of it yet.")
        if m.datatype not in DATATYPES:
            problems.append(f"{where}: datatype {m.datatype!r} is not one of {', '.join(DATATYPES)}")
        if m.signal in seen:
            problems.append(f"{where}: {m.signal!r} is mapped more than once; AMP would take "
                            f"whichever tag was polled last")
        if m.signal:
            seen.add(m.signal)
        # A counter with no declared mode is the single most expensive mistake
        # in this file. Cumulative read as per-cycle reports 120,000 parts a
        # shift; per-cycle read as cumulative reports 12. There is no safe
        # default, so there is no default.
        if m.signal and signals.is_counter(m.signal) and m.counter_mode not in COUNTER_MODES:
            problems.append(
                f"{where}: {m.signal!r} is a counter, so counter_mode must be one of "
                f"{', '.join(COUNTER_MODES)}. There is no default: reading a cumulative counter "
                f"as per-cycle multiplies a shift's output by the number of cycles in it.")
        expected = signals.expected_type(m.signal) if m.signal else None
        if expected is bool and m.datatype != "bool":
            problems.append(f"{where}: {m.signal!r} must be a bool, but the datatype is "
                            f"{m.datatype!r}")
        if m.enum is not None and not isinstance(m.enum, dict):
            problems.append(f"{where}: enum must be a table of value -> name")
        mappings.append(m)
    if problems:
        raise MappingError("; ".join(problems))
    return mappings
