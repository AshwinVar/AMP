"""AMP-native AI core: the pure-Python learners, features, baselines and splits.

WHY THIS EXISTS
---------------
The native-AI build trains and serves with the standard library only (no numpy,
no scikit-learn, no downloaded weights), so every numerical primitive is AMP's
own code. This suite pins each one against a property that would be false if it
were subtly wrong:

* the logistic regression RECOVERS coefficients it was generated from (a solver
  that merely converges to something would not), its penalty SHRINKS as it
  grows, the intercept is NOT penalised, and class weights MOVE the intercept;
* Platt scaling LOWERS calibration error on held-out data;
* the multinomial model separates a toy problem and leaves never-seen feature
  indices at exactly zero (so the artifact stores touched indices only);
* feature hashing is IDENTICAL across interpreters with different
  PYTHONHASHSEED (the built-in ``hash()`` would silently break a shipped model);
* the robust baseline IGNORES a 20% outlier contamination and honours its MAD
  floor; the shrunk covariance INVERTS when there are more signals than rows;
  the empirical calibrator is MONOTONIC;
* ``entity_time_split`` REFUSES any split where an entity appears on both sides,
  a label window crosses a boundary, or the gap before the test period is
  shorter than horizon + gap.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_core_models.py
"""
import hashlib
import json
import math
import os
import subprocess
import sys

from amp_ai.core import linalg
from amp_ai.core.logistic import BinaryLogisticRegression, PlattCalibrator
from amp_ai.core.metrics import ece
from amp_ai.core.multinomial import MultinomialLogisticRegression
from amp_ai.core.rng import make_rng, stable_hash
from amp_ai.core.robust import EmpiricalCalibrator, RobustBaseline, ShrunkCovariance
from amp_ai.core.split import (SplitError, entity_time_split, group_split, refit_rows,
                               verify_entity_time_split)
from amp_ai.core.standardize import Standardizer
from amp_ai.core.text_features import hashed_features, normalize_text

BACKEND = os.path.dirname(os.path.abspath(__file__))

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc as e:  # noqa: BLE001
        return e
    return None


def sigmoid(z):
    return 1.0 / (1.0 + math.exp(-z))


# --------------------------------------------------------------------------- rng / hashing
def section_rng():
    print("\n[rng]")
    a = make_rng(42, "fleet", "machine-7")
    b = make_rng(42, "fleet", "machine-7")
    check("same seed and stream -> same sequence",
          [a.random() for _ in range(5)] == [b.random() for _ in range(5)])
    c = make_rng(42, "fleet", "machine-8")
    d = make_rng(43, "fleet", "machine-7")
    first = make_rng(42, "fleet", "machine-7").random()
    check("different stream -> different sequence", c.random() != first)
    check("different seed -> different sequence", d.random() != first)
    check("a '|' inside a stream name is refused (it would alias two streams)",
          raises(ValueError, make_rng, 1, "a|b") is not None)
    check("a non-int seed is refused", raises(TypeError, make_rng, "1", "x") is not None)
    idx, sign = stable_hash("w1:oee", 8192)
    check("stable_hash index in range, sign is +/-1", 0 <= idx < 8192 and sign in (-1, 1))
    # Frozen spec: blake2b(digest_size=8), big-endian; sign = lowest bit;
    # index = (value >> 1) % dim. A shipped copilot artifact depends on it.
    value = int.from_bytes(hashlib.blake2b(b"w1:oee", digest_size=8).digest(), "big")
    spec = ((value >> 1) % 8192, 1 if value & 1 else -1)
    check("stable_hash follows the frozen spec (re-derived here from hashlib)",
          stable_hash("w1:oee", 8192) == spec, f"{stable_hash('w1:oee', 8192)} vs {spec}")


