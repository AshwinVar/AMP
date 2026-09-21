"""Mutation harness for the one downtime window (ai/downtime).

Each mutation brings back one half of the span this card used to disagree on:
the calendar-day cutoff that put a stoppage late on the boundary date outside
the week the tile beside it counted, the missing upper bound that counted a
stoppage dated later today, the seven-bar series under an eight-date window,
the calendar-day trend halves, and the second copy of the unlabelled-reason
rule. For each the harness applies the edit, runs the suites that are supposed
to notice, and restores the file byte for byte. A mutation that leaves the
suites green SURVIVED: that guard is untested.

RUN IT ALONE. Like every mutate_* harness it EDITS THE WORKING TREE and restores
it afterwards; anything reading those files meanwhile sees broken code.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_downtime_window.py
Exit 0 only if every mutation applied exactly once and was caught.
"""
import io
import os
import subprocess
import sys

SUITES = ["test_downtime_one_window.py", "test_downtime.py", "test_downtime_trend.py",
          "test_downtime_scan_bounded.py", "test_trend_verdict_contract.py",
          "test_briefing.py"]
SUITE_TIMEOUT = 900

DT = os.path.join("ai", "downtime.py")
WINDOWS = "oee_contract.py"

# (label, file, old, new)
MUTATIONS = [
    ("the card stops bounding its window at the near end", DT,
     "    if window.start is not None:\n        q = q.filter(models.DowntimeLog.created_at >= window.start)\n    return [d for d in q.filter(models.DowntimeLog.created_at < window.end).all()",
     "    return [d for d in q.filter(models.DowntimeLog.created_at < window.end).all()"),
    ("the card counts a stoppage dated later today again", DT,
     "    return [d for d in q.filter(models.DowntimeLog.created_at < window.end).all()\n            if d.created_at]",
     "    return [d for d in q.all() if d.created_at]"),
    ("the card keeps its own week", DT,
     "WINDOW_DAYS = oee_contract.DEFAULT_WINDOW_DAYS",
     "WINDOW_DAYS = 30"),
    ("the summary's series is drawn over seven bars again", DT,
     "    window = _window(now)\n    span, opens_mid_day = oee_contract.window_span(window)\n    logs = _logs_in(db, window)",
     "    window = _window(now)\n    span, opens_mid_day = oee_contract.window_span(window)\n    span = span[-WINDOW_DAYS:]\n    logs = _logs_in(db, window)"),
    ("the oldest bucket stops saying it is a partial day", DT,
     '              **({"partial": True} if (i == 0 and opens_mid_day) else {})}\n             for i, dd in enumerate(span)]\n\n    return {\n        "days": WINDOW_DAYS,\n        "window": window.label(),\n        "total_events": len(logs),',
     '              }\n             for i, dd in enumerate(span)]\n\n    return {\n        "days": WINDOW_DAYS,\n        "window": window.label(),\n        "total_events": len(logs),'),
    ("the drill-down reads a different span from the Pareto row that opened it", DT,
     "    logs = [d for d in _logs_in(db, window) if _norm_reason(d) == reason]",
     "    logs = [d for d in _logs_in(db, oee_contract.OeeWindow(WINDOW_DAYS * 2, now=now))\n            if _norm_reason(d) == reason]"),
    ("the trend's halves overlap instead of tiling", DT,
     "    prior_w = oee_contract.prior_window(window)",
     "    prior_w = oee_contract.OeeWindow(WINDOW_DAYS * 2, now=window.end)"),
    ("the trend's current half stops being the summary's week", DT,
     "    cur_logs = _logs_in(db, window)",
     "    cur_logs = _logs_in(db, oee_contract.OeeWindow(WINDOW_DAYS + 1, now=window.end))"),
    ("the fortnight's series is drawn over fourteen bars again", DT,
     "    span, opens_mid_day = oee_contract.window_span(\n        oee_contract.OeeWindow(TREND_WINDOW_DAYS, now=window.end))",
     "    span, opens_mid_day = oee_contract.window_span(\n        oee_contract.OeeWindow(TREND_WINDOW_DAYS, now=window.end))\n    span = span[-TREND_WINDOW_DAYS:]"),
    ("an unlabelled stop gets a second naming rule", DT,
     "    return _norm_reason_label(d.reason)",
     '    return (d.reason or "No reason").strip() or "No reason"'),
    ("a series is drawn over one day more than its window touches", WINDOWS,
     "    end_date = (window.end - _TICK).date()",
     "    end_date = window.end.date()"),
]


def run_suites():
    failed = []
    here = os.path.dirname(os.path.abspath(__file__))
    for suite in SUITES:
        try:
            proc = subprocess.run([sys.executable, suite], capture_output=True, text=True,
                                  errors="replace", cwd=here, timeout=SUITE_TIMEOUT)
            if proc.returncode != 0:
                failed.append(suite)
        except subprocess.TimeoutExpired:
            failed.append(suite + " (timeout)")
    return failed


EXPECTED_SURVIVORS = {}


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    originals = {}
    for _, path, _, _ in MUTATIONS:
        if path not in originals:
            originals[path] = io.open(os.path.join(here, path), encoding="utf-8").read()

    baseline = run_suites()
    if baseline:
        print(f"ABORT: suites already failing before any mutation: {baseline}")
        return 2
    print(f"baseline: all {len(SUITES)} suites green\n")
    print(f"{'mutation':<74} {'verdict':<10} caught by")
    print("-" * 116)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<74} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
            survived.append(f"{label} (pattern did not apply)")
            continue
        io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(
            source.replace(old, new, 1))
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source)
        if failing:
            verdict, note = "caught", ", ".join(s.replace("test_", "").replace(".py", "")[:28]
                                                for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label][:70] + "..."
        else:
            verdict, note = "SURVIVED", "-- nothing --"
        print(f"{label:<74} {verdict:<10} {note}", flush=True)
        if verdict == "SURVIVED":
            survived.append(label)

    dirty = [p for p, original in originals.items()
             if io.open(os.path.join(here, p), encoding="utf-8").read() != original]
    print()
    print(f"source files restored: {'yes' if not dirty else 'NO - DIRTY: ' + str(dirty)}")
    if dirty:
        return 3
    if survived:
        print(f"{len(survived)} MUTATION(S) SURVIVED - investigate each:")
        for s in survived:
            print("   *", s)
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
