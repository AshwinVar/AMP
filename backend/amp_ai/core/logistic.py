"""Binary logistic regression (Newton / IRLS) and Platt calibration, standard library only.

OBJECTIVE
---------
``BinaryLogisticRegression`` minimises

    sum_i  w_i * [ log(1 + exp(z_i)) - y_i * z_i ]   +   (l2 / 2) * ||coef||^2
    z_i = coef . x_i + intercept

i.e. the SUM (not the mean) of weighted log-losses plus a ridge penalty on the
coefficients only. The intercept is never penalised: penalising it would pull
every prediction towards 50% regardless of the base rate. With the sum
convention, ``l2`` is the prior precision on each coefficient (``C = 1/l2`` in
scikit-learn terms), so the same ``l2`` means less as the data grows.

``class_weight``: None (all 1), ``"balanced"`` (``n / (2 * n_class)``, as in
scikit-learn), or ``{0: w0, 1: w1}``. Weighting changes the base rate the model
believes in, so a class-weighted model's probabilities are NOT calibrated;
recalibrate with ``PlattCalibrator`` on held-out data before showing them.

SOLVER
------
Newton's method with a backtracking (Armijo) line search, starting from zero.
Each step solves the (k+1)x(k+1) Hessian system with ``linalg.solve``. It is
deterministic: same data, same order, same result. Convergence: the largest
parameter step is at most ``tol * (1 + largest |parameter|)``.

``PlattCalibrator`` fits ``p = sigmoid(a * score + b)`` with the same Newton
routine, using Platt's smoothed targets ``(n+ + 1)/(n+ + 2)`` and
``1/(n- + 2)`` so a separable calibration set cannot drive ``a`` to infinity.
Feed it the model's ``decision_function`` (logits), not probabilities.
"""
import math
from collections.abc import Mapping

from . import linalg

__all__ = ["BinaryLogisticRegression", "PlattCalibrator", "sigmoid"]


def sigmoid(z: float) -> float:
    if z >= 0.0:
        e = math.exp(-z)
        return 1.0 / (1.0 + e)
    e = math.exp(z)
    return e / (1.0 + e)


def _log1pexp(z: float) -> float:
    """log(1 + exp(z)) without overflow."""
    if z > 0.0:
        return z + math.log1p(math.exp(-z))
    return math.log1p(math.exp(z))


def _matrix(X, name="X"):
    if not isinstance(X, (list, tuple)) or not X:
        raise ValueError(f"{name} must be a non-empty list of rows")
    k = None
    rows = []
    for row in X:
        if not isinstance(row, (list, tuple)):
            raise TypeError(f"{name} rows must be lists of numbers")
        values = [float(v) for v in row]
        if k is None:
            k = len(values)
        elif len(values) != k:
            raise ValueError(f"{name} rows have different lengths")
        if not all(math.isfinite(v) for v in values):
            raise ValueError(f"{name} contains a non-finite value")
        rows.append(values)
    return rows, k


def _binary(y, n):
    if len(y) != n:
        raise ValueError(f"y has {len(y)} labels for {n} rows")
    out = []
    for v in y:
        if isinstance(v, bool):
            out.append(1 if v else 0)
        elif isinstance(v, (int, float)) and v in (0, 1):
            out.append(int(v))
        else:
            raise ValueError(f"labels must be 0 or 1, got {v!r}")
    return out


def _objective(rows, targets, weights, beta, l2, k):
    total = 0.0
    b = beta[k]
    for x, t, w in zip(rows, targets, weights):
        z = b
        for j in range(k):
            z += beta[j] * x[j]
        total += w * (_log1pexp(z) - t * z)
    if l2:
        total += 0.5 * l2 * sum(c * c for c in beta[:k])
    return total