def section_cross_process_hashing():
    print("\n[hashing is independent of PYTHONHASHSEED]")
    qs = ["Which machine is most likely to break down?", "oee line-2 today",
          "\u0938\u094d\u091f\u0949\u0915 \u0915\u092e \u0939\u0948"]
    code = (
        "import json, sys; sys.path.insert(0, %r);"
        "from amp_ai.core.text_features import hashed_features;"
        "from amp_ai.core.rng import stable_hash, make_rng;"
        "qs = json.loads(sys.argv[1]);"
        "out = {'f': [sorted(hashed_features(q).items()) for q in qs],"
        " 'h': [stable_hash(q, 8192) for q in qs],"
        " 'r': [make_rng(3, q).random() for q in qs]};"
        "print(json.dumps(out))"
    ) % BACKEND
    outputs = []
    for seed in ("0", "12345", "random"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, "-c", code, json.dumps(qs)], env=env, capture_output=True,
                           text=True, encoding="utf-8")
        check(f"subprocess with PYTHONHASHSEED={seed} ran", r.returncode == 0, r.stderr[-400:])
        outputs.append(r.stdout.strip())
    check("identical features, hashes and rng draws across three hash seeds",
          len(set(outputs)) == 1 and outputs[0] != "", str([o[:80] for o in outputs]))
    here = json.dumps({'f': [sorted(hashed_features(q).items()) for q in qs],
                       'h': [stable_hash(q, 8192) for q in qs],
                       'r': [make_rng(3, q).random() for q in qs]})
    check("in-process result equals the subprocess result",
          json.loads(here) == json.loads(outputs[0]) if outputs[0] else False)


# --------------------------------------------------------------------------- text
def section_text():
    print("\n[normalize_text / hashed_features]")
    check("punctuation stripped, apostrophe dropped, hyphen kept, whitespace collapsed",
          normalize_text("  What's the OEE\u2014on Line-2?? ") == "whats the oee on line-2",
          repr(normalize_text("  What's the OEE\u2014on Line-2?? ")))
    check("NFKC folds full-width letters", normalize_text("\uff2f\uff25\uff25") == "oee")
    check("curly apostrophe dropped like a straight one", normalize_text("don\u2019t") == "dont")
    check("tabs and newlines collapse", normalize_text("a\tb\n\nc") == "a b c")
    check("non-str refused", raises(TypeError, normalize_text, None) is not None)

    f = hashed_features("Which machine will break down next?")
    norm = math.sqrt(sum(v * v for v in f.values()))
    check("L2-normalised", abs(norm - 1.0) < 1e-12, str(norm))
    check("indices within dim", all(0 <= i < 8192 for i in f))
    check("keys come back sorted", list(f) == sorted(f))
    check("deterministic", f == hashed_features("Which machine will break down next?"))
    check("normalisation makes case/punctuation irrelevant",
          f == hashed_features("which MACHINE will break down next"))
    check("punctuation-only text -> no features", hashed_features("?!...") == {})
    words_only = hashed_features("oee today", char_ngrams=None)
    check("char n-grams can be disabled (unigrams+bigram = 3 features at most)",
          0 < len(words_only) <= 3, str(len(words_only)))
    check("bad n-gram range refused",
          raises(ValueError, hashed_features, "x", word_ngrams=(2, 1)) is not None)


# --------------------------------------------------------------------------- linalg
def section_linalg():
    print("\n[linalg]")
    a = [[2.0, 1.0, -1.0], [-3.0, -1.0, 2.0], [-2.0, 1.0, 2.0]]
    b = [8.0, -11.0, -3.0]
    x = linalg.solve(a, b)
    check("solve 3x3 (x = 2, 3, -1)", all(abs(u - v) < 1e-12 for u, v in zip(x, [2.0, 3.0, -1.0])), str(x))
    check("inputs not mutated", a[0] == [2.0, 1.0, -1.0] and b == [8.0, -11.0, -3.0])
    check("partial pivoting handles a zero leading pivot",
          linalg.solve([[0.0, 1.0], [1.0, 0.0]], [3.0, 4.0]) == [4.0, 3.0])
    inv = linalg.invert(a)
    ident = [[linalg.dot(row, [inv[r][c] for r in range(3)]) for c in range(3)] for row in a]
    check("A * inv(A) = I", all(abs(ident[i][j] - (1.0 if i == j else 0.0)) < 1e-12
                                for i in range(3) for j in range(3)), str(ident))
    check("singular matrix raises SingularMatrix",
          raises(linalg.SingularMatrix, linalg.solve, [[1.0, 2.0], [2.0, 4.0]], [1.0, 2.0]) is not None)
    check("dot", linalg.dot([1.0, 2.0, 3.0], [4.0, 5.0, 6.0]) == 32.0)
    check("dot length mismatch refused", raises(ValueError, linalg.dot, [1.0], [1.0, 2.0]) is not None)


