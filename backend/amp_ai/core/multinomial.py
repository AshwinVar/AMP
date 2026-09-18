"""Sparse multinomial (softmax) logistic regression, standard library only.

OBJECTIVE
---------
    J = (1 / sum_i w_i) * sum_i w_i * CE_i   +   (l2 / 2) * sum_{j,k} W[j][k]^2
    CE_i = logsumexp_k(s_ik) - s_i,y_i ;   s_ik = bias[k] + sum_j W[j][k] * x_ij

The data term is a weighted MEAN (unlike ``logistic.BinaryLogisticRegression``,
which uses a sum): full-batch gradient descent needs a step size ``lr`` that does
not change meaning when the corpus grows. Biases are not penalised.

SOLVER
------
Full-batch gradient descent from zero weights. If a step INCREASES the training
objective, it is undone and ``lr`` is halved (deterministically), so a too-large
``lr`` degrades speed rather than diverging. With ``X_val``/``y_val`` it tracks the
unweighted validation cross-entropy after every epoch, keeps the best parameters,
stops after ``patience`` epochs without improvement, and RESTORES the best.

Nothing here is random: ``seed`` is recorded in the artifact for provenance and
is reserved; with zero initialisation and full batches no draw is needed.

SPARSITY
--------
Inputs are ``{feature_index: value}`` dicts (``text_features.hashed_features``).
Weights exist only for indices that occur in the TRAINING rows; an index never
seen in training has weight exactly zero, and ``to_dict`` stores only rows with
a non-zero weight.

COST
----
One epoch is O(nnz * n_labels) Python operations (about 100 non-zeros x 16 labels
per example). Measure the build time before choosing corpus size and epochs.
"""
import math

__all__ = ["MultinomialLogisticRegression"]


def _labels(labels):
    labels = list(labels)
    if len(labels) < 2:
        raise ValueError("need at least two labels")
    if len(set(labels)) != len(labels) or not all(isinstance(label, str) and label for label in labels):
        raise ValueError("labels must be unique non-empty strings")
    return labels


def _sparse_rows(X, name):
    if not isinstance(X, (list, tuple)):
        raise TypeError(f"{name} must be a list of {{index: value}} dicts")
    rows = []
    for x in X:
        if not isinstance(x, dict):
            raise TypeError(f"{name} rows must be dicts of index -> value")
        row = []
        for j, v in x.items():
            if isinstance(j, bool) or not isinstance(j, int) or j < 0:
                raise ValueError(f"{name}: feature index must be a non-negative int, got {j!r}")
            v = float(v)
            if not math.isfinite(v):
                raise ValueError(f"{name}: non-finite feature value")
            if v != 0.0:
                row.append((j, v))
        rows.append(row)
    return rows


def _logsumexp(scores):
    m = max(scores)
    return m + math.log(math.fsum(math.exp(s - m) for s in scores))


