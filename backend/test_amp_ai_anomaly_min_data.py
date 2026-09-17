"""Telemetry anomaly: the minimum-data thresholds, each pinned at exactly the threshold and one below.

WHY
---
A baseline fitted on too little history is not a baseline; a score calibrated
on a handful of windows cannot mean "rarer than 99% of this machine's normal
hours". Below any threshold the scorer must say so - status
``insufficient_history``, score ``null``, and what it NEEDS against what it
HAS - instead of returning a number that looks like every other number.

  fit buckets per signal   >= 288   one day of 5-minute buckets in the fit period
  distinct days            >= 3     daily cycles seen more than once
  calibration buckets      >= 100   buckets with a scorable reading in the calibration period
  scorable signals         >= 1     a signal present in the score window with a baseline for its state

Each threshold is tested at T (scores) and T - 1 (refuses), so moving a
constant or turning ``>=`` into ``>`` fails a check (see mutate_amp_ai_anomaly.py).

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_anomaly_min_data.py
"""
from amp_ai.core.rng import make_rng
from amp_ai.telemetry_anomaly import baseline as B
from amp_ai.telemetry_anomaly import series as SR

failures = []

NEEDED = {"fit_buckets_per_signal": 288, "distinct_days": 3, "calibration_buckets": 100, "scorable_signals": 1}


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def buckets(ks, *, seed=1, values=None, state=SR.STATE_RUNNING):
    rng = make_rng(seed, "min-data")
    out = []
    for k in ks:
        v = values(k) if values else {"a": 100.0 + rng.gauss(0.0, 2.0)}
        out.append(SR.Bucket(k, state, v))
    return out


def score(signals=("a",), state=SR.STATE_RUNNING):
    return [SR.Bucket(k, state, {s: 100.0 for s in signals}) for k in range(SR.SCORE_BUCKETS)]


def spaced(n):
    """n buckets two apart from the newest baseline bucket: spans three distinct days for n >= 283."""
    return [SR.SCORE_BUCKETS + 2 * i for i in range(n)]


def expect_ok(label, result):
    check(label, result["status"] == "ok" and isinstance(result["score"], float),
          f"{result['status']} have={result.get('have')}")
    # a score is at most n / (n + 1): at every threshold the calibration must be able to express 0.99
    check(f"{label}: >= 99 calibration windows, so the score can reach 0.99",
          result.get("calibration_windows", 0) >= 99, str(result.get("calibration_windows")))


def expect_refused(label, result, key, have):
    check(label, result["status"] == "insufficient_history" and result["score"] is None
          and result["needed"] == NEEDED and result["have"].get(key) == have,
          f"{result['status']} needed={result.get('needed')} have={result.get('have')}")


def section_constants():
    print("\n[the thresholds]")
    check("constants are the documented values",
          (B.MIN_FIT_BUCKETS_PER_SIGNAL, B.MIN_DISTINCT_DAYS, B.MIN_CALIBRATION_BUCKETS, B.MIN_SCORABLE_SIGNALS)
          == (288, 3, 100, 1))
    check("fit fraction is 7/10 in integer arithmetic", (B.FIT_NUMERATOR, B.FIT_DENOMINATOR) == (7, 10))


def section_fit_buckets():
    print("\n[fit buckets per signal: 288 scores, 287 refuses]")
    n_at, n_below = 412, 411   # 412 * 7 // 10 == 288; 411 * 7 // 10 == 287
    at = B.assess(buckets(spaced(n_at)), score())
    expect_ok("288 fit buckets for the signal scores", at)
    check("have reports the 288", at["have"]["fit_buckets_per_signal"] == 288, str(at["have"]))
    expect_refused("287 fit buckets refuses", B.assess(buckets(spaced(n_below)), score()), "fit_buckets_per_signal", 287)


def section_days():
    print("\n[distinct days: 3 scores, 2 refuses]")
    contiguous_two_days = list(range(SR.SCORE_BUCKETS, 2 * SR.BUCKETS_PER_DAY))            # k 12..575: days 0-1
    below = B.assess(buckets(contiguous_two_days), score())
    expect_refused("data on 2 distinct days refuses (even with 394 fit buckets)", below, "distinct_days", 2)
    at = B.assess(buckets(contiguous_two_days + [2 * SR.BUCKETS_PER_DAY]), score())          # adds day 2
    expect_ok("one bucket on a third day scores", at)
    check("have reports 3 days", at["have"]["distinct_days"] == 3, str(at["have"]))


def section_calibration():
    print("\n[calibration buckets: 100 scores, 99 refuses]")
    ks = spaced(412)           # 288 fit + 124 calibration buckets
    newest = sorted(ks)

    def only_x_for(n_newest):
        unscorable = set(newest[:n_newest])
        rng = make_rng(2, "cal")
        return lambda k: {"x": 5.0} if k in unscorable else {"a": 100.0 + rng.gauss(0.0, 2.0)}

    at = B.assess(buckets(ks, values=only_x_for(24)), score())
    expect_ok("100 calibration buckets with a scorable reading scores", at)
    check("have reports 100", at["have"]["calibration_buckets"] == 100, str(at["have"]))
    expect_refused("99 refuses: buckets holding only an unscorable signal do not count",
                   B.assess(buckets(ks, values=only_x_for(25)), score()), "calibration_buckets", 99)


def section_scorable():
    print("\n[scorable signals: 1 scores, 0 refuses]")
    base = buckets(spaced(412))
    expect_ok("a score window with the fitted signal scores", B.assess(base, score(("a",))))
    expect_refused("a score window with only a signal the baseline never saw refuses",
                   B.assess(base, score(("x",))), "scorable_signals", 0)
    expect_refused("a score window in a state the baseline never saw refuses",
                   B.assess(base, score(("a",), state=SR.STATE_NOT_RUNNING)), "scorable_signals", 0)
    expect_refused("an empty score window refuses", B.assess(base, []), "scorable_signals", 0)
    r = B.assess(base, [])
    check("a refusal names no deviating signal and no method score",
          r["deviating"] == [] and r["score_resolution"] is None, str(r))


SECTIONS = [section_constants, section_fit_buckets, section_days, section_calibration, section_scorable]


def main():
    print("=" * 74)
    print("Telemetry anomaly: minimum data")
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


def test_amp_ai_anomaly_min_data():
    """pytest entry point; CI runs this file as a standalone script."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
