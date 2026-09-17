"""Per-request robust baseline and calibrated window score for one machine's telemetry.

Standard library plus ``amp_ai.core``. Pure: buckets in, a result dict out. No
state survives a call, so nothing learned from one tenant can reach another,
and revoking consent needs nothing deleted.

THE WINDOWS (see ``series``)
----------------------------
Score window buckets k = 0..11 (the last hour); baseline buckets k = 12..4031
(the 14 days before it). ``assess`` refuses a bucket on the wrong side, so the
score window can never be part of its own baseline.

Inside the baseline, buckets that carry any reading are ordered by time and
split: the OLDEST 70% FIT the baseline, the NEWEST 30% CALIBRATE the score. A
bucket is in exactly one of fit, calibration or score.

FIT (per machine state, per signal)
-----------------------------------
For each state (Running / NotRunning / Unknown) and signal with at least 288 fit
buckets (a full day). ``Transition`` buckets (the machine started or stopped
inside the five minutes; see ``series``) mix two regimes, so no baseline is ever
fitted for them and they are never scored, in calibration or in the score
window. Location = median, scale = 1.4826 * max(MAD, floor), with
floor = max(1% of |median|, 1.0 if every raw reading of that signal is a whole
number, 1e-6). The floor stops an integer sensor or an idle machine with MAD 0
from turning a one-count change into z = 10^9.

SCORE
-----
Per bucket b, with z clipped to [-50, 50] and averaged over the signals present:
    e_b = mean_s z_bs^2                                 (the diagonal term)
and, when a state has >= 2 scorable signals and >= 400 COMPLETE fit buckets, a
Ledoit-Wolf shrunk covariance of those z-vectors gives, for complete buckets,
    e_b = max(e_b, d_b^2 / p)                            (catches correlation breaks)
The max is taken BEFORE calibration, so the calibration covers the combination.

The window statistic over the twelve bucket positions of a window is either
    scan  max over contiguous runs I of (sum_I e_b - n_I) / sqrt(n_I)
          (n_I = buckets with data in I): a short spike and a sustained shift
          that started part-way through the hour are both a high-scoring run;
    mean  sqrt(sum e_b / n)   (reported as an ablation by the evaluation).

The score is the mid-rank of the score window's statistic among the same
statistic over every rolling one-hour window of the calibration period
(``EmpiricalCalibrator``): "rarer than this share of this machine's recent
normal hours". It moves in steps of 1 / (n + 1), reported as score_resolution;
it is NOT a probability that the machine is faulty.

Signals used are those present in the score window with a baseline for their
bucket's state; the calibration windows use exactly the same signals, so a
dropped-out sensor changes both sides alike.

MINIMUM DATA
------------
fit buckets per signal >= 288, distinct days >= 3 (days counted back from the
anchor), calibration buckets with a scorable reading >= 100, scorable signals
>= 1. Below any: status ``insufficient_history``, score None, with ``needed``
and ``have``.

A score is at most n / (n + 1), so it can only reach an alarm level of 0.99 with
n >= 99 calibration windows. The thresholds guarantee that: 288 fit buckets at a
70/30 split leave at least 124 calibration buckets, at least 100 of them
scorable, which always yields at least 100 rolling windows
(test_amp_ai_anomaly_min_data.py checks it at every threshold).
"""
import math

from ..core.robust import MAD_SCALE, EmpiricalCalibrator, RobustBaseline, ShrunkCovariance
from . import series as SR

__all__ = [
    "FIT_NUMERATOR", "FIT_DENOMINATOR", "Z_CLIP", "DEVIATION_Z", "MIN_FIT_BUCKETS_PER_SIGNAL", "MIN_DISTINCT_DAYS",
    "MIN_CALIBRATION_BUCKETS", "MIN_SCORABLE_SIGNALS", "MAHALANOBIS_MIN_FIT_BUCKETS",
    "MAHALANOBIS_MIN_SIGNALS", "AGGREGATES", "WINDOW_AGGREGATE", "LEARNED_STATES", "STATUS_OK", "STATUS_INSUFFICIENT",
    "mad_floor", "robust_fitter", "split_fit_calibration", "assess", "method_parameters",
]

