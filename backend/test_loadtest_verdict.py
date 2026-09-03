"""The load harness must not report a busy laptop as an application regression.

WHY THIS EXISTS
---------------
`loadtest.py` was run on 2026-09-03 against code identical to #508's and every
endpoint came back ~1.5x slower. That reads as a severe regression. It was not
one: the client floor had gone 7.3 -> 12.0 ms and MQTT ingest 41,347 -> 28,485
msg/s, and NEITHER of those touches AMP's request path. The machine was busier.
Run again on an idle machine, the same code produced #508's numbers to within
1.16x on the normalised column while raw milliseconds swung by 1.78x.

So the harness now divides every p50 by that run's own client floor and states a
verdict. This file pins that verdict, because a comparison that can only ever say
"no regression" is worse than no comparison at all -- it launders a real slowdown
as noise.

WHAT IS ASSERTED
----------------
    1  uniform machine drift            -> NOT a regression
    2  one endpoint genuinely slower    -> IS a regression, and named
    3  int-keyed run vs str-keyed JSON  -> still compared, not silently skipped
    4  merging                          -> a narrow run cannot destroy wide data
    5  a p50 at the client floor        -> reported as "not measured"

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_loadtest_verdict.py
"""
import io
import json
import os
import sys
import tempfile
import types
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# `loadtest.py` imports `requests` to drive HTTP. That is a convenience of the
# operator's machine, NOT a declared backend dependency — it is absent from
# requirements.txt, so CI does not install it and a bare `import loadtest` here
# would fail the suite for a reason unrelated to what it tests. The verdict
# logic under test does no I/O at all, so a stub is sufficient and keeps this
# suite runnable in the same places every other suite runs.
try:
    import requests  # noqa: F401
except ImportError:  # pragma: no cover - depends on the environment
    sys.modules["requests"] = types.ModuleType("requests")

import loadtest

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def run(floor, endpoints, measured="2026-09-03 10:00 UTC"):
    """One scale's worth of results. `endpoints` maps path -> p50."""
    return {"floor_ms": floor, "measured": measured,
            "http": [{"path": p, "reqs": 100, "rps": 10.0, "p50": v,
                      "p95": v * 2, "p99": v * 3, "errors": 0, "codes": {"200": 100}}
                     for p, v in endpoints.items()]}


def verdict_text(baseline, results):
    buf = io.StringIO()
    with redirect_stdout(buf):
        loadtest.verdict(baseline, results)
    return buf.getvalue()


# The shape of a real run: two cheap endpoints and one expensive one.
FAST = {"/machines": 24.0, "/work-orders": 27.0, "/analytics/summary": 93.0}


