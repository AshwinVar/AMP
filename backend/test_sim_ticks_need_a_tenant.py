"""A simulator tick runs for ONE bound tenant, or not at all.

WHAT WAS MEASURED (2026-09-20)
------------------------------
main._simulation_loop binds each simulated tenant before its ticks and resets
after. With NOTHING bound, tenancy's read filter is off and its write stamp is
off, so a tick read EVERY tenant's machines and work orders and stamped what it
wrote DEFAULT -- the column default. Twelve unbound rounds against the
three-factory fixtures left 41 rows (telemetry, inspections, operator jobs, a
production record) filed under the demo tenant and pointing at FACTORY_A's, B's
and C's machines. Only the heartbeat refused (ADR-0021).

The loop never runs unbound. The CLI runner (`python factory_simulator.py`)
did, and so did any script or test that called a tick directly. Now every tick
refuses with none bound, naming the rule, and the CLI binds DEFAULT itself.

audit_three_factory_simulation.py §5 proves the same against the whole
three-factory world; this suite pins it in the fast sweep, per tick, and pins
the CLI's own binding, which the audit cannot reach.

Run: DATABASE_URL="sqlite://" python backend/test_sim_ticks_need_a_tenant.py
"""
import os
import random
import sys
from datetime import datetime

os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import factory_simulator as sim  # noqa: E402
import models  # noqa: E402
import tenancy  # noqa: E402
from database import Base  # noqa: E402

# Every tick main._simulation_loop runs, plus the two only the CLI runner runs.
TICKS = ("tick_work_order_progress", "tick_iot", "tick_production", "tick_machine_status",
         "tick_inventory", "tick_quality", "tick_shift_entry", "tick_operator",
         "tick_status_heartbeat", "tick_customer_order", "tick_escalation")
FAILURES = []
CHECKS = 0


def check(label, condition, detail=""):
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{label}: {detail}")


def section(title):
    print()
    print(title)
    print("-" * len(title))


