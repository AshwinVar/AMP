"""Synthetic machine telemetry for EVALUATING the telemetry anomaly scorer (generator v1).

THIS FILE IS A SPECIFICATION
----------------------------
It was written, tested and committed BEFORE the scorer's evaluation harness,
and its SHA-256 (line endings normalised) is recorded in the evaluation JSON and
keys the evaluation ledger. Change anything here and the committed evaluation no
longer describes this generator.

It is deliberately INDEPENDENT of what it evaluates: standard library only, and
it imports none of the scorer (``series``, ``baseline``), the robust statistics
it uses, the rule-based predictors (``predictive_engine``, ``ai``), AMP's demo
simulators (``factory_simulator``, ``industrial_adapters``) or the database. The
constants below come from how a machine tool behaves, not from what the scorer
can detect. No constant was chosen by looking at a score.

WHAT A SYNTHETIC MACHINE IS
---------------------------
One reading per minute per signal (a typical PLC/IoT gateway publish rate), for
``history_days`` days of history followed by an evaluation period.

Schedule (one archetype per machine)
    two_shift    Mon-Fri 06:00-22:00, and Saturday 06:00-14:00 on half the machines
    three_shift  Mon-Sat round the clock, Sunday off
    day_shift    Mon-Fri 08:00-17:00
  Outside the schedule the machine is idle (spindle stopped, controls powered).
  Inside it the machine stops at random - material waits, changeovers, operator
  breaks - on average once per 150 running minutes, for an exponential duration
  with mean 12 minutes (at least 2).

Job load L (fraction of rated spindle power)
  Each job holds a level drawn from U[0.45, 0.90] (below 0.45 a spindle is
  oversized for the job; above 0.90 is avoided to protect the drive) for
  U{20..120} minutes. Within a job the cut varies as AR(1): x <- 0.9 x + N(0, 0.012),
  L = clamp(level + x, 0.05, 1.0). Idle machines have L = 0.

Signals (the base four are always present; each optional one on half the machines)
  spindle_load_pct      100 L + N(0, 1.2); idle 2 + N(0, 0.6). Integer percent, as PLCs report it.
  motor_current_a       I_idle + (I_rated - I_idle) L + N(0, 0.012 I_rated).
                        I_rated ~ U[20, 40] A (a 11-22 kW spindle at 400 V);
                        I_idle = U[0.05, 0.10] I_rated (drive and auxiliaries energised).
                        One decimal.
  bearing_temp_c        First-order thermal lag, tau ~ U[20, 35] min, towards
                        ambient + dT_rated L while running and ambient while idle;
                        dT_rated ~ U[20, 32] C. Ambient = U[20, 28] + U[2, 5] sin(2 pi (h - 9) / 24)
                        (coolest before dawn, warmest mid-afternoon) plus a day-to-day
                        weather random walk (N(0, 0.7) per day, clamped to +-3 C,
                        interpolated linearly). Sensor N(0, 0.3). Integer degrees.
  vibration_mm_s        RMS velocity (v0 + v1 L) while running, v_idle idle;
                        v0 ~ U[1.0, 1.8], v1 ~ U[0.4, 1.2], v_idle ~ U[0.10, 0.25];
                        multiplicative log-normal noise exp(0.08 N). Two decimals.
  power_kw (optional)   sqrt(3) * 0.4 kV * pf 0.85 = 0.589 kW per amp of the TRUE
                        current, with its own meter noise N(0, 0.008 P_rated). One decimal.
  coolant_pressure_bar  (optional) set point U[5.5, 6.5] bar + N(0, 0.06) while running;
                        pump off at idle: 0.2 + N(0, 0.03). One decimal.

Sensor layer, applied after the process
  Benign spikes     single-reading glitches (EMI, a contact bounce) on 2% of
                    5-minute buckets per signal: per-reading probability
                    1 - 0.98 ** (1 / 5), offset +-U[0.10, 0.30] of the declared range.
  Dropouts          1% of readings independently, plus gateway outages starting
                    once per 4 days on average and lasting U{10..180} minutes, in
                    which every signal is missing.
  Clamping          load in [0, 100]; every other signal >= 0.

Declared static ranges (what an engineer would type into a static alarm)
  load [0, 100] %; current [0, 1.15 I_rated]; power [0, 1.15 * 0.589 * I_rated];
  bearing temperature [5, 80] C; vibration [0, 4.5] mm/s (the ISO 10816 class II
  "satisfactory" limit); coolant pressure [0, 7.5] bar.

EVALUATION WINDOWS AND ANOMALIES
--------------------------------
Windows are one hour long, end on a 5-minute boundary, lie in the evaluation
period after the history, start with the machine running (anomaly alerts matter
during production, and positives and negatives are chosen the same way) and
never overlap on one machine. Exactly round(anomaly_fraction * total) windows,
chosen at random across the dataset, are anomalous; the six types are assigned
round-robin in a shuffled order, so they are balanced.

An anomaly is applied ONLY to the scored copy of its window (``EvalWindow.readings``),
never to ``MachineSeries.signals``: the history a baseline is fitted on is clean
apart from the benign spikes and dropouts above. Onset is a running minute in
[0, 45] (spikes: [0, 54]); the anomaly lasts to the end of the window.

  spike              a jam: load, current and power (if present) x U[1.35, 1.70] for 3-6 minutes
  level_shift        bearing temperature +U[6, 12] C (a loosened RTD connection or a
                     cooling fault step)
  slow_drift         vibration x (1 + g * progress), g ~ U[0.8, 1.5] by the window's end
                     (a bearing starting to fail)
  stuck_at           the spindle-load tag stops updating at its onset value
                     (no dropouts either: a frozen tag keeps publishing)
  correlation_break  motor current reads as if the load were an independent
                     L' ~ U[0.45, 0.90] (a transducer on the wrong drive) while
                     load and power keep tracking the real cut
  noise_burst        motor current + N(0, U[0.08, 0.15] I_rated) (VFD interference)

Misspecification variants (reported, never gated)
  heavy_tails        every noise draw is Student-t with 3 degrees of freedom scaled
                     to unit variance, and benign spikes are 3x as frequent
  regime_switching   stops once per 25 running minutes (mean 6 minutes) and jobs of
                     U{5..30} minutes
"""
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..core.rng import make_rng

