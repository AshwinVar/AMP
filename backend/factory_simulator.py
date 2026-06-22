"""
FlowMES Factory Simulator
- Seeds every module with realistic, interconnected factory data on first run
- Then continuously drives the factory: WOs progress, shifts log, quality checks run,
  operator jobs update, inventory gets consumed, IoT signals flow, escalations fire
Run: python factory_simulator.py
"""

import os
import random
import time
from datetime import datetime, date, timedelta

from dotenv import load_dotenv
load_dotenv()

from database import SessionLocal, engine, Base
import models

Base.metadata.create_all(bind=engine)

# ── Factory identity ─────────────────────────────────────────────

MACHINES = ["CNC-01", "CNC-02", "Laser-Cutter-01", "Packaging-01", "Assembly-Robot-01"]
SHIFTS   = ["Shift A", "Shift B", "Shift C"]

OPERATORS  = ["Rajan Kumar", "Suresh Patel", "Meena Iyer", "Arjun Verma", "Priya Nair"]
INSPECTORS = ["Kamal Sharma", "Divya Singh", "Quality Inspector"]

PARTS = [
    {"code": "SHAFT-001", "name": "CNC Machined Shaft",      "machine": "CNC-01"},
    {"code": "PLATE-002", "name": "Laser Cut Plate",          "machine": "Laser-Cutter-01"},
    {"code": "BEAR-003",  "name": "Bearing Housing",          "machine": "CNC-02"},
    {"code": "GEAR-004",  "name": "Gear Component",           "machine": "CNC-01"},
    {"code": "ASSY-005",  "name": "Final Assembly Kit",       "machine": "Assembly-Robot-01"},
    {"code": "PKG-006",   "name": "Packaged Finished Goods",  "machine": "Packaging-01"},
]

CUSTOMERS = [
    "Bharat Forge Ltd",
    "Tata AutoComp Systems",
    "Mahindra Gears",
    "Precision Tools Corp",
    "Industrial Dynamics Ltd",
    "SteelCraft Industries",
]

SUPPLIERS_SEED = [
    {"code": "SUP-001", "name": "Metallica Steels Pvt Ltd",  "cat": "Raw Material",  "contact": "Rakesh Gupta"},
    {"code": "SUP-002", "name": "Precision Tooling Co.",      "cat": "Tooling",       "contact": "Anita Sharma"},
    {"code": "SUP-003", "name": "Fasteners World",            "cat": "Consumables",   "contact": "Mohammed Ali"},
    {"code": "SUP-004", "name": "Lubricants India",           "cat": "Maintenance",   "contact": "Sunita Rao"},
    {"code": "SUP-005", "name": "PackSmart Solutions",        "cat": "Packaging",     "contact": "Vikram Nair"},
]

