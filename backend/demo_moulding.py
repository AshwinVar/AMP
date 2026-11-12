"""The MERIDIAN sales demo: a de-identified injection-moulding plant.

WHAT THIS IS. A fictional SME plastics manufacturer — MERIDIAN POLYMERS — seeded
as a complete, resettable AMP tenant so the whole MES story (dashboard, OEE,
downtime Pareto, quality, planning, inventory, the money card) can be walked
through in front of any prospect. It is the plastics-plant companion to
reset_factory.py's electronics plant and demo_aeron.py's OEM story: a THIRD
vertical to pitch, and the one closest to the real first customer.

WHY IT EXISTS. Our first live node (an ESP32 cycle counter on a real injection
moulder) gave us something no generated demo has: the true SHAPE of a shop
floor — a ~15 s moulding cycle, an evening-heavy running rhythm (busiest 5-11pm,
quiet at the midday shift change), energy drawn per window. This seeder is
CALIBRATED on that measured signature so the demo feels like a real week, while
containing none of the real customer's data. The real tenant is never shown to
a prospect; its shape is.

WHY IT IS SAFE TO RUN ON PRODUCTION. Same three guards as demo_aeron:

  1. THE TENANT IS A CONSTANT, NOT A PARAMETER. Nothing points this at another
     workspace.
  2. THE NAME IS ASSERTED. `_assert_demo_scope()` refuses to run unless the
     tenant code begins with "DEMO_". A future edit that repoints the constant
     at a customer fails loudly instead of wiping them.
  3. EVERY DELETE IS FILTERED to `tenant_code == DEMO_TENANT`. There is no
     unscoped statement to get wrong.

WHY IT IS SAFE TO RUN TWICE. Reset is the only mode: it clears the demo scope
and rebuilds it, so a half-walked demo from the last meeting cannot leak into
the next. REPRODUCIBLE: `DEMO_SEED` fixes every draw, so the plant is identical
on every run and the figures you rehearse are the figures on screen.

WHY THE DATA ALWAYS LOOKS CURRENT. Every record is dated relative to
`datetime.utcnow()` at seed time, so a reset rolls the whole history up to
"today". Re-run it (or wire it to a nightly Railway cron — see README) and the
demo is never stale.

    DEMO_PASSWORD=<8+ chars> python demo_moulding.py --reset    rebuild to start state
    python demo_moulding.py --status                            what exists now

Production resets reuse the RESEED pattern: set `RESEED_DEMO_MOULDING=<a new
value>` on Railway and redeploy. Each distinct value is consumed exactly once
(recorded in the append-only event_log), so a variable left set cannot rebuild
the demo on every boot.
"""
import os
import random
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import models
import tenancy
from security import hash_password

# ---------------------------------------------------------------- constants --
DEMO_TENANT = "DEMO_MOULDING"           # clearly labelled as a demo workspace
DEMO_SITE = ""                          # single site; MQTT would address it "-"

# Reproducible, like reset_factory: one seed fixes the whole plant so a rehearsed
# demo matches the one on screen. Change it only deliberately.
DEMO_SEED = 20261106

# ₹ of contribution per good moulded part. This is the tenant's own configured
# rate (ADR-0010: money appears only where a unit value is set), so the Command
# Centre can value the OEE gap in rupees rather than "good parts not made".
# A small moulded part's margin; large enough that a plant-week of losses lands
# in lakhs and shows the Indian grouping off.
DEMO_UNIT_VALUE = 3.20

# Logins. Three roles on purpose: the Operator login demonstrates that money is
# Admin-only (non-Admins never receive a ₹ figure), which is a selling point.
ADMIN_USER = "meridian_admin"
SUPERVISOR_USER = "meridian_supervisor"
OPERATOR_USER = "meridian_operator"