__all__ = [
    "GENERATOR_NAME", "GENERATOR_VERSION", "MINUTES_PER_DAY", "WINDOW_MINUTES", "BASE_SIGNALS",
    "OPTIONAL_SIGNALS", "ALL_SIGNALS", "INTEGER_SIGNALS", "ANOMALY_TYPES", "VARIANTS", "EPOCH",
    "MachineSeries", "EvalWindow", "generate_dataset", "generate_series", "state_transitions",
    "status_of", "reading_time",
]

GENERATOR_NAME = "amp_ai.telemetry_anomaly.synthetic"
GENERATOR_VERSION = "v1"
MINUTES_PER_DAY = 1440
WINDOW_MINUTES = 60
MAX_EXTRA_DAYS = 7
EPOCH = datetime(2026, 1, 5)  # a Monday; each machine starts 0-6 days later

BASE_SIGNALS = ("spindle_load_pct", "motor_current_a", "bearing_temp_c", "vibration_mm_s")
OPTIONAL_SIGNALS = ("power_kw", "coolant_pressure_bar")
ALL_SIGNALS = BASE_SIGNALS + OPTIONAL_SIGNALS
INTEGER_SIGNALS = frozenset({"spindle_load_pct", "bearing_temp_c"})
_DECIMALS = {"motor_current_a": 1, "power_kw": 1, "coolant_pressure_bar": 1, "vibration_mm_s": 2}

