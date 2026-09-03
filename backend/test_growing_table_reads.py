"""No polled endpoint may read a whole table that grows with TIME.

WHY THIS GUARD EXISTS
---------------------
`/analytics/executive-oee` and `/analytics/factory-command-center` each did
`db.query(models.DowntimeLog).all()` — no filter, no limit — on the dashboard's
3-second poll. Measured on PostgreSQL 18.3, 200 machines, handler time only:

    downtime_logs rows        executive-oee      factory-command-center
                 ~200                6.9 ms                    16.8 ms
               75,000              822.0 ms                   859.6 ms

Fixed in #531 (46.8x / 50.6x). This file exists so it cannot come back, and so
the same mistake cannot be made on a different table.

THE BLIND SPOT THIS CLOSES
--------------------------
Every performance harness in this repo — `dashboard_perf.py`, `loadtest.py`,
`oem_perf.py` — seeds rows PER MACHINE. The tables below do not grow with the
size of the factory; they grow with how long the factory has been running. So
no scale of a per-machine seed ever reaches a year of history, and all three
harnesses reported the defect above as healthy right up until it was measured
with an aged table.

A measurement harness cannot be relied on to find this class. A static check
can, because the defect has a SHAPE: hydrate every row of an append-only table
on a request that repeats every three seconds.

WHY AST AND NOT A REGEX
-----------------------
The first version of this scan was a regex over the source, and it flagged
`analytics_engine.downtime_aggregates` — whose DOCSTRING quotes the offending
line while the code does the opposite. A textual scan cannot tell code from
prose. This walks the AST, so comments and docstrings are invisible to it.

WHAT COUNTS AS BOUNDED
----------------------
    .limit(...)                     an explicit row cap
    .filter(... created_at ...)     a time window
    db.query(Model.col, func...)    an aggregate or a column projection --
                                    the row never becomes an ORM object

Only `db.query(models.<Growing>)` — the WHOLE model, every column, every row —
followed by `.all()` is a finding.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_growing_table_reads.py
"""
import ast
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))

# Tables that grow with TIME rather than with the size of the factory. Each is
# append-only in normal operation: nothing in the product deletes from them, so
# a row written on day one is still there in year three.
GROWING = {
    "DowntimeLog", "MachineEvent", "AgentAction", "AuditLog", "ProductionRecord",
    "Notification", "InventoryTransaction", "OperatorExecution",
    "QualityInspection", "ShiftData", "CostRecord", "MaintenanceTask",
}

# Reads that hydrate a whole growing table and are ACCEPTED, with the reason.
# Key: "<file>::<function>"  ->  why it is not the defect this guard is for.
#
# The bar is not "it is slow but we do not mind". It is that the read is not on
# a repeating poll, so its cost is paid once, by someone who asked for it.
ALLOWED = {
    # ---- On-demand downloads. Emitting every row IS the product here; a user
    # clicked export and is waiting for a file. Not polled by anything.
    "reports_routes.py::export_downtime_csv":
        "GET /downtime.csv — a full CSV export; every row is the deliverable",
    "reports_routes.py::export_oee_csv":
        "GET /oee.csv — full CSV export",
    "reports_routes.py::export_quality_csv":
        "GET /quality.csv — full CSV export",
    "reports_routes.py::export_shifts_csv":
        "GET /shifts.csv — full CSV export",
    "reports_routes.py::export_maintenance_csv":
        "GET /maintenance.csv — full CSV export",
    "reports_routes.py::export_intelligence_summary":
        "GET /intelligence-summary.txt — a whole-history narrative report",

    # ---- POST generators. Run on demand by an operator or an agent, never on a
    # poll, and each is a sweep whose job is to consider every row.
    "factory_ops_routes.py::generate_maintenance_overdue_escalations":
        "POST /maintenance/generate-overdue-escalations — an on-demand sweep",
    "quality_routes.py::generate_defect_escalations":
        "POST /generate-defect-escalations — an on-demand sweep",

    # /analytics/management was here as KNOWN, PENDING. It is fixed: the scan is
    # gone, build_management_summary now takes `downtime_agg` the same way it
    # already took production_sums and shift_sums, and section 2 below flagged
    # this entry as stale the moment the fix landed — which is what the honesty
    # check is for.
}

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def _chain(node):
    """Every call in an `a.b().c().d()` chain, outermost first."""
    out = []
    cur = node
    while isinstance(cur, ast.Call):
        out.append(cur)
        f = cur.func
        cur = f.value if isinstance(f, ast.Attribute) else None
    return out


