"""AMP-native AI core: the evaluation metrics every adoption gate is built on.

WHY THIS EXISTS
---------------
Every accuracy figure the native-AI build reports (failure risk vs the rule
scorer, anomaly score vs a range check, native copilot vs the keyword router)
is computed by ``amp_ai.core.metrics``. A metric that is quietly wrong does not
crash; it produces a plausible number and an adoption gate that passes for the
wrong reason. So each metric is pinned here against a WORKED EXAMPLE computed by
hand (the arithmetic is in the comments), not against another implementation of
the same idea.

The cases that matter most are the ones a naive implementation gets wrong:
tied scores (ROC-AUC must count a tie as half a win, average precision must
treat a tied group as one threshold, precision@k must not let input ORDER decide
which tied item makes the cut), a class that is absent (None, never 0.5 or 1.0),
and a label that is never predicted (its F1 is 0, not skipped).

The bootstrap tests pin the two properties the adoption gates lean on: the
interval is reproducible from its seed, and the resampling unit is the CLUSTER
(a machine, a question family), never the individual row.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_core_metrics.py
"""
import math

from amp_ai.core import metrics as M
from amp_ai.core.rng import make_rng

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc as e:  # noqa: BLE001 - the point is to catch exactly `exc`
        return e
    return None


def close(a, b, tol=1e-12):
    return a is not None and b is not None and abs(a - b) <= tol


def brute_force_auc(y, s):
    """Pairwise definition: P(score_pos > score_neg) + 0.5 * P(tie)."""
    pos = [v for v, t in zip(s, y) if t == 1]
    neg = [v for v, t in zip(s, y) if t == 0]
    wins = 0.0
    for p in pos:
        for n in neg:
            wins += 1.0 if p > n else (0.5 if p == n else 0.0)
    return wins / (len(pos) * len(neg))


def section_roc_auc():
    print("\n[roc_auc]")
    # 3 positives x 2 negatives = 6 pairs.
    #   pos 0.9 beats neg 0.5 and neg 0.1        -> 2
    #   pos 0.5 ties neg 0.5 (0.5), beats 0.1    -> 1.5
    #   pos 0.3 loses to 0.5, beats 0.1          -> 1
    #   4.5 / 6 = 0.75
    y = [1, 1, 1, 0, 0]
    s = [0.9, 0.5, 0.3, 0.5, 0.1]
    check("worked example with a tie = 0.75", M.roc_auc(y, s) == 0.75, str(M.roc_auc(y, s)))
    # Input order must not matter.
    check("same example reordered = 0.75",
          M.roc_auc([0, 1, 0, 1, 1], [0.1, 0.3, 0.5, 0.5, 0.9]) == 0.75)

    rng = make_rng(11, "metrics-test", "auc")
    y = [1 if rng.random() < 0.3 else 0 for _ in range(300)]
    s = [round(rng.random(), 1) for _ in range(300)]  # heavy ties on purpose
    check("matches the pairwise definition on 300 heavily tied scores",
          close(M.roc_auc(y, s), brute_force_auc(y, s), 1e-12),
          f"{M.roc_auc(y, s)} vs {brute_force_auc(y, s)}")

    check("perfect ranking = 1.0", M.roc_auc([0, 0, 1, 1], [0.1, 0.2, 0.3, 0.4]) == 1.0)
    check("reversed ranking = 0.0", M.roc_auc([1, 1, 0, 0], [0.1, 0.2, 0.3, 0.4]) == 0.0)
    check("all scores tied = 0.5", M.roc_auc([1, 0, 1, 0], [0.5] * 4) == 0.5)
    check("only positives -> None", M.roc_auc([1, 1, 1], [0.1, 0.2, 0.3]) is None)
    check("only negatives -> None", M.roc_auc([0, 0], [0.1, 0.2]) is None)
    check("empty -> None", M.roc_auc([], []) is None)
    check("bools are accepted as labels", M.roc_auc([True, False], [0.9, 0.1]) == 1.0)
    check("label 2 is refused", raises(ValueError, M.roc_auc, [0, 2], [0.1, 0.2]) is not None)
    check("length mismatch is refused", raises(ValueError, M.roc_auc, [0, 1], [0.1]) is not None)
    check("NaN score is refused", raises(ValueError, M.roc_auc, [0, 1], [0.1, float("nan")]) is not None)
    check("None score is refused", raises((TypeError, ValueError), M.roc_auc, [0, 1], [0.1, None]) is not None)