# ── Calibration from the real first-node signature (profiled 2026-11) ──────
# Measured: median cycle ~15 s (p25-p75 12.9-19.8), busiest IST hours 17-23 with
# the peak at 19:00, quiet at the 12-14 midday shift change and ~01:00. These
# weights reproduce that running rhythm on the hourly history below.
HOUR_WEIGHT = {
    0: 0.55, 1: 0.40, 2: 0.45, 3: 0.45, 4: 0.50, 5: 0.58,
    6: 0.62, 7: 0.68, 8: 0.74, 9: 0.80, 10: 0.82, 11: 0.78,
    12: 0.46, 13: 0.43, 14: 0.52, 15: 0.72, 16: 0.80,
    17: 0.90, 18: 0.97, 19: 1.00, 20: 0.95, 21: 0.90, 22: 0.92, 23: 0.84,
}
# Monday and Sunday ran lighter in the real week; the rest were level.
DOW_FACTOR = {0: 0.90, 1: 1.00, 2: 1.00, 3: 1.00, 4: 1.00, 5: 0.96, 6: 0.82}
IST = timedelta(hours=5, minutes=30)

# Depth of hourly history. ~2.5 months gives the monthly / daily / hourly period
# selectors something to show, at a row count (~15k) in the same range as the
# real tenant and quick to reseed.
HISTORY_DAYS = 75

# ── The fleet (de-identified) ──────────────────────────────────────────────
# 12 injection-moulding presses + 1 packing station — the real fleet's size,
# none of its identity. Each spec drives a believable, DISTINCT machine so the
# board ranks a real spread of OEE, not thirteen identical rows.
#   ideal : ideal cycle time (s)         uptime: fraction of a weighted hour it runs
#   perf  : performance vs ideal         reject: baseline reject fraction
#   kw    : running power draw            meter : reports energy_kwh
PRESS = "Moulding"
PACK = "Packing"
MACHINE_SPECS = [
    {"name": "IMM-01", "line": PRESS, "status": "Running",     "util": 86, "ideal": 13, "uptime": 0.92, "perf": 0.95, "reject": 0.02, "kw": 22, "meter": True},
    {"name": "IMM-02", "line": PRESS, "status": "Running",     "util": 81, "ideal": 14, "uptime": 0.88, "perf": 0.93, "reject": 0.03, "kw": 24, "meter": True},
    {"name": "IMM-03", "line": PRESS, "status": "Running",     "util": 78, "ideal": 20, "uptime": 0.85, "perf": 0.90, "reject": 0.03, "kw": 34, "meter": True},
    {"name": "IMM-04", "line": PRESS, "status": "Running",     "util": 72, "ideal": 16, "uptime": 0.82, "perf": 0.88, "reject": 0.15, "kw": 26, "meter": True},  # quality spike
    {"name": "IMM-05", "line": PRESS, "status": "Running",     "util": 68, "ideal": 18, "uptime": 0.78, "perf": 0.86, "reject": 0.04, "kw": 28, "meter": False},
    {"name": "IMM-06", "line": PRESS, "status": "Running",     "util": 90, "ideal": 12, "uptime": 0.94, "perf": 0.96, "reject": 0.02, "kw": 18, "meter": True},
    {"name": "IMM-07", "line": PRESS, "status": "Breakdown",   "util": 0,  "ideal": 15, "uptime": 0.80, "perf": 0.90, "reject": 0.03, "kw": 25, "meter": True},  # down now
    {"name": "IMM-08", "line": PRESS, "status": "Running",     "util": 75, "ideal": 17, "uptime": 0.80, "perf": 0.89, "reject": 0.03, "kw": 27, "meter": False},
    {"name": "IMM-09", "line": PRESS, "status": "Running",     "util": 83, "ideal": 14, "uptime": 0.87, "perf": 0.92, "reject": 0.03, "kw": 23, "meter": True},
    {"name": "IMM-10", "line": PRESS, "status": "Maintenance", "util": 0,  "ideal": 19, "uptime": 0.75, "perf": 0.87, "reject": 0.04, "kw": 30, "meter": True},  # planned PM
    {"name": "IMM-11", "line": PRESS, "status": "Running",     "util": 79, "ideal": 15, "uptime": 0.84, "perf": 0.91, "reject": 0.03, "kw": 24, "meter": True},
    {"name": "IMM-12", "line": PRESS, "status": "Running",     "util": 70, "ideal": 21, "uptime": 0.77, "perf": 0.85, "reject": 0.05, "kw": 35, "meter": True},
    {"name": "PACK-01", "line": PACK, "status": "Running",     "util": 88, "ideal": 6,  "uptime": 0.90, "perf": 0.97, "reject": 0.00, "kw": 6,  "meter": False},
]

