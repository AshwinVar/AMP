"""Mutation harness for "a list is a page, and says so" (paging.py, ADR-0036).

A page that cannot be told from a complete list is the defect every capped
list had: the newest 500 items, silently. Each line that ends it is one a tidy
refactor could drop without any passing case noticing on its own -- the cap,
the offset, the clamp, a limit of 0, an endpoint's own smaller ceiling, the
total header, the total being the whole count and not the page's, the two
inventory defaults, the CORS line that lets a browser read the header at all,
a handler that quietly goes back to a raw .limit().all(), the unread
filter that makes the header an honest unread count, and the measured count
optimisation (a short page's free total, the empty-page-past-the-end case,
the cache's TTL, tenant and query keys).

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_lists_are_pages.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_lists_are_pages.py", "test_inventory_list_paging.py",
          "test_enterprise_inventory_list_paging.py", "test_api_smoke.py"]
SUITE_TIMEOUT = 600  # seconds; the four suites finish in well under a minute

P = "paging.py"
I = "inventory_routes.py"
M = "main.py"
W = "work_orders_routes.py"
F = "factory_ops_routes.py"
E = "enterprise_inventory_routes.py"

MUTATIONS = [
    ("the cap is dropped: every row comes back", P,
     "    rows = query.offset(offset).limit(limit).all()",
     "    rows = query.offset(offset).all()"),
    ("the offset is ignored: every page is the first", P,
     "    rows = query.offset(offset).limit(limit).all()",
     "    rows = query.limit(limit).all()"),
    ("the clamp is dropped: a direct caller may ask for everything", P,
     "    return max(1, min(limit, max_page)), max(0, _int(offset, 0))",
     "    return max(1, limit), max(0, _int(offset, 0))"),
    ("a limit of 0 means everything", P,
     "    return max(1, min(limit, max_page)), max(0, _int(offset, 0))",
     "    return (min(limit, max_page) or None), max(0, _int(offset, 0))"),
    ("an endpoint's own smaller ceiling is ignored: every list caps at MAX_PAGE", P,
     "    return max(1, min(limit, max_page)), max(0, _int(offset, 0))",
     "    return max(1, min(limit, MAX_PAGE)), max(0, _int(offset, 0))"),
    ("the total header is not sent", P,
     "        response.headers[TOTAL_HEADER] = str(total)",
     "        pass"),
    ("the total is the page's count, not the tenant's", P,
     "    total = query.order_by(None).count()",
     "    total = query.order_by(None).limit(1).count()"),
    ("the items default widens to everything", I,
     "ITEMS_PAGE = 500",
     "ITEMS_PAGE = 100000"),
    ("the transactions default widens to everything", I,
     "TRANSACTIONS_PAGE = 300",
     "TRANSACTIONS_PAGE = 100000"),
    ("the count header is not exposed to browsers: the screen can never read it", M,
     '    expose_headers=["X-Total-Count"],',
     '    expose_headers=[],'),
    ("a request's response is treated as a direct call: no header on the wire", P,
     "    if response is not None:",
     "    if response is None:"),
    ("a handler quietly goes back to a raw .limit().all(): the guard must see it", W,
     "    return paging.page(response, db.query(models.WorkOrder).order_by(models.WorkOrder.id.desc()), 200, limit, offset)",
     "    return db.query(models.WorkOrder).order_by(models.WorkOrder.id.desc()).limit(200).all()"),
    ("the unread filter is dropped: the header counts every notification", F,
     '        q = q.filter(or_(models.Notification.status.is_(None), models.Notification.status != "Read"))',
     '        pass'),
    ("a NULL status stops counting as unread", F,
     '        q = q.filter(or_(models.Notification.status.is_(None), models.Notification.status != "Read"))',
     '        q = q.filter(models.Notification.status != "Read")'),
    # The measured count optimisation (paging.py header): each of these would
    # make a header wrong or the count cost return, and section 8 of
    # test_lists_are_pages.py is what sees it.
    ("an empty page past the end claims total = offset", P,
     "        if len(rows) < limit and (rows or offset == 0):",
     "        if len(rows) < limit:"),
    ("a short page still pays the count", P,
     "        if len(rows) < limit and (rows or offset == 0):",
     "        if False:"),
    ("the cached total never expires", P,
     "    if hit is not None and hit[0] > now:",
     "    if hit is not None:"),
    ("the cache ignores the tenant: a neighbour's total is served", P,
     "    return (tenancy.current_tenant(), str(compiled))",
     "    return (\"\", str(compiled))"),
    ("the cache ignores the query: a filtered total is the list's", P,
     "    return (tenancy.current_tenant(), str(compiled))",
     "    return (tenancy.current_tenant(), \"\")"),
    ("the enterprise lists lose their own ceiling of 200", E,
     "    rows = paging.page(response, db.query(models.Remnant).order_by(models.Remnant.id.desc()),\n"
     "                       _PAGE_DEFAULT, limit, offset, max_page=_PAGE_MAX)",
     "    rows = paging.page(response, db.query(models.Remnant).order_by(models.Remnant.id.desc()),\n"
     "                       _PAGE_DEFAULT, limit, offset)"),
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