# --------------------------------------------------------------------------- standardize
def section_standardizer():
    print("\n[Standardizer]")
    names = ["a", "b", "c"]
    rows = [{"a": 1.0, "b": None, "c": 5.0},
            {"a": 2.0, "b": 10.0, "c": 5.0},
            {"a": 3.0, "b": 20.0, "c": 5.0},
            {"a": 4.0, "b": 90.0, "c": 5.0}]
    st = Standardizer.fit(rows, names)
    d = st.to_dict()
    # observed b = 10, 20, 90: median 20, mean 40 - so the check tells them apart
    check("None imputed with the training MEDIAN of observed values (20, not the mean 40)",
          d["impute"][1] == 20.0, str(d))
    check("constant column: std floored to 1.0", d["std"][2] == 1.0)
    z = st.transform(rows)
    check("training column a is centred", abs(sum(r[0] for r in z)) < 1e-12)
    snapshot = json.dumps(st.to_dict(), sort_keys=True)
    st.transform([{"a": 1e6, "b": -1e6, "c": 7.0}] * 50)
    check("transform learns nothing from what it transforms",
          json.dumps(st.to_dict(), sort_keys=True) == snapshot)
    check("transform of the same row is unchanged after other batches", st.transform(rows) == z)
    again = Standardizer.from_dict(json.loads(json.dumps(st.to_dict())))
    check("to_dict/from_dict round trip transforms identically", again.transform(rows) == z)
    check("list rows aligned with names work", st.transform([[1.0, None, 5.0]]) == [z[0]])
    check("dict row missing a feature is refused (a typo must not become an imputation)",
          raises(KeyError, st.transform, [{"a": 1.0, "c": 5.0}]) is not None)
    check("feature with no observed training value refused",
          raises(ValueError, Standardizer.fit, [{"x": None}, {"x": None}], ["x"]) is not None)
    check("NaN refused", raises(ValueError, Standardizer.fit, [{"x": float("nan")}], ["x"]) is not None)


# --------------------------------------------------------------------------- logistic
def _logistic_data(n, beta, intercept, seed):
    rng = make_rng(seed, "logistic-test")
    X, y = [], []
    for _ in range(n):
        row = [rng.gauss(0.0, 1.0) for _ in beta]
        z = intercept + sum(b * v for b, v in zip(beta, row))
        X.append(row)
        y.append(1 if rng.random() < sigmoid(z) else 0)
    return X, y


