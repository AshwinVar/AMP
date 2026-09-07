"""Mutation harness for the operator job-status census (#568).

Each mutation either restores the exact-string lookup that dropped every
"In Progress" job, or opens a new hole in the vocabulary. Every one MUST be
caught by test_operator_status_census.py.

Read AND write with newline="" — these files are CRLF, and reading in text mode
without it makes the restore rewrite every line ending while multi-line patterns
silently stop matching. A pattern that does not match is a DISABLED mutation: it
measures nothing and looks exactly like a guard that works, so it is reported as
a survivor here rather than passing quietly.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_operator_census.py
"""
import io
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST = os.path.join(HERE, "test_operator_status_census.py")

MUTATIONS = [
    ("the endpoint goes back to exact-string lookups — 'In Progress' and NULL "
     "vanish from the breakdown",
     "analytics_routes.py",
     '        "started": buckets["started"],\n'
     '        "paused": buckets["paused"],\n'
     '        "completed": buckets["completed"],',
     '        "started": status_counts.get("Started", 0),\n'
     '        "paused": status_counts.get("Paused", 0),\n'
     '        "completed": status_counts.get("Completed", 0),'),
    ("'In Progress' drops out of the running set",
     "ai/workforce.py",
     'RUNNING_STATUSES = {"started", "in progress"}',
     'RUNNING_STATUSES = {"started"}'),
    ("the status is no longer lowercased/trimmed before matching",
     "ai/workforce.py",
     '    norm = (status or "").strip().lower()',
     '    norm = status or ""'),
    ("unknown words silently become 'started' instead of 'other'",
     "ai/workforce.py",
     '    return OTHER_BUCKET\n',
     '    return "started"\n'),
    ("the 'other' bucket stops being published",
     "analytics_routes.py",
     '        "other": buckets[ai.workforce.OTHER_BUCKET],\n',
     ''),
    ("completed stops deferring to _is_done and becomes a second list",
     "ai/workforce.py",
     '    if _is_done(status):\n        return "completed"',
     '    if (status or "").strip().lower() == "completed":\n        return "completed"'),
    ("paused folds into started, so the card loses a distinction it shows",
     "ai/workforce.py",
     'PAUSED_STATUSES = {"paused"}',
     'PAUSED_STATUSES = set()'),
    ("CENSUS_BUCKETS loses 'other', so the fold has nowhere to put it",
     "ai/workforce.py",
     'CENSUS_BUCKETS = ("started", "paused", "completed", OTHER_BUCKET)',
     'CENSUS_BUCKETS = ("started", "paused", "completed")'),
]


def run_test():
    env = dict(os.environ, DATABASE_URL="sqlite:///./ci_mut_op.db")
    r = subprocess.run([sys.executable, TEST], cwd=HERE, capture_output=True,
                       text=True, timeout=180, env=env)
    return r.returncode, (r.stdout + r.stderr)


def main():
    print("Baseline (unmutated) must PASS:")
    rc, out = run_test()
    if rc != 0:
        print(out[-2500:])
        print("BASELINE FAILS — fix the code before mutation testing.")
        return 1
    print("  PASS\n")

    caught, survived = 0, []
    for i, (label, rel, find, repl) in enumerate(MUTATIONS, 1):
        path = os.path.join(HERE, rel)
        original = io.open(path, encoding="utf-8", newline="").read()
        crlf = "\r\n" in original
        f = find.replace("\n", "\r\n") if crlf else find
        r_ = repl.replace("\n", "\r\n") if crlf else repl
        if f not in original:
            survived.append(f"{label}  [PATTERN DID NOT APPLY — unmeasured]")
            print(f"{i:2}. SURVIVED (pattern missing)  {label}")
            continue
        try:
            io.open(path, "w", encoding="utf-8", newline="").write(
                original.replace(f, r_, 1))
            rc, out = run_test()
        finally:
            io.open(path, "w", encoding="utf-8", newline="").write(original)
        if rc != 0:
            caught += 1
            fails = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("FAIL")]
            print(f"{i:2}. caught     {label}")
            print(f"      -> {fails[0][:92] if fails else '(non-zero exit)'}")
        else:
            survived.append(label)
            print(f"{i:2}. SURVIVED   {label}")

    print()
    print("=" * 74)
    print(f"{caught}/{len(MUTATIONS)} mutations caught")
    for s in survived:
        print(f"  SURVIVED: {s}")
    print("=" * 74)
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
