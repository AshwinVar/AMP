"""Telemetry anomaly: bucketing, per-state robust baselines and the calibrated window score.

WHAT IS PINNED HERE
-------------------
* 5-minute buckets are half-open and counted back from the anchor (the end of
  the score window): a reading exactly one hour before the anchor is in the
  SCORE window's oldest bucket, one microsecond earlier is baseline.
* Each bucket's state is the machine state at the bucket's START (the last
  status event at or before it), and baselines are fitted per state.
* The baseline is fitted on the OLDEST 70% of the baseline buckets and the
  score is calibrated on the NEWEST 30%; the score window is in neither. An
  anomaly in the score window cannot move the fitted medians, the MADs or the
  calibration sample (mutation-tested).
* A signal whose MAD is 0 (integer sensors, idle machines) uses the floor
  max(1% of |median|, quantisation step, 1e-6) instead of dividing by ~0.
* A clean window scores low, a 10-MAD spike scores >= 0.99 and names the right
  signal, the score is monotonic in the size of a spike, and a correlation
  break that no single signal shows is caught by the Mahalanobis term.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_anomaly_baseline.py
"""
import math

from amp_ai.core.rng import make_rng
from amp_ai.core.robust import MAD_SCALE, RobustBaseline, mad, median
from amp_ai.telemetry_anomaly import baseline as B
from amp_ai.telemetry_anomaly import series as SR

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception:  # noqa: BLE001 - a different exception is a failure too
        return False
    return False


# --------------------------------------------------------------------------- fixtures
def baseline_buckets(n=SR.TOTAL_BUCKETS - SR.SCORE_BUCKETS, *, seed=1, signals=("a", "b", "c"), center=100.0,
                     sd=2.0, state=SR.STATE_RUNNING, first_k=SR.SCORE_BUCKETS):
    rng = make_rng(seed, "baseline-fixture")
    out = []
    for k in range(first_k, first_k + n):
        st = state(k) if callable(state) else state
        out.append(SR.Bucket(k, st, {s: center + rng.gauss(0.0, sd) for s in signals}))
    return out


def score_window(values_for_k, state=SR.STATE_RUNNING):
    return [SR.Bucket(k, state(k) if callable(state) else state, dict(values_for_k(k))) for k in range(SR.SCORE_BUCKETS)]


def fitted_center(buckets, signal, state=SR.STATE_RUNNING):
    fit, _ = B.split_fit_calibration(buckets)
    return median([b.values[signal] for b in fit if b.state == state and signal in b.values])


def fitted_mad(buckets, signal, state=SR.STATE_RUNNING):
    fit, _ = B.split_fit_calibration(buckets)
    return mad([b.values[signal] for b in fit if b.state == state and signal in b.values])


