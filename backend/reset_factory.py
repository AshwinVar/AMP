"""Reset the DEFAULT tenant to the SMT -> IC two-line instrument-cluster factory.

Wipes the current DEFAULT machines and every row that depends on them, then
seeds a clean two-line factory:

  * SMT line  (RAW  -> SEMI): solder paste printer, pick & place, reflow, AOI
  * IC  line  (SEMI -> FIN) : cluster assembly, gauge programming, EOL test, final QC
  * 10 work orders (5 Bugatti + 5 Mercedes), each a cluster part flowing
    RAW -> SEMI -> FIN, with a linked customer order per work order
  * a digital-twin layout in two zones ("SMT Line", "IC Line")
  * production / downtime / quality / maintenance so every module reflects it

Only the DEFAULT tenant is touched; GMATS and other client tenants are left
alone. Run once:

    python backend/reset_factory.py
"""
import random
from datetime import datetime, date, timedelta

from sqlalchemy import or_

from database import SessionLocal, engine, Base
import models

TENANT = "DEFAULT"

# ── The demo is a REHEARSAL SURFACE, so it is reproducible ──────────
#
# This file imported `random` and never seeded it, so every reset drew fresh
# production, downtime, reject rates, order dates and work-order progress. The
# numbers you practised the pitch on were not the numbers on screen in the
# meeting, and "the biggest measured loss" was a different sentence each time.
# Seeding once, here, keeps the data textured (it still looks like a real week,
# not a spreadsheet) while making the whole plant identical on every run —
# test_reset_factory.py asserts exact figures because of this line.
#
# Change the seed only deliberately: it changes every number in the demo.
DEMO_SEED = 20260922

# What a good unit is worth to this plant, so the Command Centre can rank
# problems in money rather than "good units not made". No seeder set this, so
# the demo's most persuasive surface was off by default and every loss read as
# a unit count. It is the tenant's own configured figure (ADR: money appears
# only where a unit value is set), not a hardcoded tariff inside an engine.
DEMO_UNIT_VALUE_GBP = 12.50

# ── The problems the demo deliberately plants ──────────────────────
#
# A demo factory whose only discoverable problem is "a machine is down" does
# not show what AMP is for. Each entry below is planted so that ONE of the
# Command Centre's seven problem kinds fires on it, and AMP finds it by its own
# rules — nothing here is announced to the intelligence layer.
#
#   machine down now      SMT-Reflow-01 is in Breakdown (its status, above)
#   repeated downtime     SMT-Reflow-01, one dominant reason (_seed_downtime)
#   overdue maintenance   MT-9001 planned three days ago and still Open
#   quality reject spike  IC-Test-01, one defect category (_seed_quality)
#   material shortage     three items at or below reorder level (_seed_inventory)
#   plan behind target    two plans that came due short (_seed_plans)
#   late customer order   CO-5002 past its due date, undispatched
#   late work order       WO-1002, its planned end three days ago
#
# docs/sales/FACTORY-DEMO-RUNBOOK.md quotes the figures these produce.
PROBLEM_MACHINE = "SMT-Reflow-01"        # the breakdown, and the downtime story
PROBLEM_DOWNTIME_REASON = "Reflow profile fault"
PROBLEM_QUALITY_MACHINE = "IC-Test-01"
PROBLEM_QUALITY_DEFECT = "Solder defect"
OVERDUE_MAINTENANCE_DAYS = 3

# ── The two lines ─────────────────────────────────────────────────
# A part enters RAW, is surface-mounted on the SMT line (-> SEMI), then
# assembled/tested/packed on the IC line (-> FIN).
SMT_MACHINES = [
    {"name": "SMT-Printer-01",    "status": "Running",     "util": 88},
    {"name": "SMT-PickPlace-01",  "status": "Running",     "util": 92},
    {"name": "SMT-Reflow-01",     "status": "Breakdown",   "util": 0},
    {"name": "SMT-AOI-01",        "status": "Running",     "util": 74},
]
IC_MACHINES = [
    {"name": "IC-Assembly-01",    "status": "Running",     "util": 84},
    {"name": "IC-Programming-01", "status": "Running",     "util": 77},
    {"name": "IC-Test-01",        "status": "Running",     "util": 81},
    {"name": "IC-FinalQC-01",     "status": "Maintenance", "util": 0},
]

