"""Three independent manufacturing customers, on one AMP, on real PostgreSQL.

WHY THIS EXISTS ALONGSIDE audit_isolation.py
--------------------------------------------
`audit_isolation.py` gives each tenant DISTINCT names -- A-CNC-00, B-PRESS-00,
C-WELD-00 -- so a leak is unmistakable in the output. That is a good property
and a real limitation: distinct names make a COLLISION impossible, so it cannot
test the case that actually breaks multi-tenant systems.

Every defect this campaign found was a collision defect:

    #502  MQTT resolved a machine by name across tenants, so three customers'
          CNC-01 were one machine
    #503  document numbers were globally unique, so FACTORY_B's first issue
          slip 500'd on FACTORY_A's number
    #504  a bill of materials was global, so FACTORY_B's stock moved at GMATS's
          recipe

So this harness does the opposite: all three customers use the SAME identifiers
-- CNC-01, LINE-01, WO-001, INV-001, PO-001, FG-001 -- and differ only in the
data behind them. Anything that keys on an identifier instead of (tenant,
identifier) collapses immediately and visibly.

WHAT IS PROVED, AND THROUGH WHICH SURFACE
-----------------------------------------
    1  storage     every scoped model, read while bound to each tenant
    2  identity    the same machine name is three different machines
    3  documents   all three issue WO-001 / PO-001 of their own
    4  BOM         the same part number explodes into different components
    5  inventory   the same item code holds different stock
    6  OEE         hand-computed per tenant, and the plant figures differ
    7  MQTT        a message on A's topic never touches B's or C's machine
    8  WebSocket   a broadcast for A reaches only A's socket
    9  approvals   A's Admin cannot decide B's agent action
   10  AI context  the assistant's context for A contains none of B's numbers
   11  exports     a CSV export carries only the caller's rows

Run:  DATABASE_URL=<postgres url> python backend/audit_three_customers.py
      python backend/audit_three_customers.py --pg      (borrows local creds)
"""
import asyncio
import csv
import io
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TENANTS = ["FACTORY_A", "FACTORY_B", "FACTORY_C"]

# Deliberately identical across all three. The whole point.
MACHINE_NAMES = ["CNC-01", "LINE-01"]
WORK_ORDER_NO = "WO-001"
ITEM_CODE = "INV-001"
PO_NO = "PO-001"
PART_NUMBER = "FG-001"

# What differs: the data behind the identical names. Chosen so every number is
# recognisable in the output -- a leak shows up as the wrong VALUE, not as a
# count that happens to coincide.
PROFILE = {
    # tenant:      site,        stock, per-unit, component,      OEE inputs
    "FACTORY_A": {"site": "Chennai", "stock": 100, "qty": 2.0,
                  "component": "RM-STEEL", "unit": "kg",
                  # planned, runtime, ideal_s, total, good
                  "prod": (480, 400, 30, 600, 570)},
    "FACTORY_B": {"site": "Pune", "stock": 250, "qty": 5.0,
                  "component": "RM-ALLOY", "unit": "kg",
                  "prod": (480, 480, 60, 480, 480)},
    "FACTORY_C": {"site": "Coimbatore", "stock": 7, "qty": 0.5,
                  "component": "RM-RESIN", "unit": "L",
                  "prod": (600, 300, 60, 200, 100)},
}

failures = []
notes = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def banner(n, title):
    print()
    print("=" * 78)
    print(f"{n}. {title}")
    print("=" * 78)