ANOMALY_TYPES = ("spike", "level_shift", "slow_drift", "stuck_at", "correlation_break", "noise_burst")
VARIANTS = ("main", "heavy_tails", "regime_switching")
SCHEDULES = ("two_shift", "three_shift", "day_shift")

KW_PER_AMP = math.sqrt(3) * 0.4 * 0.85
_SPIKE_BUCKET_SHARE = 0.02
_READINGS_PER_BUCKET = 5
_DROPOUT_PROBABILITY = 0.01
_OUTAGE_MEAN_GAP_MINUTES = 4 * MINUTES_PER_DAY

_REGIMES = {
    # (mean running minutes between stops, mean stop minutes, job minutes lo, hi)
    "main": (150.0, 12.0, 20, 120),
    "heavy_tails": (150.0, 12.0, 20, 120),
    "regime_switching": (25.0, 6.0, 5, 30),
}


@dataclass
class EvalWindow:
    end_minute: int
    label: int
    anomaly_type: str | None
    readings: dict
    detail: dict = field(default_factory=dict)


@dataclass
class MachineSeries:
    series_id: str
    split: str
    variant: str
    index: int
    history_days: int
    minutes: int
    start: datetime
    schedule: str
    signals: dict
    running: list
    declared_ranges: dict
    integer_signals: frozenset
    benign_spikes: dict
    windows: list


# --------------------------------------------------------------------------- noise
def _noise_source(seed_or_rng, variant):
    """A zero-mean, unit-variance noise draw for ``variant``."""
    rng = make_rng(seed_or_rng, "noise-source") if isinstance(seed_or_rng, int) else seed_or_rng
    gauss = rng.gauss
    if variant == "heavy_tails":
        scale = 1.0 / math.sqrt(3.0)

        def draw():
            chi2 = gauss(0.0, 1.0) ** 2 + gauss(0.0, 1.0) ** 2 + gauss(0.0, 1.0) ** 2
            return gauss(0.0, 1.0) / math.sqrt(chi2 / 3.0) * scale
        return draw
    return lambda: gauss(0.0, 1.0)


# --------------------------------------------------------------------------- helpers
def status_of(running: bool) -> str:
    """The machine status a synthetic transition is written as."""
    return "Running" if running else "Idle"


def reading_time(series: MachineSeries, minute: int) -> datetime:
    return series.start + timedelta(minutes=minute)


def state_transitions(series: MachineSeries) -> list:
    """[(minute, running)] for minute 0 and every minute the state changes."""
    out = []
    previous = None
    for m, r in enumerate(series.running):
        if r != previous:
            out.append((m, r))
            previous = r
    return out


