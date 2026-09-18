"""Robust baselines for telemetry: median/MAD z-scores, shrunk covariance, empirical calibration.

RobustBaseline
    Per signal: median and MAD (median absolute deviation) of the baseline
    values. ``zscores(x)[s] = (x[s] - median) / (1.4826 * max(MAD, floor))``.
    1.4826 makes MAD a consistent estimate of the standard deviation for normal
    data. A single stuck sensor or a burst of outliers moves a median/MAD far
    less than a mean/std, which is why the anomaly score uses them.

    The FLOOR matters for real telemetry: integer-valued signals and idle
    machines often have MAD = 0, and dividing by zero (or by 1e-9) turns a
    one-count change into an "anomaly" of z = 10^9. ``mad_floor`` may be one
    number for every signal, a ``{signal: floor}`` mapping, or a callable
    ``floor(name, median, values) -> float``; every resolved floor must be > 0.

ShrunkCovariance
    Ledoit-Wolf (2004) shrinkage of the sample covariance towards a scaled
    identity ``m * I`` (m = mean variance), with the normalised Frobenius norm
    ``||A||^2 = tr(A A^T) / p``:
        d^2   = ||S - m I||^2
        bbar^2 = (1 / n^2) * sum_k ||x_k x_k^T - S||^2      (x_k centred)
        b^2   = min(bbar^2, d^2)
        Sigma = (b^2 / d^2) * m I + (1 - b^2 / d^2) * S
    The shrunk matrix stays invertible when there are more signals than rows,
    where the plain sample covariance is singular.

EmpiricalCalibrator
    Turns a statistic into "how unusual is this, compared with the calibration
    sample" by mid-rank: ``(#below + 0.5 * #equal) / (n + 1)``. The result is in
    ``[0, n / (n + 1)]`` and moves in steps of ``1 / (n + 1)`` - the score cannot
    claim more resolution than the calibration sample has.
"""
import bisect
import math
import statistics
from collections.abc import Mapping

from . import linalg

__all__ = ["MAD_SCALE", "median", "mad", "RobustBaseline", "ShrunkCovariance", "EmpiricalCalibrator"]

MAD_SCALE = 1.4826


def _finite_list(values, label):
    out = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise TypeError(f"{label}: expected numbers, got {type(v).__name__}")
        f = float(v)
        if not math.isfinite(f):
            raise ValueError(f"{label}: non-finite value")
        out.append(f)
    if not out:
        raise ValueError(f"{label}: no values")
    return out


def median(values) -> float:
    return float(statistics.median(_finite_list(values, "median")))


def mad(values, center=None) -> float:
    """Median absolute deviation from ``center`` (default: the median). Unscaled."""
    vals = _finite_list(values, "mad")
    c = float(statistics.median(vals)) if center is None else float(center)
    return float(statistics.median([abs(v - c) for v in vals]))


class RobustBaseline:
    def __init__(self, medians, mads, floors, counts):
        self.median = dict(medians)
        self.mad = dict(mads)
        self.floor = dict(floors)
        self.n = dict(counts)

    @classmethod
    def fit(cls, values: dict[str, list[float]], mad_floor) -> "RobustBaseline":
        if not isinstance(values, Mapping) or not values:
            raise ValueError("values must be a non-empty {signal: [values]} mapping")
        medians, mads, floors, counts = {}, {}, {}, {}
        for name in sorted(values):
            if not isinstance(name, str) or not name:
                raise ValueError("signal names must be non-empty strings")
            vals = _finite_list(values[name], f"signal {name!r}")
            med = float(statistics.median(vals))
            if callable(mad_floor):
                floor = mad_floor(name, med, vals)
            elif isinstance(mad_floor, Mapping):
                if name not in mad_floor:
                    raise KeyError(f"no MAD floor given for signal {name!r}")
                floor = mad_floor[name]
            else:
                floor = mad_floor
            floor = float(floor)
            if not math.isfinite(floor) or floor <= 0.0:
                raise ValueError(f"signal {name!r}: MAD floor must be a finite number > 0, got {floor!r}")
            medians[name] = med
            mads[name] = float(statistics.median([abs(v - med) for v in vals]))
            floors[name] = floor
            counts[name] = len(vals)
        return cls(medians, mads, floors, counts)

    def zscores(self, x) -> dict[str, float]:
        """z per signal present in BOTH ``x`` and the baseline; other signals are not scorable."""
        out = {}
        for name in sorted(x):
            if name not in self.median:
                continue
            v = x[name]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                raise ValueError(f"signal {name!r}: reading must be a finite number, got {v!r}")
            out[name] = (float(v) - self.median[name]) / (MAD_SCALE * max(self.mad[name], self.floor[name]))
        return out

    def to_dict(self) -> dict:
        return {"median": dict(self.median), "mad": dict(self.mad), "floor": dict(self.floor), "n": dict(self.n)}

    @classmethod
    def from_dict(cls, d) -> "RobustBaseline":
        return cls(d["median"], d["mad"], d["floor"], d["n"])