class MultinomialLogisticRegression:
    MODEL = "multinomial_logistic_regression"

    def __init__(self, labels, l2=1e-3, max_epochs=60, lr=0.5, seed=0, patience=5):
        self.labels = _labels(labels)
        self.l2 = float(l2)
        self.lr = float(lr)
        if not math.isfinite(self.l2) or self.l2 < 0.0:
            raise ValueError("l2 must be a finite number >= 0")
        if not math.isfinite(self.lr) or self.lr <= 0.0:
            raise ValueError("lr must be positive")
        for label, value in (("max_epochs", max_epochs), ("patience", patience), ("seed", seed)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{label} must be an int")
        if max_epochs < 1 or patience < 1:
            raise ValueError("max_epochs and patience must be >= 1")
        self.max_epochs = max_epochs
        self.patience = patience
        self.seed = seed
        k = len(self.labels)
        self.weights = {}
        self.bias = [0.0] * k
        self.final_lr = self.lr
        self.epochs_run = 0
        self.best_epoch = None
        self.stopped_early = False
        self.train_losses = []
        self.val_losses = []

    # ------------------------------------------------------------------ core math
    def _scores(self, row, weights, bias):
        s = list(bias)
        k = len(s)
        for j, v in row:
            wj = weights.get(j)
            if wj is not None:
                for c in range(k):
                    s[c] += wj[c] * v
        return s

    def _objective_and_gradient(self, rows, targets, sample_weights, total_weight, weights, bias):
        k = len(self.labels)
        grad_w = {j: [0.0] * k for j in weights}
        grad_b = [0.0] * k
        loss = 0.0
        for row, t, w in zip(rows, targets, sample_weights):
            if w == 0.0:
                continue
            s = self._scores(row, weights, bias)
            lse = _logsumexp(s)
            loss += w * (lse - s[t])
            coef = [w * math.exp(sc - lse) for sc in s]
            coef[t] -= w
            for c in range(k):
                grad_b[c] += coef[c]
            for j, v in row:
                g = grad_w[j]
                for c in range(k):
                    g[c] += coef[c] * v
        inv = 1.0 / total_weight
        loss *= inv
        penalty = 0.0
        for j, wj in weights.items():
            g = grad_w[j]
            for c in range(k):
                g[c] = g[c] * inv + self.l2 * wj[c]
                penalty += wj[c] * wj[c]
        grad_b = [g * inv for g in grad_b]
        return loss + 0.5 * self.l2 * penalty, grad_w, grad_b

    def _mean_cross_entropy(self, rows, targets, weights, bias):
        total = 0.0
        for row, t in zip(rows, targets):
            s = self._scores(row, weights, bias)
            total += _logsumexp(s) - s[t]
        return total / len(rows)

    def _targets(self, y, n, name):
        if len(y) != n:
            raise ValueError(f"{name} has {len(y)} labels for {n} rows")
        index = {label: i for i, label in enumerate(self.labels)}
        out = []
        for label in y:
            if label not in index:
                raise ValueError(f"{name}: unknown label {label!r}")
            out.append(index[label])
        return out

    # ------------------------------------------------------------------ public API
    def fit(self, X, y, sample_weight=None, X_val=None, y_val=None):
        rows = _sparse_rows(X, "X")
        if not rows:
            raise ValueError("cannot fit on zero rows")
        targets = self._targets(y, len(rows), "y")
        if sample_weight is None:
            sample_weights = [1.0] * len(rows)
        else:
            if len(sample_weight) != len(rows):
                raise ValueError("sample_weight must have one entry per row")
            sample_weights = [float(w) for w in sample_weight]
            if not all(math.isfinite(w) and w >= 0.0 for w in sample_weights):
                raise ValueError("sample weights must be finite and >= 0")
        total_weight = math.fsum(sample_weights)
        if total_weight <= 0.0:
            raise ValueError("sample weights sum to zero")
        if (X_val is None) != (y_val is None):
            raise ValueError("pass X_val and y_val together")
        val_rows = val_targets = None
        if X_val is not None:
            val_rows = _sparse_rows(X_val, "X_val")
            if not val_rows:
                raise ValueError("X_val is empty")
            val_targets = self._targets(y_val, len(val_rows), "y_val")

        k = len(self.labels)
        touched = sorted({j for row in rows for j, _ in row})
        weights = {j: [0.0] * k for j in touched}
        bias = [0.0] * k
        lr = self.lr
        loss, grad_w, grad_b = self._objective_and_gradient(rows, targets, sample_weights, total_weight, weights, bias)
        self.train_losses = []
        self.val_losses = []
        self.stopped_early = False
        self.best_epoch = None
        best = None
        best_val = math.inf
        epochs_without_improvement = 0
        epoch = 0
        for epoch in range(1, self.max_epochs + 1):
            while True:
                new_weights = {j: [wc - lr * gc for wc, gc in zip(weights[j], grad_w[j])] for j in touched}
                new_bias = [bc - lr * gc for bc, gc in zip(bias, grad_b)]
                new_loss, new_gw, new_gb = self._objective_and_gradient(
                    rows, targets, sample_weights, total_weight, new_weights, new_bias)
                if new_loss <= loss + 1e-12 * max(1.0, abs(loss)) or lr < 1e-12:
                    break
                lr *= 0.5
            weights, bias, loss, grad_w, grad_b = new_weights, new_bias, new_loss, new_gw, new_gb
            self.train_losses.append(loss)
            if val_rows is not None:
                val_loss = self._mean_cross_entropy(val_rows, val_targets, weights, bias)
                self.val_losses.append(val_loss)
                if val_loss < best_val:
                    best_val = val_loss
                    best = ({j: list(w) for j, w in weights.items()}, list(bias))
                    self.best_epoch = epoch
                    epochs_without_improvement = 0
                else:
                    epochs_without_improvement += 1
                    if epochs_without_improvement >= self.patience:
                        self.stopped_early = True
                        break
        if best is not None:
            weights, bias = best
        else:
            self.best_epoch = epoch
        self.weights = {j: w for j, w in weights.items() if any(c != 0.0 for c in w)}
        self.bias = bias
        self.final_lr = lr
        self.epochs_run = epoch
        return self

    def predict_proba(self, x) -> dict[str, float]:
        row = _sparse_rows([x], "x")[0]
        s = self._scores(row, self.weights, self.bias)
        lse = _logsumexp(s)
        return {label: math.exp(sc - lse) for label, sc in zip(self.labels, s)}

    def validation_loss(self, X, y) -> float:
        """Unweighted mean cross-entropy (no penalty) of the CURRENT parameters."""
        rows = _sparse_rows(X, "X")
        if not rows:
            raise ValueError("empty evaluation set")
        return self._mean_cross_entropy(rows, self._targets(y, len(rows), "y"), self.weights, self.bias)

    def to_dict(self) -> dict:
        return {
            "model": self.MODEL, "labels": list(self.labels), "l2": self.l2, "lr": self.lr,
            "final_lr": self.final_lr, "max_epochs": self.max_epochs, "patience": self.patience,
            "seed": self.seed, "epochs_run": self.epochs_run, "best_epoch": self.best_epoch,
            "stopped_early": self.stopped_early, "train_losses": list(self.train_losses),
            "val_losses": list(self.val_losses), "bias": list(self.bias),
            "weights": {str(j): list(self.weights[j]) for j in sorted(self.weights)},
        }

    @classmethod
    def from_dict(cls, d) -> "MultinomialLogisticRegression":
        if d.get("model") != cls.MODEL:
            raise ValueError(f"not a {cls.MODEL} dict")
        model = cls(d["labels"], l2=d["l2"], max_epochs=d["max_epochs"], lr=d["lr"],
                    seed=d["seed"], patience=d["patience"])
        k = len(model.labels)
        bias = [float(b) for b in d["bias"]]
        if len(bias) != k or not all(math.isfinite(b) for b in bias):
            raise ValueError("bias is malformed")
        weights = {}
        for key, row in d["weights"].items():
            if not isinstance(key, str) or not key.isdigit():
                raise ValueError(f"weight key {key!r} is not a feature index")
            values = [float(v) for v in row]
            if len(values) != k or not all(math.isfinite(v) for v in values):
                raise ValueError(f"weights for index {key} are malformed")
            weights[int(key)] = values
        model.bias = bias
        model.weights = weights
        model.final_lr = float(d["final_lr"])
        model.epochs_run = int(d["epochs_run"])
        model.best_epoch = d["best_epoch"]
        model.stopped_early = bool(d["stopped_early"])
        model.train_losses = [float(v) for v in d["train_losses"]]
        model.val_losses = [float(v) for v in d["val_losses"]]
        return model