def section_pr_auc():
    print("\n[pr_auc (step-wise average precision)]")
    # Thresholds, descending:  0.9 -> P=1   R=.5 ; 0.8 -> P=.5 R=.5 ;
    #                          0.7 -> P=2/3 R=1  ; 0.1 -> P=.5 R=1
    # AP = .5*1 + 0*.5 + .5*(2/3) + 0*.5 = 5/6
    y = [1, 0, 1, 0]
    s = [0.9, 0.8, 0.7, 0.1]
    check("worked example = 5/6", close(M.pr_auc(y, s), 5 / 6), str(M.pr_auc(y, s)))
    # Tied group is ONE threshold: 0.5 -> TP1 FP1 P=.5 R=.5 ; 0.2 -> TP2 FP1 P=2/3 R=1
    # AP = .5*.5 + .5*(2/3) = 7/12
    check("tied scores form one threshold = 7/12",
          close(M.pr_auc([1, 0, 1], [0.5, 0.5, 0.2]), 7 / 12), str(M.pr_auc([1, 0, 1], [0.5, 0.5, 0.2])))
    check("tie result independent of input order",
          close(M.pr_auc([0, 1, 1], [0.5, 0.5, 0.2]), 7 / 12))
    check("perfect ranking = 1.0", M.pr_auc([1, 1, 0], [0.9, 0.8, 0.1]) == 1.0)
    check("no positives -> None", M.pr_auc([0, 0], [0.3, 0.4]) is None)
    check("no negatives -> None (a one-class sample says nothing)", M.pr_auc([1, 1], [0.3, 0.4]) is None)


def section_precision_at_k():
    print("\n[precision_at_k]")
    y = [1, 0, 1, 0, 0]
    s = [0.9, 0.8, 0.7, 0.6, 0.5]
    check("top-2 has one positive = 0.5", M.precision_at_k(y, s, 2) == 0.5)
    check("top-3 has two positives = 2/3", close(M.precision_at_k(y, s, 3), 2 / 3))
    # k=2: 0.9 is strictly above; two items tie at 0.5 (one positive) for the
    # one remaining slot -> expected 1 + 1 * (1/2) = 1.5 positives out of 2.
    check("boundary tie counted as its expectation = 0.75",
          M.precision_at_k([1, 0, 1, 0], [0.9, 0.5, 0.5, 0.1], 2) == 0.75)
    check("boundary tie independent of input order",
          M.precision_at_k([1, 1, 0, 0], [0.9, 0.5, 0.5, 0.1], 2) == 0.75)
    check("k=0 refused", raises(ValueError, M.precision_at_k, y, s, 0) is not None)
    check("k > n refused", raises(ValueError, M.precision_at_k, y, s, 6) is not None)

    print("\n[precision_at_k_grouped]")
    ys, ss, gs = [], [], []
    for i in range(10):          # group A: the positive ranks first
        ys.append(1 if i == 0 else 0)
        ss.append(1.0 - i / 10)
        gs.append("A")
    for i in range(10):          # group B: the positive ranks last
        ys.append(1 if i == 9 else 0)
        ss.append(1.0 - i / 10)
        gs.append("B")
    check("mean over groups of per-group top-10% precision = 0.5",
          M.precision_at_k_grouped(ys, ss, gs, 0.1) == 0.5)
    # 0.07 * 100 is 7.000000000000001 in floating point; a naive ceil gives k=8.
    check("fixture: 0.07 * 100 really is above 7 in floating point", 0.07 * 100 > 7)
    y100 = [1] * 7 + [0] * 93
    s100 = [1000.0 - i for i in range(100)]
    check("k from frac is not inflated by float noise (0.07*100 -> 7, not 8)",
          M.precision_at_k_grouped(y100, s100, ["d"] * 100, 0.07) == 1.0,
          str(M.precision_at_k_grouped(y100, s100, ["d"] * 100, 0.07)))
    check("tiny group still gets k=1",
          M.precision_at_k_grouped([1, 0, 0], [0.9, 0.2, 0.1], ["g"] * 3, 0.1) == 1.0)
    check("empty -> None", M.precision_at_k_grouped([], [], [], 0.1) is None)


