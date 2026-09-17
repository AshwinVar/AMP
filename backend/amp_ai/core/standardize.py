"""Feature standardisation whose statistics come from training data and nothing else.

``fit`` learns, per feature: the median of the OBSERVED training values (used to
fill a missing value), then the mean and standard deviation of the filled
column. ``transform`` only ever reads those stored numbers. That separation is
the point: at serving time a tenant's rows are transformed, never learned from -
no median of the tenant's data, no running mean, nothing that would make the
model's behaviour depend on one factory's history or leak it into another's.

Missing values are ``None``. A NaN is refused rather than treated as missing: a
NaN is almost always an upstream bug (0/0), and silently imputing it would hide
that bug behind a plausible number.
"""
import math
import statistics
from collections.abc import Mapping

__all__ = ["Standardizer", "STD_FLOOR"]

# A column whose training standard deviation is below this is constant for all
# practical purposes; dividing by it would blow tiny noise up into huge z-scores.
STD_FLOOR = 1e-12


def _value(v, name):
    if v is None:
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if not isinstance(v, (int, float)):
        raise TypeError(f"feature {name!r}: expected a number or None, got {type(v).__name__}")
    f = float(v)
    if not math.isfinite(f):
        raise ValueError(f"feature {name!r}: non-finite value {v!r} (use None for missing)")
    return f


def _row_values(row, names):
    if isinstance(row, Mapping):
        # A missing KEY is a bug (a renamed feature), not a missing value.
        return [_value(row[name], name) for name in names]
    if not isinstance(row, (list, tuple)):
        raise TypeError(f"row must be a mapping or a sequence, got {type(row).__name__}")
    if len(row) != len(names):
        raise ValueError(f"row has {len(row)} values for {len(names)} features")
    return [_value(v, name) for v, name in zip(row, names)]


class Standardizer:
    def __init__(self, names, mean, std, impute):
        self.names = list(names)
        self.mean = [float(x) for x in mean]
        self.std = [float(x) for x in std]
        self.impute = [float(x) for x in impute]
        k = len(self.names)
        if not k or len(set(self.names)) != k or not all(isinstance(n, str) and n for n in self.names):
            raise ValueError("names must be unique non-empty strings")
        if not (len(self.mean) == len(self.std) == len(self.impute) == k):
            raise ValueError("mean/std/impute must have one entry per feature")
        for label, values in (("mean", self.mean), ("std", self.std), ("impute", self.impute)):
            if not all(math.isfinite(x) for x in values):
                raise ValueError(f"{label} contains a non-finite value")
        if not all(s > 0.0 for s in self.std):
            raise ValueError("std must be positive")

    @classmethod
    def fit(cls, rows, names) -> "Standardizer":
        names = list(names)
        matrix = [_row_values(r, names) for r in rows]
        if not matrix:
            raise ValueError("cannot fit a Standardizer on zero rows")
        n = len(matrix)
        mean, std, impute = [], [], []
        for j, name in enumerate(names):
            observed = [row[j] for row in matrix if row[j] is not None]
            if not observed:
                raise ValueError(f"feature {name!r} has no observed training value to impute from")
            median = float(statistics.median(observed))
            column = [median if row[j] is None else row[j] for row in matrix]
            mu = math.fsum(column) / n
            sd = math.sqrt(math.fsum((x - mu) ** 2 for x in column) / n)
            impute.append(median)
            mean.append(mu)
            std.append(sd if sd >= STD_FLOOR else 1.0)
        return cls(names, mean, std, impute)

    def transform(self, rows) -> list[list[float]]:
        """Standardise rows with the STORED statistics only. Never updates them."""
        out = []
        for row in rows:
            values = _row_values(row, self.names)
            out.append([((self.impute[j] if v is None else v) - self.mean[j]) / self.std[j]
                        for j, v in enumerate(values)])
        return out

    def to_dict(self) -> dict:
        return {"names": list(self.names), "mean": list(self.mean),
                "std": list(self.std), "impute": list(self.impute)}

    @classmethod
    def from_dict(cls, d) -> "Standardizer":
        return cls(d["names"], d["mean"], d["std"], d["impute"])
