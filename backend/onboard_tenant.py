"""Tenant onboarding — a generic starter factory for a brand-new company.

When the founder creates a company in SaaS Admin, the new tenant shouldn't open
onto an empty dashboard. This seeds a small, industry-neutral starter set —
one production line, four machines with a digital-twin layout, a few days of
production history, starter inventory/orders/work orders and one of each
operational record — so every pillar (OEE, cost, delivery, briefing, twin,
search, copilot) lights up immediately. Generic by design (no customer-specific
naming — see the build-generic principle); DEFAULT and every existing tenant
are untouched.

All rows are written under the target tenant by binding the tenant contextvar
for the duration (ADR-0002), exactly like the simulation loop does.
"""
from datetime import datetime, timedelta

import models
from tenancy import set_current_tenant, reset_current_tenant

LINE = "LINE-A"

_MACHINES = [
    ("CNC-01", "Running", 82),
    ("CNC-02", "Running", 76),
    ("ASSEMBLY-01", "Idle", 0),
    ("QC-01", "Running", 88),
]


def seed_starter_factory(db, tenant_code: str, company_name: str = "") -> bool:
    """Seed the starter factory for ``tenant_code``. Returns True if seeded,
    False if the tenant already has machines (never reseeds over real data)."""
    token = set_current_tenant(tenant_code)
    try:
        if db.query(models.Machine).first() is not None:
            return False   # the tenant already has a factory — leave it alone

        now = datetime.utcnow()
        label = company_name or tenant_code

        machines = []
        for idx, (name, status, util) in enumerate(_MACHINES):
            m = models.Machine(name=name, status=status, utilization=util,
                              downtime="0 min", line=LINE)
            db.add(m)
            machines.append(m)
        db.flush()

        for idx, m in enumerate(machines):
            db.add(models.FactoryLayoutNode(
                machine_id=m.id, node_name=m.name, node_type="machine",
                x_position=60 + idx * 280, y_position=140, width=220, height=120,
                zone=LINE))

        # A few days of production history so OEE / cost / scorecard have data.
        for m in machines[:3]:
            for d in range(3):
                db.add(models.ProductionRecord(
                    machine_id=m.id, planned_minutes=480, runtime_minutes=445 + d * 10,
                    ideal_cycle_time_seconds=45, total_count=520 + d * 20,
                    good_count=505 + d * 20, rejected_count=15 - d * 5,
                    created_at=now - timedelta(days=d)))

        db.add(models.DowntimeLog(machine_id=machines[0].id, reason="Tool change",
                                  duration="25 min", created_at=now - timedelta(days=1)))
        db.add(models.QualityInspection(
            inspection_no=f"{tenant_code}-QC-1", machine_id=machines[3].id, inspector="QA",
            inspected_quantity=200, passed_quantity=194, failed_quantity=6,
            defect_category="Dimensional", created_at=now - timedelta(days=1)))
        db.add(models.ShiftData(shift_name=f"Day – {now.date().isoformat()}",
                                target_output=1500, actual_output=1385))

        # item_code is globally unique across tenants (models.py), so starter
        # codes must be tenant-prefixed like every other seeded business key —
        # unprefixed codes made every seed after the first tenant's fail.
        db.add(models.InventoryItem(item_code=f"{tenant_code}-RM-001", item_name="Raw stock bar",
                                    category="Raw material", unit="pcs",
                                    current_stock=140, reorder_level=100, supplier="Starter Supply Co"))
        db.add(models.InventoryItem(item_code=f"{tenant_code}-RM-002", item_name="Fastener kit",
                                    category="Raw material", unit="kits",
                                    current_stock=45, reorder_level=60, supplier="Starter Supply Co"))
        db.add(models.InventoryItem(item_code=f"{tenant_code}-FG-001", item_name=f"{label} finished unit",
                                    category="Finished goods", unit="pcs",
                                    current_stock=32, reorder_level=0, supplier=""))

        for i, (cust, days) in enumerate((("First Customer", 14), ("Second Customer", 21)), start=1):
            db.add(models.CustomerOrder(
                order_no=f"{tenant_code}-CO-{i}", customer_name=cust,
                product_name=f"{label} finished unit", order_quantity=250,
                dispatched_quantity=0, priority="Medium",
                due_date=(now + timedelta(days=days)).date(), status="Pending"))

        for i, state in enumerate(("RAW", "SEMI"), start=1):
            db.add(models.WorkOrder(
                work_order_no=f"{tenant_code}-WO-{i}", part_number=f"{tenant_code}-FG-001",
                batch_number=f"B{i}", machine_id=machines[i - 1].id,
                target_quantity=250, actual_quantity=40 * i, status="In Progress",
                material_state=state))

        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        reset_current_tenant(token)