def section_logistic():
    print("\n[BinaryLogisticRegression (IRLS)]")
    true_beta, true_b = [1.5, -2.0, 0.5], -0.7
    X, y = _logistic_data(50000, true_beta, true_b, seed=7)
    model = BinaryLogisticRegression(l2=1e-6).fit(X, y)
    errs = [abs(u - v) for u, v in zip(model.coef + [model.intercept], true_beta + [true_b])]
    check("recovers known coefficients within 0.05", max(errs) < 0.05,
          f"coef={model.coef} intercept={model.intercept}")
    check("converged", model.converged, str(model.n_iter))
    again = BinaryLogisticRegression(l2=1e-6).fit(X, y)
    check("deterministic", again.coef == model.coef and again.intercept == model.intercept)

    Xs, ys = X[:3000], y[:3000]
    norms = []
    for l2 in (0.01, 10.0, 1000.0, 100000.0):
        m = BinaryLogisticRegression(l2=l2).fit(Xs, ys)
        norms.append(math.sqrt(sum(c * c for c in m.coef)))
    check("L2 norm strictly shrinks as the penalty grows",
          all(norms[i] > norms[i + 1] for i in range(len(norms) - 1)), str(norms))

    huge = BinaryLogisticRegression(l2=1e9).fit(Xs, ys)
    rate = sum(ys) / len(ys)
    check("intercept is unpenalised: huge l2 leaves intercept at logit(base rate)",
          abs(huge.intercept - math.log(rate / (1 - rate))) < 0.01 and max(abs(c) for c in huge.coef) < 1e-3,
          f"intercept={huge.intercept} logit={math.log(rate / (1 - rate))}")

    Xi, yi = _logistic_data(4000, [1.0], -3.0, seed=8)   # ~7% positives
    plain = BinaryLogisticRegression(l2=1.0).fit(Xi, yi)
    balanced = BinaryLogisticRegression(l2=1.0, class_weight="balanced").fit(Xi, yi)
    manual = BinaryLogisticRegression(l2=1.0, class_weight={0: 1.0, 1: 5.0}).fit(Xi, yi)
    check("balanced class weights raise the intercept on rare positives",
          balanced.intercept > plain.intercept + 1.0, f"{plain.intercept} -> {balanced.intercept}")
    check("explicit class weights move the intercept too", manual.intercept > plain.intercept)

    probs = model.predict_proba(X[:5])
    check("predict_proba = sigmoid(decision_function)",
          all(abs(p - sigmoid(z)) < 1e-15 for p, z in zip(probs, model.decision_function(X[:5]))))
    rt = BinaryLogisticRegression.from_dict(json.loads(json.dumps(model.to_dict())))
    check("to_dict/from_dict round trip predicts identically", rt.predict_proba(X[:50]) == model.predict_proba(X[:50]))
    check("one-class training data refused",
          raises(ValueError, BinaryLogisticRegression().fit, [[0.0], [1.0]], [1, 1]) is not None)
    check("label 2 refused", raises(ValueError, BinaryLogisticRegression().fit, [[0.0], [1.0]], [0, 2]) is not None)
    check("unfitted model refuses to predict",
          raises(RuntimeError, BinaryLogisticRegression().predict_proba, [[0.0]]) is not None)

    print("\n[PlattCalibrator]")
    Xa, ya = _logistic_data(9000, [1.2], -2.5, seed=9)
    train, val, test = (Xa[:3000], ya[:3000]), (Xa[3000:6000], ya[3000:6000]), (Xa[6000:], ya[6000:])
    skewed = BinaryLogisticRegression(l2=1.0, class_weight="balanced").fit(*train)
    raw_ece = ece(test[1], skewed.predict_proba(test[0]))
    platt = PlattCalibrator().fit(skewed.decision_function(val[0]), val[1])
    cal_ece = ece(test[1], platt.transform(skewed.decision_function(test[0])))
    check("Platt lowers held-out ECE of a class-weighted model", cal_ece < raw_ece * 0.5,
          f"raw={raw_ece:.4f} calibrated={cal_ece:.4f}")
    prt = PlattCalibrator.from_dict(json.loads(json.dumps(platt.to_dict())))
    check("Platt round trip", prt.transform([-1.0, 0.0, 2.0]) == platt.transform([-1.0, 0.0, 2.0]))
    check("constant scores refused", raises(ValueError, PlattCalibrator().fit, [0.5] * 10, [0, 1] * 5) is not None)