def section_probability_metrics():
    print("\n[brier / calibration_table / ece]")
    check("brier worked example = ((.2)^2 + (.3)^2)/2 = 0.065",
          close(M.brier([1, 0], [0.8, 0.3]), 0.065))
    check("brier refuses p > 1", raises(ValueError, M.brier, [1], [1.2]) is not None)
    check("brier empty -> None", M.brier([], []) is None)

    y = [0, 1, 0, 1]
    p = [0.05, 0.15, 0.15, 1.0]
    table = M.calibration_table(y, p, bins=10)
    check("10 bins", len(table) == 10, str(len(table)))
    check("bin counts total the sample", sum(b["n"] for b in table) == 4)
    check("bin 0 holds 0.05", table[0]["n"] == 1 and table[0]["observed_rate"] == 0.0)
    check("bin 1 holds both 0.15s, observed 0.5",
          table[1]["n"] == 2 and close(table[1]["mean_predicted"], 0.15) and table[1]["observed_rate"] == 0.5)
    check("p = 1.0 lands in the last bin", table[9]["n"] == 1 and table[9]["observed_rate"] == 1.0)
    check("an empty bin reports None, never 0",
          table[5]["n"] == 0 and table[5]["observed_rate"] is None and table[5]["mean_predicted"] is None)
    # ECE = 1/4*|.05-0| + 2/4*|.15-.5| + 1/4*|1-1| = .0125 + .175 + 0 = 0.1875
    check("ece worked example = 0.1875", close(M.ece(y, p, bins=10), 0.1875), str(M.ece(y, p, bins=10)))
    check("ece of a perfectly calibrated bin = 0", M.ece([0, 1], [0.5, 0.5], bins=10) == 0.0)
    check("ece empty -> None", M.ece([], [], bins=10) is None)


def section_classification_metrics():
    print("\n[accuracy / macro_f1 / confusion_matrix]")
    check("accuracy 2/3", close(M.accuracy(["a", "b", "c"], ["a", "x", "c"]), 2 / 3))
    check("accuracy empty -> None", M.accuracy([], []) is None)

    labels = ["a", "b", "c"]
    y = ["a", "a", "b", "b", "c"]
    yhat = ["a", "a", "a", "b", "a"]
    # a: tp2 fp2 fn0 -> F1 = 4/6 ; b: tp1 fp0 fn1 -> 2/3 ; c: never predicted -> 0
    # macro = (2/3 + 2/3 + 0) / 3 = 4/9
    check("macro-F1 with a never-predicted label = 4/9",
          close(M.macro_f1(y, yhat, labels), 4 / 9), str(M.macro_f1(y, yhat, labels)))
    check("a label absent from truth AND predictions is not averaged in",
          close(M.macro_f1(y, yhat, labels + ["d"]), 4 / 9))
    check("a value outside labels is refused",
          raises(ValueError, M.macro_f1, y, yhat[:-1] + ["zzz"], labels) is not None)

    cm = M.confusion_matrix(y, yhat, labels)
    check("confusion matrix totals the sample", sum(sum(r) for r in cm) == len(y))
    check("row a = [2,0,0]", cm[0] == [2, 0, 0], str(cm))
    check("row b = [1,1,0]", cm[1] == [1, 1, 0], str(cm))
    check("row c = [1,0,0]", cm[2] == [1, 0, 0], str(cm))


def section_mcnemar():
    print("\n[mcnemar_exact]")
    # b = A right & B wrong = 10 ; c = A wrong & B right = 2 ; plus 5 both right, 3 both wrong.
    a = [True] * 10 + [False] * 2 + [True] * 5 + [False] * 3
    b = [False] * 10 + [True] * 2 + [True] * 5 + [False] * 3
    r = M.mcnemar_exact(a, b)
    # p = 2 * sum_{i<=2} C(12,i) / 2^12 = 2 * (1 + 12 + 66) / 4096 = 158/4096
    check("b=10, c=2", r["b"] == 10 and r["c"] == 2, str(r))
    check("exact two-sided p = 158/4096 = 0.03857421875", r["p_value"] == 158 / 4096, str(r["p_value"]))
    r2 = M.mcnemar_exact(b, a)
    check("swapping systems swaps b/c, keeps p", r2["b"] == 2 and r2["c"] == 10 and r2["p_value"] == r["p_value"])
    check("no discordant pairs -> p = 1", M.mcnemar_exact([True, False], [True, False])["p_value"] == 1.0)
    check("balanced discordance caps p at 1",
          M.mcnemar_exact([True] * 5 + [False] * 5, [False] * 5 + [True] * 5)["p_value"] == 1.0)
    check("length mismatch refused", raises(ValueError, M.mcnemar_exact, [True], [True, False]) is not None)


