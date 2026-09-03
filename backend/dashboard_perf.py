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

    # The endpoints one refresh actually hits. Names are the handler functions;
    # a handler that cannot be resolved is reported rather than skipped silently,
    # because a quietly-missing endpoint would understate the whole cycle.
    import analytics_routes
    import machines_routes
    import read_model_routes
    import work_orders_routes

    user = {"sub": "perf", "role": "Admin", "tenant": TENANT}

    CYCLE = [
        ("/machines", lambda db: machines_routes.get_machines(db=db, current_user=user)),
        ("/downtime-logs", lambda db: machines_routes.get_downtime_logs(db=db, current_user=user)),
        ("/shifts", lambda db: machines_routes.get_shifts(db=db, current_user=user)),
        ("/work-orders", lambda db: work_orders_routes.get_work_orders(db=db, current_user=user)),
        ("/oee-summary", lambda db: read_model_routes.get_oee_summary(db=db, current_user=user)),
        ("/machine-health", lambda db: read_model_routes.get_machine_health(db=db, current_user=user)),
        ("/losses-summary", lambda db: read_model_routes.get_losses_summary(db=db, current_user=user)),
        ("/analytics/machine-timeline",
         lambda db: analytics_routes.get_machine_timeline(db=db, current_user=user)),
    ]

    print(f"Measuring {len(CYCLE)} of the ~46 endpoints one refresh issues.")
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
        for name, call in CYCLE:
            db = Session()
            tok = tenancy.set_current_tenant(TENANT)
            try:
                with Counted() as c:
                    call(db)
                flag = ""
                total_q += c.queries
                total_ms += c.ms
                print(f"{name:<34}{c.queries:>9}{c.ms:>9.1f}  {flag}")
            except Exception as e:                       # noqa: BLE001
                print(f"{name:<34}{'ERR':>9}{'':>9}  {type(e).__name__}: {e}"[:100])
            finally:
                tenancy.reset_current_tenant(tok)
                db.close()
        print(f"{'TOTAL (measured subset)':<34}{total_q:>9}{total_ms:>9.1f}")
        print(f"{'extrapolated to 46 endpoints':<34}"
              f"{int(total_q * 46 / max(len(CYCLE), 1)):>9}"
              f"{total_ms * 46 / max(len(CYCLE), 1):>9.1f}   per refresh\n")

    print("READ THIS AS: a query count that RISES with machine count is an N+1")
    print("and is the finding worth acting on. A flat count is fine at any size.")
    return 0


if __name__ == "__main__":
    port = sys.argv[1] if len(sys.argv) > 1 else None
    if port:
        url = f"postgresql://postgres:postgres@localhost:{port}/postgres"
    else:
        url = os.environ.get("DATABASE_URL", "sqlite:///./perf.db")
    raise SystemExit(main(url))