INVENTORY_SEED = [
    # Raw Materials
    {"code": "RM-STEEL-001",  "name": "Steel Rod 50mm",           "cat": "Raw Material",   "unit": "kg",   "stock": 850,  "reorder": 200, "loc": "RM Store A1",  "supplier": "Metallica Steels Pvt Ltd"},
    {"code": "RM-SHEET-002",  "name": "MS Sheet 3mm",             "cat": "Raw Material",   "unit": "pcs",  "stock": 320,  "reorder": 100, "loc": "RM Store A2",  "supplier": "Metallica Steels Pvt Ltd"},
    {"code": "RM-ALUM-003",   "name": "Aluminium Billet 6061",    "cat": "Raw Material",   "unit": "kg",   "stock": 140,  "reorder": 50,  "loc": "RM Store A3",  "supplier": "Alco Metals India"},
    {"code": "RM-COPPER-004", "name": "Copper Strip 2mm",         "cat": "Raw Material",   "unit": "kg",   "stock": 38,   "reorder": 40,  "loc": "RM Store A4",  "supplier": "Alco Metals India"},
    {"code": "RM-STAINLESS-005","name":"SS Rod 316L 40mm",        "cat": "Raw Material",   "unit": "kg",   "stock": 210,  "reorder": 80,  "loc": "RM Store A1",  "supplier": "Metallica Steels Pvt Ltd"},
    {"code": "RM-RUBBER-006", "name": "NBR Rubber Sheet 5mm",     "cat": "Raw Material",   "unit": "pcs",  "stock": 60,   "reorder": 25,  "loc": "RM Store B1",  "supplier": "PolyPack Industries"},
    # Tooling
    {"code": "TOOL-001",      "name": "Carbide Insert CNMG 120408","cat": "Tooling",       "unit": "pcs",  "stock": 45,   "reorder": 20,  "loc": "Tool Crib T1", "supplier": "Kennametal India"},
    {"code": "TOOL-002",      "name": "End Mill 10mm 4-Flute",    "cat": "Tooling",        "unit": "pcs",  "stock": 12,   "reorder": 10,  "loc": "Tool Crib T1", "supplier": "Kennametal India"},
    {"code": "TOOL-003",      "name": "Drill Bit HSS 8mm",        "cat": "Tooling",        "unit": "pcs",  "stock": 7,    "reorder": 10,  "loc": "Tool Crib T2", "supplier": "Kennametal India"},
    {"code": "TOOL-004",      "name": "Boring Bar 25mm",          "cat": "Tooling",        "unit": "pcs",  "stock": 3,    "reorder": 2,   "loc": "Tool Crib T1", "supplier": "Kennametal India"},
    # Consumables
    {"code": "CONS-001",      "name": "Cutting Fluid Hocut 795 20L","cat": "Consumables",  "unit": "cans", "stock": 8,    "reorder": 5,   "loc": "Consumables C1","supplier": "Blaser Swisslube India"},
    {"code": "CONS-002",      "name": "Grinding Wheel 200x25mm",  "cat": "Consumables",    "unit": "pcs",  "stock": 14,   "reorder": 6,   "loc": "Consumables C1","supplier": "Saint-Gobain India"},
    {"code": "CONS-003",      "name": "Sandpaper 120 Grit Roll",  "cat": "Consumables",    "unit": "rolls","stock": 22,   "reorder": 10,  "loc": "Consumables C2","supplier": "3M India"},
    {"code": "CONS-004",      "name": "Safety Gloves Nitrile M",  "cat": "Consumables",    "unit": "pairs","stock": 150,  "reorder": 50,  "loc": "Safety Store", "supplier": "3M India"},
    # Packaging
    {"code": "PKG-MAT-001",   "name": "Corrugated Box Large",     "cat": "Packaging",      "unit": "pcs",  "stock": 500,  "reorder": 150, "loc": "Packaging P1", "supplier": "PolyPack Industries"},
    {"code": "PKG-MAT-002",   "name": "Bubble Wrap Roll 50m",     "cat": "Packaging",      "unit": "rolls","stock": 18,   "reorder": 8,   "loc": "Packaging P1", "supplier": "PolyPack Industries"},
    {"code": "PKG-MAT-003",   "name": "Stretch Film 500mm",       "cat": "Packaging",      "unit": "rolls","stock": 30,   "reorder": 12,  "loc": "Packaging P1", "supplier": "PolyPack Industries"},
    # Finished Goods
    {"code": "FG-SHAFT-001",  "name": "Precision Shaft 50mm Ø",   "cat": "Finished Goods", "unit": "pcs",  "stock": 68,   "reorder": 30,  "loc": "FG Store F1",  "supplier": "—"},
    {"code": "FG-PLATE-002",  "name": "Laser Cut Plate 300x200",  "cat": "Finished Goods", "unit": "pcs",  "stock": 42,   "reorder": 20,  "loc": "FG Store F1",  "supplier": "—"},
    {"code": "FG-GEAR-003",   "name": "Spur Gear M2 Z40",         "cat": "Finished Goods", "unit": "pcs",  "stock": 25,   "reorder": 15,  "loc": "FG Store F2",  "supplier": "—"},
    {"code": "FG-BRACKET-004","name": "Welded Bracket Assembly",  "cat": "Finished Goods", "unit": "pcs",  "stock": 11,   "reorder": 10,  "loc": "FG Store F2",  "supplier": "—"},
    # Spare Parts
    {"code": "SPARE-001",     "name": "Bearing SKF 6205-2RS",     "cat": "Spare Parts",    "unit": "pcs",  "stock": 6,    "reorder": 4,   "loc": "Spares S1",    "supplier": "SKF India"},
    {"code": "SPARE-002",     "name": "V-Belt A50 Gates",         "cat": "Spare Parts",    "unit": "pcs",  "stock": 4,    "reorder": 3,   "loc": "Spares S1",    "supplier": "SKF India"},
    {"code": "SPARE-003",     "name": "Hydraulic Seal Kit CNC-01","cat": "Spare Parts",    "unit": "kits", "stock": 2,    "reorder": 2,   "loc": "Spares S2",    "supplier": "Bosch Rexroth India"},
    {"code": "SPARE-004",     "name": "Servo Motor Drive 5.5kW",  "cat": "Spare Parts",    "unit": "pcs",  "stock": 1,    "reorder": 1,   "loc": "Spares S2",    "supplier": "Siemens India"},
]

DOCS_SEED = [
    {"no": "SOP-001",  "title": "CNC Machine Operation SOP",           "type": "SOP",            "dept": "Production"},
    {"no": "SOP-002",  "title": "Laser Cutter Safety Procedure",       "type": "SOP",            "dept": "Production"},
    {"no": "QP-001",   "title": "Dimensional Inspection Checklist",    "type": "Quality Plan",   "dept": "Quality"},
    {"no": "QP-002",   "title": "First Article Inspection Report",     "type": "Quality Plan",   "dept": "Quality"},
    {"no": "MAINT-001","title": "Preventive Maintenance Schedule",     "type": "Maintenance Plan","dept": "Maintenance"},
    {"no": "COMP-001", "title": "ISO 9001:2015 Compliance Record",     "type": "Compliance",     "dept": "QA"},
    {"no": "COMP-002", "title": "Environment Health & Safety Manual",  "type": "Compliance",     "dept": "HR"},
]

today = date.today()


# ─────────────────────────────────────────────────────────────────
# SEED FUNCTIONS  (idempotent — skip if data already exists)
# ─────────────────────────────────────────────────────────────────

_MACHINE_SEED_STATUS = [
    {"status": "Running",   "utilization": 87, "downtime": "0 min"},
    {"status": "Running",   "utilization": 73, "downtime": "0 min"},
    {"status": "Running",   "utilization": 91, "downtime": "0 min"},
    {"status": "Breakdown", "utilization": 0,  "downtime": "2 hrs 15 min"},
    {"status": "Running",   "utilization": 68, "downtime": "0 min"},
]

def _machines(db):
    if db.query(models.Machine).count() > 0:
        return
    for i, name in enumerate(MACHINES):
        s = _MACHINE_SEED_STATUS[i % len(_MACHINE_SEED_STATUS)]
        db.add(models.Machine(name=name, status=s["status"], utilization=s["utilization"], downtime=s["downtime"]))
    db.commit()
    print("[SEED] Machines")