# Five instrument-cluster sub-assemblies; each flows SMT then IC.
CLUSTER_PARTS = [
    {"code": "CLB-PCB",      "name": "Cluster Main PCB"},
    {"code": "CLB-DISPLAY",  "name": "TFT Display Driver Board"},
    {"code": "CLB-GAUGE",    "name": "Stepper Gauge Board"},
    {"code": "CLB-BACKLIT",  "name": "LED Backlight Board"},
    {"code": "CLB-TELLTALE", "name": "Warning Telltale Board"},
]

COMPANIES = ["Bugatti", "Mercedes"]

# Material state across each company's 5 parts, so the whole pipeline is visible.
STATE_PLAN = ["RAW", "SEMI", "FIN", "SEMI", "RAW"]

DOWNTIME_REASONS = ["Feeder jam", "Solder bridging", "Reflow profile fault",
                    "Nozzle clog", "Test fixture fault", "Programming timeout"]
DEFECTS = ["Solder defect", "Component misalignment", "Tombstoning",
           "Display pixel fault", "Backlight uneven", "Cold joint"]

def _wipe(db):
    """Remove the whole DEFAULT factory. Children are matched by their *reference*
    to the machines / work orders / plans being removed (not by their own
    tenant_code — legacy rows can carry a NULL / unbackfilled tenant), so nothing
    is left dangling. Children are deleted before parents (FK-safe on Postgres)."""
    mids = [r[0] for r in db.query(models.Machine.id).filter(models.Machine.tenant_code == TENANT).all()]
    wids = [r[0] for r in db.query(models.WorkOrder.id).filter(models.WorkOrder.tenant_code == TENANT).all()]
    pids = [r[0] for r in db.query(models.ProductionPlan.id).filter(models.ProductionPlan.tenant_code == TENANT).all()]

    def wipe(model, tenant_col, refs=()):
        clauses = [tenant_col == TENANT]
        for col, ids in refs:
            if ids:
                clauses.append(col.in_(ids))
        db.query(model).filter(or_(*clauses)).delete(synchronize_session=False)

    # The industrial PLC devices/signals stay (they're the connectivity layer);
    # just detach them from the machines being removed — their machine FK is
    # nullable, so this keeps the reset FK-safe.
    if mids:
        db.query(models.IndustrialSignal).filter(models.IndustrialSignal.machine_id.in_(mids)) \
            .update({models.IndustrialSignal.machine_id: None}, synchronize_session=False)
        db.query(models.IndustrialDevice).filter(models.IndustrialDevice.linked_machine_id.in_(mids)) \
            .update({models.IndustrialDevice.linked_machine_id: None}, synchronize_session=False)

    m = models  # brevity
    wipe(m.CustomerOrder, m.CustomerOrder.tenant_code,
         [(m.CustomerOrder.linked_work_order_id, wids), (m.CustomerOrder.linked_production_plan_id, pids)])
    wipe(m.OperatorJobExecution, m.OperatorJobExecution.tenant_code,
         [(m.OperatorJobExecution.machine_id, mids), (m.OperatorJobExecution.work_order_id, wids),
          (m.OperatorJobExecution.production_plan_id, pids)])
    wipe(m.QualityInspection, m.QualityInspection.tenant_code,
         [(m.QualityInspection.machine_id, mids), (m.QualityInspection.work_order_id, wids),
          (m.QualityInspection.production_plan_id, pids)])
    wipe(m.ProductionSchedule, m.ProductionSchedule.tenant_code,
         [(m.ProductionSchedule.machine_id, mids), (m.ProductionSchedule.work_order_id, wids),
          (m.ProductionSchedule.production_plan_id, pids)])
    wipe(m.ProductionPlan, m.ProductionPlan.tenant_code,
         [(m.ProductionPlan.machine_id, mids), (m.ProductionPlan.work_order_id, wids)])
    wipe(m.MaintenanceTask, m.MaintenanceTask.tenant_code, [(m.MaintenanceTask.machine_id, mids)])
    wipe(m.Escalation, m.Escalation.tenant_code, [(m.Escalation.machine_id, mids)])
    wipe(m.DowntimeLog, m.DowntimeLog.tenant_code, [(m.DowntimeLog.machine_id, mids)])
    wipe(m.ProductionRecord, m.ProductionRecord.tenant_code, [(m.ProductionRecord.machine_id, mids)])
    wipe(m.MachineEvent, m.MachineEvent.tenant_code, [(m.MachineEvent.machine_id, mids)])
    wipe(m.FactoryLayoutNode, m.FactoryLayoutNode.tenant_code, [(m.FactoryLayoutNode.machine_id, mids)])
    wipe(m.IoTTelemetry, m.IoTTelemetry.tenant_code, [(m.IoTTelemetry.machine_id, mids)])
    wipe(m.AIRecommendation, m.AIRecommendation.tenant_code, [(m.AIRecommendation.related_machine_id, mids)])
    wipe(m.AgentAction, m.AgentAction.tenant_code, [(m.AgentAction.related_machine_id, mids)])
    wipe(m.ShiftData, m.ShiftData.tenant_code)
    wipe(m.Notification, m.Notification.tenant_code)
    wipe(m.Alert, m.Alert.tenant_code)
    wipe(m.WorkOrder, m.WorkOrder.tenant_code, [(m.WorkOrder.machine_id, mids)])
    wipe(m.Machine, m.Machine.tenant_code)

    db.commit()
    db.expunge_all()  # drop the deleted rows from the identity map before reseeding


