"""Mutation harness for the paged inventory lists (/inventory/items, /inventory/transactions).

A page that cannot be told from a complete list is the defect these endpoints
had: the newest 500 items, silently. Each line that ends it is one a tidy
refactor could drop without any passing case noticing on its own -- the cap,
the offset, the clamp, the total header, the total being the whole count and
not the page's, the two defaults, and the CORS line that lets a browser read
the header at all.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_inventory_paging.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_inventory_list_paging.py", "test_api_smoke.py"]
SUITE_TIMEOUT = 600  # seconds; both suites finish in well under a minute

I = "inventory_routes.py"
M = "main.py"

MUTATIONS = [
    ("the cap is dropped: every row comes back", I,
     "    return query.offset(offset).limit(limit).all()",
     "    return query.offset(offset).all()"),
    ("the offset is ignored: every page is the first", I,
     "    return query.offset(offset).limit(limit).all()",
     "    return query.limit(limit).all()"),
    ("the clamp is dropped: a direct caller may ask for everything", I,
     "    limit = page_default if limit is None else max(1, min(int(limit), MAX_PAGE))",
     "    limit = page_default if limit is None else max(1, int(limit))"),
    ("a limit of 0 means everything", I,
     "    limit = page_default if limit is None else max(1, min(int(limit), MAX_PAGE))",
     "    limit = page_default if limit is None else min(int(limit), MAX_PAGE) or None"),
    ("the total header is not sent", I,
     "    response.headers[TOTAL_HEADER] = str(query.order_by(None).count())",
     "    pass"),
    ("the total is the page's count, not the tenant's", I,
     "    response.headers[TOTAL_HEADER] = str(query.order_by(None).count())",
     "    response.headers[TOTAL_HEADER] = str(query.limit(limit).count())"),
    ("the items default widens to everything", I,
     "ITEMS_PAGE = 500",
     "ITEMS_PAGE = 100000"),
    ("the transactions default widens to everything", I,
     "TRANSACTIONS_PAGE = 300",
     "TRANSACTIONS_PAGE = 100000"),
    ("the count header is not exposed to browsers: the screen can never read it", M,
     '    expose_headers=["X-Total-Count"],',
     '    expose_headers=[],'),
]


def run_suites():
    failed = []
    here = os.path.dirname(os.path.abspath(__file__))
    for suite in SUITES:
        if not os.path.exists(os.path.join(here, suite)):
            continue
        try:
            proc = subprocess.run([sys.executable, suite], capture_output=True, text=True,
                                  errors="replace", cwd=here, timeout=SUITE_TIMEOUT)
        except subprocess.TimeoutExpired:
            # A suite that never finishes under a mutation has failed under it;
            # the harness must say so, not wait forever.
            failed.append(f"{suite} (timed out after {SUITE_TIMEOUT}s)")
            continue
        if proc.returncode != 0:
            failed.append(suite)
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
    print(f"{'mutation':<66} {'verdict':<10} caught by")
    print("-" * 108)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<66} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
            survived.append(f"{label} (pattern did not apply)")
            continue
        io.open(os.path.join(here, path), "w", encoding="utf-8",
                newline="\n").write(source.replace(old, new, 1))
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source)
        if failing:
            verdict, note = "caught", ", ".join(
                s.replace("test_", "").replace(".py", "")[:26] for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label]
        else:
            verdict, note = "SURVIVED", "-- nothing --"
        print(f"{label:<66} {verdict:<10} {note}")
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
            print(f"  - {s}")
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