def _growing_model_query(call):
    """If `call` is a `db.query(...)` reading a growing table, return the model.

    Matches BOTH the whole model and a column projection of it. The projection
    used to be exempt, on the grounds that "the row never becomes an ORM
    object" -- true, and not the point. ai/twin.py selected three columns of
    every downtime row to render three per machine: 75,000 rows to show 600, and
    200 ms of /machine-health's 261 ms. Projected rows still cross the wire and
    are still iterated; only the ORM construction is saved.

    An aggregate is still not a finding: `func.count()` and friends return one
    row per group, which is the fix, not the defect.
    """
    f = call.func
    if not (isinstance(f, ast.Attribute) and f.attr == "query"):
        return None
    # An AGGREGATE select is the fix, not the defect: `func.count()`,
    # `func.sum()`, `func.row_number()` all return one row per group (or per
    # partition rank), however many rows the table holds. Selecting columns
    # ALONGSIDE an aggregate is the GROUP BY shape, so the whole query is exempt.
    for arg in call.args:
        if (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute)
                and isinstance(arg.func.value, ast.Name)
                and arg.func.value.id == "func"):
            return None
        if (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute)
                and arg.func.attr in ("label", "over")):
            return None
    for arg in call.args:
        # models.DowntimeLog            -> Attribute(value=Name("models"))
        # models.DowntimeLog.machine_id -> Attribute(value=Attribute(...))
        if isinstance(arg, ast.Attribute):
            if (isinstance(arg.value, ast.Name) and arg.value.id == "models"
                    and arg.attr in GROWING):
                return arg.attr
            inner = arg.value
            if (isinstance(inner, ast.Attribute)
                    and isinstance(inner.value, ast.Name)
                    and inner.value.id == "models" and inner.attr in GROWING):
                return inner.attr
    return None


def _bounding_filter(call):
    """Does this filter actually limit how many rows come back?

    A `created_at` window does (subject to the row rate -- see
    test_predictive_risk_aggregates.py, which is the suite for THAT). So does
    pinning a column to a value or a small set: `machine_id == 4` returns one
    machine's rows, `status.in_(("Open",))` returns the open ones. A filter that
    only names the tenant does not -- every row in a single-tenant table matches
    it -- but tenant scoping is applied by the ORM hook (ADR-0002) rather than
    written here, so in practice such a predicate does not appear alone.
    """
    args = list(call.args) + [k.value for k in call.keywords]
    for a in args:
        for n in ast.walk(a):
            if isinstance(n, ast.Attribute) and n.attr == "created_at":
                return True
            if isinstance(n, ast.Compare) and any(
                    isinstance(op, (ast.Eq, ast.In)) for op in n.ops):
                return True
            # `.in_((...))` pins to a small set. `.is_(None)` does NOT bound
            # anything -- it was in this list briefly and made
            # factory_ops_routes' overdue sweep look bounded because of an
            # `or_(status.is_(None), status != "Completed")`, which selects
            # every unfinished task ever planned.
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "in_"):
                return True
    return bool(call.keywords)          # .filter_by(machine_id=4)


def scan(path):
    """Unbounded whole-table reads of a growing model. -> [(function, model)]"""
    with open(path, encoding="utf-8", errors="replace") as fh:
        tree = ast.parse(fh.read(), filename=path)
    # map each node to its enclosing function
    owner = {}
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for n in ast.walk(fn):
            owner.setdefault(n, fn.name)

    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "all"):
            continue
        chain = _chain(node)
        model = None
        bounded = False
        for c in chain:
            f = c.func
            if not isinstance(f, ast.Attribute):
                continue
            if f.attr in ("limit", "group_by"):
                bounded = True
            if f.attr in ("filter", "filter_by", "where") and _bounding_filter(c):
                bounded = True
            m = _growing_model_query(c)
            if m:
                model = m
        if model and not bounded:
            hits.append((owner.get(node, "<module>"), model))
    return sorted(set(hits))


