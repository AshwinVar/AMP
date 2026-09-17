"""Evaluation metrics for AMP-native AI, standard library only.

Every accuracy figure the native-AI build reports comes through here, and every
function is pinned to a hand-worked example in test_amp_ai_core_metrics.py.

CONVENTIONS
-----------
* Binary labels are 0/1 (bools accepted). Scores and probabilities must be
  finite numbers; a NaN raises instead of sorting somewhere arbitrary.
* A metric that is undefined on its input returns ``None``, never a stand-in
  number: ROC-AUC and average precision with one class absent, anything on an
  empty sample, the observed rate of an empty calibration bin. (A bootstrap
  replicate that happens to contain no positives must not score 0.5 or 1.0.)
* TIES are handled so that input ORDER never changes a result: ROC-AUC counts
  a tied positive/negative pair as half a win (average ranks); average
  precision treats all items with the same score as one threshold;
  precision@k counts the items tied at the cut-off at their expected value.
* The bootstrap resamples CLUSTERS (a machine, a question family), not rows,
  because rows from one cluster are correlated and a row bootstrap makes the
  interval look far tighter than the evidence supports.
"""
import math
from fractions import Fraction

from .rng import make_rng

__all__ = [
    "roc_auc", "pr_auc", "precision_at_k", "precision_at_k_grouped", "brier",
    "calibration_table", "ece", "accuracy", "macro_f1", "confusion_matrix",
    "bootstrap_ci", "paired_bootstrap_diff", "mcnemar_exact", "MIN_VALID_REPLICATE_FRACTION",
]

# A bootstrap interval is withheld (lo = hi = None) unless at least this share of
# replicates produced a defined metric. Dropping undefined replicates silently
# conditions the interval on "the resample happened to contain both classes".
MIN_VALID_REPLICATE_FRACTION = 0.95


# --------------------------------------------------------------------------- validation
def _labels01(y):
    out = []
    for v in y:
        if isinstance(v, bool):
            out.append(1 if v else 0)
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and v in (0, 1):
            out.append(int(v))
        else:
            raise ValueError(f"binary labels must be 0 or 1, got {v!r}")
    return out


def _numbers(values, n, label):
    values = list(values)
    if len(values) != n:
        raise ValueError(f"{label} has {len(values)} entries for {n} labels")
    out = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise TypeError(f"{label} must be numbers, got {type(v).__name__}")
        f = float(v)
        if not math.isfinite(f):
            raise ValueError(f"{label} contains a non-finite value")
        out.append(f)
    return out


def _probabilities(values, n):
    p = _numbers(values, n, "probabilities")
    if any(v < 0.0 or v > 1.0 for v in p):
        raise ValueError("probabilities must lie in [0, 1]")
    return p


def _pairs(y, s):
    labels = _labels01(y)
    return labels, _numbers(s, len(labels), "scores")


def _tie_groups_descending(labels, scores):
    """[(score, n_items, n_positive)] from highest score to lowest."""
    groups = {}
    for t, v in zip(labels, scores):
        n, pos = groups.get(v, (0, 0))
        groups[v] = (n + 1, pos + t)
    return [(v, groups[v][0], groups[v][1]) for v in sorted(groups, reverse=True)]