def _suppliers(db):
    if db.query(models.Supplier).count() > 0:
        return
    for s in SUPPLIERS_SEED:
        db.add(models.Supplier(
            supplier_code=s["code"], supplier_name=s["name"],
            contact_person=s["contact"], category=s["cat"],
            email=f"orders@{s['name'].lower()[:8].replace(' ','')}.com",
            phone=f"+91-98{random.randint(10000000,99999999)}",
            status="Active",
        ))
    db.commit()
    print("[SEED] Suppliers")


def _inventory(db):
    if db.query(models.InventoryItem).count() > 0:
        return
    for item in INVENTORY_SEED:
        db.add(models.InventoryItem(
            item_code=item["code"], item_name=item["name"],
            category=item["cat"], unit=item["unit"],
            current_stock=item["stock"], reorder_level=item["reorder"],
            location=item.get("loc", "Warehouse A"),
            supplier=item.get("supplier", "—"),
        ))
    db.commit()
    print("[SEED] Inventory")


def _inventory_transactions(db):
    if db.query(models.InventoryTransaction).count() > 0:
        return
    items = {i.item_code: i for i in db.query(models.InventoryItem).all()}
    if not items:
        return

    txn_templates = [
        # (item_code, type, qty, reference, notes, days_ago)
        ("RM-STEEL-001",    "IN",         500, "PO-2025-041", "Monthly steel stock replenishment", 28),
        ("RM-STEEL-001",    "OUT",         80, "WO-1001",     "Consumed for shaft batch WO-1001",   25),
        ("RM-STEEL-001",    "OUT",         60, "WO-1003",     "Consumed for gear batch WO-1003",    20),
        ("RM-STEEL-001",    "IN",         400, "PO-2025-055", "Emergency restock — low buffer",     14),
        ("RM-STEEL-001",    "OUT",        110, "WO-1007",     "CNC shaft run WO-1007",               7),
        ("RM-SHEET-002",    "IN",         200, "PO-2025-042", "MS sheet delivery from Metallica",   27),
        ("RM-SHEET-002",    "OUT",         50, "WO-1002",     "Laser cut plate run WO-1002",        22),
        ("RM-SHEET-002",    "OUT",         30, "WO-1006",     "Bracket fabrication batch",          10),
        ("RM-ALUM-003",     "IN",         100, "PO-2025-043", "Aluminium billet — Alco Metals",     26),
        ("RM-ALUM-003",     "OUT",         40, "WO-1005",     "Gear machining WO-1005",             18),
        ("RM-ALUM-003",     "OUT",         20, "WO-1009",     "Prototype run WO-1009",               5),
        ("RM-COPPER-004",   "IN",          60, "PO-2025-048", "Copper strip procurement",           20),
        ("RM-COPPER-004",   "OUT",         22, "WO-1004",     "Electrical bracket WO-1004",         12),
        ("RM-STAINLESS-005","IN",         300, "PO-2025-050", "SS rod — hygiene-grade parts order", 15),
        ("RM-STAINLESS-005","OUT",         90, "WO-1010",     "SS shaft run WO-1010",                8),
        ("TOOL-001",        "IN",          30, "PO-2025-044", "Carbide insert quarterly order",     30),
        ("TOOL-001",        "OUT",         10, "WO-1001",     "Tooling issue — CNC-01 setup",       25),
        ("TOOL-001",        "OUT",          5, "WO-1007",     "Tooling issue — CNC-02 setup",        6),
        ("TOOL-002",        "IN",          15, "PO-2025-045", "End mill restock",                   28),
        ("TOOL-002",        "OUT",          3, "WO-1003",     "Milling op — CNC-01",                20),
        ("TOOL-003",        "IN",          10, "PO-2025-051", "Drill bit restock",                  18),
        ("TOOL-003",        "OUT",          3, "WO-1008",     "Drilling op — CNC-02",                9),
        ("CONS-001",        "IN",          10, "PO-2025-046", "Cutting fluid bulk order",           25),
        ("CONS-001",        "OUT",          2, "MAINT-JUNE",  "Monthly CNC coolant top-up",         15),
        ("CONS-002",        "IN",          20, "PO-2025-052", "Grinding wheel stock",               16),
        ("CONS-002",        "OUT",          6, "WO-1005",     "Surface grinding op",                11),
        ("PKG-MAT-001",     "IN",         300, "PO-2025-047", "Box stock for Q2 dispatch",          22),
        ("PKG-MAT-001",     "OUT",        100, "SHIP-BF-031", "Dispatch to Bharat Forge",           10),
        ("PKG-MAT-001",     "OUT",         80, "SHIP-TC-019", "Dispatch to Tata AutoComp",           4),
        ("FG-SHAFT-001",    "IN",          40, "WO-1001",     "Shaft batch WO-1001 completed",      23),
        ("FG-SHAFT-001",    "OUT",         20, "SHIP-BF-031", "Delivery — Bharat Forge PO BF-031",  10),
        ("FG-SHAFT-001",    "OUT",         10, "SHIP-MG-012", "Delivery — Mahindra Gears PO MG-012", 3),
        ("FG-PLATE-002",    "IN",          30, "WO-1002",     "Plate batch WO-1002 completed",      21),
        ("FG-PLATE-002",    "OUT",         18, "SHIP-TC-019", "Delivery — Tata AutoComp",            4),
        ("FG-GEAR-003",     "IN",          20, "WO-1005",     "Gear batch WO-1005 completed",       17),
        ("FG-GEAR-003",     "OUT",          8, "SHIP-MG-012", "Delivery — Mahindra Gears",           3),
        ("SPARE-001",       "IN",           5, "PO-2025-049", "Bearing restock — maintenance kit",  20),
        ("SPARE-001",       "OUT",          2, "MAINT-CNC01", "Bearing replaced — CNC-01 PM",       12),
        ("SPARE-002",       "IN",           4, "PO-2025-053", "V-belt restock",                     14),
        ("SPARE-002",       "OUT",          1, "MAINT-CNC02", "Belt replaced — CNC-02 PM",           7),
        ("SPARE-003",       "IN",           2, "PO-2025-054", "Hydraulic seal kit — preventive",    10),
        ("SPARE-004",       "ADJUSTMENT",   1, "ADJ-2025-01", "Stock audit — count verified",        5),
    ]

    for code, txn_type, qty, ref, notes, days_ago in txn_templates:
        item = items.get(code)
        if not item:
            continue
        db.add(models.InventoryTransaction(
            item_id=item.id,
            transaction_type=txn_type,
            quantity=qty,
            reference=ref,
            notes=notes,
            created_at=datetime.now() - timedelta(days=days_ago),
        ))
    db.commit()
    print("[SEED] Inventory Transactions")


