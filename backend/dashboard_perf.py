"""What does ONE dashboard refresh actually cost?

WHY THIS EXISTS
---------------
`frontend/app/dashboard/page.tsx` calls `usePolling(fetchAll, 3000)`, and
`fetchAll` issues 3 mandatory requests followed by 43 optional ones — 46 per
round, every three seconds, per open tab. The file's own comment says "~47
requests". Nobody has ever measured what that costs.

That is the point. `docs/PERFORMANCE.md` opens with "nothing in this document has
been measured" and `load/thresholds.js` calls its numbers "DERIVED BUDGETS, not
measured baselines". Optimising the dashboard before measuring it would be
guessing, and the obvious guesses (batch the calls, raise the interval) trade
away behaviour for a saving nobody has sized.

WHAT THIS MEASURES, AND WHAT IT DOES NOT
----------------------------------------
It calls the route FUNCTIONS directly against a seeded database and counts the
SQL statements each one issues, plus wall time. That is the same technique
`oem_perf.py` uses, and it is deliberately NOT an HTTP benchmark:

  * MEASURED   — SQL statements per endpoint, and where they concentrate.
                 A count that grows with the number of machines is an N+1, and
                 that is the failure that actually bites at scale.
  * MEASURED   — relative wall time between endpoints, on this machine.
  * NOT MEASURED — HTTP latency, serialisation, network, or the browser. Use
                 `loadtest.py` for those; it drives a real uvicorn.

Absolute milliseconds here are not a production number and must not be quoted as
one. The query COUNTS are the durable finding: they do not depend on this
laptop, and a count that scales with row count is a defect wherever it runs.

Run:  DATABASE_URL="sqlite:///./perf.db" python backend/dashboard_perf.py
      python backend/dashboard_perf.py 5432        # against scratch PostgreSQL
"""
import os
import sys
import time

SCALES = (10, 50, 200)