def main():
    print("=" * 74)
    print("1. A UNIFORM SLOWDOWN IS THE MACHINE, NOT THE APPLICATION")
    print("=" * 74)
    # Everything 1.6x slower INCLUDING the floor -- exactly what 2026-09-03 saw.
    base = {"10": run(7.3, FAST)}
    busy = {"10": run(12.0, {p: v * 1.64 for p, v in FAST.items()})}
    out = verdict_text(base, busy)
    check("uniform drift is NOT called a regression", "REGRESSED" not in out, out)
    check("...and the verdict says so explicitly", "NO REGRESSION" in out, out)
    check("...naming the machine as the cause",
          "MACHINE being slower" in out, out)
    check("...while still surfacing the raw number it is correcting",
          "raw p50 change" in out and "1.6" in out, out)

    print()
    print("=" * 74)
    print("2. A REAL REGRESSION IS STILL CAUGHT, AND NAMED")
    print("=" * 74)
    # Same floor, one endpoint doubled. Nothing may explain this away.
    slow = {"10": run(7.3, dict(FAST, **{"/work-orders": 54.0}))}
    out = verdict_text(base, slow)
    check("a doubled endpoint IS reported", "REGRESSED" in out, out)
    check("...and the culprit is named", "/work-orders" in out.split("REGRESSED")[1], out)
    check("...and the innocent endpoints are not",
          "/machines" not in out.split("REGRESSED")[1], out)

    # The important boundary: a regression HIDDEN inside machine drift. The
    # floor moved 1.64x and so did two endpoints, but the third moved 3.3x.
    sneaky = {"10": run(12.0, {"/machines": 24.0 * 1.64,
                               "/work-orders": 27.0 * 1.64,
                               "/analytics/summary": 93.0 * 3.3})}
    out = verdict_text(base, sneaky)
    check("a regression HIDDEN under machine drift is still caught",
          "REGRESSED" in out and "/analytics/summary" in out.split("REGRESSED")[1], out)
    check("...and the two endpoints that only drifted are not blamed",
          "/machines" not in out.split("REGRESSED")[1], out)

    print()
    print("=" * 74)
    print("3. INT-KEYED RUN vs STR-KEYED BASELINE")
    print("=" * 74)
    # In-process results are keyed by int; JSON keys are strings. Unhandled,
    # the intersection is empty and every run reports "no baseline" forever --
    # a comparison that silently never happens.
    out = verdict_text(base, {10: run(12.0, {p: v * 1.64 for p, v in FAST.items()})})
    check("an int-keyed run is compared against a str-keyed baseline",
          "no comparable baseline" not in out, out)
    check("...and produces the same verdict as the str-keyed one",
          "NO REGRESSION" in out, out)

    print()
    print("=" * 74)
    print("4. GROWTH WITH FACTORY SIZE, AND ITS PROVENANCE")
    print("=" * 74)
    same = "2026-09-03 10:00 UTC"
    wide = {"10": run(7.3, FAST, same),
            "1000": run(7.3, {"/machines": 189.0, "/work-orders": 51.0,
                              "/analytics/summary": 492.0}, same)}
    out = verdict_text({}, wide)
    check("an endpoint that grows 7.8x with size is reported",
          "SCALES WITH FACTORY SIZE" in out and "/machines" in out, out)
    check("...one that stays flat is not",
          "/work-orders" not in out.split("SCALES WITH FACTORY SIZE")[1].split("Query COUNT")[0],
          out)
    check("...and same-run scales carry NO provenance caution",
          "CAUTION" not in out, out)

    mixed = {"10": run(7.3, FAST, "2026-08-27 09:00 UTC"),
             "1000": run(7.3, {"/machines": 189.0, "/work-orders": 51.0,
                               "/analytics/summary": 492.0}, "2026-09-03 22:00 UTC")}
    out = verdict_text({}, mixed)
    check("scales from DIFFERENT runs are flagged as not comparable",
          "CAUTION" in out, out)

    print()
    print("=" * 74)
    print("5. A LATENCY AT THE CLIENT FLOOR MEASURED NOTHING")
    print("=" * 74)
    out = verdict_text({}, {"10": run(12.0, {"/machines": 13.0})})
    check("a p50 at 1.1x the floor is reported as NOT MEASURED",
          "NOT MEASURED" in out, out)
    out = verdict_text({}, {"10": run(7.3, FAST)})
    check("...and a p50 at 3.3x the floor is not",
          "NOT MEASURED" not in out, out)

    print()
    print("=" * 74)
    print("6. A NARROW RUN MUST NOT DESTROY A WIDE RESULTS FILE")
    print("=" * 74)
    # The bug this closes: `python loadtest.py 10 50` wrote a two-scale file
    # over the four-scale one from #508, deleting the only evidence in the repo
    # that latency grows with factory size.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "loadtest_results.json")
        four = {s: run(7.3, FAST) for s in ("10", "50", "250", "1000")}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(four, fh)

        real_here = loadtest.HERE
        try:
            loadtest.HERE = tmp
            loaded = loadtest.load_baseline()
            check("the baseline is read before it is overwritten",
                  sorted(loaded, key=int) == ["10", "50", "250", "1000"],
                  str(sorted(loaded)))
        finally:
            loadtest.HERE = real_here

    # THE SHIPPED merge function, not a copy of it. An earlier draft of this
    # suite re-implemented the merge inline, and therefore proved nothing about
    # the code that actually runs.
    narrow = {10: run(9.0, FAST)}
    merged, kept = loadtest.merge_results(loaded, narrow, "2026-09-03 22:00 UTC")
    check("re-running ONE scale keeps the other three",
          sorted(merged, key=int) == ["10", "50", "250", "1000"], str(sorted(merged)))
    check("...and the re-run scale is the NEW measurement",
          merged["10"]["floor_ms"] == 9.0, str(merged["10"]["floor_ms"]))
    check("...and the untouched scales are the OLD ones",
          merged["250"]["floor_ms"] == 7.3, str(merged["250"]["floor_ms"]))
    check("...and the run reports which scales it did not re-measure",
          kept == ["50", "250", "1000"], str(kept))
    check("...stamping only what this run measured",
          merged["10"]["measured"] == "2026-09-03 22:00 UTC"
          and merged["250"]["measured"] != "2026-09-03 22:00 UTC",
          f"{merged['10']['measured']!r} / {merged['250']['measured']!r}")
    # Not "the key is absent" -- `run()` always sets one. The property that
    # matters is that stamping the merged copy did not reach back and rewrite
    # the caller's own dict, which a plain `v["measured"] = stamp` would do.
    check("...without mutating the caller's results dict",
          narrow[10]["measured"] == "2026-09-03 10:00 UTC",
          f"caller's dict was rewritten to {narrow[10]['measured']!r}")

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


if __name__ == "__main__":
    raise SystemExit(main())
