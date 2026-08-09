"""How does the OEM fleet behave at 10, 100, 1,000 and 10,000 machines?

An OEM fleet is not a factory. A factory has tens of machines; a compressor
manufacturer with a few hundred customers has thousands, spread across sites it
does not control, and the fleet view is the first screen its service desk opens
every morning.

WHAT IS MEASURED
----------------
    fleet query           the paginated /oem/fleet path
    service queue         every recommendation across the whole fleet
    customer drill-down   one customer's machines
    QUERY COUNT           how many SQL statements each of those issued

The query count is the number that matters. Latency on a laptop says little
about a production database; "this endpoint issues one query per machine" says
the same thing on every machine anybody will ever run it on. It is measured by
counting real statements on the engine, not estimated.

Run: python backend/oem_perf.py [port]        (PostgreSQL only)
"""
import os
import statistics
import sys
import time
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SCALES = [10, 100, 1000, 10000]
CUSTOMERS = 8


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "5432"
    import pg_scratch
    version = pg_scratch.ensure(port, "amp_oem_perf")
    url = pg_scratch.scratch_url(port, "amp_oem_perf")
    os.environ["DATABASE_URL"] = url

    from sqlalchemy import event, create_engine
    import models
    import oem_service
    import oem_sharing
    import tenancy
    from database import Base

    engine = create_engine(url)
    from sqlalchemy.orm import sessionmaker
    Session = sessionmaker(bind=engine)

    print(version.split(",")[0])
    print(f"{CUSTOMERS} customers per fleet; grants on half of them\n")

    # Count every statement the engine actually executes.
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

    print(f"{'machines':>9} {'fleet page':>12} {'q':>4} "
          f"{'service queue':>15} {'q':>5} {'one customer':>14} {'q':>4}")
    print("-" * 72)

    results = {}
    for scale in SCALES:
        db = Session()
        tok = tenancy.set_current_tenant(None)
        db.query(models.MachineInstallation).delete()
        db.query(models.OemDataSharingPolicy).delete()
        db.query(models.MachineModel).delete()
        db.query(models.OemOrganization).delete()
        db.commit()

        db.add(models.OemOrganization(oem_code="OEM_PERF", name="Perf"))
        model = models.MachineModel(oem_code="OEM_PERF", family="Compressor",
                                    model_code="X200", name="X200",
                                    service_interval_hours=2000)
        db.add(model)
        db.flush()
        rows = []
        for i in range(scale):
            customer = f"FACTORY_{i % CUSTOMERS:02d}"
            rows.append(models.MachineInstallation(
                oem_code="OEM_PERF", serial_number=f"SN-{i:06d}",
                model_id=model.id, factory_tenant_code=customer, site="Plant 1",
                status="Active", operating_hours=1900.0 + (i % 400),
                last_seen_at=datetime.utcnow(),
                warranty_start=date(2026, 1, 1), warranty_end=date(2028, 1, 1)))
        db.bulk_save_objects(rows)
        for c in range(0, CUSTOMERS, 2):
            db.add(models.OemDataSharingPolicy(
                oem_code="OEM_PERF", tenant_code=f"FACTORY_{c:02d}",
                grants="SHARE_OPERATING_HOURS"))
        db.commit()
        tenancy.reset_current_tenant(tok)

        catalogue = {model.id: model}

        # 1. A fleet PAGE — what the screen actually asks for.
        page_ms, page_q = [], 0
        for _ in range(3):
            with Counted() as c:
                insts = oem_sharing.installations_for(db, "OEM_PERF")
                page = insts[:100]
                grants_cache = {}
                for inst in page:
                    t = inst.factory_tenant_code
                    if t not in grants_cache:
                        grants_cache[t] = oem_sharing.grants_for(db, "OEM_PERF", t)
                    oem_sharing.fleet_row(db, inst, grants_cache[t],
                                          catalogue.get(inst.model_id))
            page_ms.append(c.ms)
            page_q = c.queries

        # 2. The service queue across the WHOLE fleet.
        with Counted() as c:
            insts = oem_sharing.installations_for(db, "OEM_PERF")
            recs = oem_service.fleet_recommendations(db, "OEM_PERF", insts, catalogue)
        queue_ms, queue_q, n_recs = c.ms, c.queries, len(recs)

        # 3. One customer's machines.
        cust_ms, cust_q = [], 0
        for _ in range(3):
            with Counted() as c:
                one = oem_sharing.installations_for(db, "OEM_PERF",
                                                    tenant_code="FACTORY_00")
                g = oem_sharing.grants_for(db, "OEM_PERF", "FACTORY_00")
                for inst in one[:100]:
                    oem_sharing.fleet_row(db, inst, g, catalogue.get(inst.model_id))
            cust_ms.append(c.ms)
            cust_q = c.queries

        print(f"{scale:>9} {statistics.median(page_ms):>10.1f}ms {page_q:>4} "
              f"{queue_ms:>13.1f}ms {queue_q:>5} "
              f"{statistics.median(cust_ms):>12.1f}ms {cust_q:>4}")
        results[scale] = {
            "fleet_page_ms": round(statistics.median(page_ms), 1),
            "fleet_page_queries": page_q,
            "service_queue_ms": round(queue_ms, 1),
            "service_queue_queries": queue_q,
            "customer_ms": round(statistics.median(cust_ms), 1),
            "customer_queries": cust_q,
            "recommendations": n_recs,
        }
        db.close()

    print()
    print("QUERY COUNT IS THE NUMBER THAT MATTERS")
    print("-" * 72)
    counts = {s: r["fleet_page_queries"] for s, r in results.items()}
    flat = len(set(counts.values())) == 1
    print(f"  fleet page queries by scale: {counts}")
    print(f"  -> {'CONSTANT: no N+1' if flat else 'GROWS WITH FLEET SIZE — N+1'}")
    qcounts = {s: r["service_queue_queries"] for s, r in results.items()}
    qflat = len(set(qcounts.values())) == 1
    print(f"  service queue queries:       {qcounts}")
    print(f"  -> {'CONSTANT' if qflat else 'GROWS WITH FLEET SIZE'}")

    print()
    print("HONEST LIMITS")
    print("-" * 72)
    print("  * one machine, local PostgreSQL, no network between app and database")
    print("  * the fleet PAGE is measured, not 'render 10,000 rows' — the API")
    print("    caps a page at 200 and the screen asks for 100")
    print("  * the service queue deliberately walks the WHOLE fleet, which is why")
    print("    its wall-clock grows; it is a back-office view, not a poll")

    import json
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "oem_perf_results.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    print(f"\nraw results -> {out}")
    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
