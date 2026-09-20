"""Three factories animated by one simulator loop, and nothing crosses.

WHY THIS EXISTS, WHEN TWO ISOLATION AUDITS ALREADY PASS
-------------------------------------------------------
`audit_three_customers.py` proves three tenants' data never cross when READ
through every surface. `audit_isolation.py` proves every scoped model, read and
written by REQUEST code, stays with its tenant. Both exercise writers that run
inside a request: a user, a JWT, a middleware that bound the tenant.

The simulator is the one writer in AMP that has none of those. It runs with no
request and no user, for several tenants from ONE loop, every 45 seconds, for
as long as the process lives (main._simulation_loop). Its only scoping is a
contextvar the loop binds per tenant before each tick and resets after. Nothing
proved that the rows it writes land where it was bound -- and nothing at all
said what a tick does when NOTHING is bound.

Measured before this audit, with no tenant bound: every tick read every
tenant's machines and work orders (the read filter is off when no tenant is
bound) and stamped what it wrote DEFAULT (the column default). Twelve rounds
left 41 rows -- telemetry, inspections, operator jobs, a production record --
filed under the demo tenant and pointing at FACTORY_A's, B's and C's
machines. Section 5 pins the guard that now refuses that.

WHAT IS PROVED
--------------
    1  the loop      main._simulation_loop's exact tick sequence, per tenant,
                     bound the way main binds it, for N rounds
    2  ownership     every row the ticks wrote belongs to the tenant whose
                     tick wrote it, in every table that carries a tenant
    3  parents       no row points at another tenant's machine, work order,
                     item or plan (the shape of the defect measured above)
    4  movement      each factory actually advanced, so a pass is not vacuous;
                     and where a factory has nothing to advance (C has no work
                     orders) the tick did nothing, which is right
    5  no tenant     with none bound every tick refuses and writes nothing;
                     CONTROL: bound, the same tick writes
    6  nobody else   no row ever lands in DEFAULT, a NULL tenant or the OEM
                     sentinel namespace -- nothing seeded those
    7  numbers       the documents the ticks issue come from each tenant's own
                     sequence: A and B both issued the same number, which is
                     two documents, not one collision
    8  the screens   after simulation, each owner's surfaces carry none of the
                     other factories' words; and reading them wrote nothing

A, B and C come from copilot_eval.fixtures -- the three-factory environment
ADR-0022 built, with colliding identifiers and per-tenant MARKERS -- and the
owner's surfaces are built by audit_owner_questions.surfaces, the same calls
the routes make. This audit adds only the simulation.

Run: python backend/audit_three_factory_simulation.py            (SQLite, in memory)
     python backend/audit_three_factory_simulation.py --pg       (a disposable PostgreSQL,
                                                                  borrowing local credentials)
"""
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# --pg: the same audit against a disposable PostgreSQL database (pg_scratch),
# decided BEFORE `database` is imported, because that module builds its engine
# from DATABASE_URL at import time. PostgreSQL enforces the foreign keys and
# unique constraints SQLite leaves unchecked by default, so a tick that wrote a
# row pointing at a parent that does not exist, or a document number twice,
# would pass here on SQLite and be refused there -- the difference this mode
# exists to catch. CI runs both.
if "--pg" in sys.argv:
    import pg_scratch
    print(pg_scratch.ensure(5432, "amp_three_factory_sim").split(",")[0])
    os.environ["DATABASE_URL"] = pg_scratch.scratch_url(5432, "amp_three_factory_sim")
else:
    os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import factory_simulator as sim  # noqa: E402
import models  # noqa: E402
import tenancy  # noqa: E402
from database import Base  # noqa: E402
from copilot_eval import fixtures as F  # noqa: E402
from audit_owner_questions import surfaces  # noqa: E402

FAILURES = []
CHECKS = 0

A, B, C = F.A, F.B, F.C
TENANTS = (A, B, C)
ROUNDS = 20
SEED = 20260920      # the date this audit was written; any fixed seed will do