FIT_NUMERATOR, FIT_DENOMINATOR = 7, 10
Z_CLIP = 50.0
DEVIATION_Z = 3.5
MIN_FIT_BUCKETS_PER_SIGNAL = SR.BUCKETS_PER_DAY
MIN_DISTINCT_DAYS = 3
MIN_CALIBRATION_BUCKETS = 100
MIN_SCORABLE_SIGNALS = 1
MAHALANOBIS_MIN_FIT_BUCKETS = 400
MAHALANOBIS_MIN_SIGNALS = 2
MAD_FLOOR_RELATIVE = 0.01
MAD_FLOOR_QUANTIZATION_STEP = 1.0
MAD_FLOOR_ABSOLUTE = 1e-6
AGGREGATES = ("scan", "mean")
WINDOW_AGGREGATE = "scan"

_STATES = SR.STATES
LEARNED_STATES = (SR.STATE_RUNNING, SR.STATE_NOT_RUNNING, SR.STATE_UNKNOWN)   # never Transition
STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient_history"


def mad_floor(median, integer_valued) -> float:
    return max(MAD_FLOOR_RELATIVE * abs(median),
               MAD_FLOOR_QUANTIZATION_STEP if integer_valued else 0.0,
               MAD_FLOOR_ABSOLUTE)


def robust_fitter(values_by_signal, integer_signals):
    """{signal: (median, 1.4826 * max(MAD, floor))} - the scale ``RobustBaseline.zscores`` divides by."""
    rb = RobustBaseline.fit(values_by_signal,
                            mad_floor=lambda name, med, vals: mad_floor(med, name in integer_signals))
    return {s: (rb.median[s], MAD_SCALE * max(rb.mad[s], rb.floor[s])) for s in rb.median}


robust_fitter.method_name = "robust"


def split_fit_calibration(buckets):
    """(fit, calibration): the oldest 70% and newest 30% of the buckets that carry any reading."""
    bearing = sorted((b for b in buckets if b.values), key=lambda b: -b.k)
    n_fit = len(bearing) * FIT_NUMERATOR // FIT_DENOMINATOR
    return bearing[:n_fit], bearing[n_fit:]


def method_parameters() -> dict:
    return {
        "bucket_seconds": SR.BUCKET_SECONDS,
        "score_buckets": SR.SCORE_BUCKETS,
        "baseline_days": SR.BASELINE_DAYS,
        "fit_fraction": FIT_NUMERATOR / FIT_DENOMINATOR,
        "z_clip": Z_CLIP,
        "deviation_z": DEVIATION_Z,
        "mad_scale": MAD_SCALE,
        "mad_floor": {"relative": MAD_FLOOR_RELATIVE, "quantization_step": MAD_FLOOR_QUANTIZATION_STEP,
                      "absolute": MAD_FLOOR_ABSOLUTE},
        "min_fit_buckets_per_signal": MIN_FIT_BUCKETS_PER_SIGNAL,
        "min_distinct_days": MIN_DISTINCT_DAYS,
        "min_calibration_buckets": MIN_CALIBRATION_BUCKETS,
        "min_scorable_signals": MIN_SCORABLE_SIGNALS,
        "mahalanobis_min_fit_buckets": MAHALANOBIS_MIN_FIT_BUCKETS,
        "mahalanobis_min_signals": MAHALANOBIS_MIN_SIGNALS,
        "window_statistic": WINDOW_AGGREGATE,
        "states": list(_STATES),
        "learned_states": list(LEARNED_STATES),
    }


# --------------------------------------------------------------------------- helpers
def _validate(buckets, lo, hi, label):
    seen = set()
    for b in buckets:
        k = b.k
        if isinstance(k, bool) or not isinstance(k, int) or not lo <= k <= hi:
            raise ValueError(f"{label} bucket k={k!r} is outside [{lo}, {hi}]")
        if k in seen:
            raise ValueError(f"duplicate {label} bucket k={k}")
        seen.add(k)
        if b.state not in _STATES:
            raise ValueError(f"{label} bucket k={k} has unknown state {b.state!r}")
        for name, v in b.values.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError(f"{label} bucket k={k}: {name!r} is not a finite number")


def _clip(z):
    return Z_CLIP if z > Z_CLIP else (-Z_CLIP if z < -Z_CLIP else z)