PROBLEM_MACHINE = "IMM-07"              # the breakdown + the downtime story
PROBLEM_DOWNTIME_REASON = "Hydraulic pump fault"
PROBLEM_QUALITY_MACHINE = "IMM-04"
PROBLEM_QUALITY_DEFECT = "Short shot"
OVERDUE_MAINTENANCE_DAYS = 3

DOWNTIME_REASONS = ["Mould change", "Hydraulic pump fault", "Material feed jam",
                    "Barrel temperature fault", "Ejector fault", "Screw purge"]
DEFECTS = ["Short shot", "Flash", "Sink marks", "Warping", "Black specks", "Flow lines"]

# De-identified parts and customers — plausible for an Indian moulding SME,
# invented for the demo.
PARTS = [
    ("CAP-28", "28mm Bottle Cap"),
    ("PAIL-HNDL", "20L Pail Handle"),
    ("CRATE-CNR", "Crate Corner Block"),
    ("BIN-LID10", "10L Dustbin Lid"),
    ("ELBOW-32", "32mm Pipe Elbow"),
    ("BASE-500", "500ml Container Base"),
    ("SPOOL-IND", "Industrial Cable Spool"),
    ("TRAY-SEED", "72-cell Seedling Tray"),
]
CUSTOMERS = ["Aquapure Bottling", "Greenfield Foods", "Metro Logistics", "Rexo Pipes"]

# Granules, masterbatch and packing. The first three sit at or below reorder, so
# the stock problem has something to rank; PP granules are the headline.
INVENTORY_SEED = [
    ("RM-PP-01", "Polypropylene Granules (Natural)", "Raw Material", "kg", 450, 2000, "Silo 1"),       # SHORT
    ("RM-MB-BLUE", "Blue Masterbatch", "Raw Material", "kg", 8, 25, "Store A"),                        # SHORT
    ("CN-CARTON-01", "Shipping Carton 5-ply", "Consumable", "pcs", 120, 500, "Despatch"),              # SHORT
    ("RM-HDPE-01", "HDPE Granules", "Raw Material", "kg", 3800, 1500, "Silo 2"),
    ("RM-PP-TALC", "PP Talc-filled Compound", "Raw Material", "kg", 5200, 2000, "Silo 3"),
    ("SP-NOZZLE-01", "Hot-runner Nozzle Tip", "Spare Part", "pcs", 14, 6, "Tool Crib"),
    ("CN-STRETCH-01", "Stretch Wrap Film", "Consumable", "rolls", 60, 20, "Despatch"),
]


class DemoRefused(Exception):
    """A refusal to seed. Deliberately NOT SystemExit: this module is called from
    the application's startup hook, and SystemExit is not caught by
    `except Exception` — a missing password must skip the demo, not fail the
    whole deployment."""


def _assert_demo_scope():
    """The guard that makes this file safe to keep in a production image."""
    if not DEMO_TENANT.startswith("DEMO_"):
        raise DemoRefused(
            f"demo tenant {DEMO_TENANT!r} is not named as a demo. This script "
            f"only ever touches a DEMO_ workspace; to seed a real customer you "
            f"want a different tool.")


def _password():
    pw = os.environ.get("DEMO_PASSWORD", "").strip()
    if len(pw) < 8:
        raise DemoRefused(
            "set DEMO_PASSWORD (8+ characters) before seeding. This script will "
            "not ship a default password — a demo login on the public internet "
            "with a guessable password is a real account.")
    return pw


