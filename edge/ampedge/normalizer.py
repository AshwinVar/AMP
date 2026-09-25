"""Raw readings become canonical samples — and counters become production.

THIS IS THE FILE THAT CAN INVENT PRODUCTION THAT NEVER HAPPENED. Everything
else in the gateway moves values around; this one does arithmetic on them, and
the arithmetic decides what a plant believes it made. Four ways it can go wrong,
all of them real, all of them silent:

  1. A CUMULATIVE COUNTER READ AS AN INCREMENT. The tag says 48,210 — total
     since the PLC was commissioned. Adding it every poll reports millions.
  2. A ROLLOVER READ AS A RESET. A 16-bit counter goes 65,530 -> 3. That is 9
     parts, not -65,527 and not 3.
  3. A RESET READ AS A ROLLOVER. A shift counter goes 412 -> 0. That is a reset:
     0 new parts. Reading it as a rollover invents 65,124.
  4. A REPLAYED SAMPLE COUNTED TWICE. The buffer reconnects and re-sends; the
     same counter reading arrives again; the shift's output doubles.

The rule that resolves all four: THE GATEWAY NEVER GUESSES WHICH IT IS. The
mapping declares `counter_mode`, `validate()` refuses a counter without one, and
a decrease this module cannot explain from the declared mode produces ZERO and a
recorded note — never an invented quantity. Under-reporting is visible to an
operator who knows what they made. Over-reporting is not.

AND THE OTHER HALF: absence stays absence. A PLC that has gone away produces no
sample at all, not a zero and not a False. `running=False` means the PLC said
so; a missing `running` means AMP does not know, and the AMP side already has a
vocabulary for that.
"""
import time

from . import mapping as mapping_mod
from . import signals
from .adapters import base

# A PLC clock that is ahead of the gateway's happens constantly (a PLC rarely
# has NTP). A little skew is tolerable; a lot means the timestamp is unusable,
# and a sample stamped in the future poisons every "in the last hour" query in
# AMP for as long as it takes the clock to catch up.
FUTURE_TOLERANCE_S = 60.0

# Older than this and a value is not current any more. It is still true of the
# moment it was taken — which is why a buffered sample keeps its own timestamp
# and is NOT presented as live.
DEFAULT_STALE_AFTER_S = 120.0


class Sample:
    """One canonical signal, ready to publish. Immutable by convention."""

    __slots__ = ("signal", "value", "timestamp", "unit", "source_time", "note")

    def __init__(self, signal, value, timestamp, unit="", source_time=False, note=""):
        self.signal = signal
        self.value = value
        self.timestamp = timestamp
        self.unit = unit
        self.source_time = source_time
        self.note = note

    def as_dict(self):
        out = {"signal": self.signal, "value": self.value, "ts": round(self.timestamp, 3)}
        if self.unit:
            out["unit"] = self.unit
        if self.note:
            out["note"] = self.note
        return out

    def __repr__(self):
        return f"<Sample {self.signal}={self.value!r} @{self.timestamp:.0f}{' ' + self.note if self.note else ''}>"


class Rejection:
    """A reading that produced no sample, and why. Counted, never silent.

    Every one of these is a thing a commissioning engineer needs to see: forty
    readings in and two rejections means two tags are wrong, and without this
    the screen would simply show two signals that never appear.
    """

    __slots__ = ("tag", "signal", "reason")

    def __init__(self, tag, signal, reason):
        self.tag = tag
        self.signal = signal
        self.reason = reason

    def __repr__(self):
        return f"<Rejection {self.tag}: {self.reason}>"


class _CounterState:
    __slots__ = ("last_raw", "last_ts", "total")

    def __init__(self):
        self.last_raw = None
        self.last_ts = None
        self.total = 0