def _seed_machines(db):
    machines = {}
    for line, specs in (("SMT", SMT_MACHINES), ("IC", IC_MACHINES)):
        for spec in specs:
            down = ("2 hrs 40 min" if spec["status"] == "Breakdown"
                    else "1 hr 05 min" if spec["status"] == "Maintenance" else "0 min")
            m = models.Machine(tenant_code=TENANT, name=spec["name"], status=spec["status"],
                               utilization=spec["util"], downtime=down, line=line)
            db.add(m)
            machines[spec["name"]] = m
    db.commit()
    return machines


def _seed_layout(db, machines):
    xs = [70, 250, 430, 610]
    for zone, specs, y in (("SMT Line", SMT_MACHINES, 120), ("IC Line", IC_MACHINES, 340)):
        for i, spec in enumerate(specs):
            m = machines[spec["name"]]
            db.add(models.FactoryLayoutNode(
                tenant_code=TENANT, machine_id=m.id, node_name=spec["name"],
                node_type="Machine", x_position=xs[i], y_position=y, zone=zone))
    db.commit()


def _seed_orders(db, machines):
    """10 work orders (5 per company); each part's material state places it on a
    line: RAW -> processing on SMT, SEMI -> on IC, FIN -> packed at final QC."""
    smt_machine = machines["SMT-PickPlace-01"]
    ic_machine = machines["IC-Assembly-01"]
    fin_machine = machines["IC-FinalQC-01"]
    wo_seq, co_seq = 1000, 5000
    for company in COMPANIES:
        tag = company[:3].upper()
        for i, part in enumerate(CLUSTER_PARTS):
            wo_seq += 1
            co_seq += 1
            state = STATE_PLAN[i]
            machine = smt_machine if state == "RAW" else fin_machine if state == "FIN" else ic_machine
            target = random.randint(200, 500)
            if state == "FIN":
                actual, status = target, "Completed"
            elif state == "SEMI":
                actual, status = int(target * random.uniform(0.4, 0.8)), "In Progress"
            else:
                actual, status = 0, "Planned"
            # ONE ORDER IS LATE, on purpose (see the customer order below).
            late = company == COMPANIES[0] and i == 1
            started = datetime.utcnow() - timedelta(days=random.randint(1, 8))
            wo = models.WorkOrder(
                tenant_code=TENANT, work_order_no=f"WO-{wo_seq}",
                part_number=f"{part['code']}-{tag}", batch_number=f"{tag}-B{i + 1:02d}",
                machine_id=machine.id, target_quantity=target, actual_quantity=actual,
                status=status, material_state=state,
                # created_at matters: ai.flow ages an open order from it, and
                # leaving it to default to NOW while planned_start sits days in
                # the past produced "open 0 days and its date has gone" on the
                # radar — both halves true, the sentence nonsense. The order was
                # raised when it was planned to start.
                created_at=started,
                planned_start=started,
                # The late order's WORK ORDER is late too, which is WHY the order
                # is: an owner who clicks through from "CO-5002 is past its date"
                # should find the job that caused it, not two unrelated problems
                # that happen to share a customer. Every other planned_end is in
                # the future, so this is the only work order AMP can judge late.
                planned_end=(datetime.utcnow() - timedelta(days=3) if late
                             else datetime.utcnow() + timedelta(days=random.randint(1, 10))))
            db.add(wo)
            db.flush()
            co_status = "Dispatched" if state == "FIN" else "In Production" if state == "SEMI" else "Pending"
            # Every due date used to be `today + 3..20`, so no order could ever be
            # overdue and the Command Centre's delivery problem was unreachable on
            # the demo plant. The second Bugatti part is in production, its work
            # order has blown its planned end, and its date has passed.
            due = (date.today() - timedelta(days=2) if late
                   else date.today() + timedelta(days=random.randint(3, 20)))
            db.add(models.CustomerOrder(
                tenant_code=TENANT, order_no=f"CO-{co_seq}", customer_name=company,
                product_name=f"{company} {part['name']}", linked_work_order_id=wo.id,
                order_quantity=target, dispatched_quantity=actual if state == "FIN" else 0,
                priority="High" if late else random.choice(["High", "High", "Medium"]),
                due_date=due, status=co_status))
    db.commit()