# main._simulation_loop, in order. There the last seven run behind random
# cadence gates (`if random.random() < 0.2`): those decide HOW OFTEN a tick
# runs, never WHAT it may touch, so here every tick runs every round and each
# is exercised ROUNDS times per tenant. tick_industrial is the PLC poller; with
# no devices seeded it is a no-op, and it is kept in the sequence because the
# loop runs it there.
def loop_sequence(db):
    import industrial_adapters
    from ai import agents
    sim.tick_work_order_progress(db)
    sim.tick_iot(db)
    industrial_adapters.tick_industrial(db)
    sim.tick_production(db)
    sim.tick_machine_status(db)
    sim.tick_inventory(db)
    sim.tick_quality(db)
    sim.tick_shift_entry(db)
    sim.tick_operator(db)
    for m in db.query(models.Machine).filter(models.Machine.status == "Running").all():
        m.utilization = sim.drift_utilization(m.utilization, random.randint(-5, 5))
    db.commit()
    sim.tick_status_heartbeat(db)
    # The loop's second pass: the escalation agent, bound per tenant as well.
    agents.escalate_from_briefing(db, tenancy.current_tenant())
    db.commit()


# The ticks the CLI runner (factory_simulator.run_simulation) adds to the set.
CLI_TICKS = ("tick_customer_order", "tick_escalation")
ALL_TICKS = ("tick_work_order_progress", "tick_iot", "tick_production", "tick_machine_status",
             "tick_inventory", "tick_quality", "tick_shift_entry", "tick_operator",
             "tick_status_heartbeat") + CLI_TICKS


def check(label, condition, detail=""):
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{label}: {detail}")


def banner(n, title):
    print()
    print("=" * 78)
    print(f"{n}. {title}")
    print("=" * 78)


def build():
    if os.environ["DATABASE_URL"].startswith("postgresql"):
        from database import engine    # the disposable scratch database chosen at the top
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
    else:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                               poolclass=StaticPool)
        Base.metadata.create_all(engine)
    tenancy.install_scoping()
    Session = sessionmaker(bind=engine)
    F.seed(Session)
    # tick_inventory issues "Raw Material" stock with more than 20 on hand; the
    # fixtures' items are category "Raw". One eligible item per factory, with a
    # marker name, so the consumption ledger is exercised and attributable.
    db = Session()
    tok = tenancy.set_current_tenant(None)
    try:
        for t in TENANTS:
            db.add(models.InventoryItem(tenant_code=t, item_code="RM-SIM", item_name=f"{t} sim stock",
                                        category="Raw Material", current_stock=500, reorder_level=50,
                                        unit="kg", supplier="Local"))
        db.commit()
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()
    return engine, Session


# ---------------------------------------------------------------------------
# Every table that carries a tenant, read raw: the audit must not itself go
# through the scoped ORM to decide whether the scoped ORM misfiled a row.
# ---------------------------------------------------------------------------
def tenant_tables():
    return sorted(t.name for t in Base.metadata.sorted_tables if "tenant_code" in t.columns)


def rows(engine, table):
    """{id: tenant_code} for a table with an id column, else the multiset of rows."""
    cols = Base.metadata.tables[table].columns
    with engine.connect() as c:
        if "id" in cols:
            return {i: t for i, t in c.execute(text(f"SELECT id, tenant_code FROM {table}"))}
        return {tuple(r): None for r in c.execute(text(f"SELECT * FROM {table}"))}


def snapshot(engine):
    return {table: rows(engine, table) for table in tenant_tables()}


def new_rows(before, after):
    """{table: {id: tenant}} of rows present after and not before."""
    out = {}
    for table, now in after.items():
        fresh = {k: v for k, v in now.items() if k not in before.get(table, {})}
        if fresh:
            out[table] = fresh
    return out


def parent_links():
    """(child table, fk column, parent table) for every foreign key between two
    tenant-carrying tables: the joins along which a misfiled row shows."""
    links = []
    tenant = set(tenant_tables())
    for table in Base.metadata.sorted_tables:
        if table.name not in tenant:
            continue
        for fk in table.foreign_keys:
            parent = fk.column.table.name
            if parent in tenant and fk.column.name == "id":
                links.append((table.name, fk.parent.name, parent))
    return sorted(links)


def cross_parent_rows(engine):
    """Rows whose tenant differs from the tenant of the parent row they point at."""
    found = []
    with engine.connect() as c:
        for child, col, parent in parent_links():
            n = c.execute(text(f"SELECT count(*) FROM {child} r JOIN {parent} p ON p.id = r.{col} "
                               f"WHERE r.tenant_code != p.tenant_code")).scalar()
            if n:
                found.append(f"{child}.{col}->{parent}: {n}")
    return found