# --------------------------------------------------------------------------- multinomial
def section_multinomial():
    print("\n[MultinomialLogisticRegression]")
    rng = make_rng(3, "multinomial-test")
    signal = {"a": 1, "b": 2, "c": 3}
    X, y = [], []
    for i in range(60):
        label = "abc"[i % 3]
        x = {signal[label]: 1.0, 10: rng.random() * 0.2, 11 + (i % 5): 0.3}
        norm = math.sqrt(sum(v * v for v in x.values()))
        X.append({k: v / norm for k, v in x.items()})
        y.append(label)
    Xv, yv = X[::4], y[::4]
    model = MultinomialLogisticRegression(["a", "b", "c"], l2=1e-3, max_epochs=80, lr=0.5, patience=5)
    model.fit(X, y, X_val=Xv, y_val=yv)

    def argmax(p):
        return max(sorted(p), key=lambda k: p[k])

    acc = sum(argmax(model.predict_proba(x)) == t for x, t in zip(X, y)) / len(X)
    check("3-class toy problem: training accuracy 1.0", acc == 1.0, str(acc))
    p = model.predict_proba(X[0])
    check("probabilities sum to 1", abs(sum(p.values()) - 1.0) < 1e-12)
    d = model.to_dict()
    check("artifact stores touched indices only", set(d["weights"]) <= {"1", "2", "3", "10", "11", "12", "13", "14", "15"},
          str(sorted(d["weights"])))
    check("never-seen index 999 has zero weight",
          "999" not in d["weights"] and model.predict_proba({999: 1.0}) == model.predict_proba({}))
    check("early stopping restored the best validation epoch",
          model.val_losses and abs(model.validation_loss(Xv, yv) - min(model.val_losses)) < 1e-12,
          f"{model.validation_loss(Xv, yv)} vs {min(model.val_losses) if model.val_losses else None}")

    # Early stopping must actually STOP and RESTORE: a validation set whose labels
    # contradict training gets worse with every epoch, so the best epoch is the
    # first, training stops `patience` epochs later, and epoch-1 weights come back.
    Xc = [{1: 1.0}, {2: 1.0}] * 10
    yc = ["a", "b"] * 10
    contrary = MultinomialLogisticRegression(["a", "b"], l2=0.0, max_epochs=50, lr=0.5, patience=3)
    contrary.fit(Xc, yc, X_val=[{1: 1.0}, {2: 1.0}], y_val=["b", "a"])
    check("contradicting validation: stopped early after patience epochs",
          contrary.stopped_early and contrary.best_epoch == 1 and contrary.epochs_run == 4,
          f"stopped={contrary.stopped_early} best={contrary.best_epoch} run={contrary.epochs_run}")
    check("contradicting validation: the epoch-1 parameters were restored",
          abs(contrary.validation_loss([{1: 1.0}, {2: 1.0}], ["b", "a"]) - contrary.val_losses[0]) < 1e-12
          and contrary.val_losses[-1] > contrary.val_losses[0],
          str(contrary.val_losses))

    greedy = MultinomialLogisticRegression(["a", "b", "c"], l2=1e-3, max_epochs=15, lr=200.0, patience=5)
    greedy.fit(X, y)
    losses = greedy.train_losses
    check("a far-too-large lr is halved instead of diverging (training loss never rises)",
          greedy.final_lr < 200.0 and all(losses[i + 1] <= losses[i] + 1e-12 for i in range(len(losses) - 1))
          and all(math.isfinite(v) for v in losses),
          f"final_lr={greedy.final_lr} losses={losses[:4]}")
    check("without validation it runs max_epochs and reports the last epoch as best",
          greedy.epochs_run == 15 and greedy.best_epoch == 15 and not greedy.stopped_early)

    twin = MultinomialLogisticRegression(["a", "b", "c"], l2=1e-3, max_epochs=80, lr=0.5, patience=5)
    twin.fit(X, y, X_val=Xv, y_val=yv)
    check("deterministic", twin.to_dict() == d)
    rt = MultinomialLogisticRegression.from_dict(json.loads(json.dumps(d)))
    check("round trip predicts identically", rt.predict_proba(X[5]) == model.predict_proba(X[5]))
    check("unknown label refused",
          raises(ValueError, MultinomialLogisticRegression(["a", "b"]).fit, [{1: 1.0}], ["z"]) is not None)
    check("negative sample weight refused",
          raises(ValueError, MultinomialLogisticRegression(["a", "b"]).fit, [{1: 1.0}, {2: 1.0}], ["a", "b"],
                 sample_weight=[1.0, -1.0]) is not None)
    check("X_val without y_val refused",
          raises(ValueError, MultinomialLogisticRegression(["a", "b"]).fit, [{1: 1.0}, {2: 1.0}], ["a", "b"],
                 X_val=[{1: 1.0}]) is not None)