def _window_statistics(energy_by_k, k_lo, k_hi, aggregate):
    """The window statistic for every run of SCORE_BUCKETS consecutive positions in [k_lo, k_hi] holding data."""
    width = SR.SCORE_BUCKETS
    span = k_hi - k_lo + 1
    if span < width:
        return []
    pe = [0.0] * (span + 1)
    pc = [0] * (span + 1)
    for i in range(span):
        e = energy_by_k.get(k_lo + i)
        pe[i + 1] = pe[i] + (e if e is not None else 0.0)
        pc[i + 1] = pc[i] + (e is not None)
    out = []
    if aggregate == "mean":
        for j in range(span - width + 1):
            n = pc[j + width] - pc[j]
            if n:
                out.append(math.sqrt((pe[j + width] - pe[j]) / n))
        return out
    best = []
    for i in range(span):
        row = []
        running_max = -math.inf
        for length in range(1, min(width, span - i) + 1):
            n = pc[i + length] - pc[i]
            if n:
                v = (pe[i + length] - pe[i] - n) / math.sqrt(n)
                if v > running_max:
                    running_max = v
            row.append(running_max)
        best.append(row)
    for j in range(span - width + 1):
        if pc[j + width] == pc[j]:
            continue
        out.append(max(best[i][j + width - i - 1] for i in range(j, j + width)))
    return out


def _window_state(buckets):
    states = {b.state for b in buckets}
    if not states:
        return None
    return states.pop() if len(states) == 1 else "mixed"