class Normalizer:
    """Holds exactly the state counters need, and nothing else.

    One per machine. It is deliberately NOT persisted across a gateway restart:
    a restarted gateway does not know what happened while it was down, and the
    safe answer to "how many parts since my last reading three hours ago" is to
    re-baseline and count zero, not to subtract two numbers across a gap that
    may contain a shift change, a reset, or a rollover.
    """

    def __init__(self, mappings, stale_after=DEFAULT_STALE_AFTER_S):
        self.mappings = {m.tag: m for m in mappings}
        # AN ADAPTER RETURNS READINGS KEYED BY ADDRESS, not by the human label
        # in the config — `ns=2;i=7` rather than "parts". Indexing only by tag
        # meant every reading came back unmapped and the whole pipeline silently
        # produced nothing, which the first end-to-end run caught. Both indexes
        # are kept because a mapping's tag IS its address for protocols where a
        # register number is the only name a value has.
        self.by_address = {str(m.address): m for m in mappings
                           if m.address is not None and m.address != ""}
        self.by_signal = {m.signal: m for m in mappings if m.signal}
        self.stale_after = float(stale_after)
        self._counters = {}
        self._last_ts = {}
        self._last_value = {}
        self.rejections = []
        self.counter_notes = []

    # ── the entry point ─────────────────────────────────────────────
    def absorb(self, readings, now=None):
        """Readings in, canonical Samples out. Rejections recorded, not raised."""
        now = time.time() if now is None else now
        self.rejections = []
        out = []
        for reading in readings:
            sample = self._one(reading, now)
            if sample is not None:
                out.append(sample)
        return out

    def _one(self, reading, now):
        spec = self.by_address.get(str(reading.tag)) or self.mappings.get(reading.tag)
        if spec is None:
            self.rejections.append(Rejection(reading.tag, None, "no mapping for this tag"))
            return None

        # 1. Absence. The PLC did not give us a usable value, so there is no
        #    sample. NOT a zero, NOT the previous value, NOT False.
        if not reading.is_usable:
            self.rejections.append(Rejection(
                reading.tag, spec.signal,
                f"{reading.quality.lower()}: {reading.detail or 'no value'}"))
            return None

        # 2. The clock. Done before conversion because a value with an unusable
        #    timestamp is unusable whatever it converts to.
        ts = float(reading.timestamp)
        if ts > now + FUTURE_TOLERANCE_S:
            self.rejections.append(Rejection(
                reading.tag, spec.signal,
                f"timestamped {int(ts - now)}s in the future; check the PLC clock"))
            return None
        previous_ts = self._last_ts.get(spec.signal)
        if previous_ts is not None and ts < previous_ts:
            # Out of order. For a state signal this would show a stale value as
            # current; for a counter it would compute a negative delta.
            self.rejections.append(Rejection(
                reading.tag, spec.signal,
                f"older than the last sample for {spec.signal} by {previous_ts - ts:.1f}s"))
            return None

        # 3. Conversion. The mapper refuses rather than defaults.
        try:
            value = spec.apply(reading.value)
        except mapping_mod.ValueRefused as e:
            self.rejections.append(Rejection(reading.tag, spec.signal, str(e)))
            return None

        # 4. Type. A `running` that arrives as 1 is a mapping bug, and letting it
        #    through means AMP decides what 1 means.
        expected = signals.expected_type(spec.signal)
        if expected is not None and not isinstance(value, expected):
            if expected is float and isinstance(value, int) and not isinstance(value, bool):
                value = float(value)
            else:
                self.rejections.append(Rejection(
                    reading.tag, spec.signal,
                    f"{spec.signal} must be {getattr(expected, '__name__', expected)}, "
                    f"got {type(value).__name__}"))
                return None

        self._last_ts[spec.signal] = ts

        # 5. Counters become production. Everything else is reported as it is.
        if signals.is_counter(spec.signal):
            return self._counter(spec, value, ts, reading.source_time)

        # A repeat of the same state at a later time is not news, but it IS
        # evidence the PLC is alive, so it is kept rather than deduplicated:
        # AMP's freshness is computed from the last sample, and dropping repeats
        # would make a steadily-running machine look disconnected.
        self._last_value[spec.signal] = value
        return Sample(spec.signal, value, ts, unit=spec.unit, source_time=reading.source_time)

    # ── counters ────────────────────────────────────────────────────
    def _counter(self, spec, raw, ts, source_time):
        state = self._counters.setdefault(spec.signal, _CounterState())
        mode = spec.counter_mode
        note = ""

        if mode == "per_cycle":
            # The tag holds what the LAST CYCLE produced. It is production when
            # it changes, and the same number sitting there for six polls is one
            # cycle, not six. A cycle that genuinely repeats the same count is
            # therefore missed — documented, and the reason `cumulative` is the
            # recommended mapping for anything that offers both.
            if state.last_raw is not None and raw == state.last_raw:
                return None
            state.last_raw = raw
            state.last_ts = ts
            delta = int(raw)
        elif state.last_raw is None:
            # FIRST READING IS A BASELINE, NEVER PRODUCTION. A cumulative
            # counter at 48,210 on the first poll is 48,210 parts made before
            # this gateway existed.
            state.last_raw = raw
            state.last_ts = ts
            return None
        elif raw >= state.last_raw:
            delta = int(raw - state.last_raw)
            state.last_raw = raw
            state.last_ts = ts
        else:
            delta, note = self._decrease(spec, state, raw, ts, mode)
            if delta is None:
                return None

        state.total += delta
        return Sample(spec.signal, delta, ts, unit=spec.unit, source_time=source_time, note=note)

    def _decrease(self, spec, state, raw, ts, mode):
        """The counter went backwards. Explain it from the declared mode or refuse."""
        if mode == "resets":
            # Declared to reset — at shift change, on a button, nightly. The new
            # value IS production since the reset, because the counter restarts
            # at zero and counts up.
            state.last_raw = raw
            state.last_ts = ts
            self.counter_notes.append(
                f"{spec.signal}: counter reset ({state.last_raw} -> {raw}), counted {int(raw)}")
            return int(raw), "counter_reset"

        # Cumulative. A decrease is either a rollover or something we do not
        # understand, and the difference is only knowable from a DECLARED
        # maximum. Without one, guessing rollover is how 412 -> 0 becomes 65,124
        # phantom parts.
        ceiling = spec.raw.get("counter_max")
        if ceiling:
            ceiling = int(ceiling)
            # Only near the top. A counter at 300 dropping to 3 is not a
            # rollover of a 65,535 register, whatever the config says.
            if state.last_raw >= ceiling * 0.9:
                delta = int((ceiling - state.last_raw) + raw + 1)
                state.last_raw = raw
                state.last_ts = ts
                return delta, "counter_rollover"

        previous = state.last_raw
        state.last_raw = raw
        state.last_ts = ts
        self.counter_notes.append(
            f"{spec.signal}: went backwards {previous} -> {raw} and counter_mode is "
            f"{mode!r} with no counter_max that explains it; counted 0 and re-baselined")
        self.rejections.append(Rejection(
            spec.tag, spec.signal,
            f"counter went backwards ({previous} -> {raw}); counted 0 rather than invent "
            f"production. If this counter resets, set counter_mode: resets. If it rolls "
            f"over, set counter_max."))
        return 0, "counter_rebaselined"

    # ── what the health screen asks ─────────────────────────────────
    def freshness(self, now=None):
        """Per signal: how long since a sample. None means never — not zero."""
        now = time.time() if now is None else now
        return {signal: (now - ts) for signal, ts in self._last_ts.items()}

    def is_stale(self, signal, now=None):
        now = time.time() if now is None else now
        ts = self._last_ts.get(signal)
        if ts is None:
            return None              # never seen is not stale; it is unknown
        return (now - ts) > self.stale_after

    def totals(self):
        return {signal: state.total for signal, state in self._counters.items()}