def _seed_production(db, machines):
    """A week of production per running machine, so OEE / production / trends are live."""
    for m in machines.values():
        if m.status in ("Breakdown", "Maintenance"):
            continue
        for d in range(7):
            runtime = random.randint(400, 465)
            ideal = random.randint(20, 40)
            total = int(runtime * 60 / ideal * random.uniform(0.85, 0.97))
            rejected = int(total * random.uniform(0.01, 0.05))
            db.add(models.ProductionRecord(
                tenant_code=TENANT, machine_id=m.id, planned_minutes=480, runtime_minutes=runtime,
                ideal_cycle_time_seconds=ideal, total_count=total,
                good_count=total - rejected, rejected_count=rejected,
                created_at=datetime.utcnow() - timedelta(days=d, hours=random.randint(0, 8))))
    db.commit()


def _seed_downtime(db, machines):
    """Stoppages, with ONE story that dominates.

    The other machines get scattered short stops, as a real week has. The
    breakdown machine gets four long ones under a single reason, so the Pareto
    has a clear top row and "the biggest measured loss" is the same sentence at
    every rehearsal. Before this the reason, the duration and the day were all
    drawn fresh, so the top cause changed between the practice run and the
    meeting."""
    # The planted story: the reflow oven, one reason, 6h 5m across four stops.
    for hours_ago, minutes in ((6, 95), (30, 140), (54, 60), (78, 70)):
        db.add(models.DowntimeLog(
            tenant_code=TENANT, machine_id=machines[PROBLEM_MACHINE].id,
            reason=PROBLEM_DOWNTIME_REASON, duration=f"{minutes} min",
            created_at=datetime.utcnow() - timedelta(hours=hours_ago)))
    # Background noise, so the Pareto has a tail to rank against.
    for name in ("SMT-PickPlace-01", "IC-Test-01", "IC-FinalQC-01"):
        m = machines[name]
        for _ in range(random.randint(2, 4)):
            db.add(models.DowntimeLog(
                tenant_code=TENANT, machine_id=m.id, reason=random.choice(DOWNTIME_REASONS),
                duration=f"{random.randint(15, 45)} min",
                created_at=datetime.utcnow() - timedelta(days=random.randint(0, 6), hours=random.randint(0, 10))))
    db.commit()


# ── Material, plan and shift: the three surfaces the demo had nothing on ──

# Consumables for the SMT -> IC plant. The first three sit at or below their
# reorder level, so ai.inventory's `current_stock <= reorder_level` rule fires
# and the Command Centre's stock problem has something to rank. Solder paste is
# the headline: it is the one the reflow oven and the printer both need.
INVENTORY_SEED = [
    ("RM-PASTE-01", "SAC305 Solder Paste", "Raw Material", "kg", 4, 12, "Cold Store A"),
    ("RM-PCB-01", "Cluster Main PCB Blank", "Raw Material", "pcs", 180, 250, "Rack B1"),
    ("SP-NOZZLE-01", "Pick & Place Nozzle 0402", "Spare Part", "pcs", 2, 6, "Tool Crib"),
    ("RM-CONN-01", "24-Way Header Connector", "Raw Material", "pcs", 4200, 1500, "Rack B2"),
    ("RM-LED-01", "Backlight LED (white)", "Raw Material", "pcs", 9800, 4000, "Rack C1"),
    ("SP-THERMO-01", "Reflow Thermocouple", "Spare Part", "pcs", 5, 2, "Tool Crib"),
    ("CN-TRAY-01", "ESD Shipping Tray", "Consumable", "pcs", 640, 200, "Despatch"),
]