def counts(engine, table):
    with engine.connect() as c:
        return {t: n for t, n in
                c.execute(text(f"SELECT tenant_code, count(*) FROM {table} GROUP BY tenant_code"))}


def simulate(engine, Session, rounds):
    """main._simulation_loop, for `rounds` passes over the three tenants. Returns
    {tenant: {table: {id: tenant_code}}} of the rows each tenant's ticks wrote."""
    written = {t: {} for t in TENANTS}
    db = Session()
    try:
        for _ in range(rounds):
            for t in TENANTS:
                before = snapshot(engine)
                scope = tenancy.set_current_tenant(t)
                try:
                    loop_sequence(db)
                except Exception as e:      # noqa: BLE001 - the loop logs and rolls back too
                    db.rollback()
                    check(f"{t}: a round of the loop completed", False, f"{type(e).__name__}: {e}")
                finally:
                    tenancy.reset_current_tenant(scope)
                for table, fresh in new_rows(before, snapshot(engine)).items():
                    written[t].setdefault(table, {}).update(fresh)
    finally:
        db.close()
    return written


def main():
    random.seed(SEED)
    engine, Session = build()
    seeded = snapshot(engine)
    seeded_tenants = {v for table in seeded.values() for v in table.values() if v}
    print(f"database: {'PostgreSQL (disposable scratch)' if os.environ['DATABASE_URL'].startswith('postgresql') else 'SQLite, in memory'}; "
          f"tables carrying a tenant: {len(tenant_tables())}; parent links: {len(parent_links())}; "
          f"tenants seeded: {sorted(seeded_tenants)}")

    # ------------------------------------------------------------ 1 + 2 + 3 --
    banner(1, f"THE LOOP'S TICK SEQUENCE, PER TENANT, {ROUNDS} ROUNDS")
    written = simulate(engine, Session, ROUNDS)
    total = sum(len(ids) for tables in written.values() for ids in tables.values())
    check(f"the loop wrote rows ({total})", total > 0)

    banner(2, "EVERY ROW A TICK WROTE BELONGS TO THE TENANT IT RAN FOR")
    for t in TENANTS:
        for table, fresh in sorted(written[t].items()):
            wrong = {i: v for i, v in fresh.items() if v != t}
            check(f"{t}: {len(fresh)} new {table} row(s) are {t}'s", not wrong,
                  f"misfiled {len(wrong)}: {sorted(set(wrong.values()))}")

    banner(3, "NO ROW POINTS AT ANOTHER TENANT'S PARENT")
    crossed = cross_parent_rows(engine)
    check(f"across {len(parent_links())} parent links, no child row names another tenant's parent",
          not crossed, "; ".join(crossed))

    # ---------------------------------------------------------------- 4 --
    banner(4, "EACH FACTORY ADVANCED, AND ONLY WHERE IT HAD SOMETHING TO ADVANCE")
    per_table = {table: counts(engine, table) for table in tenant_tables()}
    for t in TENANTS:
        for table in ("iot_telemetry", "shift_data", "production_records", "machine_events",
                      "inventory_transactions", "machine_telemetry_spans"):
            check(f"{t}: {table} grew ({len(written[t].get(table, {}))})",
                  len(written[t].get(table, {})) > 0)
    # quality and operator ticks need an In Progress work order. A and B have
    # them; C has no work orders at all, so those ticks must have done nothing
    # for C -- an honest no-op, not a silent write against somebody else's order.
    for t in (A, B):
        for table in ("quality_inspections", "operator_job_executions"):
            check(f"{t}: {table} grew ({len(written[t].get(table, {}))})",
                  len(written[t].get(table, {})) > 0)
    for table in ("quality_inspections", "operator_job_executions"):
        check(f"CONTROL {C}: {table} did not grow (C has no work orders)",
              table not in written[C], f"{len(written[C].get(table, {}))} rows")
    # B's CNC-01 is seeded in Breakdown: its escalation pass may raise; A's and
    # C's plants are healthy. What matters is where whatever was raised went.
    for t in TENANTS:
        esc = per_table["escalations"].get(t, 0)
        print(f"        {t}: {esc} escalation(s) after simulation")

    # ---------------------------------------------------------------- 5 --
    banner(5, "WITH NO TENANT BOUND, EVERY TICK REFUSES AND WRITES NOTHING")
    db = Session()
    tok = tenancy.set_current_tenant(None)
    try:
        for name in ALL_TICKS:
            before = snapshot(engine)
            refused = False
            try:
                getattr(sim, name)(db)
            except ValueError as e:
                refused = "bound tenant" in str(e)
            except Exception as e:     # noqa: BLE001 - any other exception is the wrong refusal
                db.rollback()
                refused = f"{type(e).__name__}: {e}"
            db.rollback()
            wrote = new_rows(before, snapshot(engine))
            check(f"{name}: refuses with no tenant bound, naming the tenant", refused is True,
                  str(refused))
            check(f"{name}: wrote nothing with no tenant bound", not wrote,
                  {k: len(v) for k, v in wrote.items()})
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()
    # CONTROL: the same ticks, bound, write -- so the section above is testing
    # the guard and not a tick that never writes.
    db = Session()
    tok = tenancy.set_current_tenant(A)
    try:
        before = snapshot(engine)
        sim.tick_iot(db)
        sim.tick_shift_entry(db)
        wrote = new_rows(before, snapshot(engine))
        check("CONTROL: bound to FACTORY_A, tick_iot and tick_shift_entry write FACTORY_A rows",
              set(wrote) >= {"iot_telemetry", "shift_data"}
              and all(v == A for fresh in wrote.values() for v in fresh.values()),
              str({k: sorted(set(v.values())) for k, v in wrote.items()}))
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()

    # ---------------------------------------------------------------- 6 --
    banner(6, "NOTHING LANDED IN DEFAULT, A NULL TENANT OR THE OEM NAMESPACE")
    # Re-read after section 5: whatever an unbound tick wrote must show here too.
    per_table = {table: counts(engine, table) for table in tenant_tables()}
    strays = {}
    for table, by_tenant in per_table.items():
        for tenant, n in by_tenant.items():
            if tenant is None or tenant == tenancy.DEFAULT_TENANT or tenancy.is_reserved_tenant_code(tenant):
                strays[f"{table}/{tenant}"] = n
    check("no table holds a row for DEFAULT, NULL or a reserved tenant", not strays, str(strays))
    check("every tenant with rows after simulation was one of the three seeded",
          {v for by_tenant in per_table.values() for v in by_tenant} <= set(TENANTS),
          str({v for by_tenant in per_table.values() for v in by_tenant} - set(TENANTS)))

    # ---------------------------------------------------------------- 7 --
    banner(7, "THE DOCUMENTS THE TICKS ISSUED COME FROM EACH TENANT'S OWN SEQUENCE")
    with engine.connect() as c:
        issued = {t: {n for (n,) in c.execute(text(
            "SELECT inspection_no FROM quality_inspections WHERE tenant_code = :t AND inspector != 'QA'"),
            {"t": t})} for t in (A, B)}
    check(f"A issued {len(issued[A])} inspection numbers, none reused",
          len(issued[A]) == len(written[A].get("quality_inspections", {})) > 0)
    check(f"B issued {len(issued[B])} inspection numbers, none reused",
          len(issued[B]) == len(written[B].get("quality_inspections", {})) > 0)
    check("A and B issued the SAME numbers: two sequences, two documents, no collision",
          len(issued[A] & issued[B]) > 0, f"A={sorted(issued[A])[:3]} B={sorted(issued[B])[:3]}")

    # ---------------------------------------------------------------- 8 --
    banner(8, "AFTER SIMULATION, EACH OWNER'S SCREENS CARRY ONLY THEIR OWN FACTORY")
    before = snapshot(engine)
    payloads = {t: json.dumps(surfaces(Session, t), default=str) for t in TENANTS}
    for t in TENANTS:
        for other, markers in F.MARKERS.items():
            if other == t:
                continue
            seen = [m for m in markers if m in payloads[t]]
            check(f"{t}'s surfaces carry none of {other}'s words", not seen, str(seen))
    check("CONTROL: B's surfaces do name B's own machine (the marker search works)",
          "WELD-07" in payloads[B])
    wrote = new_rows(before, snapshot(engine))
    check("reading every owner surface wrote nothing", not wrote, {k: len(v) for k, v in wrote.items()})

    print()
    print("=" * 74)
    if FAILURES:
        print(f"{len(FAILURES)} FAILED of {CHECKS}")
        for f in FAILURES:
            print(" -", f)
        print("=" * 74)
        return 1
    print(f"ALL {CHECKS} CHECKS PASSED")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