# --------------------------------------------------------------------------- series
def section_series():
    print("\n[series: half-open 5-minute buckets counted back from the anchor]")
    us = SR.BUCKET_MICROSECONDS
    check("bucket length is 5 minutes", us == 300 * 1_000_000 and SR.BUCKET_SECONDS == 300)
    check("1 microsecond before the anchor is bucket 0", SR.bucket_index(1) == 0)
    check("exactly 5 minutes before the anchor is still bucket 0", SR.bucket_index(us) == 0)
    check("5 minutes + 1 microsecond is bucket 1", SR.bucket_index(us + 1) == 1)
    hour = 3600 * 1_000_000
    check("exactly 1 hour before the anchor is the score window's oldest bucket (11)",
          SR.bucket_index(hour) == SR.SCORE_BUCKETS - 1)
    check("1 hour + 1 microsecond is the newest baseline bucket (12)", SR.bucket_index(hour + 1) == SR.SCORE_BUCKETS)
    check("exactly 14 days before the anchor is the oldest bucket", SR.bucket_index(14 * 86400 * 1_000_000) == SR.TOTAL_BUCKETS - 1)
    check("a reading AT the anchor has no bucket", raises(ValueError, SR.bucket_index, 0))
    check("a reading after the anchor has no bucket", raises(ValueError, SR.bucket_index, -5))
    check("a float offset is refused", raises(TypeError, SR.bucket_index, 1.5))

    med = SR.bucket_medians([(1, "t", 1.0), (2, "t", 3.0), (3, "t", 100.0), (us + 1, "t", 7.0), (us + 2, "t", 9.0),
                             (4, "u", 5.0)])
    check("bucket value is the median of its readings (outlier ignored)", med[0]["t"] == 3.0)
    check("an even count takes the mean of the middle two", med[1]["t"] == 8.0)
    check("signals are bucketed independently", med[0]["u"] == 5.0 and "u" not in med[1])
    check("a non-finite reading is refused", raises(ValueError, SR.bucket_medians, [(1, "t", float("nan"))]))
    check("integer_valued_signals finds whole-number sensors",
          SR.integer_valued_signals([(1, "i", 3.0), (2, "i", 4.0), (3, "f", 3.5), (4, "f", 3.0)]) == frozenset({"i"}))

    print("\n[series: a bucket's state holds throughout it, else it is a Transition]")
    k = 3
    start = (k + 1) * us
    end = k * us
    states = SR.bucket_states([k], [(start, True)], initial_running=False)
    check("an event exactly at the bucket start applies to that bucket", states[k] == SR.STATE_RUNNING)
    states = SR.bucket_states([k], [(start - 1, True)], initial_running=False)
    check("a state change 1 microsecond after the bucket start makes it a Transition", states[k] == SR.STATE_TRANSITION)
    states = SR.bucket_states([k], [(end + 1, True)], initial_running=False)
    check("a state change 1 microsecond before the bucket end makes it a Transition", states[k] == SR.STATE_TRANSITION)
    states = SR.bucket_states([k, k - 1], [(end, True)], initial_running=False)
    check("a change exactly at the bucket end belongs to the NEXT bucket's start",
          states[k] == SR.STATE_NOT_RUNNING and states[k - 1] == SR.STATE_RUNNING, str(states))
    states = SR.bucket_states([k], [(start - 5, False), (start - 9, False)], initial_running=False)
    check("an event inside the bucket that does not change the state leaves it as it was",
          states[k] == SR.STATE_NOT_RUNNING)
    states = SR.bucket_states([k], [(start - 5, True), (start - 9, False)], initial_running=False)
    check("stopping and restarting inside one bucket is still a Transition", states[k] == SR.STATE_TRANSITION)
    states = SR.bucket_states([k], [(start - 5, None)], initial_running=True)
    check("a change to an unknown status inside the bucket is a Transition", states[k] == SR.STATE_TRANSITION)
    states = SR.bucket_states([k, 20, 40], [(start + 10, False), (start + 5, True), (30 * us, False)],
                              initial_running=None)
    check("the LAST event at or before the start wins, whatever order events arrive in",
          states[k] == SR.STATE_RUNNING and states[20] == SR.STATE_NOT_RUNNING and states[40] == SR.STATE_UNKNOWN,
          str(states))
    check("no event and no known initial state is Unknown, never guessed",
          SR.bucket_states([k], [], initial_running=None)[k] == SR.STATE_UNKNOWN)
    check("an event with an unknown status makes the state Unknown",
          SR.bucket_states([k], [(start, None)], initial_running=True)[k] == SR.STATE_UNKNOWN)

    rows = SR.build_buckets([(1, "t", 1.0), (us * 20, "t", 2.0)], [], initial_running=True,
                            first_k=0, last_k=SR.TOTAL_BUCKETS - 1)
    check("build_buckets returns buckets ordered by k with states",
          [b.k for b in rows] == [0, 19] and all(b.state == SR.STATE_RUNNING for b in rows))
    check("build_buckets refuses a reading outside the window it was queried for",
          raises(ValueError, SR.build_buckets, [(SR.SCORE_BUCKETS * us + 1, "t", 1.0)], [], True,
                 first_k=0, last_k=SR.SCORE_BUCKETS - 1))