def route_files():
    """Application route modules only.

    `test_*_routes.py` is excluded deliberately: a test that asserts a table's
    contents SHOULD read all of it, and folding those in would mean allowlisting
    a dozen suites for doing exactly what they are meant to do.
    """
    return sorted(p for p in glob.glob(os.path.join(HERE, "*_routes.py"))
                  + [os.path.join(HERE, "analytics_engine.py"),
                     os.path.join(HERE, "predictive_engine.py")]
                  # ai/ IS application code on the request path: ai/twin.py backs
                  # /machine-health, ai/prediction.py backs
                  # /analytics/predictive-maintenance. Leaving the package out is
                  # why this guard did not see the projection scan in
                  # ai/twin.py::_downtime_by_machine -- the very defect the
                  # projection exemption was also hiding. Two holes, same miss.
                  + glob.glob(os.path.join(HERE, "ai", "*.py"))
                  if not os.path.basename(p).startswith("test_"))


def main():
    print("=" * 74)
    print("1. NO UNBOUNDED READ OF A GROWING TABLE OUTSIDE THE ALLOWLIST")
    print("=" * 74)
    found = {}
    for path in route_files():
        name = os.path.basename(path)
        for fn, model in scan(path):
            found[f"{name}::{fn}"] = model
    unexpected = {k: v for k, v in found.items() if k not in ALLOWED}
    check(f"scanned {len(route_files())} modules, "
          f"{len(found)} whole-table read(s), {len(ALLOWED)} allowed",
          not unexpected,
          "NEW unbounded read(s): " + ", ".join(f"{k} ({v})" for k, v in unexpected.items()))
    if unexpected:
        print()
        print("  A read like this costs nothing today and grows forever. If it is")
        print("  on a poll, bound it (see analytics_engine.downtime_aggregates for")
        print("  the GROUP BY trick when a column is a string). If it is genuinely")
        print("  on demand, add it to ALLOWED with the reason.")

    print()
    print("=" * 74)
    print("2. THE ALLOWLIST IS HONEST")
    print("=" * 74)
    # Copied from test_date_basis_guard: an allowlist nobody re-checks silently
    # grants permission to code that no longer needs it, and then to code that
    # never did. Every entry must still name a real, still-present read.
    stale = [k for k in ALLOWED if k not in found]
    check(f"every one of the {len(ALLOWED)} allowlist entries still names a real read",
          not stale, "stale (fixed or renamed — delete these): " + ", ".join(stale))

    print()
    print("=" * 74)
    print("3. THE SCAN ACTUALLY DETECTS THE DEFECT IT WAS WRITTEN FOR")
    print("=" * 74)
    # A guard that finds nothing proves nothing until it is shown finding
    # something. This is the exact code #531 removed.
    import tempfile
    sample = '''
import models
def get_executive_oee(db, current_user):
    """A docstring mentioning db.query(models.DowntimeLog).all() must NOT count."""
    downtime_logs = db.query(models.DowntimeLog).all()
    return len(downtime_logs)

def bounded_by_limit(db):
    return db.query(models.DowntimeLog).order_by(models.DowntimeLog.id).limit(100).all()

def bounded_by_window(db, cutoff):
    return db.query(models.DowntimeLog).filter(models.DowntimeLog.created_at >= cutoff).all()

def aggregated(db):
    return db.query(models.DowntimeLog.reason, func.count()).group_by(
        models.DowntimeLog.reason).all()

def not_a_growing_table(db):
    return db.query(models.Machine).all()
'''
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "sample_routes.py")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(sample)
        hits = dict(scan(p))
    check("the unbounded scan IS detected",
          hits.get("get_executive_oee") == "DowntimeLog", str(hits))
    check("a .limit() is NOT flagged", "bounded_by_limit" not in hits, str(hits))
    check("a created_at window is NOT flagged", "bounded_by_window" not in hits, str(hits))
    check("a GROUP BY aggregate is NOT flagged", "aggregated" not in hits, str(hits))
    check("a table that grows with MACHINES is not this guard's business",
          "not_a_growing_table" not in hits, str(hits))
    # The regex version of this scan flagged a function whose docstring quoted
    # the offending line. Walking the AST is what makes prose invisible.
    check("a docstring quoting the pattern is NOT a finding",
          len(hits) == 1, f"expected exactly 1 hit, got {hits}")

    print()
    print("=" * 74)
    print("4. THE TWO ENDPOINTS #531 FIXED ARE STILL FIXED")
    print("=" * 74)
    ar = dict(scan(os.path.join(HERE, "analytics_routes.py")))
    for fn in ("get_executive_oee", "get_factory_command_center", "analytics_summary"):
        check(f"{fn} does not hydrate a growing table", fn not in ar,
              f"regressed: reads {ar.get(fn)}")

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