# --------------------------------------------------------------------------- robust
def section_robust():
    print("\n[RobustBaseline]")
    rng = make_rng(4, "robust-test")
    clean = [10.0 + rng.gauss(0.0, 1.0) for _ in range(80)]
    dirty = clean + [1000.0] * 20
    rb = RobustBaseline.fit({"temp": dirty}, mad_floor=1e-6)
    z = rb.zscores({"temp": 10.5})
    check("20% outliers do not drag the centre: z(10.5) is small", abs(z["temp"]) < 1.5, str(z))
    check("an outlier-sized reading still stands out", rb.zscores({"temp": 1000.0})["temp"] > 50)

    flat = RobustBaseline.fit({"count": [5.0] * 50}, mad_floor=0.5)
    check("MAD 0 uses the floor: z(6) = 1 / (1.4826 * 0.5)",
          flat.zscores({"count": 6.0})["count"] == 1.0 / (1.4826 * 0.5))
    per_signal = RobustBaseline.fit({"x": [1.0] * 10, "y": [2.0] * 10}, mad_floor={"x": 1.0, "y": 2.0})
    zz = per_signal.zscores({"x": 2.0, "y": 4.0})
    check("per-signal floors", zz["x"] == 1.0 / 1.4826 and zz["y"] == 2.0 / (1.4826 * 2.0), str(zz))
    fn = RobustBaseline.fit({"x": [100.0] * 10}, mad_floor=lambda name, med, vals: max(0.01 * abs(med), 1e-6))
    check("callable floor sees the median", fn.zscores({"x": 101.0})["x"] == 1.0 / 1.4826)
    check("signals unknown to the baseline are not scored", rb.zscores({"other": 1.0}) == {})
    check("zero floor refused", raises(ValueError, RobustBaseline.fit, {"x": [1.0, 2.0]}, mad_floor=0.0) is not None)
    check("empty signal refused", raises(ValueError, RobustBaseline.fit, {"x": []}, mad_floor=1.0) is not None)

    print("\n[ShrunkCovariance]")
    rows = [[1.0, 2.0, 0.5, -1.0, 3.0], [2.0, 1.0, 0.0, 0.0, 2.5], [0.5, 1.5, 1.0, -0.5, 3.5]]
    sample_cov_singular = True
    try:
        mean = [sum(c) / 3 for c in zip(*rows)]
        cov = [[sum((r[i] - mean[i]) * (r[j] - mean[j]) for r in rows) / 3 for j in range(5)] for i in range(5)]
        linalg.invert(cov)
        sample_cov_singular = False
    except linalg.SingularMatrix:
        pass
    check("the plain sample covariance (k=5 > n=3) is singular", sample_cov_singular)
    sc = ShrunkCovariance.fit(rows)
    d2 = sc.mahalanobis2([1.0, 1.0, 1.0, 1.0, 1.0])
    check("shrunk covariance inverts anyway and gives a finite distance", math.isfinite(d2) and d2 >= 0, str(d2))
    check("shrinkage in (0, 1]", 0.0 < sc.shrinkage <= 1.0, str(sc.shrinkage))
    check("distance of the mean is 0", sc.mahalanobis2(sc.mean) == 0.0)
    big = [[rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 1)] for _ in range(2000)]
    sb = ShrunkCovariance.fit(big)
    check("isotropic data: a 1-sd step along an axis is ~1", abs(sb.mahalanobis2([1.0, 0.0, 0.0]) - 1.0) < 0.15,
          str(sb.mahalanobis2([1.0, 0.0, 0.0])))
    check("zero-variance data refused", raises(ValueError, ShrunkCovariance.fit, [[1.0, 1.0]] * 5) is not None)

    print("\n[EmpiricalCalibrator]")
    cal = EmpiricalCalibrator.fit([1.0, 2.0, 2.0, 3.0])
    check("n", cal.n == 4)
    check("mid-rank on ties: score(2) = (1 + 0.5*2) / 5 = 0.4", cal.score(2.0) == 0.4)
    check("below the minimum -> 0", cal.score(-100.0) == 0.0)
    check("above the maximum -> n/(n+1)", cal.score(1e9) == 4 / 5)
    grid = [x / 10 for x in range(-10, 50)]
    scores = [cal.score(g) for g in grid]
    check("monotonic non-decreasing", all(scores[i] <= scores[i + 1] for i in range(len(scores) - 1)))
    check("NaN refused", raises(ValueError, cal.score, float("nan")) is not None)
    check("empty calibration refused", raises(ValueError, EmpiricalCalibrator.fit, []) is not None)


# --------------------------------------------------------------------------- split
def _samples():
    days = [d for d in range(120, 400, 7)] + [232, 233, 292, 293]
    return [{"machine": f"m{m:02d}", "day": d} for m in range(40) for d in sorted(set(days))]


SPLIT = dict(test_frac=0.25, val_frac=0.15, seed=20260917, train_end=284,
             val_start=240, test_start=300, horizon=7, gap=8)