# --------------------------------------------------------------------- wipe --
def wipe(db):
    """Remove the demo scope, and only the demo scope.

    Children before parents (FK-safe on Postgres); every statement is filtered to
    `tenant_code == DEMO_TENANT`. Unlike reset_factory (which keeps DEFAULT's
    users and config), this seeder OWNS the whole tenant, so it also clears the
    logins, the licence/branding row and the company record."""
    _assert_demo_scope()
    m = models
    mids = [r[0] for r in db.query(m.Machine.id).filter(m.Machine.tenant_code == DEMO_TENANT).all()]

    # Detach the industrial/twin layer before the machines it points at. A device
    # or signal owned by ANOTHER tenant is only unlinked, never deleted (it is not
    # demo scope); demo-owned ones go with the tenant.
    if mids:
        db.query(m.IndustrialSignal).filter(m.IndustrialSignal.machine_id.in_(mids)) \
            .update({m.IndustrialSignal.machine_id: None}, synchronize_session=False)
        db.query(m.IndustrialDevice).filter(m.IndustrialDevice.linked_machine_id.in_(mids)) \
            .update({m.IndustrialDevice.linked_machine_id: None}, synchronize_session=False)

    for M in (m.QualityInspection, m.ProductionPlan, m.MaintenanceTask, m.Escalation,
              m.DowntimeLog, m.ProductionRecord, m.MachineEvent, m.FactoryLayoutNode,
              m.CustomerOrder, m.WorkOrder, m.ShiftData, m.Notification, m.Alert,
              m.InventoryItem, m.Machine, m.User, m.TenantConfig, m.EventLog):
        db.query(M).filter(M.tenant_code == DEMO_TENANT).delete(synchronize_session=False)
    db.query(m.CompanyTenant).filter(m.CompanyTenant.company_code == DEMO_TENANT) \
        .delete(synchronize_session=False)

    db.commit()
    db.expunge_all()


# --------------------------------------------------------------------- seed --
def _seed_identity(db):
    """The company, its licence/branding, and three logins."""
    pw = _password()
    db.add(models.CompanyTenant(
        company_code=DEMO_TENANT, company_name="Meridian Polymers (DEMO)",
        industry="Plastics — injection moulding", plan_name="Enterprise",
        subscription_status="Active", seats=25, monthly_fee=0))
    db.add(models.TenantConfig(
        tenant_code=DEMO_TENANT, plan="enterprise",
        brand_name="Meridian Polymers", brand_color="#0E7C66",
        subscription_status="active", unit_value_gbp=DEMO_UNIT_VALUE))
    for user, role in ((ADMIN_USER, "Admin"), (SUPERVISOR_USER, "Supervisor"),
                       (OPERATOR_USER, "Operator")):
        db.add(models.User(username=user, password=hash_password(pw), role=role,
                           tenant_code=DEMO_TENANT, is_active=True))
    db.commit()


def _seed_machines(db):
    machines = {}
    for spec in MACHINE_SPECS:
        down = ("3 hrs 10 min" if spec["status"] == "Breakdown"
                else "1 hr 20 min" if spec["status"] == "Maintenance" else "0 min")
        mc = models.Machine(
            tenant_code=DEMO_TENANT, site=DEMO_SITE, name=spec["name"],
            status=spec["status"],
            utilization=(spec["util"] if spec["status"] == "Running" else 0),
            downtime=down, line=spec["line"])
        db.add(mc)
        machines[spec["name"]] = mc
    db.commit()
    return machines