class ShrunkCovariance:
    def __init__(self, mean, covariance, precision, shrinkage, n):
        self.mean = list(mean)
        self.covariance = [list(r) for r in covariance]
        self.precision = [list(r) for r in precision]
        self.shrinkage = shrinkage
        self.n = n
        self.p = len(self.mean)

    @classmethod
    def fit(cls, rows) -> "ShrunkCovariance":
        if not isinstance(rows, (list, tuple)) or len(rows) < 2:
            raise ValueError("need at least two rows")
        data = [_finite_list(r, "row") for r in rows]
        p = len(data[0])
        if any(len(r) != p for r in data):
            raise ValueError("rows have different lengths")
        n = len(data)
        mean = [math.fsum(r[j] for r in data) / n for j in range(p)]
        centred = [[r[j] - mean[j] for j in range(p)] for r in data]
        sample = [[math.fsum(x[i] * x[j] for x in centred) / n for j in range(p)] for i in range(p)]
        m = math.fsum(sample[i][i] for i in range(p)) / p
        if m <= 0.0:
            raise ValueError("zero variance in every signal; there is no covariance to estimate")
        d2 = math.fsum((sample[i][j] - (m if i == j else 0.0)) ** 2 for i in range(p) for j in range(p)) / p
        sum_sq_sample = math.fsum(sample[i][j] ** 2 for i in range(p) for j in range(p))
        per_row = []
        for x in centred:
            norm2 = math.fsum(v * v for v in x)
            quad = math.fsum(x[i] * sample[i][j] * x[j] for i in range(p) for j in range(p))
            per_row.append((norm2 * norm2 - 2.0 * quad + sum_sq_sample) / p)
        bbar2 = math.fsum(per_row) / (n * n)
        b2 = min(bbar2, d2)
        shrinkage = b2 / d2 if d2 > 0.0 else 0.0
        covariance = [[shrinkage * (m if i == j else 0.0) + (1.0 - shrinkage) * sample[i][j]
                       for j in range(p)] for i in range(p)]
        precision = linalg.invert(covariance)
        return cls(mean, covariance, precision, shrinkage, n)

    def mahalanobis2(self, v) -> float:
        x = _finite_list(v, "vector")
        if len(x) != self.p:
            raise ValueError(f"vector has {len(x)} entries, covariance has {self.p}")
        diff = [a - b for a, b in zip(x, self.mean)]
        total = 0.0
        for i in range(self.p):
            total += diff[i] * linalg.dot(self.precision[i], diff)
        return max(0.0, total)


class EmpiricalCalibrator:
    def __init__(self, sorted_stats):
        self._stats = list(sorted_stats)
        self.n = len(self._stats)

    @classmethod
    def fit(cls, stats) -> "EmpiricalCalibrator":
        return cls(sorted(_finite_list(stats, "calibration statistics")))

    @property
    def resolution(self) -> float:
        return 1.0 / (self.n + 1)

    def score(self, s) -> float:
        if isinstance(s, bool) or not isinstance(s, (int, float)):
            raise TypeError("statistic must be a number")
        s = float(s)
        if math.isnan(s):
            raise ValueError("statistic is NaN")
        below = bisect.bisect_left(self._stats, s)
        equal = bisect.bisect_right(self._stats, s) - below
        return (below + 0.5 * equal) / (self.n + 1)