def section_split():
    print("\n[entity_time_split]")
    samples = _samples()
    train, val, test = entity_time_split(samples, "machine", "day", **SPLIT)
    te, ve, se = ({r["machine"] for r in part} for part in (train, val, test))
    check("entity sets pairwise disjoint", not (te & ve) and not (te & se) and not (ve & se))
    check("fractions are of the whole fleet: 10 test, 6 val, 24 train entities",
          (len(se), len(ve), len(te)) == (10, 6, 24), str((len(se), len(ve), len(te))))
    check("train label windows end before val_start (t + 7 < 240)", all(r["day"] + 7 < 240 for r in train))
    check("val rows in [240, 300 - 7)", all(240 <= r["day"] and r["day"] + 7 < 300 for r in val))
    check("test rows t >= 300", all(r["day"] >= 300 for r in test))
    train_days = {r["day"] for r in train}
    check("boundary: t=232 (label ends 239) is kept in train", 232 in train_days)
    check("boundary: t=233 (label reaches 240) is excluded from train", 233 not in train_days)
    val_days = {r["day"] for r in val}
    check("boundary: t=292 kept in val, t=293 (label reaches 300) excluded", 292 in val_days and 293 not in val_days)
    again = entity_time_split(samples, "machine", "day", **SPLIT)
    check("deterministic under its seed", again == (train, val, test))
    other = entity_time_split(samples, "machine", "day", **dict(SPLIT, seed=1))
    check("a different seed partitions entities differently",
          {r["machine"] for r in other[2]} != se)
    check("callable keys work like string keys",
          entity_time_split(samples, lambda r: r["machine"], lambda r: r["day"], **SPLIT) == (train, val, test))

    refit = refit_rows(train, val, "day", train_end=284)
    check("refit rows = train + val rows with t <= train_end",
          all(r["day"] <= 284 for r in refit) and len(refit) == len(train) + sum(r["day"] <= 284 for r in val))

    e = raises(SplitError, entity_time_split, samples, "machine", "day", **dict(SPLIT, train_end=286))
    check("gap too small (300 < 286 + 7 + 8) refused", e is not None)
    e = raises(SplitError, entity_time_split, samples, "machine", "day", **dict(SPLIT, test_start=10_000, train_end=100))
    check("an empty test split refused", e is not None)
    e = raises(SplitError, entity_time_split, samples[:2], "machine", "day", **SPLIT)
    check("too few entities to fill three splits refused", e is not None)

    print("\n[verify_entity_time_split refuses leaky splits]")
    kw = dict(train_end=284, val_start=240, test_start=300, horizon=7, gap=8)
    ok_train = [{"machine": "a", "day": 200}]
    ok_val = [{"machine": "b", "day": 250}]
    ok_test = [{"machine": "c", "day": 310}]
    check("a clean hand-built split passes",
          raises(SplitError, verify_entity_time_split, ok_train, ok_val, ok_test, "machine", "day", **kw) is None)
    check("entity overlap train/test refused",
          raises(SplitError, verify_entity_time_split, ok_train, ok_val, [{"machine": "a", "day": 310}],
                 "machine", "day", **kw) is not None)
    check("entity overlap train/val refused",
          raises(SplitError, verify_entity_time_split, ok_train, [{"machine": "a", "day": 250}], ok_test,
                 "machine", "day", **kw) is not None)
    check("train label window crossing val_start refused (t=233)",
          raises(SplitError, verify_entity_time_split, [{"machine": "a", "day": 233}], ok_val, ok_test,
                 "machine", "day", **kw) is not None)
    check("val label window crossing test_start refused (t=293)",
          raises(SplitError, verify_entity_time_split, ok_train, [{"machine": "b", "day": 293}], ok_test,
                 "machine", "day", **kw) is not None)
    check("test row before test_start refused",
          raises(SplitError, verify_entity_time_split, ok_train, ok_val, [{"machine": "c", "day": 299}],
                 "machine", "day", **kw) is not None)
    check("empty split refused",
          raises(SplitError, verify_entity_time_split, ok_train, [], ok_test, "machine", "day", **kw) is not None)

    print("\n[group_split]")
    items = [{"family": f"f{i % 20}", "text": str(i)} for i in range(200)]
    tr, va, ts = group_split(items, "family", test_frac=0.15, val_frac=0.15, seed=5)
    fam = [{r["family"] for r in part} for part in (tr, va, ts)]
    check("families disjoint across splits", not (fam[0] & fam[1]) and not (fam[0] & fam[2]) and not (fam[1] & fam[2]))
    check("every item assigned exactly once", len(tr) + len(va) + len(ts) == 200)
    check("3 test / 3 val / 14 train families", [len(f) for f in fam] == [14, 3, 3], str([len(f) for f in fam]))
    check("deterministic", group_split(items, "family", test_frac=0.15, val_frac=0.15, seed=5) == (tr, va, ts))
    check("too few groups refused",
          raises(SplitError, group_split, items[:2], "family", test_frac=0.15, val_frac=0.15, seed=5) is not None)


def main():
    print("=" * 74)
    print("AMP-native AI core: learners, features, baselines, splits")
    print("=" * 74)
    section_rng()
    section_cross_process_hashing()
    section_text()
    section_linalg()
    section_standardizer()
    section_logistic()
    section_multinomial()
    section_robust()
    section_split()
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


def test_amp_ai_core_models():
    """pytest entry point; CI runs this file as a standalone script."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