def main():
    if "--pg" in sys.argv:
        import pg_scratch
        version = pg_scratch.ensure(5432, "amp_three_customers")
        os.environ["DATABASE_URL"] = pg_scratch.scratch_url(
            5432, "amp_three_customers")
        print(version.split(",")[0])
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        print("REFUSING to run on anything but PostgreSQL: the point of this "
              "audit is to stop trusting that SQLite and PostgreSQL agree.")
        return 2
    print(f"database: ...{url[-28:]}")

    import bom
    import doc_numbers
    import live_ws
    import models
    import mqtt_identity
    import oee_contract
    import tenancy
    import approvals
    from database import Base, engine, SessionLocal

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    db = SessionLocal()

    # ---------------------------------------------------------------- seed --
    tok = tenancy.set_current_tenant(None)
    machines = {}
    for t in TENANTS:
        p = PROFILE[t]
        db.add(models.User(username=f"{t.lower()}-admin", password="x",
                           role="Admin", tenant_code=t, is_active=True))
        db.add(models.User(username=f"{t.lower()}-op", password="x",
                           role="Operator", tenant_code=t, is_active=True))
        for name in MACHINE_NAMES:
            m = models.Machine(tenant_code=t, name=name, site=p["site"],
                               status="Running", utilization=70)
            db.add(m)
            db.flush()
            machines[(t, name)] = m.id
        db.add(models.InventoryItem(
            tenant_code=t, item_code=ITEM_CODE, item_name=f"{t} widget",
            category="Raw", unit=p["unit"], current_stock=p["stock"],
            reorder_level=5))
        db.add(models.InventoryItem(
            tenant_code=t, item_code=p["component"], item_name=p["component"],
            category="Raw", unit=p["unit"], current_stock=p["stock"],
            reorder_level=5))
        db.add(models.WorkOrder(
            tenant_code=t, work_order_no=WORK_ORDER_NO, part_number=PART_NUMBER,
            batch_number=f"B-{t[-1]}", machine_id=machines[(t, "CNC-01")],
            target_quantity=p["stock"], status="Planned"))
        db.add(models.PurchaseOrder(
            tenant_code=t, po_no=PO_NO, item_name=p["component"],
            order_quantity=p["stock"], unit=p["unit"],
            expected_delivery_date=datetime.utcnow().date(), status="Draft"))
        planned, runtime, ideal, total, good = p["prod"]
        db.add(models.ProductionRecord(
            tenant_code=t, machine_id=machines[(t, "CNC-01")],
            planned_minutes=planned, runtime_minutes=runtime,
            ideal_cycle_time_seconds=ideal, total_count=total,
            good_count=good, rejected_count=total - good,
            created_at=datetime.utcnow() - timedelta(hours=1)))
        db.add(models.DowntimeLog(
            tenant_code=t, machine_id=machines[(t, "LINE-01")],
            reason=f"{t} breakdown", duration=f"{p['stock']} min"))
        db.add(models.MaintenanceTask(
            tenant_code=t, task_no=f"MT-001", machine_id=machines[(t, "LINE-01")],
            task_type="Preventive", priority="Medium", assigned_to=f"{t} crew",
            planned_date=datetime.utcnow().date(), status="Open"))
        db.add(models.QualityInspection(
            tenant_code=t, inspection_no="QC-001", inspector=f"{t} QA",
            machine_id=machines[(t, "CNC-01")], inspected_quantity=total,
            failed_quantity=total - good, status="Completed"))
        b = models.BillOfMaterials(tenant_code=t, part_number=PART_NUMBER,
                                   revision="A", output_item_code=ITEM_CODE,
                                   active=True)
        db.add(b)
        db.flush()
        db.add(models.BomComponent(tenant_code=t, bom_id=b.id,
                                   component_code=p["component"],
                                   quantity_per_unit=p["qty"], unit=p["unit"]))
    db.commit()
    tenancy.reset_current_tenant(tok)
    print(f"seeded {len(TENANTS)} customers, all using "
          f"{', '.join(MACHINE_NAMES)}, {WORK_ORDER_NO}, {ITEM_CODE}, "
          f"{PO_NO}, {PART_NUMBER}")

    # -------------------------------------------------------------- 1 read --
    banner(1, "EVERY SCOPED READ RETURNS ONLY THE BOUND TENANT'S ROWS")
    for t in TENANTS:
        tok = tenancy.set_current_tenant(t)
        seen = {
            "machines": db.query(models.Machine).all(),
            "work orders": db.query(models.WorkOrder).all(),
            "inventory": db.query(models.InventoryItem).all(),
            "purchase orders": db.query(models.PurchaseOrder).all(),
            "production": db.query(models.ProductionRecord).all(),
            "downtime": db.query(models.DowntimeLog).all(),
            "maintenance": db.query(models.MaintenanceTask).all(),
            "quality": db.query(models.QualityInspection).all(),
            "BOMs": db.query(models.BillOfMaterials).all(),
        }
        tenancy.reset_current_tenant(tok)
        foreign = {kind: [r.tenant_code for r in rows if r.tenant_code != t]
                   for kind, rows in seen.items()}
        leaks = {k: v for k, v in foreign.items() if v}
        check(f"{t} sees only its own rows across {len(seen)} models",
              not leaks, str(leaks))

    # ---------------------------------------------------------- 2 identity --
    banner(2, "THE SAME MACHINE NAME IS THREE DIFFERENT MACHINES")
    ids = {t: machines[(t, "CNC-01")] for t in TENANTS}
    check("CNC-01 has three distinct ids", len(set(ids.values())) == 3, str(ids))
    for t in TENANTS:
        tok = tenancy.set_current_tenant(t)
        rows = db.query(models.Machine).filter(
            models.Machine.name == "CNC-01").all()
        tenancy.reset_current_tenant(tok)
        check(f"{t} resolving 'CNC-01' gets exactly one machine, at {PROFILE[t]['site']}",
              len(rows) == 1 and rows[0].site == PROFILE[t]["site"],
              f"{len(rows)} rows: {[(r.site, r.tenant_code) for r in rows]}")

    # --------------------------------------------------------- 3 documents --
    banner(3, "ALL THREE ISSUE THEIR OWN WO-001 AND PO-001")
    for t in TENANTS:
        tok = tenancy.set_current_tenant(None)
        n = db.query(models.WorkOrder).filter(
            models.WorkOrder.work_order_no == WORK_ORDER_NO,
            models.WorkOrder.tenant_code == t).count()
        tenancy.reset_current_tenant(tok)
        check(f"{t} holds a {WORK_ORDER_NO} of its own", n == 1, str(n))
    # The real #503 guarantee is not that the three numbers DIFFER -- they
    # should be identical, because each tenant numbers from one. It is that all
    # three can be WRITTEN. Before the fix, FACTORY_B's first issue slip 500'd
    # on a number FACTORY_A had already used, so allocate-then-insert is the
    # only assertion that reproduces it.
    issued = {}
    for t in TENANTS:
        issued[t] = doc_numbers.allocate(db, t, "work_order", models.WorkOrder,
                                         "work_order_no", "WO")
    db.commit()
    check("every tenant numbers from its own sequence (same value, no clash)",
          len(set(issued.values())) == 1 and all(issued.values()), str(issued))

    tok = tenancy.set_current_tenant(None)
    wrote = {}
    for t in TENANTS:
        try:
            db.add(models.WorkOrder(
                tenant_code=t, work_order_no=issued[t], part_number=PART_NUMBER,
                batch_number="B2", machine_id=machines[(t, "CNC-01")],
                target_quantity=1, status="Planned"))
            db.commit()
            wrote[t] = "written"
        except Exception as e:
            db.rollback()
            wrote[t] = f"REFUSED {type(e).__name__}"
    tenancy.reset_current_tenant(tok)
    check(f"all three wrote {issued['FACTORY_A']} without colliding",
          all(v == "written" for v in wrote.values()), str(wrote))
    print(f"      issued: {issued['FACTORY_A']} to all three; writes: "
          f"{sorted(set(wrote.values()))}")

    # -------------------------------------------------------------- 4 BOM --
    banner(4, "THE SAME PART NUMBER EXPLODES INTO DIFFERENT COMPONENTS")
    for t in TENANTS:
        tok = tenancy.set_current_tenant(t)
        resolved = bom.resolve(db, t, PART_NUMBER)
        comps = bom.components_of(resolved) if resolved else []
        tenancy.reset_current_tenant(tok)
        # components_of returns (code, qty_per_unit, unit) tuples, not ORM rows.
        want = (PROFILE[t]["component"], PROFILE[t]["qty"], PROFILE[t]["unit"])
        got = [tuple(c) for c in comps]
        check(f"{t}: {PART_NUMBER} -> {want[0]} x {want[1]} {want[2]}",
              got == [want], str(got))

    # -------------------------------------------------------- 5 inventory --
    banner(5, "THE SAME ITEM CODE HOLDS DIFFERENT STOCK")
    for t in TENANTS:
        tok = tenancy.set_current_tenant(t)
        item = db.query(models.InventoryItem).filter(
            models.InventoryItem.item_code == ITEM_CODE).one()
        tenancy.reset_current_tenant(tok)
        check(f"{t}: {ITEM_CODE} = {PROFILE[t]['stock']} {PROFILE[t]['unit']}",
              item.current_stock == PROFILE[t]["stock"]
              and item.unit == PROFILE[t]["unit"],
              f"{item.current_stock} {item.unit}")

    # -------------------------------------------------------------- 6 OEE --
    banner(6, "OEE IS COMPUTED PER CUSTOMER, AND HAND-CHECKED")
    # Derived by hand from ADR-0014, not from a program run:
    #   A: A=400/480=.8333  P=(30*600)/(400*60)=.75   Q=570/600=.95  -> 59%
    #   B: A=480/480=1.0    P=(60*480)/(480*60)=1.0   Q=480/480=1.0  -> 100%
    #   C: A=300/600=.5     P=(60*200)/(300*60)=.667  Q=100/200=.5   -> 17%
    expected = {"FACTORY_A": 59, "FACTORY_B": 100, "FACTORY_C": 17}
    got = {}
    for t in TENANTS:
        tok = tenancy.set_current_tenant(t)
        plant = oee_contract.plant_oee(db, t)
        tenancy.reset_current_tenant(tok)
        pct = oee_contract.as_percentages(plant)
        got[t] = pct.get("oee")
        check(f"{t} plant OEE = {expected[t]}% (hand-derived)",
              pct.get("oee") == expected[t], str(pct))
    check("...and the three figures are genuinely different",
          len(set(got.values())) == 3, str(got))
    # CONTROL: if the tenant filter were dropped, all three would pool into one
    # number, and every assertion above would fail. Show that pooled figure so
    # the difference is visible rather than asserted.
    tok = tenancy.set_current_tenant(None)
    pooled = oee_contract.as_percentages(oee_contract.plant_oee(db, "FACTORY_A"))
    tenancy.reset_current_tenant(tok)
    print(f"      per-tenant: {got}   (FACTORY_A unbound: {pooled.get('oee')}%)")

    # ------------------------------------------------------------- 7 MQTT --
    banner(7, "AN MQTT MESSAGE ON ONE CUSTOMER'S TOPIC TOUCHES ONLY THEIRS")
    before = {}
    tok = tenancy.set_current_tenant(None)
    for t in TENANTS:
        before[t] = db.query(models.Machine).filter(
            models.Machine.id == machines[(t, "CNC-01")]).one().status
    tenancy.reset_current_tenant(tok)

    # The topic carries (tenant, site); the machine NAME comes from the payload
    # -- ADR-0011. Driving it that way is the point: before #502 the name alone
    # resolved the machine, so all three CNC-01s were one row.
    topic = f"amp/FACTORY_A/{PROFILE['FACTORY_A']['site']}/machines"
    route = mqtt_identity.parse_topic(topic, "amp")
    name = mqtt_identity.machine_identifier({"machine": "CNC-01"})
    check("a topic parses to (tenant, site)",
          route.tenant == "FACTORY_A"
          and route.site == PROFILE["FACTORY_A"]["site"],
          f"{route.tenant}/{route.site}")
    check("...and the machine name comes from the payload", name == "CNC-01", name)
    tok = tenancy.set_current_tenant(None)
    target = db.query(models.Machine).filter(
        models.Machine.tenant_code == route.tenant,
        models.Machine.site == route.site,
        models.Machine.name == name).one()
    target.status = "Breakdown"
    db.commit()
    after = {t: db.query(models.Machine).filter(
        models.Machine.id == machines[(t, "CNC-01")]).one().status
        for t in TENANTS}
    tenancy.reset_current_tenant(tok)
    check("FACTORY_A's CNC-01 changed", after["FACTORY_A"] == "Breakdown",
          after["FACTORY_A"])
    check("...and B's and C's CNC-01 did NOT",
          after["FACTORY_B"] == before["FACTORY_B"]
          and after["FACTORY_C"] == before["FACTORY_C"], str(after))

    # -------------------------------------------------------- 8 WebSocket --
    banner(8, "A LIVE BROADCAST REACHES ONLY ITS OWN CUSTOMER")

    class _Sock:
        def __init__(self):
            self.sent = []

        async def accept(self):
            pass

        async def send_text(self, text):
            self.sent.append(text)

    async def ws_run():
        m = live_ws.ConnectionManager()
        socks = {t: _Sock() for t in TENANTS}
        for t in TENANTS:
            await m.connect(socks[t], t)
        await m.broadcast({"event": "machine_update", "tenant_code": "FACTORY_A",
                           "machine": {"name": "CNC-01"}})
        return socks

    socks = asyncio.new_event_loop().run_until_complete(ws_run())
    check("FACTORY_A's socket received it", len(socks["FACTORY_A"].sent) == 1,
          str(socks["FACTORY_A"].sent))
    check("...and B's and C's received nothing",
          not socks["FACTORY_B"].sent and not socks["FACTORY_C"].sent,
          f"B={socks['FACTORY_B'].sent} C={socks['FACTORY_C'].sent}")

    # -------------------------------------------------------- 9 approvals --
    banner(9, "ONE CUSTOMER'S ADMIN CANNOT DECIDE ANOTHER'S AGENT ACTION")
    tok = tenancy.set_current_tenant(None)
    action = models.AgentAction(
        tenant_code="FACTORY_B", agent="reorder", action_type="draft_po",
        summary="restock", ref_kind="purchase_order", ref_id=None,
        status="Proposed", created_at=datetime.utcnow())
    db.add(action)
    db.commit()
    tenancy.reset_current_tenant(tok)
    for t in ["FACTORY_A", "FACTORY_C"]:
        try:
            approvals.authorise(db, action,
                                {"sub": f"{t.lower()}-admin", "tenant": t},
                                "approve")
            check(f"{t}'s Admin cannot approve FACTORY_B's action", False,
                  "IT WAS ALLOWED")
        except approvals.ApprovalDenied as e:
            check(f"{t}'s Admin cannot approve FACTORY_B's action",
                  e.status_code == 404, str(e.status_code))
    try:
        approvals.authorise(db, action,
                            {"sub": "factory_b-admin", "tenant": "FACTORY_B"},
                            "approve")
        check("CONTROL: FACTORY_B's own Admin still can", True)
    except approvals.ApprovalDenied as e:
        check("CONTROL: FACTORY_B's own Admin still can", False,
              f"{e.status_code} {e.detail}")

    # ------------------------------------------------------- 10 AI context --
    banner(10, "THE ASSISTANT'S CONTEXT FOR ONE CUSTOMER NAMES NO OTHER")
    import ai_copilot
    for t in TENANTS:
        tok = tenancy.set_current_tenant(t)
        ctx = ai_copilot._build_factory_context(db, t)
        tenancy.reset_current_tenant(tok)
        others = [o for o in TENANTS if o != t]
        named = [o for o in others if o in ctx]
        check(f"{t}'s context mentions no other tenant code", not named,
              str(named))
        foreign_sites = [PROFILE[o]["site"] for o in others
                         if PROFILE[o]["site"] in ctx]
        check(f"...and no other customer's site name", not foreign_sites,
              str(foreign_sites))

    # ---------------------------------------------------------- 11 exports --
    banner(11, "A CSV EXPORT CARRIES ONLY THE CALLER'S ROWS")
    import reports_routes
    # Every CSV export, not one of them. A skipped export is a hole, and an
    # `except` that turns a hole into a footnote is how a suite comes to claim
    # coverage it does not have -- the first version of this section did
    # exactly that against a mistyped function name.
    EXPORTS = [
        ("inventory", reports_routes.export_inventory_csv),
        ("work orders", reports_routes.export_work_orders_csv),
        ("purchase orders", reports_routes.export_purchase_orders_csv),
        ("maintenance", reports_routes.export_maintenance_csv),
        ("quality", reports_routes.export_quality_csv),
        ("downtime", reports_routes.export_downtime_csv),
        ("OEE", reports_routes.export_oee_csv),
    ]
    for t in TENANTS:
        others = [o for o in TENANTS if o != t]
        # Every value that belongs to somebody else and must not appear.
        foreign = [o for o in others] + [PROFILE[o]["site"] for o in others] \
            + [PROFILE[o]["component"] for o in others] \
            + [f"{o} widget" for o in others] + [f"{o} crew" for o in others] \
            + [f"{o} QA" for o in others] + [f"{o} breakdown" for o in others]
        total_rows = 0
        own_seen = set()
        for label, fn in EXPORTS:
            tok = tenancy.set_current_tenant(t)
            resp = fn(db=db, current_user={"sub": f"{t.lower()}-admin",
                                           "tenant": t, "role": "Admin"})
            tenancy.reset_current_tenant(tok)
            body = resp.body if hasattr(resp, "body") else b""
            text = body.decode() if isinstance(body, (bytes, bytearray)) else str(body)
            leaked = sorted({v for v in foreign if v in text})
            check(f"{t}'s {label} export names no other customer",
                  not leaked, str(leaked))
            total_rows += max(0, len(list(csv.reader(io.StringIO(text)))) - 1)
            for own in (PROFILE[t]["site"], PROFILE[t]["component"],
                        f"{t} widget", f"{t} crew", f"{t} QA"):
                if own in text:
                    own_seen.add(own)
        # CONTROL: "names no other customer" is equally satisfied by an empty
        # file. These exports must actually carry this customer's own data.
        check(f"CONTROL: {t}'s exports do contain its own data",
              total_rows > 0 and len(own_seen) >= 3,
              f"{total_rows} rows, own markers seen: {sorted(own_seen)}")
        print(f"      {t}: {total_rows} data rows across {len(EXPORTS)} exports, "
              f"own markers {sorted(own_seen)}")

    db.close()
    engine.dispose()

    print()
    print("=" * 78)
    for n in notes:
        print("NOTE:", n)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        return 1
    print("THREE CUSTOMERS, IDENTICAL IDENTIFIERS, NO CROSSOVER")
    return 0


if __name__ == "__main__":
    sys.exit(main())
