"""Mutation harness for the purchase-order status census (#569).

Each mutation restores the exact-string lookup that put six of nine POs in no
bucket, or opens a new hole in the vocabulary. Every one MUST be caught by
test_po_status_census.py.

Read AND write with newline="" — these files are CRLF, and reading in text mode
without it makes the restore rewrite every line ending while multi-line patterns
silently stop matching. A pattern that does not match is a DISABLED mutation: it
measures nothing while looking exactly like a guard that works, so it is
reported here as a survivor rather than passing quietly.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_po_census.py
"""
import io
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST = os.path.join(HERE, "test_po_status_census.py")

MUTATIONS = [
    ("the endpoint goes back to exact-string lookups — 'Partially Received', "
     "'Draft', 'Overdue', 'Approved' and NULL all vanish",
     "orders_routes.py",
     '        "open": buckets["open"],\n'
     '        "partial": buckets["partial"],\n'
     '        "received": buckets["received"],\n'
     '        "cancelled": buckets["cancelled"],',
     '        "open": status_counts.get("Open", 0),\n'
     '        "partial": status_counts.get("Partial", 0),\n'
     '        "received": status_counts.get("Received", 0),\n'
     '        "cancelled": status_counts.get("Cancelled", 0),'),
    ("'Partially Received' drops out — the Partial KPI can only read zero again",
     "ai/supply.py",
     '    "partially received": "partial",\n',
     ''),
    ("Draft folds into open, overstating committed spend",
     "ai/supply.py",
     '    "draft": "draft",',
     '    "draft": "open",'),
    ("Approved stops being an open order",
     "ai/supply.py",
     '    "approved": "open",\n',
     ''),
    ("the 'Overdue' status word stops being an open order",
     "ai/supply.py",
     '    **{s: "open" for s in LATE_STATUSES},\n',
     ''),
    ("the received set is restated instead of derived — two lists again",
     "ai/supply.py",
     '    **{s: "received" for s in RECEIVED_STATUSES},',
     '    "received": "received",'),
    ("the cancelled set is restated instead of derived",
     "ai/supply.py",
     '    **{s: "cancelled" for s in CANCELLED_STATUSES},',
     '    "cancelled": "cancelled",'),
    ("the status is no longer lowercased/trimmed before the lookup",
     "ai/supply.py",
     '    return STATUS_BUCKETS.get((status or "").strip().lower(), OTHER_BUCKET)',
     '    return STATUS_BUCKETS.get(status, OTHER_BUCKET)'),
    ("unknown words silently become 'open' instead of 'other'",
     "ai/supply.py",
     '    return STATUS_BUCKETS.get((status or "").strip().lower(), OTHER_BUCKET)',
     '    return STATUS_BUCKETS.get((status or "").strip().lower(), "open")'),
    ("the 'other' bucket stops being published",
     "orders_routes.py",
     '        "other": buckets[ai.supply.OTHER_BUCKET],\n',
     ''),
    ("the draft bucket stops being published",
     "orders_routes.py",
     '        "draft": buckets["draft"],\n',
     ''),
    ("overdue is folded into the status partition, double-counting lateness",
     "orders_routes.py",
     '        "overdue": overdue,',
     '        "overdue": overdue + buckets["draft"],'),
]


def run_test():
    env = dict(os.environ, DATABASE_URL="sqlite:///./ci_mut_cen.db")
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
            print(f"      -> {fails[0][:88] if fails else '(non-zero exit)'}")
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