# --------------------------------------------------------------------------- assess
def assess(baseline_buckets, score_buckets, *, integer_signals=frozenset(), fitter=robust_fitter,
           mahalanobis=True, aggregate=WINDOW_AGGREGATE, diagnostics=False) -> dict:
    """Score the last hour of one machine against a baseline fitted from its previous 14 days."""
    if aggregate not in AGGREGATES:
        raise ValueError(f"unknown window statistic {aggregate!r}; expected one of {AGGREGATES}")
    baseline_buckets = list(baseline_buckets)
    score_buckets = list(score_buckets)
    _validate(baseline_buckets, SR.SCORE_BUCKETS, SR.TOTAL_BUCKETS - 1, "baseline")
    _validate(score_buckets, 0, SR.SCORE_BUCKETS - 1, "score")
    score_buckets = [b for b in score_buckets if b.values]
    integer_signals = frozenset(integer_signals)

    fit, cal = split_fit_calibration(baseline_buckets)
    fit_values = {}
    for b in fit:
        per_state = fit_values.setdefault(b.state, {})
        for name, v in b.values.items():
            per_state.setdefault(name, []).append(v)
    counts = {(g, s): len(v) for g, per_state in fit_values.items() for s, v in per_state.items()}

    def scorable(state, signal):
        return state in LEARNED_STATES and counts.get((state, signal), 0) >= MIN_FIT_BUCKETS_PER_SIGNAL

    signals = sorted({s for b in score_buckets for s in b.values if scorable(b.state, s)})
    signal_set = set(signals)
    needed_pairs = ({(b.state, s) for b in score_buckets for s in b.values if b.state in LEARNED_STATES}
                    or {p for p in counts if p[0] in LEARNED_STATES})
    cal_bearing = [b for b in cal if any(s in signal_set and scorable(b.state, s) for s in b.values)]
    needed = {
        "fit_buckets_per_signal": MIN_FIT_BUCKETS_PER_SIGNAL,
        "distinct_days": MIN_DISTINCT_DAYS,
        "calibration_buckets": MIN_CALIBRATION_BUCKETS,
        "scorable_signals": MIN_SCORABLE_SIGNALS,
    }
    have = {
        "fit_buckets_per_signal": max((counts.get(p, 0) for p in needed_pairs), default=0),
        "distinct_days": len({b.k // SR.BUCKETS_PER_DAY for b in fit + cal}),
        "calibration_buckets": len(cal_bearing),
        "scorable_signals": len(signals),
    }
    state = _window_state(score_buckets)
    method_name = getattr(fitter, "method_name", getattr(fitter, "__name__", "custom"))
    refused = {"status": STATUS_INSUFFICIENT, "score": None, "score_resolution": None, "calibration_windows": 0,
               "state": state, "method": None, "statistic": aggregate, "signals_used": signals, "deviating": [],
               "needed": needed, "have": have}
    if any(have[key] < needed[key] for key in needed):
        return refused

    # fit: location and scale per state, for the signals the score window needs
    fitted = {}
    for g in sorted({b.state for b in cal_bearing} | {b.state for b in score_buckets}):
        values = {s: fit_values[g][s] for s in signals if scorable(g, s)}
        if values:
            fitted[g] = fitter(values, integer_signals)

    covariances = {}
    if mahalanobis:
        for g, params in fitted.items():
            names = sorted(params)
            if len(names) < MAHALANOBIS_MIN_SIGNALS:
                continue
            rows = [[_clip((b.values[s] - params[s][0]) / params[s][1]) for s in names]
                    for b in fit if b.state == g and all(s in b.values for s in names)]
            if len(rows) < MAHALANOBIS_MIN_FIT_BUCKETS:
                continue
            try:
                covariances[g] = (names, ShrunkCovariance.fit(rows))
            except ValueError:   # no variance at all, or a singular matrix: the diagonal term stands alone
                continue

    def energy(b):
        params = fitted.get(b.state)
        if not params:
            return None
        z = {s: (v - params[s][0]) / params[s][1] for s, v in b.values.items() if s in params}
        if not z:
            return None
        e = sum(_clip(v) ** 2 for v in z.values()) / len(z)
        cov = covariances.get(b.state)
        if cov is not None:
            names, model = cov
            if all(s in z for s in names):
                e = max(e, model.mahalanobis2([_clip(z[s]) for s in names]) / len(names))
        return e

    cal_energy = {}
    for b in cal:
        e = energy(b)
        if e is not None:
            cal_energy[b.k] = e
    k_new, k_old = min(b.k for b in cal), max(b.k for b in cal)
    statistics_ = _window_statistics(cal_energy, k_new, k_old, aggregate)
    score_energy = {}
    for b in score_buckets:
        e = energy(b)
        if e is not None:
            score_energy[b.k] = e
    window = _window_statistics(score_energy, 0, SR.SCORE_BUCKETS - 1, aggregate)
    if not statistics_ or not window:
        have = dict(have, calibration_buckets=len(cal_energy) if not statistics_ else have["calibration_buckets"])
        return dict(refused, have=have)
    calibrator = EmpiricalCalibrator.fit(statistics_)
    value = window[0]

    strongest = {}
    for b in score_buckets:
        params = fitted.get(b.state, {})
        for s, v in b.values.items():
            if s not in params:
                continue
            loc, scale = params[s]
            z = (v - loc) / scale
            if s not in strongest or abs(z) > abs(strongest[s][0]):
                strongest[s] = (z, v, loc, scale, b)
    deviating = []
    for s, (z, v, loc, scale, b) in strongest.items():
        if abs(z) >= DEVIATION_Z:
            deviating.append({
                "signal": s, "value": v, "median": loc,
                "typical_range": [loc - DEVIATION_Z * scale, loc + DEVIATION_Z * scale],
                "direction": "above" if z > 0 else "below", "z": z, "state": b.state, "bucket": b.k,
            })
    deviating.sort(key=lambda d: (-abs(d["z"]), d["signal"]))

    used_covariance = any(b.state in covariances for b in score_buckets)
    result = {
        "status": STATUS_OK,
        "score": calibrator.score(value),
        "score_resolution": calibrator.resolution,
        "calibration_windows": calibrator.n,
        "state": state,
        "method": f"{method_name}_diagonal" + ("+mahalanobis" if used_covariance else ""),
        "statistic": aggregate,
        "signals_used": signals,
        "deviating": deviating,
        "needed": needed,
        "have": have,
    }
    if diagnostics:
        result["diagnostics"] = {
            "fitted": {g: {s: {"location": loc, "scale": scale, "n": counts[(g, s)]}
                           for s, (loc, scale) in params.items()} for g, params in fitted.items()},
            "calibration_statistics": sorted(statistics_),
            "calibration_k_range": [k_new, k_old],
            "statistic_value": value,
            "score_z": {s: t[0] for s, t in strongest.items()},
            "covariance_states": sorted(covariances),
        }
    return result