def _seed_layout(db, machines):
    """A single 'Moulding Hall' zone so the digital twin is not empty."""
    per_row = 5
    for i, spec in enumerate(MACHINE_SPECS):
        db.add(models.FactoryLayoutNode(
            tenant_code=DEMO_TENANT, machine_id=machines[spec["name"]].id,
            node_name=spec["name"], node_type="Machine",
            x_position=70 + (i % per_row) * 150,
            y_position=100 + (i // per_row) * 160, zone="Moulding Hall"))
    db.commit()


def _spec_by_name():
    return {s["name"]: s for s in MACHINE_SPECS}


def _seed_production(db, machines, now=None):
    """Hourly production for the last HISTORY_DAYS, calibrated on the real rhythm.

    Each record is one hour (planned_minutes=60); the IST-hour weight and the
    per-machine uptime decide how much of the hour it ran, so the evening peak and
    the midday dip are visible in the hourly view and roll up correctly into the
    daily and monthly views. Dated relative to `now`, so a reset ends 'today'.
    `now` is injectable so a test can pin an exact window (and so a scheduled
    refresh can seed 'as of' a chosen time)."""
    specs = _spec_by_name()
    now = now or datetime.utcnow()
    start = (now - timedelta(days=HISTORY_DAYS)).replace(minute=0, second=0, microsecond=0)
    for mc in machines.values():
        spec = specs[mc.name]
        buf = []
        t = start
        while t <= now:
            ist = t + IST
            weight = HOUR_WEIGHT[ist.hour] * DOW_FACTOR[ist.weekday()] * spec["uptime"]
            # The breakdown press stopped two days ago; before that it ran like any
            # other, so its downtime history and its sudden stop are both real.
            if spec["status"] == "Breakdown" and t > now - timedelta(days=2):
                weight = 0.0
            # The machine under planned maintenance idles for the recent day.
            if spec["status"] == "Maintenance" and t > now - timedelta(hours=20):
                weight = 0.0
            runtime = min(60, max(0, round(60 * weight * random.uniform(0.9, 1.1))))
            if runtime >= 3:
                total = round(runtime * 60 / spec["ideal"] * spec["perf"] * random.uniform(0.95, 1.03))
                rejected = min(total, round(total * spec["reject"] * random.uniform(0.6, 1.4)))
                energy = round(spec["kw"] * (runtime / 60) * random.uniform(0.9, 1.1), 2) if spec["meter"] else None
                buf.append(models.ProductionRecord(
                    tenant_code=DEMO_TENANT, machine_id=mc.id, planned_minutes=60,
                    runtime_minutes=runtime, ideal_cycle_time_seconds=spec["ideal"],
                    total_count=total, good_count=total - rejected, rejected_count=rejected,
                    energy_kwh=energy, created_at=t))
            t += timedelta(hours=1)
        # Commit per machine so a 15k-row seed streams in manageable batches.
        db.add_all(buf)
        db.commit()


def _seed_downtime(db, machines):
    """Stoppages, with one story that dominates the Pareto.

    The broken press gets four long stops under a single reason (so 'the biggest
    measured loss' is the same sentence at every rehearsal); the others get
    scattered short stops, as a real week has."""
    for hours_ago, minutes in ((5, 110), (28, 145), (50, 70), (74, 85)):
        db.add(models.DowntimeLog(
            tenant_code=DEMO_TENANT, machine_id=machines[PROBLEM_MACHINE].id,
            reason=PROBLEM_DOWNTIME_REASON, duration=f"{minutes} min",
            created_at=datetime.utcnow() - timedelta(hours=hours_ago)))
    for name in ("IMM-03", "IMM-04", "IMM-12", "IMM-10"):
        for _ in range(random.randint(2, 4)):
            db.add(models.DowntimeLog(
                tenant_code=DEMO_TENANT, machine_id=machines[name].id,
                reason=random.choice(DOWNTIME_REASONS),
                duration=f"{random.randint(15, 50)} min",
                created_at=datetime.utcnow() - timedelta(days=random.randint(0, 6),
                                                         hours=random.randint(0, 10))))
    db.commit()


def _seed_orders(db, machines):
    """Eight work orders with a linked customer order each; one is late on purpose
    so the delivery problem is reachable."""
    press_names = [s["name"] for s in MACHINE_SPECS if s["line"] == PRESS and s["status"] == "Running"]
    wo_seq, co_seq = 2000, 6000
    for i, (code, name) in enumerate(PARTS):
        wo_seq += 1
        co_seq += 1
        customer = CUSTOMERS[i % len(CUSTOMERS)]
        machine = machines[press_names[i % len(press_names)]]
        target = random.randint(5000, 40000)
        late = (i == 1)                           # the second order runs late
        if i % 3 == 0:
            actual, wo_status, co_status, state = target, "Completed", "Dispatched", "FIN"
        elif i % 3 == 1:
            actual, wo_status, co_status, state = int(target * random.uniform(0.4, 0.8)), "In Progress", "In Production", "SEMI"
        else:
            actual, wo_status, co_status, state = 0, "Planned", "Pending", "RAW"
        started = datetime.utcnow() - timedelta(days=random.randint(1, 10))
        wo = models.WorkOrder(
            tenant_code=DEMO_TENANT, work_order_no=f"WO-{wo_seq}",
            part_number=code, batch_number=f"B{i + 1:02d}", machine_id=machine.id,
            target_quantity=target, actual_quantity=actual, status=wo_status,
            material_state=state, created_at=started, planned_start=started,
            planned_end=(datetime.utcnow() - timedelta(days=3) if late
                         else datetime.utcnow() + timedelta(days=random.randint(2, 12))))
        db.add(wo)
        db.flush()
        due = (date.today() - timedelta(days=2) if late
               else date.today() + timedelta(days=random.randint(3, 20)))
        db.add(models.CustomerOrder(
            tenant_code=DEMO_TENANT, order_no=f"CO-{co_seq}", customer_name=customer,
            product_name=name, linked_work_order_id=wo.id, order_quantity=target,
            dispatched_quantity=actual if state == "FIN" else 0,
            priority="High" if late else random.choice(["High", "Medium", "Medium"]),
            due_date=due, status=co_status))
    db.commit()


def _seed_quality(db, machines):
    """One inspection per work order, with a planted spike on IMM-04.

    Background fail rates stay in the 1-6% noise band; the spike machine runs at
    ~15% on one defect, so the quality problem has a stable worst machine and a
    stable top defect."""
    spike = machines[PROBLEM_QUALITY_MACHINE]
    wos = db.query(models.WorkOrder).filter(models.WorkOrder.tenant_code == DEMO_TENANT) \
            .order_by(models.WorkOrder.id).all()
    for i, wo in enumerate(wos):
        use_spike = (i % 3 == 0)              # route a third of inspections to the spike press
        machine = spike if use_spike else machines[f"IMM-{(i % 6) + 1:02d}"]
        inspected = random.randint(150, 400)
        if machine is spike:
            failed = int(inspected * 0.15)
            defect = PROBLEM_QUALITY_DEFECT
        else:
            failed = int(inspected * random.uniform(0.01, 0.06))
            defect = random.choice(DEFECTS) if failed else None
        db.add(models.QualityInspection(
            tenant_code=DEMO_TENANT, inspection_no=f"QC-{8000 + i}", work_order_id=wo.id,
            machine_id=machine.id, inspector="IPQC",
            inspected_quantity=inspected, passed_quantity=inspected - failed,
            failed_quantity=failed, defect_category=defect,
            created_at=datetime.utcnow() - timedelta(days=random.randint(0, 6))))
    db.commit()


def _seed_plans(db, machines):
    """Shift plans for the last three days, two of which came up short — so plan
    attainment has something to compare against and a real shortfall to show."""
    a, b = machines["IMM-01"], machines["IMM-06"]
    rows = [
        (3, "Day", a, 22000, 22100),
        (3, "Night", b, 24000, 23800),
        (2, "Day", a, 22000, 16800),          # short
        (2, "Night", b, 24000, 24050),
        (1, "Day", a, 22000, 19600),          # short
        (1, "Night", b, 24000, 24200),
    ]
    for i, (days_ago, shift, machine, planned, actual) in enumerate(rows):
        db.add(models.ProductionPlan(
            tenant_code=DEMO_TENANT, plan_no=f"PLAN-{4100 + i}", machine_id=machine.id,
            planned_quantity=planned, actual_quantity=actual,
            plan_date=date.today() - timedelta(days=days_ago), shift_name=shift,
            status="Behind" if actual < planned else "Completed"))
    db.commit()


def _seed_shifts(db):
    for shift, target, actual in (("Day", 22000, 19600), ("Night", 24000, 24200),
                                  ("Evening", 23000, 22100)):
        db.add(models.ShiftData(tenant_code=DEMO_TENANT, shift_name=shift,
                                target_output=target, actual_output=actual))
    db.commit()


def _seed_inventory(db):
    """Stock, with three items at or below reorder level. UPSERT so a reset keeps
    the shortage story identical without colliding on (tenant, item_code)."""
    existing = {r.item_code: r for r in db.query(models.InventoryItem)
                .filter(models.InventoryItem.tenant_code == DEMO_TENANT).all()}
    for code, name, category, unit, stock, reorder, location in INVENTORY_SEED:
        row = existing.get(code)
        if row is None:
            row = models.InventoryItem(tenant_code=DEMO_TENANT, item_code=code)
            db.add(row)
        row.item_name, row.category, row.unit = name, category, unit
        row.supplier, row.location = "Reliance Polymers", location
        row.current_stock, row.reorder_level = stock, reorder
    db.commit()


def _seed_maintenance(db, machines):
    db.add(models.MaintenanceTask(
        tenant_code=DEMO_TENANT, task_no="MT-7001", machine_id=machines[PROBLEM_MACHINE].id,
        task_type="Corrective", priority="Critical", assigned_to="Maintenance team",
        planned_date=date.today() - timedelta(days=OVERDUE_MAINTENANCE_DAYS), status="Open",
        notes="Hydraulic pump seal replacement — press stopped, under repair."))
    db.add(models.MaintenanceTask(
        tenant_code=DEMO_TENANT, task_no="MT-7002", machine_id=machines["IMM-10"].id,
        task_type="Preventive", priority="Medium", assigned_to="Maintenance team",
        planned_date=date.today(), status="Open",
        notes="Scheduled mould service and lubrication."))
    db.commit()


def seed(db, now=None):
    """Build the demo plant to its known starting state. Reproducible: the fixed
    seed fixes every draw, and `now` (default utcnow) fixes the history window."""
    _assert_demo_scope()
    _password()                      # fail fast before any wipe if unset
    random.seed(DEMO_SEED)
    wipe(db)
    _seed_identity(db)
    machines = _seed_machines(db)
    _seed_layout(db, machines)
    _seed_production(db, machines, now)
    _seed_downtime(db, machines)
    _seed_orders(db, machines)
    _seed_quality(db, machines)
    _seed_plans(db, machines)
    _seed_shifts(db)
    _seed_inventory(db)
    _seed_maintenance(db, machines)
    return machines


def status(db):
    mc = db.query(models.Machine).filter(models.Machine.tenant_code == DEMO_TENANT)
    pr = db.query(models.ProductionRecord).filter(models.ProductionRecord.tenant_code == DEMO_TENANT)
    cfg = db.query(models.TenantConfig).filter(models.TenantConfig.tenant_code == DEMO_TENANT).first()
    running = mc.filter(models.Machine.status == "Running").count()
    print(f"tenant            : {DEMO_TENANT}")
    print(f"company config    : {'present' if cfg else 'MISSING'} "
          f"(unit value {cfg.unit_value_gbp if cfg else '-'})")
    print(f"machines          : {mc.count()}  ({running} running)")
    print(f"production records : {pr.count()}")
    print("READY FOR A DEMO" if (cfg and mc.count() and pr.count()) else "NOT seeded — run --reset")


def main(argv):
    from database import SessionLocal
    tenancy.install_scoping()
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    try:
        if "--reset" in argv:
            seed(db)
            print(f"DEMO REBUILT — tenant {DEMO_TENANT}")
            print(f"  admin login      : {ADMIN_USER}")
            print(f"  supervisor login : {SUPERVISOR_USER}")
            print(f"  operator login   : {OPERATOR_USER}  (money hidden — Admin-only)")
            print(f"  machines         : {len(MACHINE_SPECS)}")
            return 0
        if "--status" in argv:
            status(db)
            return 0
        print(__doc__)
        return 1
    except DemoRefused as e:
        print(f"REFUSED: {e}")
        return 2
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