# --------------------------------------------------------------------------- baseline
def section_windows_are_separate():
    print("\n[baseline: fit, calibration and score never overlap]")
    base = baseline_buckets(1000)
    fit, cal = B.split_fit_calibration(base)
    check("70% of data-bearing buckets fit, 30% calibrate", len(fit) == 700 and len(cal) == 300, f"{len(fit)}/{len(cal)}")
    check("fit is the OLDEST part: every fit bucket is older than every calibration bucket",
          min(b.k for b in fit) > max(b.k for b in cal))
    gappy = [b if b.k % 3 else SR.Bucket(b.k, b.state, {}) for b in base]
    fit2, cal2 = B.split_fit_calibration(gappy)
    bearing = len([b for b in base if b.k % 3])
    check("buckets with no readings do not count towards the split",
          all(b.values for b in fit2 + cal2) and len(fit2) + len(cal2) == bearing
          and len(fit2) == bearing * 7 // 10, f"{len(fit2)}+{len(cal2)} of {bearing}")
    score = score_window(lambda k: {"a": 100.0})
    check("a baseline bucket inside the score window is refused",
          raises(ValueError, B.assess, base + [SR.Bucket(3, SR.STATE_RUNNING, {"a": 1.0})], score))
    check("a score bucket outside the score window is refused",
          raises(ValueError, B.assess, base, score + [SR.Bucket(SR.SCORE_BUCKETS, SR.STATE_RUNNING, {"a": 1.0})]))
    check("a bucket older than 14 days is refused",
          raises(ValueError, B.assess, base + [SR.Bucket(SR.TOTAL_BUCKETS, SR.STATE_RUNNING, {"a": 1.0})], score))
    check("duplicate buckets are refused", raises(ValueError, B.assess, base + base[:1], score))

    print("\n[baseline: an anomaly in the score window cannot move the baseline or the calibration]")
    base = baseline_buckets()
    clean = B.assess(base, score_window(lambda k: {"a": 100.0, "b": 100.0, "c": 100.0}), diagnostics=True)
    wild = B.assess(base, score_window(lambda k: {"a": 1e4, "b": -1e4, "c": 100.0}), diagnostics=True)
    check("both windows are scored", clean["status"] == "ok" and wild["status"] == "ok")
    check("fitted medians and scales are identical", clean["diagnostics"]["fitted"] == wild["diagnostics"]["fitted"])
    check("the calibration sample is identical",
          clean["diagnostics"]["calibration_statistics"] == wild["diagnostics"]["calibration_statistics"])
    check("the calibration sample is the newest 30% of the baseline, in 1-hour rolling windows",
          clean["calibration_windows"] == len(clean["diagnostics"]["calibration_statistics"])
          and clean["diagnostics"]["calibration_k_range"][0] >= SR.SCORE_BUCKETS)
    check("score_resolution is 1 / (n + 1)", abs(clean["score_resolution"] - 1.0 / (clean["calibration_windows"] + 1)) < 1e-15)
    check("the wild window scores far higher than the clean one", wild["score"] > 0.99 > 0.9 > clean["score"])


def section_scores():
    print("\n[baseline: clean point, 10-MAD spike, monotonic]")
    base = baseline_buckets()
    centers = {s: fitted_center(base, s) for s in "abc"}
    mads = {s: fitted_mad(base, s) for s in "abc"}
    clean = B.assess(base, score_window(lambda k: centers))
    check("a window sitting exactly on the baseline scores < 0.9", clean["status"] == "ok" and clean["score"] < 0.9,
          str(clean.get("score")))
    check("a clean window names no deviating signal", clean["deviating"] == [])
    check("the method is reported", clean["method"] in ("robust_diagonal", "robust_diagonal+mahalanobis"),
          clean["method"])

    def spiked(size):
        return score_window(lambda k: dict(centers, b=centers["b"] + size * mads["b"]) if k == 5 else centers)

    spike = B.assess(base, spiked(10.0))
    check("a 10-MAD spike in one bucket scores >= 0.99", spike["score"] >= 0.99, str(spike["score"]))
    names = [d["signal"] for d in spike["deviating"]]
    check("the spike names the right signal, and only it", names == ["b"], str(names))
    d = spike["deviating"][0]
    expected_z = 10.0 * mads["b"] / (MAD_SCALE * mads["b"])
    check("the deviation reports value, median, typical range, direction and z",
          d["direction"] == "above" and abs(d["value"] - (centers["b"] + 10 * mads["b"])) < 1e-9
          and abs(d["median"] - centers["b"]) < 1e-12 and abs(d["z"] - expected_z) < 1e-9
          and d["typical_range"][0] < d["median"] < d["typical_range"][1] < d["value"]
          and abs(d["typical_range"][1] - (d["median"] + B.DEVIATION_Z * MAD_SCALE * mads["b"])) < 1e-9, str(d))
    dip = B.assess(base, score_window(lambda k: dict(centers, c=centers["c"] - 12 * mads["c"]) if k == 0 else centers))
    check("a dip is reported below", [x["direction"] for x in dip["deviating"]] == ["below"])

    scores = [B.assess(base, spiked(size))["score"] for size in (0.0, 1.0, 2.0, 4.0, 6.0, 10.0, 40.0)]
    check("the score never falls as a spike grows", all(b >= a for a, b in zip(scores, scores[1:])), str(scores))
    check("scores stay inside [0, n/(n+1)]", all(0.0 <= s < 1.0 for s in scores))
    shifted = score_window(lambda k: dict(centers, b=centers["b"] + 6.0 * mads["b"]))
    for aggregate in B.AGGREGATES:
        r = B.assess(base, shifted, aggregate=aggregate)
        check(f"the {aggregate!r} window statistic scores a sustained 6-MAD shift >= 0.99", r["score"] >= 0.99,
              str(r["score"]))
    check("an unknown window statistic is refused", raises(ValueError, B.assess, base, spiked(1.0), aggregate="max"))


def section_floor():
    print("\n[baseline: the MAD floor]")
    check("integer sensor, median 50: floor is the quantisation step 1.0", B.mad_floor(50.0, True) == 1.0)
    check("integer sensor, median 3000: floor is 1% of the median", B.mad_floor(3000.0, True) == 30.0)
    check("float sensor, median 50.5: floor is 1% of the median", abs(B.mad_floor(50.5, False) - 0.505) < 1e-12)
    check("float sensor, median 0: floor is 1e-6, never 0", B.mad_floor(0.0, False) == 1e-6)

    base = [SR.Bucket(k, SR.STATE_RUNNING, {"i": 50.0, "f": 0.0}) for k in range(SR.SCORE_BUCKETS, SR.SCORE_BUCKETS + 1000)]
    r = B.assess(base, score_window(lambda k: {"i": 52.0, "f": 0.0} if k == 2 else {"i": 50.0, "f": 0.0}),
                 integer_signals=frozenset({"i"}), diagnostics=True)
    fitted = r["diagnostics"]["fitted"][SR.STATE_RUNNING]
    check("a constant integer sensor (MAD 0) gets scale 1.4826 * 1.0", abs(fitted["i"]["scale"] - MAD_SCALE) < 1e-12,
          str(fitted["i"]))
    check("a 2-count change on it is z = 2 / 1.4826, not 2 / 0",
          r["status"] == "ok" and r["deviating"] == [] and abs(r["diagnostics"]["score_z"]["i"] - 2.0 / MAD_SCALE) < 1e-9,
          str(r.get("diagnostics", {}).get("score_z")))
    r2 = B.assess(base, score_window(lambda k: {"i": 50.0, "f": 0.001} if k == 2 else {"i": 50.0, "f": 0.0}),
                  integer_signals=frozenset({"i"}), diagnostics=True)
    check("a constant float sensor at 0 uses the 1e-6 floor (a tiny change is a huge, clipped z)",
          abs(r2["diagnostics"]["fitted"][SR.STATE_RUNNING]["f"]["scale"] - MAD_SCALE * 1e-6) < 1e-18
          and [d["signal"] for d in r2["deviating"]] == ["f"])

    values = [100.0 + ((-1) ** i) * (i % 7) for i in range(400)]
    rb = RobustBaseline.fit({"s": values}, mad_floor=lambda name, med, vals: B.mad_floor(med, False))
    loc, scale = B.robust_fitter({"s": values}, frozenset())["s"]
    check("robust_fitter gives the same z as core RobustBaseline.zscores (one formula)",
          abs((107.0 - loc) / scale - rb.zscores({"s": 107.0})["s"]) < 1e-12)


def section_states():
    print("\n[baseline: separate baselines per machine state]")
    rng = make_rng(3, "states")
    running = lambda k: SR.STATE_RUNNING if (k // 36) % 2 else SR.STATE_NOT_RUNNING
    base = [SR.Bucket(k, running(k), {"p": (50.0 if running(k) == SR.STATE_RUNNING else 1.0) + rng.gauss(0.0, 0.5)})
            for k in range(SR.SCORE_BUCKETS, SR.TOTAL_BUCKETS)]
    idle_ok = B.assess(base, score_window(lambda k: {"p": 1.0}, state=SR.STATE_NOT_RUNNING))
    check("an idle machine at its idle level scores low", idle_ok["status"] == "ok" and idle_ok["score"] < 0.9,
          str(idle_ok.get("score")))
    check("and reports its state", idle_ok["state"] == SR.STATE_NOT_RUNNING)
    run_low = B.assess(base, score_window(lambda k: {"p": 1.0}, state=SR.STATE_RUNNING))
    check("a RUNNING machine at the idle level scores high", run_low["score"] >= 0.99, str(run_low["score"]))
    mixed = B.assess(base, score_window(lambda k: {"p": 1.0 if k < 6 else 50.0},
                                        state=lambda k: SR.STATE_NOT_RUNNING if k < 6 else SR.STATE_RUNNING))
    check("a window that changes state is scored against both baselines and says 'mixed'",
          mixed["state"] == "mixed" and mixed["score"] < 0.9, f"{mixed['state']} {mixed.get('score')}")

    print("\n[baseline: Transition buckets are never fitted and never scored]")
    # every 5th bucket is a Transition whose median sits half-way between the two regimes
    with_transitions = [SR.Bucket(b.k, SR.STATE_TRANSITION, {"p": 25.0}) if b.k % 5 == 0 else b for b in base]
    r = B.assess(with_transitions, score_window(lambda k: {"p": 1.0}, state=SR.STATE_NOT_RUNNING), diagnostics=True)
    check("no baseline is fitted for the Transition state, even with hundreds of Transition buckets",
          r["status"] == "ok" and SR.STATE_TRANSITION not in r["diagnostics"]["fitted"]
          and sum(b.state == SR.STATE_TRANSITION for b in with_transitions) >= B.MIN_FIT_BUCKETS_PER_SIGNAL,
          str(r.get("diagnostics", {}).get("fitted", {}).keys()))
    only_transitions = B.assess(with_transitions, score_window(lambda k: {"p": 25.0}, state=SR.STATE_TRANSITION))
    check("a score window made only of Transition buckets is refused, not scored",
          only_transitions["status"] == "insufficient_history" and only_transitions["have"]["scorable_signals"] == 0,
          str(only_transitions))
    clean_idle = score_window(lambda k: {"p": 1.0}, state=SR.STATE_NOT_RUNNING)
    plus_wild = [SR.Bucket(b.k, SR.STATE_TRANSITION, {"p": 1e6}) if b.k == 4 else b for b in clean_idle]
    without = B.assess(with_transitions, [b for b in clean_idle if b.k != 4], diagnostics=True)
    wild = B.assess(with_transitions, plus_wild, diagnostics=True)
    check("a wild Transition bucket in the score window changes nothing: same score, nothing deviating",
          wild["score"] == without["score"] and wild["deviating"] == []
          and wild["diagnostics"]["statistic_value"] == without["diagnostics"]["statistic_value"],
          f"{wild.get('score')} vs {without.get('score')} {wild.get('deviating')}")
    check("Transition is a valid bucket state and is not a learned state",
          SR.STATE_TRANSITION in SR.STATES and SR.STATE_TRANSITION not in B.LEARNED_STATES)


def section_mahalanobis():
    print("\n[baseline: Mahalanobis catches a correlation break]")
    rng = make_rng(5, "maha")
    base = []
    for k in range(SR.SCORE_BUCKETS, SR.TOTAL_BUCKETS):
        a = 100.0 + rng.gauss(0.0, 5.0)
        base.append(SR.Bucket(k, SR.STATE_RUNNING, {"a": a, "b": a + rng.gauss(0.0, 0.3)}))
    ca, cb = fitted_center(base, "a"), fitted_center(base, "b")
    sa, sb = MAD_SCALE * fitted_mad(base, "a"), MAD_SCALE * fitted_mad(base, "b")
    broken = score_window(lambda k: {"a": ca + 1.0 * sa, "b": cb - 1.0 * sb})
    diag = B.assess(base, broken, mahalanobis=False)
    maha = B.assess(base, broken)
    check("each signal alone looks ordinary: the diagonal score is < 0.9", diag["score"] < 0.9, str(diag["score"]))
    check("the joint break scores >= 0.99 with Mahalanobis", maha["score"] >= 0.99, str(maha["score"]))
    check("the method says Mahalanobis was used", maha["method"] == "robust_diagonal+mahalanobis", maha["method"])

    def with_complete(n_complete):
        out = []
        fit, _ = B.split_fit_calibration(base)
        fit_ks = sorted((b.k for b in fit), reverse=True)
        incomplete = set(fit_ks[n_complete:])
        for b in base:
            out.append(SR.Bucket(b.k, b.state, {"a": b.values["a"]} if b.k in incomplete else dict(b.values)))
        return out

    at = B.assess(with_complete(B.MAHALANOBIS_MIN_FIT_BUCKETS), broken)
    below = B.assess(with_complete(B.MAHALANOBIS_MIN_FIT_BUCKETS - 1), broken)
    check(f"exactly {B.MAHALANOBIS_MIN_FIT_BUCKETS} complete fit buckets enables Mahalanobis",
          at["method"] == "robust_diagonal+mahalanobis", at["method"])
    check(f"{B.MAHALANOBIS_MIN_FIT_BUCKETS - 1} complete fit buckets falls back to the diagonal",
          below["method"] == "robust_diagonal", below["method"])
    single = B.assess([SR.Bucket(b.k, b.state, {"a": b.values["a"]}) for b in base], score_window(lambda k: {"a": ca}))
    check("one signal cannot have a Mahalanobis term", single["method"] == "robust_diagonal")


def section_parameters():
    print("\n[baseline: the published parameters are the constants the code runs]")
    p = B.method_parameters()
    check("parameters name every threshold", {
        "bucket_seconds", "score_buckets", "baseline_days", "fit_fraction", "z_clip", "deviation_z",
        "min_fit_buckets_per_signal", "min_distinct_days", "min_calibration_buckets", "min_scorable_signals",
        "mahalanobis_min_fit_buckets", "mahalanobis_min_signals", "mad_floor", "window_statistic"} <= set(p), str(sorted(p)))
    check("values match the module constants",
          p["min_fit_buckets_per_signal"] == B.MIN_FIT_BUCKETS_PER_SIGNAL == 288
          and p["min_distinct_days"] == B.MIN_DISTINCT_DAYS == 3
          and p["min_calibration_buckets"] == B.MIN_CALIBRATION_BUCKETS == 100
          and p["min_scorable_signals"] == B.MIN_SCORABLE_SIGNALS == 1
          and p["z_clip"] == B.Z_CLIP == 50.0 and p["deviation_z"] == B.DEVIATION_Z == 3.5
          and p["window_statistic"] == B.WINDOW_AGGREGATE and p["fit_fraction"] == 0.7
          and p["score_buckets"] * p["bucket_seconds"] == 3600 and p["baseline_days"] == 14)


SECTIONS = [section_series, section_windows_are_separate, section_scores, section_floor, section_states,
            section_mahalanobis, section_parameters]


def main():
    print("=" * 74)
    print("Telemetry anomaly: series and baseline")
    print("=" * 74)
    for section in SECTIONS:
        section()
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


def test_amp_ai_anomaly_baseline():
    """pytest entry point; CI runs this file as a standalone script."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