# --------------------------------------------------------------------------- ranking metrics
def roc_auc(y, s):
    """Mann-Whitney AUC with average ranks for ties; None if a class is absent."""
    labels, scores = _pairs(y, s)
    n_pos = sum(labels)
    n_neg = len(labels) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    rank_sum_pos = 0.0
    position = 0
    for _, n_items, n_positive in reversed(_tie_groups_descending(labels, scores)):
        # items occupy ranks position+1 .. position+n_items; ties share the average
        average_rank = position + (n_items + 1) / 2.0
        rank_sum_pos += n_positive * average_rank
        position += n_items
    return (rank_sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def pr_auc(y, s):
    """Step-wise average precision: sum over distinct thresholds of (R_k - R_{k-1}) * P_k.

    None if either class is absent.
    """
    labels, scores = _pairs(y, s)
    n_pos = sum(labels)
    if n_pos == 0 or n_pos == len(labels):
        return None
    tp = fp = 0
    previous_recall = 0.0
    total = 0.0
    for _, n_items, n_positive in _tie_groups_descending(labels, scores):
        tp += n_positive
        fp += n_items - n_positive
        recall = tp / n_pos
        total += (recall - previous_recall) * (tp / (tp + fp))
        previous_recall = recall
    return total


def precision_at_k(y, s, k):
    """Share of positives among the k highest scores; a tie at the cut-off counts at its expectation."""
    labels, scores = _pairs(y, s)
    if isinstance(k, bool) or not isinstance(k, int) or k < 1 or k > len(labels):
        raise ValueError(f"k must be an int in [1, {len(labels)}], got {k!r}")
    positives = 0.0
    taken = 0
    for _, n_items, n_positive in _tie_groups_descending(labels, scores):
        if taken + n_items <= k:
            positives += n_positive
            taken += n_items
            if taken == k:
                break
        else:
            need = k - taken
            positives += need * (n_positive / n_items)
            break
    return positives / k


def precision_at_k_grouped(y, s, groups, frac):
    """Mean over groups (e.g. as-of dates) of precision@k_g, k_g = max(1, ceil(frac * n_g)).

    Every group counts, including groups with no positives (they score 0 for
    any scorer). None if there are no items.
    """
    labels, scores = _pairs(y, s)
    groups = list(groups)
    if len(groups) != len(labels):
        raise ValueError("groups must have one entry per label")
    if isinstance(frac, bool) or not isinstance(frac, (int, float)) or not 0.0 < frac <= 1.0:
        raise ValueError(f"frac must be in (0, 1], got {frac!r}")
    by_group = {}
    for g, t, v in zip(groups, labels, scores):
        by_group.setdefault(g, ([], []))
        by_group[g][0].append(t)
        by_group[g][1].append(v)
    if not by_group:
        return None
    values = []
    for g in sorted(by_group, key=lambda key: (type(key).__name__, key)):
        ys, ss = by_group[g]
        k = min(len(ys), max(1, math.ceil(round(frac * len(ys), 9))))
        values.append(precision_at_k(ys, ss, k))
    return math.fsum(values) / len(values)


# --------------------------------------------------------------------------- probability metrics
def brier(y, p):
    labels = _labels01(y)
    probs = _probabilities(p, len(labels))
    if not labels:
        return None
    return math.fsum((q - t) ** 2 for q, t in zip(probs, labels)) / len(labels)


def calibration_table(y, p, bins=10):
    """Equal-width bins on [0, 1]; bin i holds p in [i/bins, (i+1)/bins), the last bin includes 1.0."""
    if isinstance(bins, bool) or not isinstance(bins, int) or bins < 1:
        raise ValueError("bins must be a positive int")
    labels = _labels01(y)
    probs = _probabilities(p, len(labels))
    counts = [0] * bins
    sum_p = [[] for _ in range(bins)]
    positives = [0] * bins
    for t, q in zip(labels, probs):
        i = min(int(q * bins), bins - 1)
        counts[i] += 1
        sum_p[i].append(q)
        positives[i] += t
    table = []
    for i in range(bins):
        n = counts[i]
        table.append({
            "bin": i,
            "lo": i / bins,
            "hi": (i + 1) / bins,
            "n": n,
            "mean_predicted": math.fsum(sum_p[i]) / n if n else None,
            "observed_rate": positives[i] / n if n else None,
        })
    return table


def ece(y, p, bins=10):
    """Expected calibration error: sum over non-empty bins of (n_b / N) * |mean_p_b - observed_b|."""
    table = calibration_table(y, p, bins)
    total = sum(row["n"] for row in table)
    if total == 0:
        return None
    return math.fsum(row["n"] / total * abs(row["mean_predicted"] - row["observed_rate"])
                     for row in table if row["n"])


# --------------------------------------------------------------------------- classification metrics
def _paired_labels(y, yhat, labels=None):
    y, yhat = list(y), list(yhat)
    if len(y) != len(yhat):
        raise ValueError(f"y has {len(y)} entries, yhat has {len(yhat)}")
    if labels is not None:
        allowed = set(labels)
        if len(allowed) != len(labels):
            raise ValueError("labels must be unique")
        for v in y + yhat:
            if v not in allowed:
                raise ValueError(f"value {v!r} is not one of the labels")
    return y, yhat


def accuracy(y, yhat):
    y, yhat = _paired_labels(y, yhat)
    if not y:
        return None
    return sum(1 for a, b in zip(y, yhat) if a == b) / len(y)


def macro_f1(y, yhat, labels):
    """Unweighted mean F1 over labels. F1 = 2tp / (2tp + fp + fn), so a label that
    occurs but is never predicted scores 0. A label absent from both y and yhat has
    no F1 and is left out of the mean. None if no label occurs.
    """
    labels = list(labels)
    y, yhat = _paired_labels(y, yhat, labels)
    scores = []
    for label in labels:
        tp = sum(1 for a, b in zip(y, yhat) if a == label and b == label)
        fp = sum(1 for a, b in zip(y, yhat) if a != label and b == label)
        fn = sum(1 for a, b in zip(y, yhat) if a == label and b != label)
        if tp + fp + fn == 0:
            continue
        scores.append(2 * tp / (2 * tp + fp + fn))
    return math.fsum(scores) / len(scores) if scores else None


def confusion_matrix(y, yhat, labels):
    """rows = true label, columns = predicted label, both in ``labels`` order."""
    labels = list(labels)
    y, yhat = _paired_labels(y, yhat, labels)
    index = {label: i for i, label in enumerate(labels)}
    matrix = [[0] * len(labels) for _ in labels]
    for a, b in zip(y, yhat):
        matrix[index[a]][index[b]] += 1
    return matrix


def mcnemar_exact(correct_a, correct_b):
    """Exact two-sided McNemar test on paired correctness.

    b = cases A got right and B got wrong; c = the reverse.
    p = min(1, 2 * P[Binomial(b + c, 1/2) <= min(b, c)]), computed exactly.
    """
    a = _labels01(correct_a)
    b_list = _labels01(correct_b)
    if len(a) != len(b_list):
        raise ValueError("correct_a and correct_b must be the same length")
    b = sum(1 for x, z in zip(a, b_list) if x and not z)
    c = sum(1 for x, z in zip(a, b_list) if z and not x)
    m = b + c
    if m == 0:
        p_value = 1.0
    else:
        tail = sum(math.comb(m, i) for i in range(min(b, c) + 1))
        p_value = float(min(Fraction(1), Fraction(2 * tail, 2 ** m)))
    return {"b": b, "c": c, "p_value": p_value}


# --------------------------------------------------------------------------- bootstrap
def _clusters(groups, n):
    if groups is None:
        return [[i] for i in range(n)]
    groups = list(groups)
    if len(groups) != n:
        raise ValueError(f"groups has {len(groups)} entries for {n} rows")
    members = {}
    for i, g in enumerate(groups):
        members.setdefault(g, []).append(i)
    return [members[g] for g in sorted(members, key=lambda key: (type(key).__name__, key))]


def _percentile(sorted_values, q):
    """Linear interpolation between order statistics (the common 'type 7' definition)."""
    position = (len(sorted_values) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower)


def _check_bootstrap_args(n, alpha, seed):
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError("n must be a positive int")
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an int")


def _resampled_indices(clusters, n, seed):
    """Yield n cluster-bootstrap index lists. Same (clusters, n, seed) -> same resamples."""
    rng = make_rng(seed, "cluster_bootstrap")
    count = len(clusters)
    for _ in range(n):
        indices = []
        for _ in range(count):
            indices.extend(clusters[rng.randrange(count)])
        yield indices


def _interval(values, n, alpha):
    if len(values) < math.ceil(MIN_VALID_REPLICATE_FRACTION * n) or not values:
        return None, None
    ordered = sorted(values)
    return _percentile(ordered, alpha / 2.0), _percentile(ordered, 1.0 - alpha / 2.0)


def bootstrap_ci(metric_fn, *arrays, groups, n=1000, seed, alpha=0.05):
    """Percentile cluster-bootstrap interval for ``metric_fn(*arrays)``.

    ``groups`` gives each row's cluster; ``None`` means every row is its own
    cluster. Replicates where the metric is None are dropped and counted; if
    fewer than MIN_VALID_REPLICATE_FRACTION of them are defined, lo and hi are
    None.

    Returns {"estimate", "lo", "hi", "n_boot", "n_valid", "alpha"}.
    """
    _check_bootstrap_args(n, alpha, seed)
    if not arrays:
        raise ValueError("pass at least one array")
    columns = [list(a) for a in arrays]
    size = len(columns[0])
    if any(len(c) != size for c in columns):
        raise ValueError("arrays must all be the same length")
    estimate = metric_fn(*columns)
    values = []
    for indices in _resampled_indices(_clusters(groups, size), n, seed):
        v = metric_fn(*[[c[i] for i in indices] for c in columns])
        if v is not None:
            values.append(float(v))
    lo, hi = _interval(values, n, alpha)
    return {"estimate": estimate, "lo": lo, "hi": hi, "n_boot": n, "n_valid": len(values), "alpha": alpha}


def paired_bootstrap_diff(metric_fn, y, s_a, s_b, groups, n=1000, *, seed, alpha=0.05):
    """Cluster bootstrap of metric_fn(y, s_a) - metric_fn(y, s_b) with the SAME resample for both.

    Uses the same resamples as ``bootstrap_ci`` for the same (groups, n, seed).
    Returns {"mean", "estimate", "lo", "hi", "n_boot", "n_valid", "alpha"};
    ``mean`` is the mean replicate difference, ``estimate`` the difference on
    the full data (None if either metric is undefined there).
    """
    _check_bootstrap_args(n, alpha, seed)
    y, s_a, s_b = list(y), list(s_a), list(s_b)
    if not (len(y) == len(s_a) == len(s_b)):
        raise ValueError("y, s_a and s_b must be the same length")
    full_a, full_b = metric_fn(y, s_a), metric_fn(y, s_b)
    estimate = None if full_a is None or full_b is None else full_a - full_b
    diffs = []
    for indices in _resampled_indices(_clusters(groups, len(y)), n, seed):
        yy = [y[i] for i in indices]
        a = metric_fn(yy, [s_a[i] for i in indices])
        b = metric_fn(yy, [s_b[i] for i in indices])
        if a is not None and b is not None:
            diffs.append(float(a) - float(b))
    lo, hi = _interval(diffs, n, alpha)
    mean = math.fsum(diffs) / len(diffs) if diffs else None
    return {"mean": mean, "estimate": estimate, "lo": lo, "hi": hi, "n_boot": n,
            "n_valid": len(diffs), "alpha": alpha}