def _documents(db):
    if db.query(models.ComplianceDocument).count() > 0:
        return
    for doc in DOCS_SEED:
        db.add(models.ComplianceDocument(
            document_no=doc["no"], title=doc["title"],
            document_type=doc["type"], department=doc["dept"],
            version="1.0", owner="QA Lead", approval_status="Approved",
            review_due_date=today + timedelta(days=random.randint(30, 180)),
        ))
    db.commit()
    print("[SEED] Documents")


def _work_orders(db):
    machines = db.query(models.Machine).all()
    if not machines:
        return
    existing = {wo.work_order_no for wo in db.query(models.WorkOrder).all()}

    statuses = ["Planned", "In Progress", "In Progress", "In Progress", "Completed", "On Hold"]
    for i in range(1, 16):
        wo_no = f"WO-{1000 + i}"
        if wo_no in existing:
            continue
        part    = PARTS[(i - 1) % len(PARTS)]
        machine = next((m for m in machines if m.name == part["machine"]), random.choice(machines))
        target  = random.randint(200, 600)
        status  = statuses[i % len(statuses)]
        actual  = target if status == "Completed" else random.randint(0, int(target * 0.8))
        db.add(models.WorkOrder(
            work_order_no=wo_no,
            part_number=part["code"],
            batch_number=f"BATCH-2025-{i:03d}",
            machine_id=machine.id,
            target_quantity=target,
            actual_quantity=actual,
            status=status,
            planned_start=datetime.now() - timedelta(days=random.randint(0, 10)),
            planned_end=datetime.now()   + timedelta(days=random.randint(1, 14)),
        ))
    db.commit()
    print("[SEED] Work Orders")


def _production_plans(db):
    wos      = db.query(models.WorkOrder).all()
    machines = db.query(models.Machine).all()
    if not wos or not machines:
        return
    existing = {pp.plan_no for pp in db.query(models.ProductionPlan).all()}

    for i, wo in enumerate(wos):
        plan_no = f"PP-{2000 + i}"
        if plan_no in existing:
            continue
        machine  = next((m for m in machines if m.id == wo.machine_id), random.choice(machines))
        planned  = random.randint(100, 300)
        actual   = planned if wo.status == "Completed" else random.randint(0, int(planned * 0.85))
        db.add(models.ProductionPlan(
            plan_no=plan_no,
            work_order_id=wo.id,
            machine_id=machine.id,
            planned_quantity=planned,
            actual_quantity=actual,
            plan_date=today - timedelta(days=random.randint(0, 5)),
            shift_name=random.choice(SHIFTS),
            status=wo.status if wo.status != "On Hold" else "Planned",
        ))
    db.commit()
    print("[SEED] Production Plans")


def _schedules(db):
    wos      = db.query(models.WorkOrder).all()
    plans    = db.query(models.ProductionPlan).all()
    machines = db.query(models.Machine).all()
    if not wos or not machines:
        return
    existing = {s.schedule_no for s in db.query(models.ProductionSchedule).all()}

    sched_statuses = ["Scheduled", "Scheduled", "In Progress", "Completed", "Delayed"]
    for i in range(1, 20):
        sched_no = f"SCHED-{3000 + i}"
        if sched_no in existing:
            continue
        wo      = wos[(i - 1) % len(wos)]
        plan    = plans[(i - 1) % len(plans)] if plans else None
        machine = next((m for m in machines if m.id == wo.machine_id), random.choice(machines))
        db.add(models.ProductionSchedule(
            schedule_no=sched_no,
            work_order_id=wo.id,
            production_plan_id=plan.id if plan else None,
            machine_id=machine.id,
            shift_name=SHIFTS[i % 3],
            scheduled_date=today + timedelta(days=random.randint(-2, 7)),
            priority=random.choice(["High", "Medium", "Medium", "Low"]),
            planned_quantity=random.randint(50, 200),
            estimated_minutes=random.choice([240, 360, 480]),
            status=sched_statuses[i % len(sched_statuses)],
        ))
    db.commit()
    print("[SEED] Production Schedules")