def _newton_fit(rows, targets, weights, l2, max_iter, tol):
    """Minimise the weighted (soft-target) log-loss + ridge. Returns (beta, n_iter, converged).

    ``beta`` has k coefficients followed by the intercept.
    """
    k = len(rows[0])
    size = k + 1
    beta = [0.0] * size
    loss = _objective(rows, targets, weights, beta, l2, k)
    converged = False
    iterations = 0
    for iterations in range(1, max_iter + 1):
        grad = [0.0] * size
        hess = [[0.0] * size for _ in range(size)]
        b = beta[k]
        for x, t, w in zip(rows, targets, weights):
            z = b
            for j in range(k):
                z += beta[j] * x[j]
            p = sigmoid(z)
            g = w * (p - t)
            h = w * p * (1.0 - p)
            xs = x + [1.0]
            for j in range(size):
                xj = xs[j]
                if xj == 0.0:
                    continue
                grad[j] += g * xj
                hj = h * xj
                row = hess[j]
                for m in range(j, size):
                    row[m] += hj * xs[m]
        for j in range(size):
            for m in range(j):
                hess[j][m] = hess[m][j]
        for j in range(k):
            grad[j] += l2 * beta[j]
            hess[j][j] += l2
        step = linalg.solve(hess, grad)
        decrease = linalg.dot(grad, step)
        scale = 1.0
        while True:
            candidate = [bj - scale * sj for bj, sj in zip(beta, step)]
            new_loss = _objective(rows, targets, weights, candidate, l2, k)
            if new_loss <= loss - 1e-4 * scale * decrease + 1e-12 * max(1.0, abs(loss)):
                break
            scale *= 0.5
            if scale < 1e-10:
                return beta, iterations, False
        largest_step = max(abs(scale * s) for s in step)
        beta, loss = candidate, new_loss
        if largest_step <= tol * (1.0 + max(abs(v) for v in beta)):
            converged = True
            break
    return beta, iterations, converged


def _class_weights(class_weight, labels):
    n = len(labels)
    n1 = sum(labels)
    n0 = n - n1
    if class_weight is None:
        return 1.0, 1.0
    if class_weight == "balanced":
        return n / (2.0 * n0), n / (2.0 * n1)
    if isinstance(class_weight, Mapping):
        w = {}
        for key, value in class_weight.items():
            label = int(key) if str(key) in ("0", "1") else None
            if label is None:
                raise ValueError(f"class_weight keys must be 0 and 1, got {key!r}")
            w[label] = float(value)
        if set(w) != {0, 1} or not all(math.isfinite(v) and v > 0 for v in w.values()):
            raise ValueError("class_weight must give a positive finite weight for both 0 and 1")
        return w[0], w[1]
    raise ValueError(f"class_weight must be None, 'balanced' or a mapping, got {class_weight!r}")