def section_bootstrap():
    print("\n[bootstrap_ci / paired_bootstrap_diff]")
    rng = make_rng(5, "metrics-test", "bootstrap")
    groups, y, s_good, s_noise = [], [], [], []
    for m in range(40):
        for _ in range(6):
            label = 1 if rng.random() < 0.3 else 0
            groups.append(f"m{m}")
            y.append(label)
            s_good.append(0.6 * label + rng.random())  # informative but overlapping
            s_noise.append(rng.random())

    one = M.bootstrap_ci(M.roc_auc, y, s_good, groups=groups, n=200, seed=99)
    two = M.bootstrap_ci(M.roc_auc, y, s_good, groups=groups, n=200, seed=99)
    other = M.bootstrap_ci(M.roc_auc, y, s_good, groups=groups, n=200, seed=100)
    check("deterministic under its seed", one == two, f"{one} vs {two}")
    check("a different seed gives a different interval", (one["lo"], one["hi"]) != (other["lo"], other["hi"]))
    check("estimate is the metric on the full data", one["estimate"] == M.roc_auc(y, s_good))
    check("lo <= estimate <= hi", one["lo"] <= one["estimate"] <= one["hi"], str(one))
    check("every replicate valid here", one["n_valid"] == 200 and one["n_boot"] == 200)

    # Cluster property: every group is resampled whole, so each group's row
    # count in a replicate is a multiple of its size (6).
    seen_bad = []

    def whole_groups(gs):
        counts = {}
        for g in gs:
            counts[g] = counts.get(g, 0) + 1
        if any(c % 6 for c in counts.values()):
            seen_bad.append(counts)
        return 0.0

    M.bootstrap_ci(whole_groups, groups, groups=groups, n=50, seed=1)
    check("resampling unit is the cluster, never the row", not seen_bad, str(seen_bad[:1]))

    rows_seen = []

    def count_rows(gs):
        rows_seen.append(len(gs))
        return 0.0

    M.bootstrap_ci(count_rows, groups, groups=None, n=20, seed=1)
    check("groups=None resamples rows (i.i.d.)", all(r == len(groups) for r in rows_seen))

    # Too many undefined replicates -> no interval rather than a biased one.
    calls = {"n": 0}

    def mostly_none(values):
        calls["n"] += 1
        return 1.0 if calls["n"] % 3 == 0 else None

    sparse = M.bootstrap_ci(mostly_none, y, groups=groups, n=60, seed=3)
    check("interval withheld when too few replicates are defined",
          sparse["lo"] is None and sparse["hi"] is None and sparse["n_valid"] < 60, str(sparse))

    diff = M.paired_bootstrap_diff(M.roc_auc, y, s_good, s_noise, groups, n=200, seed=7)
    check("informative scorer beats noise: paired lower bound > 0", diff["lo"] > 0, str(diff))
    check("paired diff deterministic", diff == M.paired_bootstrap_diff(M.roc_auc, y, s_good, s_noise, groups, n=200, seed=7))
    same = M.paired_bootstrap_diff(M.roc_auc, y, s_good, list(s_good), groups, n=200, seed=7)
    # Only a PAIRED resample gives exactly zero on every replicate.
    check("identical scorers: every paired replicate is exactly 0",
          same["lo"] == 0.0 and same["hi"] == 0.0 and same["mean"] == 0.0, str(same))

    def auc_gap(yy, a, b):
        ra, rb = M.roc_auc(yy, a), M.roc_auc(yy, b)
        return None if ra is None or rb is None else ra - rb

    via_ci = M.bootstrap_ci(auc_gap, y, s_good, s_noise, groups=groups, n=200, seed=7)
    check("paired diff draws the same resamples as bootstrap_ci for the same seed",
          (via_ci["lo"], via_ci["hi"]) == (diff["lo"], diff["hi"]), f"{via_ci} vs {diff}")
    check("n must be positive", raises(ValueError, M.bootstrap_ci, M.roc_auc, y, s_good, groups=groups, n=0, seed=1) is not None)


def main():
    print("=" * 74)
    print("AMP-native AI core: evaluation metrics")
    print("=" * 74)
    section_roc_auc()
    section_pr_auc()
    section_precision_at_k()
    section_probability_metrics()
    section_classification_metrics()
    section_mcnemar()
    section_bootstrap()
    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


def test_amp_ai_core_metrics():
    """pytest entry point; CI runs this file as a standalone script."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