def _customer_orders(db):
    wos   = db.query(models.WorkOrder).all()
    plans = db.query(models.ProductionPlan).all()
    if not wos:
        return
    existing = {co.order_no for co in db.query(models.CustomerOrder).all()}

    co_statuses = ["Pending", "In Production", "In Production", "Ready to Dispatch", "Dispatched", "Partially Dispatched"]
    for i in range(1, 13):
        order_no = f"ORD-{5000 + i}"
        if order_no in existing:
            continue
        wo       = wos[(i - 1) % len(wos)]
        plan     = plans[(i - 1) % len(plans)] if plans else None
        qty      = random.randint(100, 500)
        status   = co_statuses[i % len(co_statuses)]
        dispatched = qty if status == "Dispatched" else random.randint(0, int(qty * 0.9))
        db.add(models.CustomerOrder(
            order_no=order_no,
            customer_name=CUSTOMERS[i % len(CUSTOMERS)],
            product_name=PARTS[i % len(PARTS)]["name"],
            linked_work_order_id=wo.id,
            linked_production_plan_id=plan.id if plan else None,
            order_quantity=qty,
            dispatched_quantity=dispatched,
            priority=random.choice(["High", "Medium", "Medium", "Low"]),
            due_date=today + timedelta(days=random.randint(-3, 21)),
            status=status,
        ))
    db.commit()
    print("[SEED] Customer Orders")


def _purchase_orders(db):
    suppliers = db.query(models.Supplier).all()
    items     = db.query(models.InventoryItem).all()
    if not suppliers:
        return
    existing = {po.po_no for po in db.query(models.PurchaseOrder).all()}

    po_statuses = ["Open", "Open", "Partially Received", "Received", "Overdue"]
    for i in range(1, 10):
        po_no = f"PO-{6000 + i}"
        if po_no in existing:
            continue
        supplier = suppliers[i % len(suppliers)]
        item     = items[i % len(items)] if items else None
        qty      = random.randint(50, 300)
        status   = po_statuses[i % len(po_statuses)]
        received = qty if status == "Received" else random.randint(0, int(qty * 0.7))
        db.add(models.PurchaseOrder(
            po_no=po_no,
            supplier_id=supplier.id,
            item_id=item.id if item else None,
            item_name=item.item_name if item else "General Supply",
            order_quantity=qty,
            received_quantity=received,
            unit=item.unit if item else "pcs",
            expected_delivery_date=today + timedelta(days=random.randint(-2, 14)),
            status=status,
        ))
    db.commit()
    print("[SEED] Purchase Orders")


def _shifts(db):
    if db.query(models.ShiftData).count() > 5:
        return
    for day_offset in range(7):
        d = today - timedelta(days=day_offset)
        for shift in SHIFTS:
            target = random.randint(300, 500)
            actual = int(target * random.uniform(0.72, 0.98))
            db.add(models.ShiftData(
                shift_name=f"{shift} – {d.strftime('%d %b')}",
                target_output=target,
                actual_output=actual,
                created_at=datetime.combine(d, datetime.min.time()),
            ))
    db.commit()
    print("[SEED] Shifts (7 days history)")