class BinaryLogisticRegression:
    MODEL = "binary_logistic_regression"

    def __init__(self, l2=1.0, class_weight=None, max_iter=50, tol=1e-8):
        l2 = float(l2)
        if not math.isfinite(l2) or l2 < 0.0:
            raise ValueError("l2 must be a finite number >= 0")
        if isinstance(max_iter, bool) or not isinstance(max_iter, int) or max_iter < 1:
            raise ValueError("max_iter must be a positive int")
        tol = float(tol)
        if not math.isfinite(tol) or tol <= 0.0:
            raise ValueError("tol must be positive")
        if class_weight is not None and class_weight != "balanced" and not isinstance(class_weight, Mapping):
            raise ValueError(f"class_weight must be None, 'balanced' or a mapping, got {class_weight!r}")
        self.l2 = l2
        self.class_weight = class_weight
        self.max_iter = max_iter
        self.tol = tol
        self.coef = None
        self.intercept = None
        self.class_weights_used = None
        self.n_iter = 0
        self.converged = False

    def fit(self, X, y):
        rows, k = _matrix(X)
        labels = _binary(y, len(rows))
        if not 0 < sum(labels) < len(labels):
            raise ValueError("training data must contain both classes")
        w0, w1 = _class_weights(self.class_weight, labels)
        weights = [w1 if t else w0 for t in labels]
        beta, n_iter, converged = _newton_fit(rows, [float(t) for t in labels], weights,
                                              self.l2, self.max_iter, self.tol)
        self.coef = beta[:k]
        self.intercept = beta[k]
        self.class_weights_used = [w0, w1]
        self.n_iter = n_iter
        self.converged = converged
        return self

    def _check_fitted(self):
        if self.coef is None:
            raise RuntimeError("model is not fitted")

    def decision_function(self, X) -> list[float]:
        self._check_fitted()
        rows, k = _matrix(X)
        if k != len(self.coef):
            raise ValueError(f"rows have {k} features, model has {len(self.coef)}")
        return [linalg.dot(self.coef, x) + self.intercept for x in rows]

    def predict_proba(self, X) -> list[float]:
        return [sigmoid(z) for z in self.decision_function(X)]

    def to_dict(self) -> dict:
        self._check_fitted()
        cw = self.class_weight
        if isinstance(cw, Mapping):
            cw = {str(int(k)): float(v) for k, v in cw.items()}
        return {"model": self.MODEL, "coef": list(self.coef), "intercept": self.intercept,
                "l2": self.l2, "class_weight": cw, "class_weights_used": list(self.class_weights_used),
                "max_iter": self.max_iter, "tol": self.tol, "n_iter": self.n_iter,
                "converged": self.converged, "n_features": len(self.coef)}

    @classmethod
    def from_dict(cls, d) -> "BinaryLogisticRegression":
        if d.get("model") != cls.MODEL:
            raise ValueError(f"not a {cls.MODEL} dict")
        model = cls(l2=d["l2"], class_weight=d["class_weight"], max_iter=d["max_iter"], tol=d["tol"])
        coef = [float(c) for c in d["coef"]]
        intercept = float(d["intercept"])
        if len(coef) != d["n_features"] or not all(math.isfinite(c) for c in coef + [intercept]):
            raise ValueError("coef/intercept are malformed")
        model.coef = coef
        model.intercept = intercept
        model.class_weights_used = [float(v) for v in d["class_weights_used"]]
        model.n_iter = int(d["n_iter"])
        model.converged = bool(d["converged"])
        return model


class PlattCalibrator:
    MODEL = "platt_calibrator"

    def __init__(self):
        self.a = None
        self.b = None

    def fit(self, scores, y):
        values = [float(s) for s in scores]
        if not values or not all(math.isfinite(s) for s in values):
            raise ValueError("scores must be a non-empty list of finite numbers")
        labels = _binary(y, len(values))
        n_pos = sum(labels)
        n_neg = len(labels) - n_pos
        if n_pos == 0 or n_neg == 0:
            raise ValueError("calibration data must contain both classes")
        if max(values) == min(values):
            raise ValueError("scores are constant; there is nothing to calibrate against")
        hi = (n_pos + 1.0) / (n_pos + 2.0)
        lo = 1.0 / (n_neg + 2.0)
        targets = [hi if t else lo for t in labels]
        beta, _, _ = _newton_fit([[s] for s in values], targets, [1.0] * len(values), 0.0, 100, 1e-10)
        self.a, self.b = beta[0], beta[1]
        return self

    def transform(self, scores) -> list[float]:
        if self.a is None:
            raise RuntimeError("calibrator is not fitted")
        out = []
        for s in scores:
            s = float(s)
            if not math.isfinite(s):
                raise ValueError("score is not finite")
            out.append(sigmoid(self.a * s + self.b))
        return out

    def to_dict(self) -> dict:
        if self.a is None:
            raise RuntimeError("calibrator is not fitted")
        return {"model": self.MODEL, "a": self.a, "b": self.b}

    @classmethod
    def from_dict(cls, d) -> "PlattCalibrator":
        if d.get("model") != cls.MODEL:
            raise ValueError(f"not a {cls.MODEL} dict")
        cal = cls()
        cal.a, cal.b = float(d["a"]), float(d["b"])
        if not (math.isfinite(cal.a) and math.isfinite(cal.b)):
            raise ValueError("a/b must be finite")
        return cal
