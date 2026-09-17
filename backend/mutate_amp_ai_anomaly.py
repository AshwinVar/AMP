"""Mutation harness for the telemetry anomaly capability (amp_ai/telemetry_anomaly).

Each mutation is a small, plausible edit that quietly breaks a guard the
anomaly scorer relies on: the half-open bucket boundary, the score window kept
out of its own baseline and calibration, fit-before-calibrate ordering, the MAD
floors, every minimum-data threshold, the explicit tenant filter on every
query, consent before data, the pinned evaluation, the evaluation harness's
window assembly, and every term of the adoption gate and the test-set ledger.
For each one the harness applies the edit, runs the suite that should notice,
and restores the file byte for byte. A mutation that leaves its suite green
SURVIVED: that guard is untested.

RUN IT ALONE. It EDITS THE WORKING TREE and restores it afterwards; anything
reading those files meanwhile sees broken code. The committed evaluation JSON
is also snapshotted and restored after every mutation, because one mutation
disables the refusal to overwrite it.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_amp_ai_anomaly.py
Exit 0 only if every mutation applied exactly once and was caught.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

BASELINE = "test_amp_ai_anomaly_baseline.py"
MIN_DATA = "test_amp_ai_anomaly_min_data.py"
DB = "test_amp_ai_anomaly_db.py"
EVAL = "test_amp_ai_anomaly_eval.py"
PURITY = "test_amp_ai_anomaly_purity.py"

SERIES = "amp_ai/telemetry_anomaly/series.py"
BASE = "amp_ai/telemetry_anomaly/baseline.py"
DBT = "amp_ai/telemetry_anomaly/db_telemetry.py"
SERVICE = "amp_ai/telemetry_anomaly/service.py"
HARNESS = "amp_ai/telemetry_anomaly/build_eval.py"
EVAL_JSON = "amp_ai/artifacts/telemetry_anomaly_v1.eval.json"

# (label, file, old, new, suite that must go red)
MUTATIONS = [
    # --- series: buckets and states -------------------------------------------------
    ("a reading exactly 1 h before now lands in the baseline, not the score window", SERIES,
     "    return (delta_us - 1) // BUCKET_MICROSECONDS", "    return delta_us // BUCKET_MICROSECONDS", BASELINE),
    ("an event exactly at a bucket's start does not set that bucket's state", SERIES,
     "        i = bisect.bisect_right(times, start_time)", "        i = bisect.bisect_left(times, start_time)", BASELINE),
    ("a bucket in which the machine stops or starts keeps its starting state (no Transition)", SERIES,
     "        if any(state_label(ordered[x][1]) != state for x in range(i, j)):", "        if False:", BASELINE),
    ("an event exactly at a bucket's end is treated as inside it", SERIES,
     "        j = bisect.bisect_left(times, end_time)", "        j = bisect.bisect_right(times, end_time)", BASELINE),
    ("build_buckets accepts readings outside the window it was queried for", SERIES,
     "    if outside:", "    if False:", BASELINE),
    ("a bucket's value is the mean, so one glitched reading moves it", SERIES,
     "float(statistics.median(vals))", "float(statistics.fmean(vals))", BASELINE),

    # --- baseline: windows, floors, Mahalanobis, deviations ---------------------------
    ("Transition buckets get a baseline of their own and are scored", BASE,
     "        return state in LEARNED_STATES and counts.get((state, signal), 0) >= MIN_FIT_BUCKETS_PER_SIGNAL",
     "        return counts.get((state, signal), 0) >= MIN_FIT_BUCKETS_PER_SIGNAL", BASELINE),
    ("fit on the NEWEST 70% and calibrate on the oldest", BASE,
     "key=lambda b: -b.k)", "key=lambda b: b.k)", BASELINE),
    ("a baseline bucket inside the score window is accepted", BASE,
     '    _validate(baseline_buckets, SR.SCORE_BUCKETS, SR.TOTAL_BUCKETS - 1, "baseline")',
     '    _validate(baseline_buckets, 0, SR.TOTAL_BUCKETS - 1, "baseline")', BASELINE),
    ("the calibration sample includes the scored window itself", BASE,
     "    calibrator = EmpiricalCalibrator.fit(statistics_)",
     "    calibrator = EmpiricalCalibrator.fit(statistics_ + window)", BASELINE),
    ("integer sensors lose the quantisation-step MAD floor", BASE,
     "               MAD_FLOOR_QUANTIZATION_STEP if integer_valued else 0.0,", "               0.0,", BASELINE),
    ("the relative (1% of |median|) MAD floor is dropped", BASE,
     "    return max(MAD_FLOOR_RELATIVE * abs(median),", "    return max(0.0,", BASELINE),
    ("the Mahalanobis term runs on 399 complete fit buckets", BASE,
     "            if len(rows) < MAHALANOBIS_MIN_FIT_BUCKETS:",
     "            if len(rows) < MAHALANOBIS_MIN_FIT_BUCKETS - 1:", BASELINE),
    ("signals are named as deviating below |z| = 3.5", BASE,
     "        if abs(z) >= DEVIATION_Z:", "        if abs(z) >= 1.0:", BASELINE),

    # --- baseline: minimum data ----------------------------------------------------------
    ("288 fit buckets no longer make a signal scorable (>= became >)", BASE,
     ">= MIN_FIT_BUCKETS_PER_SIGNAL", "> MIN_FIT_BUCKETS_PER_SIGNAL", MIN_DATA),
    ("a window one short of a minimum-data threshold is still scored", BASE,
     "    if any(have[key] < needed[key] for key in needed):",
     "    if any(have[key] < needed[key] - 1 for key in needed):", MIN_DATA),
    ("calibration buckets holding only unscorable signals are counted", BASE,
     "    cal_bearing = [b for b in cal if any(s in signal_set and scorable(b.state, s) for s in b.values)]",
     "    cal_bearing = list(cal)", MIN_DATA),
    ("distinct days are over-counted by one", BASE,
     '"distinct_days": len({b.k // SR.BUCKETS_PER_DAY for b in fit + cal}),',
     '"distinct_days": len({b.k // SR.BUCKETS_PER_DAY for b in fit + cal}) + 1,', MIN_DATA),

    # --- db_telemetry: windows, tenant filters, values -----------------------------------
    ("the baseline window is widened to now, overlapping the score window", DBT,
     "    baseline = OeeWindow(days=SR.BASELINE_DAYS - _SCORE_DAYS, now=score.start)",
     "    baseline = OeeWindow(days=SR.BASELINE_DAYS, now=score.end)", DB),
    ("iot_telemetry is read without the explicit tenant filter", DBT,
     ".filter(T.tenant_code == tenant, T.machine_id == machine_id,", ".filter(T.machine_id == machine_id,", DB),
    ("industrial_signals are read without the explicit tenant filter", DBT,
     ".filter(S.tenant_code == tenant, S.machine_id == machine_id, S.quality == GOOD_QUALITY,",
     ".filter(S.machine_id == machine_id, S.quality == GOOD_QUALITY,", DB),
    ("readings whose quality is not Good are used", DBT,
     "S.machine_id == machine_id, S.quality == GOOD_QUALITY,", "S.machine_id == machine_id,", DB),
    ("in-window status events are read without the tenant filter", DBT,
     "                  .filter(E.tenant_code == tenant, E.machine_id == machine_id,\n"
     "                          E.created_at >= baseline.start",
     "                  .filter(E.machine_id == machine_id,\n"
     "                          E.created_at >= baseline.start", DB),
    ("the last event before the window is read without the tenant filter", DBT,
     "                  .filter(E.tenant_code == tenant, E.machine_id == machine_id,\n"
     "                          E.created_at >= lookback.start",
     "                  .filter(E.machine_id == machine_id,\n"
     "                          E.created_at >= lookback.start", DB),
    # A Python-side filter drops rows older than the window, so these three are visible only through
    # the row cap: unbounded, older rows fill LIMIT cap + 1 and the load is falsely truncated.
    ("the iot_telemetry query is unbounded below", DBT,
     "T.created_at >= start, T.created_at < end)", "T.created_at < end)", DB),
    ("the industrial_signals query is unbounded below", DBT,
     "S.created_at >= start, S.created_at < end)", "S.created_at < end)", DB),
    ("the in-window status event query is unbounded below", DBT,
     "E.created_at >= baseline.start, E.created_at < score.end)", "E.created_at < score.end)", DB),
    ("the iot_telemetry query is unbounded above", DBT,
     "T.created_at >= start, T.created_at < end)", "T.created_at >= start)", DB),
    ("the state lookback query is unbounded below", DBT,
     "E.created_at >= lookback.start, E.created_at < lookback.end)", "E.created_at < lookback.end)", DB),
    ("non-finite readings are kept", DBT,
     "    return v if math.isfinite(v) else None", "    return v", DB),
    ("a capped query does not report truncated", DBT,
     "        truncated=score_cut or base_cut or events_cut,", "        truncated=False,", DB),

    # --- service: authorization order, consent, the pinned evaluation -----------------------
    ("another tenant's machine is accepted (ownership not tenant-filtered)", SERVICE,
     "models.Machine.id == machine_id, models.Machine.tenant_code == tenant)", "models.Machine.id == machine_id)", DB),
    ("a refused consent decision is ignored", SERVICE,
     "    if not decision.granted:", "    if False:", DB),
    ("a grant for a different capability is accepted", SERVICE,
     "    if decision.capability != CAPABILITY_TELEMETRY_BASELINE:", "    if False:", DB),
    ("an object that is not a ConsentGate is accepted", SERVICE,
     "    if not isinstance(gate, ConsentGate):", "    if False:", DB),
    ("the service pins a hash that is not the committed evaluation's", SERVICE,
     "expected_sha256=expected_sha256 or EVAL_SHA256)", 'expected_sha256=expected_sha256 or "f" * 64)', DB),
    ("an evaluation of different method parameters is accepted", SERVICE,
     '    if artifact["parameters"] != B.method_parameters():', "    if False:", DB),

    # --- build_eval: window assembly, baselines, provenance --------------------------------
    ("the evaluation's baseline includes the scored hour", HARNESS,
     "        for k in range(SR.SCORE_BUCKETS, SR.TOTAL_BUCKETS):", "        for k in range(0, SR.TOTAL_BUCKETS):", EVAL),
    ("the static range check ignores readings below the range", HARNESS,
     "            excursion = max(lo - v, v - hi, 0.0) / width", "            excursion = max(v - hi, 0.0) / width", EVAL),
    ("the mean/std baseline uses the n - 1 standard deviation", HARNESS,
     "/ len(values))\n        out[name]", "/ (len(values) - 1))\n        out[name]", EVAL),
    ("the data hash ignores the seed (a new seed would not be fresh test data)", HARNESS,
     '        "seed": seed, "split": split, "variant": variant, "config": dict(config),',
     '        "split": split, "variant": variant, "config": dict(config),', EVAL),
    ("the generator hash depends on line endings", HARNESS,
     r'fh.read().replace(b"\r\n", b"\n")', "fh.read()", EVAL),

    # --- build_eval: gate and ledger ------------------------------------------------------
    ("gate: a PR-AUC lower bound of exactly 0 passes", HARNESS,
     "        passed = lo is not None and lo > 0.0", "        passed = lo is not None and lo >= 0.0", EVAL),
    ("gate: only baselines present in the comparison are checked", HARNESS,
     "    for name in GATED_BASELINES:\n        lo = _number(", "    for name in paired:\n        lo = _number(", EVAL),
    ("gate: the false-alarm limit becomes exclusive", HARNESS,
     "    passed = far is not None and far <= MAX_CLEAN_FALSE_ALARM_RATE",
     "    passed = far is not None and far < MAX_CLEAN_FALSE_ALARM_RATE", EVAL),
    ("gate: a reused test set can adopt", HARNESS,
     "    elif count > 1:", "    elif False:", EVAL),
    ("the test run is not recorded in the ledger", HARNESS,
     "    ledger = record_test_evaluation(prior_ledger, data_hash=main_hash, test_set=TEST_SET)",
     "    ledger = dict(prior_ledger)", EVAL),
    ("the smoke build may overwrite the committed evaluation", HARNESS,
     "        if _same_path(args.out, EVAL_PATH):", "        if False:", EVAL),
]


def run(suite):
    # PYTHONDONTWRITEBYTECODE is load-bearing: a same-length mutation restored within
    # the same second can leave a .pyc of the MUTATED code that Python trusts.
    env = dict(os.environ, DATABASE_URL=os.environ.get("DATABASE_URL", "sqlite:///./ci.db"),
               PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, suite], cwd=HERE, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=900)
    return proc.returncode


def drop_bytecode(path):
    cache = os.path.join(os.path.dirname(path), "__pycache__")
    stem = os.path.splitext(os.path.basename(path))[0] + "."
    if os.path.isdir(cache):
        for name in os.listdir(cache):
            if name.startswith(stem) and name.endswith(".pyc"):
                os.remove(os.path.join(cache, name))


def _abs(rel):
    return os.path.join(HERE, *rel.split("/"))


def main():
    suites = sorted({m[4] for m in MUTATIONS} | {PURITY})
    for rel in sorted({m[1] for m in MUTATIONS}):
        drop_bytecode(_abs(rel))
    for suite in suites:
        if run(suite) != 0:
            print(f"ABORT: {suite} fails before any mutation")
            return 2
    with open(_abs(EVAL_JSON), "rb") as fh:
        committed_eval = fh.read()
    print(f"baseline: {len(suites)} suites green\n")
    print(f"{'mutation':<80} verdict")
    print("-" * 96)
    problems = []
    for label, rel, old, new, suite in MUTATIONS:
        path = _abs(rel)
        with open(path, "rb") as fh:
            original = fh.read()
        text = original.decode("utf-8")
        crlf = "\r\n" in text
        needle = old.replace("\n", "\r\n") if crlf else old
        replacement = new.replace("\n", "\r\n") if crlf else new
        hits = text.count(needle)
        if hits != 1:
            print(f"{label:<80} NOT APPLIED ({hits} matches in {rel})")
            problems.append(label)
            continue
        try:
            with open(path, "wb") as fh:
                fh.write(text.replace(needle, replacement, 1).encode("utf-8"))
            code = run(suite)
        finally:
            with open(path, "wb") as fh:
                fh.write(original)
            drop_bytecode(path)
            with open(_abs(EVAL_JSON), "wb") as fh:
                fh.write(committed_eval)
        with open(path, "rb") as fh:
            if fh.read() != original:
                print(f"FATAL: {rel} was not restored byte for byte")
                return 3
        verdict = "caught" if code != 0 else "SURVIVED"
        print(f"{label:<80} {verdict} ({suite})")
        if code == 0:
            problems.append(label)
    print()
    for suite in suites:
        if run(suite) != 0:
            print(f"FATAL: {suite} fails after restoring every file")
            return 3
    if problems:
        print(f"{len(problems)} of {len(MUTATIONS)} mutations not caught or not applied:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"all {len(MUTATIONS)} mutations caught; every file restored; suites green again")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