def _quality(db):
    if db.query(models.QualityInspection).count() > 5:
        return
    wos      = db.query(models.WorkOrder).all()
    machines = db.query(models.Machine).all()
    if not wos:
        return
    for i in range(1, 13):
        wo      = wos[i % len(wos)]
        machine = next((m for m in machines if m.id == wo.machine_id), random.choice(machines))
        inspected = random.randint(50, 200)
        failed    = random.randint(0, max(1, int(inspected * 0.07)))
        passed    = inspected - failed
        db.add(models.QualityInspection(
            inspection_no=f"QI-{7000 + i}",
            work_order_id=wo.id,
            machine_id=machine.id,
            inspector=random.choice(INSPECTORS),
            inspected_quantity=inspected,
            passed_quantity=passed,
            failed_quantity=failed,
            defect_category=random.choice(["Dimensional", "Surface Finish", "Burr", "Scratch", ""]),
            rework_quantity=random.randint(0, failed),
            scrap_quantity=random.randint(0, max(0, failed // 3)),
            status="Passed" if failed == 0 else random.choice(["Failed", "Rework"]),
        ))
    db.commit()
    print("[SEED] Quality Inspections")


def _maintenance(db):
    if db.query(models.MaintenanceTask).count() > 3:
        return
    machines = db.query(models.Machine).all()
    if not machines:
        return
    task_types = ["Preventive", "Corrective", "Predictive", "Lubrication", "Calibration"]
    spares     = ["Bearing SKF 6205", "O-Ring Kit", "Spindle Belt", "Filter Element", ""]
    for i, machine in enumerate(machines * 2):
        db.add(models.MaintenanceTask(
            task_no=f"MAINT-{8000 + i}",
            machine_id=machine.id,
            task_type=task_types[i % len(task_types)],
            priority=random.choice(["High", "Medium", "Medium", "Low"]),
            assigned_to="Maintenance Team",
            planned_date=today + timedelta(days=random.randint(-5, 10)),
            downtime_minutes=random.randint(0, 120),
            spare_parts_used=spares[i % len(spares)],
            status=random.choice(["Open", "In Progress", "Completed", "Open"]),
        ))
    db.commit()
    print("[SEED] Maintenance Tasks")


def _operator_jobs(db):
    if db.query(models.OperatorJobExecution).count() > 5:
        return
    machines = db.query(models.Machine).all()
    wos      = db.query(models.WorkOrder).all()
    plans    = db.query(models.ProductionPlan).all()
    if not machines or not wos:
        return
    for i in range(1, 13):
        machine = machines[i % len(machines)]
        wo      = wos[i % len(wos)]
        plan    = plans[i % len(plans)] if plans else None
        good    = random.randint(30, 150)
        reject  = random.randint(0, max(0, int(good * 0.04)))
        db.add(models.OperatorJobExecution(
            execution_no=f"EXE-{9000 + i}",
            operator_name=OPERATORS[i % len(OPERATORS)],
            machine_id=machine.id,
            work_order_id=wo.id,
            production_plan_id=plan.id if plan else None,
            job_status=random.choice(["Completed", "In Progress", "Completed", "Paused"]),
            good_count=good,
            rejected_count=reject,
        ))
    db.commit()
    print("[SEED] Operator Jobs")


def _costs(db):
    if db.query(models.CostRecord).count() > 5:
        return
    wos = db.query(models.WorkOrder).all()
    cost_types = ["Material", "Labour", "Overhead", "Tooling", "Maintenance"]
    for i in range(1, 16):
        cost_type = cost_types[i % len(cost_types)]
        db.add(models.CostRecord(
            cost_no=f"COST-{10000 + i}",
            cost_type=cost_type,
            reference_type="WorkOrder" if wos else "",
            reference_id=wos[i % len(wos)].id if wos else 0,
            description=f"{cost_type} – production run {i}",
            amount=random.randint(500, 25000),
            department=random.choice(["Production", "Maintenance", "Quality", "Stores"]),
        ))
    db.commit()
    print("[SEED] Cost Records")


def _escalations(db):
    if db.query(models.Escalation).count() > 3:
        return
    machines = db.query(models.Machine).all()
    if not machines:
        return
    titles = [
        "{machine} – Repeated Breakdown",
        "Quality Rejection Rate Above Threshold",
        "Low Stock Alert – Steel Rod 50mm",
        "Overdue Purchase Order – PO-6002",
        "Missed Preventive Maintenance – {machine}",
    ]
    for i, title_tpl in enumerate(titles):
        machine = machines[i % len(machines)]
        db.add(models.Escalation(
            machine_id=machine.id,
            title=title_tpl.format(machine=machine.name),
            severity=["Critical", "High", "High", "Medium", "Medium"][i],
            owner=random.choice(["Maintenance Lead", "Production Manager", "Quality Head"]),
            department=random.choice(["Maintenance", "Production", "Quality"]),
            status=random.choice(["Open", "In Progress", "Open"]),
            source="system",
        ))
    db.commit()
    print("[SEED] Escalations")


def _iot(db):
    if db.query(models.IoTTelemetry).count() > 10:
        return
    machines = db.query(models.Machine).all()
    signals  = [
        ("temperature",   "°C",    28, 85),
        ("vibration",     "mm/s",   1, 12),
        ("spindle_speed", "RPM",  800, 3000),
        ("feed_rate",     "mm/min",100, 500),
        ("pressure",      "bar",    4, 8),
        ("power",         "kW",    10, 45),
    ]
    for machine in machines:
        for sig_name, unit, lo, hi in signals:
            val = random.randint(lo, hi)
            db.add(models.IoTTelemetry(
                machine_id=machine.id,
                signal_name=sig_name,
                signal_value=str(val),
                numeric_value=val,
                unit=unit,
                source="MQTT",
            ))
    db.commit()
    print("[SEED] IoT Telemetry")


def _ai_recs(db):
    if db.query(models.AIRecommendation).count() > 3:
        return
    machines = db.query(models.Machine).all()
    templates = [
        ("Predictive Maintenance", "High",   "Schedule bearing inspection – {m}",
         "Vibration signature on {m} suggests bearing wear. Inspect within 72 h."),
        ("Quality",               "Medium",  "Tooling change recommended – {m}",
         "Tool wear pattern indicates >15% rejection risk. Change insert now."),
        ("Energy Optimisation",   "Low",     "Idle time reduction – {m}",
         "{m} has 22% idle time this shift. Schedule job resequencing."),
        ("Maintenance",           "Medium",  "Lubrication cycle overdue – {m}",
         "Lubrication cycle for {m} is 3 days overdue per PM schedule."),
    ]
    for i, machine in enumerate(machines):
        t = templates[i % len(templates)]
        db.add(models.AIRecommendation(
            recommendation_type=t[0], severity=t[1],
            title=t[2].format(m=machine.name),
            message=t[3].format(m=machine.name),
            related_machine_id=machine.id,
            confidence=random.randint(70, 96),
            status="Open",
        ))
    db.commit()
    print("[SEED] AI Recommendations")


def _notifications(db):
    if db.query(models.Notification).count() > 5:
        return
    notifs = [
        ("Alert",   "Critical", "Machine Breakdown – CNC-01",       "CNC-01 entered breakdown state. Maintenance dispatched."),
        ("Warning", "High",     "Low Inventory – Steel Rod 50mm",   "Steel Rod stock at 85 kg, below reorder level of 200 kg."),
        ("Info",    "Medium",   "Shift Handover Due",                "Shift A ends in 30 minutes. Complete handover checklist."),
        ("Alert",   "High",     "Quality Rejection Spike – CNC-02", "Rejection rate exceeded 5% on CNC-02 in last hour."),
        ("Info",    "Low",      "PM Scheduled – Laser-Cutter-01",   "Preventive maintenance is due tomorrow at Shift A start."),
        ("Warning", "Medium",   "OEE Below Target",                  "Factory OEE at 68% this shift, below 75% target."),
    ]
    for n_type, sev, title, msg in notifs:
        db.add(models.Notification(
            notification_type=n_type, severity=sev, title=title, message=msg, status="Unread"
        ))
    db.commit()
    print("[SEED] Notifications")


def _downtime_logs(db):
    if db.query(models.DowntimeLog).count() > 0:
        return
    machines = db.query(models.Machine).all()
    machine_map = {m.name: m.id for m in machines}

    logs = [
        {"machine": "Packaging-01",     "reason": "Mechanical Failure", "duration": "2 hrs 15 min", "notes": "Drive belt snapped. Replacement ordered."},
        {"machine": "CNC-01",           "reason": "Tooling Change",     "duration": "45 min",        "notes": "Scheduled insert change between jobs."},
        {"machine": "CNC-02",           "reason": "Setup / Changeover", "duration": "30 min",        "notes": "Job changeover from SHAFT-001 to BEAR-003."},
        {"machine": "Laser-Cutter-01",  "reason": "Power Fluctuation",  "duration": "15 min",        "notes": "UPS tripped. Power restored, recalibrated."},
        {"machine": "CNC-01",           "reason": "Quality Hold",       "duration": "1 hr 10 min",   "notes": "Batch QI-7003 failed dimensional check. Rework in progress."},
        {"machine": "Assembly-Robot-01","reason": "Sensor Fault",       "duration": "50 min",        "notes": "End-effector proximity sensor error. Reset and tested OK."},
    ]
    for entry in logs:
        mid = machine_map.get(entry["machine"])
        if not mid:
            continue
        db.add(models.DowntimeLog(
            machine_id=mid,
            reason=entry["reason"],
            duration=entry["duration"],
            notes=entry["notes"],
        ))
    db.commit()
    print("[SEED] Downtime Logs")


def _tenant(db):
    if db.query(models.CompanyTenant).count() > 0:
        return
    db.add(models.CompanyTenant(
        company_code="DEMO-001",
        company_name="Precision Parts Pvt Ltd",
        industry="Automotive Components",
        plan_name="Enterprise",
        subscription_status="Active",
        seats=25,
        monthly_fee=49999,
    ))
    db.commit()
    print("[SEED] Demo Tenant")


def seed_all(db):
    print("\n=== FlowMES Factory Simulator — Initial Seed ===\n")
    _machines(db)
    _suppliers(db)
    _inventory(db)
    _inventory_transactions(db)
    _documents(db)
    _work_orders(db)
    _production_plans(db)
    _schedules(db)
    _customer_orders(db)
    _purchase_orders(db)
    _shifts(db)
    _quality(db)
    _maintenance(db)
    _operator_jobs(db)
    _costs(db)
    _downtime_logs(db)
    _escalations(db)
    _iot(db)
    _ai_recs(db)
    _notifications(db)
    _tenant(db)
    print("\n[SEED] Complete. All modules populated.\n")


# ─────────────────────────────────────────────────────────────────
# LIVE SIMULATION TICKS
# ─────────────────────────────────────────────────────────────────

def tick_work_order_progress(db):
    """Advance the next In Progress WO. Flip Planned → In Progress if none."""
    wo = db.query(models.WorkOrder).filter(
        models.WorkOrder.status == "In Progress"
    ).order_by(models.WorkOrder.id).first()

    if not wo:
        wo = db.query(models.WorkOrder).filter(
            models.WorkOrder.status == "Planned"
        ).first()
        if wo:
            wo.status = "In Progress"

    if wo and wo.actual_quantity < wo.target_quantity:
        wo.actual_quantity = min(wo.target_quantity, wo.actual_quantity + random.randint(8, 35))
        if wo.actual_quantity >= wo.target_quantity:
            wo.status = "Completed"
            plan = db.query(models.ProductionPlan).filter(
                models.ProductionPlan.work_order_id == wo.id
            ).first()
            if plan:
                plan.status     = "Completed"
                plan.actual_quantity = plan.planned_quantity
        db.commit()
        print(f"  WO {wo.work_order_no}: {wo.actual_quantity}/{wo.target_quantity} [{wo.status}]")


def tick_shift_entry(db):
    """Log a shift output record for the current shift."""
    hour  = datetime.now().hour
    shift = SHIFTS[hour // 8 % 3]
    target = random.randint(280, 450)
    actual = int(target * random.uniform(0.74, 0.97))
    db.add(models.ShiftData(
        shift_name=f"{shift} – {today.strftime('%d %b')}",
        target_output=target, actual_output=actual,
    ))
    db.commit()
    print(f"  Shift log: {shift} | actual {actual}/{target}")


def tick_quality(db):
    """Add a quality inspection for an active work order."""
    wos      = db.query(models.WorkOrder).filter(models.WorkOrder.status == "In Progress").all()
    machines = db.query(models.Machine).all()
    if not wos or not machines:
        return
    wo      = random.choice(wos)
    machine = next((m for m in machines if m.id == wo.machine_id), random.choice(machines))
    inspected = random.randint(20, 100)
    failed    = random.randint(0, max(1, int(inspected * 0.07)))
    passed    = inspected - failed
    count     = db.query(models.QualityInspection).count()
    db.add(models.QualityInspection(
        inspection_no=f"QI-{7000 + count + 1}",
        work_order_id=wo.id, machine_id=machine.id,
        inspector=random.choice(INSPECTORS),
        inspected_quantity=inspected, passed_quantity=passed, failed_quantity=failed,
        defect_category=random.choice(["Dimensional", "Surface Finish", "Burr", ""]) if failed > 0 else "",
        rework_quantity=random.randint(0, failed), scrap_quantity=0,
        status="Passed" if failed == 0 else random.choice(["Failed", "Rework"]),
    ))
    db.commit()
    print(f"  Quality: {inspected} inspected, {passed} passed, {failed} failed")


def tick_operator(db):
    """Update an in-progress operator job or start a new one."""
    job = db.query(models.OperatorJobExecution).filter(
        models.OperatorJobExecution.job_status == "In Progress"
    ).first()
    if job:
        job.good_count += random.randint(5, 20)
        if random.random() < 0.3:
            job.job_status  = "Completed"
            job.completed_at = datetime.utcnow()
        db.commit()
        print(f"  Operator {job.execution_no}: {job.job_status} | good={job.good_count}")
    else:
        machines = db.query(models.Machine).filter(models.Machine.status == "Running").all()
        wos      = db.query(models.WorkOrder).filter(models.WorkOrder.status == "In Progress").all()
        if machines and wos:
            count = db.query(models.OperatorJobExecution).count()
            db.add(models.OperatorJobExecution(
                execution_no=f"EXE-{9000 + count + 1}",
                operator_name=random.choice(OPERATORS),
                machine_id=random.choice(machines).id,
                work_order_id=random.choice(wos).id,
                job_status="In Progress",
                good_count=0, rejected_count=0,
            ))
            db.commit()
            print("  New operator job started")


def tick_iot(db):
    """Push new IoT telemetry rows for a random machine."""
    machines = db.query(models.Machine).all()
    if not machines:
        return
    machine  = random.choice(machines)
    running  = machine.status == "Running"
    signals  = [
        ("temperature",   "°C",    28 if running else 22, 85 if running else 40),
        ("vibration",     "mm/s",   2 if running else 0,  12 if running else 3),
        ("spindle_speed", "RPM",  800 if running else 0, 3000 if running else 0),
        ("power",         "kW",   10  if running else 1,  45  if running else 3),
    ]
    for sig_name, unit, lo, hi in random.sample(signals, 2):
        val = random.randint(lo, hi)
        db.add(models.IoTTelemetry(
            machine_id=machine.id, signal_name=sig_name,
            signal_value=str(val), numeric_value=val, unit=unit, source="MQTT",
        ))
    db.commit()
    print(f"  IoT: signals pushed for {machine.name}")


def tick_inventory(db):
    """Consume raw material inventory for production — capped at 120 auto transactions."""
    count = db.query(models.InventoryTransaction).filter(
        models.InventoryTransaction.notes == "Auto-issued by simulator"
    ).count()
    if count >= 120:
        return
    items = db.query(models.InventoryItem).filter(
        models.InventoryItem.category == "Raw Material",
        models.InventoryItem.current_stock > 20,
    ).all()
    if not items:
        return
    item = random.choice(items)
    qty  = random.randint(5, 25)
    db.add(models.InventoryTransaction(
        item_id=item.id, transaction_type="Issue",
        quantity=qty, reference="Production",
        notes="Issued to production line",
    ))
    item.current_stock = max(0, item.current_stock - qty)
    db.commit()
    print(f"  Inventory: issued {qty} {item.unit} of {item.item_name} (remaining: {item.current_stock})")


def tick_escalation(db):
    """Raise an escalation when a machine is in breakdown."""
    machines = db.query(models.Machine).filter(
        models.Machine.status == "Breakdown"
    ).all()
    if not machines:
        return
    machine = random.choice(machines)
    count   = db.query(models.Escalation).count()
    db.add(models.Escalation(
        machine_id=machine.id,
        title=f"{machine.name} – Breakdown Alert #{count + 1}",
        severity="Critical",
        owner="Maintenance Lead",
        department="Maintenance",
        status="Open", source="system",
        notes="Auto-generated by factory simulator",
    ))
    db.commit()
    print(f"  Escalation raised for {machine.name}")


def tick_customer_order(db):
    """Advance dispatched quantity on a pending customer order."""
    order = db.query(models.CustomerOrder).filter(
        models.CustomerOrder.status.in_(["In Production", "Ready to Dispatch"])
    ).first()
    if not order:
        return
    order.dispatched_quantity = min(order.order_quantity,
                                    order.dispatched_quantity + random.randint(10, 50))
    if order.dispatched_quantity >= order.order_quantity:
        order.status = "Dispatched"
    elif order.dispatched_quantity > 0:
        order.status = "Partially Dispatched"
    db.commit()
    print(f"  Order {order.order_no}: dispatched {order.dispatched_quantity}/{order.order_quantity} [{order.status}]")


def run_simulation(db):
    """One simulation tick: randomly choose 2-4 actions to simulate."""
    actions = [
        (tick_work_order_progress, 30),
        (tick_shift_entry,         8),
        (tick_quality,             15),
        (tick_operator,            15),
        (tick_iot,                 20),
        (tick_inventory,           8),
        (tick_customer_order,      10),
        (tick_escalation,          3),
    ]
    fns, weights = zip(*actions)
    chosen = random.choices(fns, weights=weights, k=random.randint(2, 4))
    for fn in chosen:
        try:
            fn(db)
        except Exception as e:
            db.rollback()
            print(f"  [WARN] {fn.__name__} failed: {e}")


# ─────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db = SessionLocal()
    try:
        seed_all(db)
        tick = 0
        print("Live simulation running. Press Ctrl+C to stop.\n")
        while True:
            tick += 1
            print(f"[Tick #{tick:04d}]")
            run_simulation(db)
            time.sleep(5)
    except KeyboardInterrupt:
        print("\nSimulator stopped.")
    finally:
        db.close()
