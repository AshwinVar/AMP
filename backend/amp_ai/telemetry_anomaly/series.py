"""5-minute bucket medians and machine-state labels for telemetry anomaly scoring.

Standard library only. Works on integer MICROSECOND OFFSETS BEFORE AN ANCHOR,
never on datetimes, so the database path (``db_telemetry``) and the synthetic
evaluation (``build_eval``) share one implementation of every rule here.

THE ANCHOR AND THE BUCKETS
--------------------------
The anchor is the END of the score window ("now", exclusive). Bucket ``k``
covers the half-open interval ``[anchor - (k + 1) * 5 min, anchor - k * 5 min)``:

  k = 0 .. 11          the score window, [anchor - 1 h, anchor)
  k = 12 .. 4031       the baseline window, [anchor - 14 d, anchor - 1 h)

A reading exactly one hour before the anchor is in bucket 11 - the score window
- and one microsecond earlier is in bucket 12. Counting back from the anchor
means the score window is always exactly twelve whole buckets and the baseline
exactly 4020, whatever the wall-clock time.

Which ROWS belong to which window is decided once, by the bounded SQL filter in
``db_telemetry``. ``build_buckets`` does not filter; it REFUSES a reading whose
bucket lies outside the range the caller says it queried, so the two can never
silently disagree.

A BUCKET'S VALUE AND STATE
--------------------------
* value: per signal, the median of that bucket's readings (one glitched reading
  among five cannot move it).
* state: the machine's state THROUGHOUT the bucket. The state at the bucket's
  start is the last status event at or before that instant (``Running`` /
  ``NotRunning``, or ``Unknown`` when the status is not known). If a status
  event strictly inside the bucket changes that label, the bucket is
  ``Transition``: its median mixes two regimes (a machine that stopped half-way
  through five minutes reads half-way between running and idle), so it belongs
  to neither state's baseline and is never fitted or scored. The state is not
  guessed from the readings.

  Why not simply the state at the start: a bucket labelled Running whose machine
  stopped two minutes in is scored against the running baseline and looks wildly
  abnormal. Stops recur every few hours, so those artifacts fill the top of every
  calibration sample and bury real anomalies. This was found on development data
  (the validation split and a separate diagnostics split), before the test split
  was ever scored; build_eval's ``development_notes`` record it.
"""
import bisect
import math
import statistics

__all__ = [
    "BUCKET_SECONDS", "BUCKET_MICROSECONDS", "BUCKETS_PER_DAY", "SCORE_BUCKETS", "BASELINE_DAYS",
    "TOTAL_BUCKETS", "STATE_RUNNING", "STATE_NOT_RUNNING", "STATE_UNKNOWN", "STATE_TRANSITION", "STATES", "Bucket",
    "bucket_index", "bucket_medians", "integer_valued_signals", "state_label", "bucket_states", "build_buckets",
]

BUCKET_SECONDS = 300
BUCKET_MICROSECONDS = BUCKET_SECONDS * 1_000_000
BUCKETS_PER_DAY = 86400 // BUCKET_SECONDS
SCORE_BUCKETS = 12                                   # one hour
BASELINE_DAYS = 14                                   # telemetry retention (retention.py) is 14 days
TOTAL_BUCKETS = BASELINE_DAYS * BUCKETS_PER_DAY      # score + baseline

STATE_RUNNING = "Running"
STATE_NOT_RUNNING = "NotRunning"
STATE_UNKNOWN = "Unknown"
STATE_TRANSITION = "Transition"
STATES = (STATE_RUNNING, STATE_NOT_RUNNING, STATE_UNKNOWN, STATE_TRANSITION)


class Bucket:
    """One 5-minute bucket: its index back from the anchor, the machine state at its start, and signal medians."""

    __slots__ = ("k", "state", "values")

    def __init__(self, k, state, values):
        self.k = k
        self.state = state
        self.values = values

    def __repr__(self):
        return f"Bucket(k={self.k}, state={self.state!r}, values={self.values!r})"


def bucket_index(delta_us: int) -> int:
    """Bucket of a reading taken ``delta_us`` microseconds before the anchor (``delta_us >= 1``)."""
    if isinstance(delta_us, bool) or not isinstance(delta_us, int):
        raise TypeError(f"offset must be an int number of microseconds, got {type(delta_us).__name__}")
    if delta_us < 1:
        raise ValueError("a reading at or after the anchor belongs to no bucket")
    return (delta_us - 1) // BUCKET_MICROSECONDS


def bucket_medians(readings) -> dict:
    """{k: {signal: median}} from (delta_us, signal, value) readings."""
    grouped = {}
    for delta_us, name, value in readings:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"reading for {name!r} must be a finite number, got {value!r}")
        grouped.setdefault(bucket_index(delta_us), {}).setdefault(name, []).append(float(value))
    return {k: {name: float(statistics.median(vals)) for name, vals in per.items()} for k, per in grouped.items()}


def integer_valued_signals(readings) -> frozenset:
    """Signals whose every reading is a whole number (their MAD floor includes the quantisation step)."""
    seen, fractional = set(), set()
    for _, name, value in readings:
        seen.add(name)
        if not float(value).is_integer():
            fractional.add(name)
    return frozenset(seen - fractional)


def state_label(running) -> str:
    if running is True:
        return STATE_RUNNING
    if running is False:
        return STATE_NOT_RUNNING
    return STATE_UNKNOWN


def bucket_states(ks, events, initial_running) -> dict:
    """{k: the machine's state throughout bucket k}.

    ``events`` are (delta_us, running) status changes, running being True, False
    or None (a status that is not known); ``initial_running`` is the state before
    the earliest event. Bucket k covers offsets ``(k * B, (k + 1) * B]`` before
    the anchor (B = BUCKET_MICROSECONDS). Its starting state is set by the last
    event at or before its start (offset >= (k + 1) * B). An event strictly
    inside the bucket (k * B < offset < (k + 1) * B) whose label differs from the
    starting state makes the bucket ``Transition``. An event exactly at the
    bucket's end (offset == k * B) is the next bucket's start, not this one's.
    """
    ordered = sorted(events, key=lambda e: -e[0])       # oldest first
    times = [-e[0] for e in ordered]                     # ascending "time" = -offset
    out = {}
    for k in ks:
        start_time = -(k + 1) * BUCKET_MICROSECONDS
        end_time = -k * BUCKET_MICROSECONDS
        i = bisect.bisect_right(times, start_time)         # events at or before the start
        j = bisect.bisect_left(times, end_time)            # events strictly before the end
        state = state_label(ordered[i - 1][1] if i else initial_running)
        if any(state_label(ordered[x][1]) != state for x in range(i, j)):
            state = STATE_TRANSITION
        out[k] = state
    return out


def build_buckets(readings, events, initial_running, *, first_k, last_k) -> list:
    """Buckets (ordered by k) for readings the caller queried from buckets ``first_k..last_k``.

    Refuses a reading that falls outside that range: the window is decided by
    the query, and a mismatch means the query and the buckets disagree.
    """
    readings = list(readings)
    medians = bucket_medians(readings)
    outside = [k for k in medians if not first_k <= k <= last_k]
    if outside:
        raise ValueError(f"readings fall in buckets {sorted(outside)[:3]} outside the queried range "
                         f"[{first_k}, {last_k}]")
    ks = sorted(medians)
    states = bucket_states(ks, events, initial_running)
    return [Bucket(k, states[k], medians[k]) for k in ks]
