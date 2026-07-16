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
            wo = models.WorkOrder(
                tenant_code=TENANT, work_order_no=f"WO-{wo_seq}",
                part_number=f"{part['code']}-{tag}", batch_number=f"{tag}-B{i + 1:02d}",
                machine_id=machine.id, target_quantity=target, actual_quantity=actual,
                status=status, material_state=state,
                planned_start=datetime.utcnow() - timedelta(days=random.randint(1, 8)),
                planned_end=datetime.utcnow() + timedelta(days=random.randint(1, 10)))
            db.add(wo)
            db.flush()
            co_status = "Dispatched" if state == "FIN" else "In Production" if state == "SEMI" else "Pending"
            db.add(models.CustomerOrder(
                tenant_code=TENANT, order_no=f"CO-{co_seq}", customer_name=company,
                product_name=f"{company} {part['name']}", linked_work_order_id=wo.id,
                order_quantity=target, dispatched_quantity=actual if state == "FIN" else 0,
                priority=random.choice(["High", "High", "Medium"]),
                due_date=date.today() + timedelta(days=random.randint(3, 20)), status=co_status))
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
    for name in ("SMT-Reflow-01", "SMT-PickPlace-01", "IC-Test-01", "IC-FinalQC-01"):
        m = machines[name]
        for _ in range(random.randint(2, 4)):
            db.add(models.DowntimeLog(
                tenant_code=TENANT, machine_id=m.id, reason=random.choice(DOWNTIME_REASONS),
                duration=f"{random.randint(15, 120)} min",
                created_at=datetime.utcnow() - timedelta(days=random.randint(0, 6), hours=random.randint(0, 10))))
    db.commit()


def _seed_quality(db, machines):
    """One inspection per work order — AOI for RAW/SMT parts, EOL test otherwise."""
    aoi, test = machines["SMT-AOI-01"], machines["IC-Test-01"]
    wos = db.query(models.WorkOrder).filter(models.WorkOrder.tenant_code == TENANT).order_by(models.WorkOrder.id).all()
    for i, wo in enumerate(wos):
        machine = aoi if wo.material_state == "RAW" else test
        inspected = random.randint(80, 200)
        failed = int(inspected * random.uniform(0.01, 0.08))
        db.add(models.QualityInspection(
            tenant_code=TENANT, inspection_no=f"QC-{7000 + i}", work_order_id=wo.id,
            machine_id=machine.id, inspector="AOI System" if machine is aoi else "EOL Test",
            inspected_quantity=inspected, passed_quantity=inspected - failed, failed_quantity=failed,
            defect_category=random.choice(DEFECTS) if failed else None,
            created_at=datetime.utcnow() - timedelta(days=random.randint(0, 6))))
    db.commit()


def _seed_maintenance(db, machines):
    db.add(models.MaintenanceTask(
        tenant_code=TENANT, task_no="MT-9001", machine_id=machines["SMT-Reflow-01"].id,
        task_type="Corrective", priority="Critical", assigned_to="Maintenance team",
        planned_date=date.today(), status="Open",
        notes="Reflow oven zone-3 thermocouple fault — line stopped, under repair."))
    db.add(models.MaintenanceTask(
        tenant_code=TENANT, task_no="MT-9002", machine_id=machines["IC-FinalQC-01"].id,
        task_type="Preventive", priority="Medium", assigned_to="Maintenance team",
        planned_date=date.today(), status="Open",
        notes="Final QC station scheduled calibration."))
    db.commit()


def rebuild_factory(db):
    """Wipe the DEFAULT factory and rebuild it as the SMT -> IC two-line plant."""
    _wipe(db)
    machines = _seed_machines(db)
    _seed_layout(db, machines)
    _seed_orders(db, machines)
    _seed_production(db, machines)
    _seed_downtime(db, machines)
    _seed_quality(db, machines)
    _seed_maintenance(db, machines)
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
