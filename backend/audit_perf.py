"""PHASE 9 — measured read-model latency against real PostgreSQL at factory scale.

NOT a substitute for a k6 HTTP load test. This measures the DATABASE-side cost
of the read models the dashboard polls, in-process, with no HTTP, no network and
no concurrency. What it can answer: which query degrades first as a factory grows,
and roughly how fast. What it cannot answer: p95 under concurrent users, uvicorn
worker saturation, or WebSocket/MQTT throughput.

That distinction is the point. docs/PERFORMANCE.md's baseline tables stay
UNMEASURED because they specify an HTTP load profile this does not run.

Run:  DATABASE_URL=<disposable postgres> python backend/audit_perf.py
"""
import os
import statistics
from sqlalchemy import func
import sys
import time
from datetime import datetime, timedelta

SIZES = [("SMALL", 10), ("MEDIUM", 50), ("LARGE", 250)]
TENANT = "PERF_AUDIT"


def _purge(db, models, tenancy):
    """Delete this audit's rows CHILDREN-FIRST.

    PostgreSQL enforces the foreign keys (production_records.machine_id ->
    machines.id has no ON DELETE CASCADE), so deleting machines before their
    production records raises ForeignKeyViolation. SQLite does not enforce FKs
    unless PRAGMA foreign_keys is on, which is a real engine difference and the
    reason this audit runs on PostgreSQL.
    """
    from database import Base
    by_table = {m.__tablename__: m for m in tenancy.SCOPED_MODELS
                if hasattr(m, "tenant_code")}
    # Base.metadata.sorted_tables is parents-first; reversed is children-first.
    for table in reversed(Base.metadata.sorted_tables):
        model = by_table.get(table.name)
        if model is not None:
            db.query(model).filter(model.tenant_code == TENANT).delete(
                synchronize_session=False)
    db.commit()


def seed(db, models, tenancy, n_machines):
    """A factory of n machines with a day of production behind it."""
    tok = tenancy.set_current_tenant(None)
    _purge(db, models, tenancy)

    now = datetime.utcnow()
    machines = []
    for i in range(n_machines):
        m = models.Machine(name=f"PERF-{i:04d}", status="Running",
                           utilization=60 + (i % 35), tenant_code=TENANT)
        db.add(m)
        machines.append(m)
    db.commit()

    # One production record per machine per hour for 24h — the shape the OEE
    # read models actually aggregate over.
    recs = []
    for m in machines:
        for h in range(24):
            recs.append(models.ProductionRecord(
                machine_id=m.id, tenant_code=TENANT,
                planned_minutes=60, runtime_minutes=52,
                ideal_cycle_time_seconds=30,
                total_count=100, good_count=96, rejected_count=4,
                created_at=now - timedelta(hours=h)))
    db.bulk_save_objects(recs)
    db.commit()
    tenancy.reset_current_tenant(tok)
    return len(machines), len(recs)


def timeit(fn, rounds=5):
    """Median of `rounds`, after one warm-up. Median not mean: one slow first
    call (plan cache, cold buffers) should not become the headline."""
    try:
        fn()
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:70]}"
    ms = []
    for _ in range(rounds):
        t = time.perf_counter()
        try:
            fn()
        except Exception as e:
            return None, f"{type(e).__name__}: {str(e)[:70]}"
        ms.append((time.perf_counter() - t) * 1000)
    return statistics.median(ms), None


def main():
    import models
    import tenancy
    import analytics_engine
    from database import Base, engine, SessionLocal

    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    db = SessionLocal()

    print(f"engine: {engine.dialect.name} {'.'.join(str(x) for x in engine.dialect.server_version_info or [])}")
    print()
    header = f"{'probe':<34}" + "".join(f"{lbl:>12}" for lbl, _ in SIZES)
    rows = {}

    for label, n in SIZES:
        nm, nr = seed(db, models, tenancy, n)
        tok = tenancy.set_current_tenant(TENANT)

        probes = {
            "machine list (dashboard card)":
                lambda: db.query(models.Machine).all(),
            "production records (24h scan)":
                lambda: db.query(models.ProductionRecord).all(),
            # The ORM-hydrating path: fetch every record, then pool in Python.
            # This is the shape the older read models used and is the one that
            # grows with the factory.
            "pooled OEE (hydrate + pool)":
                lambda: analytics_engine.pooled_oee(
                    db.query(models.ProductionRecord).all()),
            # The SQL-aggregate path, mirroring analytics_routes.py:68-104
            # EXACTLY: sum(ideal*total) rather than avg(), and int() on every
            # value. Those int() casts are load-bearing on PostgreSQL, where
            # SUM over an Integer column returns Decimal — mixing Decimal with
            # float raises TypeError. SQLite returns float, so a test suite on
            # SQLite alone would never see it. Production casts; this measures
            # what production does.
            "pooled OEE (SQL aggregate)":
                lambda: analytics_engine.pooled_oee_from_sums(
                    *(lambda r: (int(r[0]), int(r[1]), int(r[2]), int(r[3]),
                                 int(r[4]), r[5] > 0))(
                        db.query(
                            func.coalesce(func.sum(models.ProductionRecord.planned_minutes), 0),
                            func.coalesce(func.sum(models.ProductionRecord.runtime_minutes), 0),
                            func.coalesce(func.sum(models.ProductionRecord.total_count), 0),
                            func.coalesce(func.sum(models.ProductionRecord.good_count), 0),
                            func.coalesce(func.sum(
                                models.ProductionRecord.ideal_cycle_time_seconds
                                * models.ProductionRecord.total_count), 0),
                            func.count(models.ProductionRecord.id),
                        ).one())),
        }
        for name, fn in probes.items():
            ms, err = timeit(fn)
            rows.setdefault(name, {})[label] = (ms, err)

        tenancy.reset_current_tenant(tok)
        print(f"  seeded {label:<7} machines={nm:<5} production_records={nr}")

    print()
    print(header)
    print("-" * len(header))
    for name, per in rows.items():
        line = f"{name:<34}"
        for label, _ in SIZES:
            ms, err = per.get(label, (None, "n/a"))
            line += f"{('ERR' if err else f'{ms:.1f}ms'):>12}"
        print(line)
        for label, _ in SIZES:
            ms, err = per.get(label, (None, None))
            if err:
                print(f"    {label}: {err}")

    # cleanup
    tok = tenancy.set_current_tenant(None)
    _purge(db, models, tenancy)
    tenancy.reset_current_tenant(tok)
    db.close()
    print()
    print("Cleaned up. Median of 5 runs after warm-up; single-threaded, no HTTP.")


if __name__ == "__main__":
    if "postgresql" not in os.environ.get("DATABASE_URL", ""):
        print("REFUSING: point DATABASE_URL at a disposable PostgreSQL database.")
        sys.exit(2)
    main()