def world():
    """Two tenants, each with everything a tick looks for: a machine (SIM_B's
    is in Breakdown, for tick_escalation), an In Progress work order, raw
    material with stock, an order in production. Nothing for DEFAULT."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    Session = sessionmaker(bind=engine)
    db = Session()
    tok = tenancy.set_current_tenant(None)
    try:
        for t in ("SIM_A", "SIM_B"):
            m = models.Machine(tenant_code=t, name="CNC-01", site="P1", line="L1", utilization=70,
                               downtime="0 min", status="Breakdown" if t == "SIM_B" else "Running")
            db.add(m)
            db.flush()
            db.add(models.WorkOrder(tenant_code=t, work_order_no="WO-001", part_number="FG-001", batch_number="B1",
                                    machine_id=m.id, target_quantity=100, actual_quantity=0, status="In Progress"))
            db.add(models.InventoryItem(tenant_code=t, item_code="RM-1", item_name=f"{t} steel", category="Raw Material",
                                        current_stock=500, reorder_level=50, unit="kg", supplier="Local"))
            db.add(models.CustomerOrder(tenant_code=t, order_no="ORD-1", customer_name=f"{t} customer",
                                        product_name="FG-001", order_quantity=100, dispatched_quantity=0,
                                        due_date=datetime.utcnow().date(), status="In Production"))
        db.commit()
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()
    return engine, Session


def tenant_tables():
    return [t.name for t in Base.metadata.sorted_tables if "tenant_code" in t.columns]


def rows_by_tenant(engine):
    """{tenant: rows} over every table that carries a tenant, read raw."""
    out = {}
    with engine.connect() as c:
        for table in tenant_tables():
            for tenant, n in c.execute(text(f"SELECT tenant_code, count(*) FROM {table} GROUP BY tenant_code")):
                out[tenant] = out.get(tenant, 0) + n
    return out


def main():
    random.seed(1)
    engine, Session = world()
    before = rows_by_tenant(engine)
    check("the world holds SIM_A's and SIM_B's rows and nobody else's", set(before) == {"SIM_A", "SIM_B"}, str(before))

    section("1. WITH NO TENANT BOUND, EVERY TICK REFUSES, NAMES THE RULE, AND WRITES NOTHING")
    db = Session()
    try:
        for name in TICKS:
            outcome = None
            try:
                getattr(sim, name)(db)
            except ValueError as e:
                outcome = str(e)
            except Exception as e:      # noqa: BLE001 - the wrong kind of refusal
                outcome = f"{type(e).__name__}: {e}"
            db.rollback()
            check(f"{name} refuses with a ValueError that says 'bound tenant'",
                  isinstance(outcome, str) and outcome.startswith("simulator ticks run for one bound tenant"),
                  str(outcome))
        check("nothing was written by the eleven refusals", rows_by_tenant(engine) == before,
              str(rows_by_tenant(engine)))
        # The CLI runner swallows a failing tick and prints a WARN so the person
        # watching sees which one; the refusal must NOT be swallowed that way.
        refused = False
        try:
            sim.run_simulation(db)
        except ValueError as e:
            refused = "bound tenant" in str(e)
        db.rollback()
        check("run_simulation (the CLI's tick chooser) refuses too, before choosing a tick", refused)
        check("and wrote nothing", rows_by_tenant(engine) == before, str(rows_by_tenant(engine)))
    finally:
        db.close()

    section("2. CONTROL: BOUND, THE SAME TICKS WRITE, AND ONLY THE BOUND TENANT'S ROWS")
    db = Session()
    tok = tenancy.set_current_tenant("SIM_A")
    try:
        check("_bound_tenant() returns the bound tenant", sim._bound_tenant() == "SIM_A")
        for _ in range(8):
            for name in TICKS:
                getattr(sim, name)(db)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()
    after = rows_by_tenant(engine)
    check("SIM_A gained rows", after.get("SIM_A", 0) > before["SIM_A"], f"{before['SIM_A']} -> {after.get('SIM_A')}")
    check("SIM_B did not", after.get("SIM_B") == before["SIM_B"], f"{before['SIM_B']} -> {after.get('SIM_B')}")
    check("no other tenant appeared", set(after) == {"SIM_A", "SIM_B"}, str(set(after)))

    section("3. THE CLI BINDS THE DEMO WORKSPACE ITSELF, AND LEAVES NOTHING BOUND BEHIND")
    # The CLI is `seed_all` then `run_cli`; the seed is DEFAULT's. Give DEFAULT
    # one running machine and a work order so its ticks have something to do.
    db = Session()
    tok = tenancy.set_current_tenant(None)
    try:
        m = models.Machine(tenant_code="DEFAULT", name="CNC-09", site="P1", line="L1", utilization=70,
                           downtime="0 min", status="Running")
        db.add(m)
        db.flush()
        db.add(models.WorkOrder(tenant_code="DEFAULT", work_order_no="WO-009", part_number="FG-001", batch_number="B1",
                                machine_id=m.id, target_quantity=100, actual_quantity=0, status="In Progress"))
        db.commit()
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()
    before = rows_by_tenant(engine)
    db = Session()
    try:
        check("nothing is bound before the CLI runs", tenancy.current_tenant() is None)
        sim.run_cli(db, rounds=6, pause=0)
        check("nothing is bound after it returns", tenancy.current_tenant() is None, str(tenancy.current_tenant()))
    finally:
        db.close()
    after = rows_by_tenant(engine)
    check("the CLI's rounds wrote DEFAULT rows", after.get("DEFAULT", 0) > before.get("DEFAULT", 0),
          f"{before.get('DEFAULT')} -> {after.get('DEFAULT')}")
    check("and touched neither SIM_A nor SIM_B",
          after.get("SIM_A") == before["SIM_A"] and after.get("SIM_B") == before["SIM_B"],
          f"A {before['SIM_A']}->{after.get('SIM_A')} B {before['SIM_B']}->{after.get('SIM_B')}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED of {CHECKS}")
        for f in FAILURES:
            print(" -", f)
        return 1
    print(f"ALL {CHECKS} SIM TENANT-GUARD TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