def _seed_inventory(db):
    """Stock, with three items at or below reorder level.

    reset_factory created NO InventoryItem rows at all, so `_stock_problem`
    returned None on every demo: the Inventory screen was empty, the shortage
    card had nothing to size, and the Copilot answered "no stock items are set
    up" to "what should I reorder?" — on the plant we were selling.

    UPSERT, not insert-after-wipe. `_wipe` deliberately does not touch stock:
    items hang off stock movements, purchase orders and issue slips, and
    unpicking that graph is `reseed_inventory.py`'s job, which already does it
    with a production refusal and a mandatory --tenant. inventory_items also
    carries a unique (tenant_code, item_code), so a plain insert would fail on
    the second reset. Writing the seeded levels back keeps the shortage story
    identical on every run without reaching into anything's parents.
    """
    existing = {
        row.item_code: row
        for row in db.query(models.InventoryItem).filter(
            models.InventoryItem.tenant_code == TENANT).all()
    }
    for code, name, category, unit, stock, reorder, location in INVENTORY_SEED:
        row = existing.get(code)
        if row is None:
            row = models.InventoryItem(tenant_code=TENANT, item_code=code)
            db.add(row)
        row.item_name, row.category, row.unit = name, category, unit
        row.supplier, row.location = "Nordson Components", location
        row.current_stock, row.reorder_level = stock, reorder
    db.commit()


def _seed_plans(db, machines):
    """Shift plans for the last three days, two of which came up short.

    reset_factory created no ProductionPlan rows either, so plan attainment was
    NOT CONFIGURED and the Command Centre's headline literally read "no
    production plan is set to compare against" — on a demo whose whole point is
    "are we hitting target?". The shortfalls are deliberate and fixed."""
    line = machines["SMT-PickPlace-01"]
    assembly = machines["IC-Assembly-01"]
    # (days ago, shift, machine, planned, actual). Day 2 and day 1 fall short.
    rows = [
        (3, "Day", line, 1200, 1200),
        (3, "Night", assembly, 900, 910),
        (2, "Day", line, 1200, 870),        # short by 330
        (2, "Night", assembly, 900, 900),
        (1, "Day", line, 1200, 1040),       # short by 160
        (1, "Night", assembly, 900, 905),
    ]
    for i, (days_ago, shift, machine, planned, actual) in enumerate(rows):
        db.add(models.ProductionPlan(
            tenant_code=TENANT, plan_no=f"PLAN-{3100 + i}", machine_id=machine.id,
            planned_quantity=planned, actual_quantity=actual,
            plan_date=date.today() - timedelta(days=days_ago), shift_name=shift,
            status="Behind" if actual < planned else "Completed"))
    db.commit()


def _seed_shifts(db):
    """Shift attainment, which the wipe cleared and nothing reseeded — so the
    Shifts screen was blank in every demo."""
    for shift, target, actual in (("Day", 1200, 1040), ("Night", 900, 905), ("Evening", 1000, 960)):
        db.add(models.ShiftData(tenant_code=TENANT, shift_name=shift,
                                target_output=target, actual_output=actual))
    db.commit()


def _seed_unit_value(db):
    """What a good unit is worth here, so losses read in money.

    Without it every figure on the Command Centre is "N good units not made",
    which is true but does not sell. This is the tenant's own configured rate,
    the only place money may come from."""
    row = db.query(models.TenantConfig).filter(models.TenantConfig.tenant_code == TENANT).first()
    if row is None:
        row = models.TenantConfig(tenant_code=TENANT)
        db.add(row)
    row.unit_value_gbp = DEMO_UNIT_VALUE_GBP
    db.commit()


def _seed_quality(db, machines):
    """One inspection per work order — AOI for RAW/SMT parts, EOL test otherwise."""
    aoi, test = machines["SMT-AOI-01"], machines["IC-Test-01"]
    wos = db.query(models.WorkOrder).filter(models.WorkOrder.tenant_code == TENANT).order_by(models.WorkOrder.id).all()
    for i, wo in enumerate(wos):
        machine = aoi if wo.material_state == "RAW" else test
        inspected = random.randint(80, 200)
        # THE PLANTED SPIKE. Background fail rates stay in the 1-8% noise band;
        # the EOL tester runs at ~18% on one defect category, so the quality
        # problem has a stable "worst machine" and a stable top defect. Before
        # this every inspection drew from the same uniform band, so there was no
        # outlier to find and the Command Centre's quality row named whichever
        # machine the dice favoured that morning.
        if machine is test:
            failed = int(inspected * 0.18)
            defect = PROBLEM_QUALITY_DEFECT
        else:
            failed = int(inspected * random.uniform(0.01, 0.05))
            defect = random.choice(DEFECTS) if failed else None
        db.add(models.QualityInspection(
            tenant_code=TENANT, inspection_no=f"QC-{7000 + i}", work_order_id=wo.id,
            machine_id=machine.id, inspector="AOI System" if machine is aoi else "EOL Test",
            inspected_quantity=inspected, passed_quantity=inspected - failed, failed_quantity=failed,
            defect_category=defect,
            created_at=datetime.utcnow() - timedelta(days=random.randint(0, 6))))
    db.commit()