def main(url):
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    import models
    import tenancy
    from database import Base

    TENANT = "PERF_FACTORY"

    engine = create_engine(url)
    Session = sessionmaker(bind=engine)

    counter = {"n": 0, "on": False}

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        if counter["on"]:
            counter["n"] += 1

    class Counted:
        def __enter__(self):
            counter["n"] = 0
            counter["on"] = True
            self.t0 = time.perf_counter()
            return self

        def __exit__(self, *exc):
            self.ms = (time.perf_counter() - self.t0) * 1000
            self.queries = counter["n"]
            counter["on"] = False
            return False

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()

    # THE REAL POLL CYCLE, resolved from the app rather than hand-listed.
    #
    # frontend/app/dashboard/page.tsx fetchAll(): 3 mandatory + 43 optional.
    # Handlers are looked up BY PATH on main.app.routes — the same technique
    # test_api_smoke.py uses — because hand-writing 46 function names is how the
    # first version of this file reported "ERR" for three endpoints whose
    # handlers had different names than I guessed.
    import main

    PATHS = [
        # mandatory
        "/machines", "/downtime-logs", "/shifts",
        # optional (Promise.allSettled)
        "/analytics/machine-timeline", "/analytics/machine-state-summary",
        "/work-orders", "/analytics/work-orders",
        "/analytics/predictive-maintenance",
        "/production-plans", "/analytics/production-plans",
        "/escalations", "/analytics/escalations",
        "/inventory/items", "/inventory/transactions", "/analytics/inventory",
        "/quality/inspections", "/analytics/quality",
        "/analytics/executive-oee",
        "/factory-layout/nodes", "/analytics/factory-command-center",
        "/customer-orders", "/analytics/customer-orders",
        "/suppliers", "/purchase-orders", "/analytics/purchasing",
        "/documents", "/analytics/documents",
        "/maintenance/tasks", "/analytics/maintenance",
        "/production-schedules", "/analytics/production-schedules",
        "/iot/telemetry", "/analytics/iot-command",
        "/ai/recommendations", "/analytics/ai-insights",
        "/saas/tenants", "/analytics/saas",
        "/cost-records", "/analytics/costing",
        "/operator/executions", "/analytics/operator-terminal",
        "/audit-logs", "/notifications", "/reports",
        "/analytics/system-health", "/analytics/final-executive-summary",
        # read-models the dashboard's own sections fetch on top of fetchAll
        "/machine-health", "/oee-summary", "/losses-summary",
    ]

    by_path = {}
    for r in main.app.routes:
        path = getattr(r, "path", None)
        methods = getattr(r, "methods", set()) or set()
        if path and "GET" in methods:
            by_path.setdefault(path, getattr(r, "endpoint", None))

    user = {"sub": "perf", "role": "Admin", "tenant": TENANT}

    def _call(fn):
        """Invoke a handler with only the kwargs it declares."""
        import inspect
        kwargs = {}
        params = inspect.signature(fn).parameters
        if "db" in params:
            kwargs["db"] = None                      # replaced per call below
        return params

    CYCLE = []
    unresolved = []
    for path in PATHS:
        fn = by_path.get(path)
        if fn is None:
            unresolved.append(path)
            continue
        CYCLE.append((path, fn))

    per_scale = {}
    print(f"Measuring {len(CYCLE)} endpoints of the poll cycle."
          + (f"  UNRESOLVED: {unresolved}" if unresolved else ""))
    print("Query COUNT is the durable number; ms is this machine only.\n")

    for scale in SCALES:
        db = Session()
        tok = tenancy.set_current_tenant(None)
        db.query(models.ProductionRecord).delete()
        db.query(models.DowntimeLog).delete()
        db.query(models.Machine).filter(models.Machine.tenant_code == TENANT).delete()
        db.add(models.TenantConfig(tenant_code=TENANT)) if not db.query(
            models.TenantConfig).filter_by(tenant_code=TENANT).first() else None
        db.commit()
        for i in range(scale):
            db.add(models.Machine(tenant_code=TENANT, site="Plant 1",
                                  name=f"M-{i:04d}", status="Running",
                                  utilization=70, downtime="0 min"))
        db.commit()
        machines = db.query(models.Machine).filter(
            models.Machine.tenant_code == TENANT).all()
        for m in machines:
            db.add(models.ProductionRecord(
                tenant_code=TENANT, machine_id=m.id, planned_minutes=480,
                runtime_minutes=400, ideal_cycle_time_seconds=30,
                total_count=100, good_count=95, rejected_count=5))
        db.commit()
        tenancy.reset_current_tenant(tok)
        db.close()

        print(f"--- {scale} machines " + "-" * 46)
        print(f"{'endpoint':<34}{'queries':>9}{'ms':>9}  {'':<10}")
        total_q = 0
        total_ms = 0.0
        rows = []
        import inspect
        for name, fn in CYCLE:
            db = Session()
            tok = tenancy.set_current_tenant(TENANT)
            try:
                params = inspect.signature(fn).parameters
                kwargs = {}
                if "db" in params:
                    kwargs["db"] = db
                if "current_user" in params:
                    kwargs["current_user"] = user
                with Counted() as c:
                    fn(**kwargs)
                flag = ""
                total_q += c.queries
                total_ms += c.ms
                rows.append((name, c.queries))
                print(f"{name:<34}{c.queries:>9}{c.ms:>9.1f}  {flag}")
            except Exception as e:                       # noqa: BLE001
                print(f"{name:<34}{'ERR':>9}{'':>9}  {type(e).__name__}: {e}"[:100])
            finally:
                tenancy.reset_current_tenant(tok)
                db.close()
        print(f"{'TOTAL for one refresh':<34}{total_q:>9}{total_ms:>9.1f}")
        print()
        per_scale[scale] = dict(rows)

    # THE ACTUAL ANSWER. A table of numbers is not a finding; "this endpoint
    # costs more when the customer has more machines" is.
    lo, hi = min(per_scale), max(per_scale)
    print("=" * 62)
    print(f"VERDICT - does anything scale with the factory? ({lo} -> {hi} machines)")
    print("=" * 62)
    growing = [(n, per_scale[lo][n], q) for n, q in sorted(per_scale[hi].items())
               if n in per_scale[lo] and q > per_scale[lo][n]]
    if growing:
        print(f"  {len(growing)} endpoint(s) GREW - these are N+1s:")
        for n, a, b in growing:
            print(f"    {n:<38} {a:>4} -> {b:<5} (+{(b-a)/float(hi-lo):.2f}/machine)")
    else:
        print("  NONE. Every endpoint issues the same number of queries at")
        print(f"  {hi} machines as at {lo}. The poll cycle is flat.")
    print()
    print(f"  whole refresh: {sum(per_scale[lo].values())} queries at {lo} "
          f"machines, {sum(per_scale[hi].values())} at {hi}")
    print()
    print("  ms figures are this laptop only, not a production number.")
    print("  The query counts are the durable result.")
    return 0


if __name__ == "__main__":
    port = sys.argv[1] if len(sys.argv) > 1 else None
    if port:
        url = f"postgresql://postgres:postgres@localhost:{port}/postgres"
    else:
        url = os.environ.get("DATABASE_URL", "sqlite:///./perf.db")
    raise SystemExit(main(url))