def _scheduled(schedule, saturday_morning, day_offset, minute):
    dow = (day_offset + minute // MINUTES_PER_DAY) % 7
    hour = (minute % MINUTES_PER_DAY) / 60.0
    if schedule == "three_shift":
        return dow <= 5
    if schedule == "day_shift":
        return dow <= 4 and 8.0 <= hour < 17.0
    if dow <= 4:
        return 6.0 <= hour < 22.0
    return dow == 5 and saturday_morning and 6.0 <= hour < 14.0


def _check_args(n_series, windows_per_series, min_history_days, max_history_days, anomaly_fraction, variant):
    for name, value in (("n_series", n_series), ("windows_per_series", windows_per_series),
                        ("min_history_days", min_history_days), ("max_history_days", max_history_days)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be an int >= 1, got {value!r}")
    if max_history_days < min_history_days:
        raise ValueError("max_history_days must be >= min_history_days")
    if isinstance(anomaly_fraction, bool) or not isinstance(anomaly_fraction, (int, float)) \
            or not 0.0 <= anomaly_fraction <= 1.0:
        raise ValueError("anomaly_fraction must be in [0, 1]")
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}; expected one of {VARIANTS}")


# --------------------------------------------------------------------------- dataset
def generate_dataset(seed, split, *, n_series, windows_per_series, min_history_days, max_history_days,
                     anomaly_fraction, variant="main"):
    """Yield ``n_series`` machines, each with ``windows_per_series`` labelled evaluation windows."""
    _check_args(n_series, windows_per_series, min_history_days, max_history_days, anomaly_fraction, variant)
    if not isinstance(split, str) or not split:
        raise ValueError("split must be a non-empty string")
    total = n_series * windows_per_series
    n_positive = math.floor(anomaly_fraction * total + 0.5)
    rng = make_rng(seed, "telemetry_anomaly", split, variant, "labels")
    slots = sorted(rng.sample(range(total), n_positive))
    order = list(ANOMALY_TYPES)
    rng.shuffle(order)
    types = [order[i % len(order)] for i in range(n_positive)]
    rng.shuffle(types)
    positives = dict(zip(slots, types))
    for i in range(n_series):
        mine = {slot - i * windows_per_series: kind for slot, kind in positives.items()
                if i * windows_per_series <= slot < (i + 1) * windows_per_series}
        params_rng = make_rng(seed, "telemetry_anomaly", split, "history-days", str(i))
        history_days = params_rng.randint(min_history_days, max_history_days)
        yield generate_series(seed, split, i, history_days=history_days,
                              windows_per_series=windows_per_series, positives=mine, variant=variant)


def generate_series(seed, split, index, *, history_days, windows_per_series, positives, variant="main"):
    """One synthetic machine. ``positives`` maps window position -> anomaly type."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}; expected one of {VARIANTS}")
    sid = str(index)
    p = make_rng(seed, "telemetry_anomaly", split, "params", sid)
    schedule = p.choice(SCHEDULES)
    saturday_morning = p.random() < 0.5
    day_offset = p.randrange(7)
    names = list(BASE_SIGNALS) + [n for n in OPTIONAL_SIGNALS if p.random() < 0.5]
    i_rated = p.uniform(20.0, 40.0)
    i_idle = p.uniform(0.05, 0.10) * i_rated
    dt_rated = p.uniform(20.0, 32.0)
    tau = p.uniform(20.0, 35.0)
    amb_base = p.uniform(20.0, 28.0)
    amb_amp = p.uniform(2.0, 5.0)
    v0, v1, v_idle = p.uniform(1.0, 1.8), p.uniform(0.4, 1.2), p.uniform(0.10, 0.25)
    p_set = p.uniform(5.5, 6.5)
    ranges = {
        "spindle_load_pct": (0.0, 100.0),
        "motor_current_a": (0.0, 1.15 * i_rated),
        "power_kw": (0.0, 1.15 * KW_PER_AMP * i_rated),
        "bearing_temp_c": (5.0, 80.0),
        "vibration_mm_s": (0.0, 4.5),
        "coolant_pressure_bar": (0.0, 7.5),
    }
    ranges = {n: ranges[n] for n in names}

    between_stops, mean_stop, job_lo, job_hi = _REGIMES[variant]
    proc = make_rng(seed, "telemetry_anomaly", split, variant, "process", sid)
    noise = _noise_source(make_rng(seed, "telemetry_anomaly", split, variant, "noise", sid), variant)
    sensor = make_rng(seed, "telemetry_anomaly", split, variant, "sensor", sid)
    spike_p = (1.0 - (1.0 - _SPIKE_BUCKET_SHARE) ** (1.0 / _READINGS_PER_BUCKET)) * (3.0 if variant == "heavy_tails" else 1.0)

    clean = {n: [] for n in names}
    load_fraction = []
    running = []
    weather = [0.0]
    state = {"stop_until": 0, "job_until": 0, "level": 0.6, "ar": 0.0, "temp": None}

    def simulate_day(day):
        while len(weather) <= day + 1:
            weather.append(max(-3.0, min(3.0, weather[-1] + proc.gauss(0.0, 0.7))))
        for mod in range(MINUTES_PER_DAY):
            m = day * MINUTES_PER_DAY + mod
            if _scheduled(schedule, saturday_morning, day_offset, m):
                if m >= state["stop_until"] and proc.random() < 1.0 / between_stops:
                    state["stop_until"] = m + max(2, round(proc.expovariate(1.0 / mean_stop)))
                on = m >= state["stop_until"]
            else:
                on = False
            if m >= state["job_until"]:
                state["level"] = proc.uniform(0.45, 0.90)
                state["job_until"] = m + proc.randint(job_lo, job_hi)
            state["ar"] = 0.9 * state["ar"] + proc.gauss(0.0, 0.012)
            load = min(1.0, max(0.05, state["level"] + state["ar"])) if on else 0.0
            frac = mod / MINUTES_PER_DAY
            ambient = (amb_base + amb_amp * math.sin(2.0 * math.pi * (mod / 60.0 - 9.0) / 24.0)
                       + weather[day] + (weather[day + 1] - weather[day]) * frac)
            if state["temp"] is None:
                state["temp"] = ambient
            target = ambient + dt_rated * load if on else ambient
            state["temp"] += (target - state["temp"]) / tau
            current_true = i_idle + (i_rated - i_idle) * load if on else i_idle
            running.append(on)
            load_fraction.append(load)
            clean["spindle_load_pct"].append(100.0 * load + 1.2 * noise() if on else 2.0 + 0.6 * noise())
            clean["motor_current_a"].append(current_true + 0.012 * i_rated * noise())
            clean["bearing_temp_c"].append(state["temp"] + 0.3 * noise())
            clean["vibration_mm_s"].append((v0 + v1 * load if on else v_idle) * math.exp(0.08 * noise()))
            if "power_kw" in clean:
                clean["power_kw"].append(KW_PER_AMP * current_true + 0.008 * KW_PER_AMP * i_rated * noise())
            if "coolant_pressure_bar" in clean:
                clean["coolant_pressure_bar"].append(p_set + 0.06 * noise() if on else 0.2 + 0.03 * noise())

    for day in range(history_days):
        simulate_day(day)

    # evaluation period: add whole days until the windows fit
    wrng = make_rng(seed, "telemetry_anomaly", split, variant, "windows", sid)
    chosen = []
    day = history_days
    while True:
        if day >= history_days + MAX_EXTRA_DAYS:
            raise RuntimeError(f"machine {sid}: no room for {windows_per_series} windows in {MAX_EXTRA_DAYS} days")
        simulate_day(day)
        day += 1
        first = history_days * MINUTES_PER_DAY
        candidates = [s for s in range(first, len(running) - WINDOW_MINUTES + 1, 5) if running[s]]
        wrng.shuffle(candidates)
        chosen = []
        for s in candidates:
            if all(abs(s - c) >= WINDOW_MINUTES for c in chosen):
                chosen.append(s)
                if len(chosen) == windows_per_series:
                    break
        if len(chosen) == windows_per_series:
            break
    minutes = len(running)

    # sensor layer: dropouts, outages, benign spikes (history and windows share it)
    dropped = {n: set() for n in names}
    spikes = {n: {} for n in names}
    outage_minutes = set()
    m = 0
    while True:
        m += max(1, round(sensor.expovariate(1.0 / _OUTAGE_MEAN_GAP_MINUTES)))
        if m >= minutes:
            break
        outage_minutes.update(range(m, min(minutes, m + sensor.randint(10, 180))))
    for n in names:
        lo, hi = ranges[n]
        width = hi - lo
        for minute in range(minutes):
            if minute in outage_minutes or sensor.random() < _DROPOUT_PROBABILITY:
                dropped[n].add(minute)
            if sensor.random() < spike_p:
                spikes[n][minute] = (1.0 if sensor.random() < 0.5 else -1.0) * sensor.uniform(0.10, 0.30) * width

    def observe(name, minute, value, *, sensor_faults=True):
        if sensor_faults:
            if minute in dropped[name]:
                return None
            value += spikes[name].get(minute, 0.0)
        value = min(100.0, max(0.0, value)) if name == "spindle_load_pct" else max(0.0, value)
        if name in INTEGER_SIGNALS:
            return float(round(value))
        return round(value, _DECIMALS[name])

    signals = {n: [observe(n, minute, clean[n][minute]) for minute in range(minutes)] for n in names}

    windows = []
    for position, start in enumerate(sorted(chosen)):
        end = start + WINDOW_MINUTES
        kind = positives.get(position)
        readings = {n: signals[n][start:end] for n in names}
        if kind is None:
            windows.append(EvalWindow(end_minute=end, label=0, anomaly_type=None, readings=readings))
            continue
        arng = make_rng(seed, "telemetry_anomaly", split, variant, "anomaly", sid, str(position))
        latest_onset = 54 if kind == "spike" else 45
        onset = arng.choice([w for w in range(latest_onset + 1) if running[start + w]])
        readings = {n: list(v) for n, v in readings.items()}
        detail = {"type": kind, "onset_minute": onset}
        if kind == "spike":
            duration = arng.randint(3, 6)
            factor = arng.uniform(1.35, 1.70)
            affected = [n for n in ("spindle_load_pct", "motor_current_a", "power_kw") if n in names]
            for w in range(onset, min(onset + duration, WINDOW_MINUTES)):
                for n in affected:
                    readings[n][w] = observe(n, start + w, clean[n][start + w] * factor)
            detail.update(signals=affected, duration_minutes=duration, factor=factor)
        elif kind == "level_shift":
            delta = arng.uniform(6.0, 12.0)
            n = "bearing_temp_c"
            for w in range(onset, WINDOW_MINUTES):
                readings[n][w] = observe(n, start + w, clean[n][start + w] + delta)
            detail.update(signals=[n], offset=delta)
        elif kind == "slow_drift":
            gain = arng.uniform(0.8, 1.5)
            n = "vibration_mm_s"
            span = WINDOW_MINUTES - onset
            for w in range(onset, WINDOW_MINUTES):
                readings[n][w] = observe(n, start + w, clean[n][start + w] * (1.0 + gain * (w - onset + 1) / span))
            detail.update(signals=[n], final_gain=gain)
        elif kind == "stuck_at":
            n = "spindle_load_pct"
            frozen = observe(n, start + onset, clean[n][start + onset], sensor_faults=False)
            for w in range(onset, WINDOW_MINUTES):
                readings[n][w] = frozen
            detail.update(signals=[n], frozen_value=frozen)
        elif kind == "correlation_break":
            other = arng.uniform(0.45, 0.90)
            n = "motor_current_a"
            for w in range(onset, WINDOW_MINUTES):
                m = start + w
                value = clean[n][m] - (i_rated - i_idle) * (load_fraction[m] - other) if running[m] else clean[n][m]
                readings[n][w] = observe(n, m, value)
            detail.update(signals=[n], other_load=other)
        elif kind == "noise_burst":
            sigma = arng.uniform(0.08, 0.15) * i_rated
            n = "motor_current_a"
            for w in range(onset, WINDOW_MINUTES):
                readings[n][w] = observe(n, start + w, clean[n][start + w] + arng.gauss(0.0, sigma))
            detail.update(signals=[n], sigma=sigma)
        else:  # pragma: no cover - ANOMALY_TYPES is closed
            raise ValueError(f"unknown anomaly type {kind!r}")
        windows.append(EvalWindow(end_minute=end, label=1, anomaly_type=kind, readings=readings, detail=detail))

    return MachineSeries(
        series_id=f"{split}-{variant}-{sid}", split=split, variant=variant, index=index,
        history_days=history_days, minutes=minutes, start=EPOCH + timedelta(days=day_offset),
        schedule=schedule, signals=signals, running=running, declared_ranges=ranges,
        integer_signals=frozenset(n for n in names if n in INTEGER_SIGNALS),
        benign_spikes={n: dict(v) for n, v in spikes.items() if v}, windows=windows,
    )