def _seed_maintenance(db, machines):
    db.add(models.MaintenanceTask(
        tenant_code=TENANT, task_no="MT-9001", machine_id=machines["SMT-Reflow-01"].id,
        task_type="Corrective", priority="Critical", assigned_to="Maintenance team",
        # PLANNED IN THE PAST, deliberately. ai.maintenance's overdue predicate is
        # strictly `planned_date < today`, so both tasks dated today meant the
        # overdue count was ZERO on the day you reset — and the Command Centre's
        # maintenance problem never fired unless you happened to reset the day
        # before the meeting. Now the reflow job is three days late on every run.
        planned_date=date.today() - timedelta(days=OVERDUE_MAINTENANCE_DAYS), status="Open",
        notes="Reflow oven zone-3 thermocouple fault — line stopped, under repair."))
    db.add(models.MaintenanceTask(
        tenant_code=TENANT, task_no="MT-9002", machine_id=machines["IC-FinalQC-01"].id,
        task_type="Preventive", priority="Medium", assigned_to="Maintenance team",
        planned_date=date.today(), status="Open",
        notes="Final QC station scheduled calibration."))
    db.commit()


def rebuild_factory(db, seed=DEMO_SEED):
    """Wipe the DEFAULT factory and rebuild it as the SMT -> IC two-line plant.

    REPRODUCIBLE. `seed` fixes every draw below, so two resets produce the same
    plant and a rehearsed demo matches the one on screen. Pass a different seed
    to get a different (but equally repeatable) factory; pass None to go back to
    the old behaviour of drawing fresh each time, which no caller does.
    """
    if seed is not None:
        random.seed(seed)
    _wipe(db)
    machines = _seed_machines(db)
    _seed_layout(db, machines)
    _seed_orders(db, machines)
    _seed_production(db, machines)
    _seed_downtime(db, machines)
    _seed_quality(db, machines)
    _seed_maintenance(db, machines)
    # The four surfaces the demo plant had nothing on at all: stock, the plan it
    # is measured against, shift attainment, and what a unit is worth.
    _seed_inventory(db)
    _seed_plans(db, machines)
    _seed_shifts(db)
    _seed_unit_value(db)
    return machines


if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)  # ensure tables exist before seeding
    db = SessionLocal()
    try:
        print("\n=== Rebuilding DEFAULT factory: SMT -> IC instrument-cluster plant ===\n")
        rebuild_factory(db)
        m = db.query(models.Machine).filter(models.Machine.tenant_code == TENANT)
        wo = db.query(models.WorkOrder).filter(models.WorkOrder.tenant_code == TENANT)
        co = db.query(models.CustomerOrder).filter(models.CustomerOrder.tenant_code == TENANT)
        print(f"  Machines   : {m.count()}  (SMT {m.filter(models.Machine.line == 'SMT').count()} | "
              f"IC {m.filter(models.Machine.line == 'IC').count()})")
        print(f"  Work orders: {wo.count()}  "
              f"(RAW {wo.filter(models.WorkOrder.material_state == 'RAW').count()} | "
              f"SEMI {wo.filter(models.WorkOrder.material_state == 'SEMI').count()} | "
              f"FIN {wo.filter(models.WorkOrder.material_state == 'FIN').count()})")
        print(f"  Cust orders: {co.count()}  "
              f"(Bugatti {co.filter(models.CustomerOrder.customer_name == 'Bugatti').count()} | "
              f"Mercedes {co.filter(models.CustomerOrder.customer_name == 'Mercedes').count()})")
        print(f"  Prod records: {db.query(models.ProductionRecord).filter(models.ProductionRecord.tenant_code == TENANT).count()}")
        print("\n[OK] DEFAULT factory rebuilt. GMATS and other tenants untouched.\n")
    finally:
        db.close()
