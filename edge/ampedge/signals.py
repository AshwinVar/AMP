"""The canonical AMP signal vocabulary.

NOTHING ABOVE THE ADAPTER KNOWS WHAT A PLC IS. An OPC UA node id
(`ns=3;s=Machine.Running`) and a Modbus coil (`coil 17`) are two ways of saying
the same thing, and the moment either of them reaches the normalizer, the queue,
the publisher or AMP itself, every one of those has to learn a protocol. So the
adapters emit THESE names and nothing else.

    OPC UA   ns=3;s=Machine.Running  ┐
                                     ├─→  running = True
    Modbus   coil 17                 ┘

WHY A CLOSED SET. A pilot's tag list is written by a controls engineer who has
never seen AMP, and "MachineRunning", "Run", "running_state" and "RUN_BIT" are
all the same idea. Mapping them onto one name is the entire job of the tag
mapper; letting arbitrary names through would move that job into AMP, where
every read-model would have to guess.

Anything outside this set is PROCESS TELEMETRY — temperature, pressure, a
spindle speed. That is deliberately open-ended and carries its own unit, because
a plant measures things AMP cannot enumerate in advance. It is stored and
charted; it never drives machine state.
"""

# ── State: what the machine is doing right now ──────────────────────
RUNNING = "running"                  # bool  — producing, as the PLC sees it
CYCLE_ACTIVE = "cycle_active"        # bool  — mid-cycle (a press in stroke)
FAULT_ACTIVE = "fault_active"        # bool  — an alarm is standing
FAULT_CODE = "fault_code"            # int/str — the code behind fault_active
MACHINE_MODE = "machine_mode"        # str   — Auto / Manual / Setup, plant words

# ── Counters: what it has made ──────────────────────────────────────
#
# THESE ARE THE DANGEROUS ONES. A counter is cumulative on most PLCs and resets
# on others, and the difference decides whether AMP reports 12 parts or 120,000.
# `counter_mode` on the mapping says which, and the normalizer — never a
# read-model — turns it into a delta.
PART_COUNT = "part_count"            # int — everything made
GOOD_COUNT = "good_count"            # int — passed
REJECT_COUNT = "reject_count"        # int — scrap or rework

# ── Timing ──────────────────────────────────────────────────────────
CYCLE_TIME = "cycle_time"            # float, seconds — last completed cycle
OPERATING_HOURS = "operating_hours"  # float, hours   — cumulative runtime

STATE_SIGNALS = (RUNNING, CYCLE_ACTIVE, FAULT_ACTIVE, FAULT_CODE, MACHINE_MODE)
COUNTER_SIGNALS = (PART_COUNT, GOOD_COUNT, REJECT_COUNT)
TIMING_SIGNALS = (CYCLE_TIME, OPERATING_HOURS)

CANONICAL = STATE_SIGNALS + COUNTER_SIGNALS + TIMING_SIGNALS

# What each one must be once the mapper is done with it. A `running` that
# arrives as the string "1" is a mapping bug, not a value AMP should interpret:
# guessing what "0", "off", "FALSE" and "" mean is how a stopped machine ends up
# reported as running.
TYPES = {
    RUNNING: bool,
    CYCLE_ACTIVE: bool,
    FAULT_ACTIVE: bool,
    FAULT_CODE: (int, str),
    MACHINE_MODE: str,
    PART_COUNT: int,
    GOOD_COUNT: int,
    REJECT_COUNT: int,
    CYCLE_TIME: float,
    OPERATING_HOURS: float,
}

# Process telemetry AMP knows how to chart if a pilot maps it. NOT a closed set:
# anything else a mapping names is carried through with its unit and stored, it
# simply gets no special meaning.
KNOWN_TELEMETRY = ("temperature", "pressure", "speed", "power", "energy",
                   "vibration", "flow", "level", "torque", "current")


def is_canonical(name: str) -> bool:
    return name in CANONICAL


def is_counter(name: str) -> bool:
    return name in COUNTER_SIGNALS


def expected_type(name: str):
    """The Python type a canonical signal must arrive as, or None for telemetry."""
    return TYPES.get(name)
