"""The three-factory acceptance environment, built deterministically (ADR-0022).

    FACTORY_A  healthy     every machine reporting, plans met, stock fine, a unit
                           value set, so losses are priced
    FACTORY_B  problems    a machine in breakdown, a recurring hydraulic fault,
                           plans behind and missed, stock out, a late order,
                           overdue maintenance, and NO unit value, so money must
                           not appear
    FACTORY_C  partial     one machine never reports (coverage 2 of 3), no plan,
                           no targets, no inspections, no stock records
    OEM:ACME   an OEM with one installation at FACTORY_B

THE IDENTIFIERS COLLIDE ON PURPOSE. Every factory has a CNC-01 and a LINE-01 and
a WO-001 (the lesson of audit_three_customers.py: distinct names cannot catch the
defect that keys on a name instead of on tenant and name). What differs is the
data behind them, and each factory has MARKERS, strings no other factory holds, so
a leak shows up as text: `MARKERS[tenant]`.

Every figure the evaluation expects is computed here, from the numbers this file
seeds, by the textbook formula, and never by the code under test (`oracle`).
"""
from datetime import datetime, timedelta

import models
import tenancy

A, B, C = "FACTORY_A", "FACTORY_B", "FACTORY_C"
TENANTS = (A, B, C)
OEM_CODE = "ACME"

MARKERS = {
    A: ["Alpha fixture swap", "Alpha bearing", "Aurora Foods", "Alpha SOP-01", "PRESS-01",
        "ORD-A-1", "ORD-A-2", "MT-A-1", "PLAN-A"],
    B: ["Hydraulic seal leak", "Material shortage", "Porosity", "Steel coil", "Weld wire",
        "Borealis Motors", "WELD-07", "PLAN-B", "Bravo weld procedure", "Bravo gasket",
        "ORD-B-9", "ORD-B-10", "MT-B-1", "MT-B-2", "WO-003"],
    C: ["Oven purge", "OVEN-03", "Cygnus Labs", "ORD-C-1"],
    "OEM": ["ACME-SN-7731", "Acme Industrial", "AC-500"],
}

# (machine, status, line) per factory.
MACHINES = {
    A: [("CNC-01", "Running", "L1"), ("LINE-01", "Running", "L1"), ("PRESS-01", "Running", "L2")],
    B: [("CNC-01", "Breakdown", "L1"), ("LINE-01", "Running", "L1"), ("WELD-07", "Maintenance", "L2")],
    C: [("CNC-01", "Running", "L1"), ("LINE-01", "Running", "L1"), ("OVEN-03", "Running", "L2")],
}

# production: machine -> [(days_ago, planned, runtime, ideal_s, total, good)]
PRODUCTION = {
    A: {m: [(d, 480, 456, 30, 900, 891) for d in range(1, 6)] for m in ("CNC-01", "LINE-01", "PRESS-01")},
    B: {"CNC-01": [(d, 480, 240, 60, 200, 170) for d in range(1, 6)],
        "LINE-01": [(d, 480, 400, 60, 360, 342) for d in range(1, 6)],
        "WELD-07": [(5, 480, 300, 60, 250, 240)]},
    # OVEN-03 reports nothing: the gateway-down story. Coverage must say 2 of 3.
    C: {m: [(d, 480, 420, 45, 500, 480) for d in range(1, 4)] for m in ("CNC-01", "LINE-01")},
}

# downtime: (machine, reason, minutes, days_ago)
DOWNTIME = {
    A: [("CNC-01", "Alpha fixture swap", 12, 2)],
    B: [("CNC-01", "Hydraulic seal leak", 45, d) for d in range(1, 6)]
       + [("LINE-01", "Material shortage", 30, d) for d in (2, 4)],
    C: [("OVEN-03", "Oven purge", 20, 1)],
}

# plans: (plan_no, machine, days_ago, planned, actual, status)
PLANS = {
    A: [(f"PLAN-A{i}", "CNC-01" if i % 2 else "LINE-01", i, 800, 816, "Completed") for i in range(1, 5)],
    B: [("PLAN-B1", "CNC-01", 1, 1000, 400, "In Progress"), ("PLAN-B2", "CNC-01", 2, 500, 0, "Planned"),
        ("PLAN-B3", "LINE-01", 3, 600, 600, "Completed"), ("PLAN-B4", "LINE-01", 4, 400, 300, "In Progress")],
    C: [],
}

# shifts: (name, target, actual) x days 1..n
SHIFTS = {A: ("Day", 1000, 1010, 5), B: ("Night", 1000, 610, 5), C: ("Morning", 0, 700, 3)}

# inspections: (machine, inspected, passed, failed, defect)
INSPECTIONS = {
    A: [("CNC-01", 100, 100, 0, None)] * 3,
    B: [("CNC-01", 200, 170, 30, "Porosity"), ("LINE-01", 300, 290, 10, "Porosity")],
    C: [],
}

# stock: (code, name, stock, reorder, unit)
STOCK = {
    A: [("INV-001", "Alpha bearing", 500, 100, "pcs"), ("INV-002", "Alpha coolant", 200, 50, "L"),
        ("INV-003", "Alpha steel", 900, 300, "kg")],
    B: [("INV-001", "Steel coil", 0, 50, "kg"), ("INV-002", "Weld wire", 10, 40, "kg"),
        ("INV-003", "Bravo gasket", 300, 50, "pcs")],
    C: [],
}

# orders: (order_no, customer, qty, dispatched, due_in_days)
ORDERS = {
    A: [("ORD-A-1", "Aurora Foods", 100, 100, 10), ("ORD-A-2", "Aurora Foods", 200, 0, 20)],
    B: [("ORD-B-9", "Borealis Motors", 500, 100, -3), ("ORD-B-10", "Borealis Motors", 100, 0, 2)],
    C: [("ORD-C-1", "Cygnus Labs", 50, 0, 30)],
}

# maintenance: (task_no, machine, type, planned_in_days, status)
MAINTENANCE = {
    A: [("MT-A-1", "PRESS-01", "Preventive", 5, "Open")],
    B: [("MT-B-1", "CNC-01", "Hydraulic overhaul", -4, "Open"), ("MT-B-2", "WELD-07", "Weld tip change", -1, "Open")],
    C: [],
}

# documents: (doc_no, title, review_in_days, approval)
DOCUMENTS = {
    A: [("DOC-001", "Alpha SOP-01", 60, "Approved")],
    B: [("DOC-001", "Bravo weld procedure", -10, "Approved")],
    C: [],
}

# work orders: (wo_no, status, material_state)
WORK_ORDERS = {
    A: [("WO-001", "In Progress", "RAW"), ("WO-002", "Completed", "FIN")],
    B: [("WO-001", "In Progress", "RAW"), ("WO-003", "In Progress", "SEMI")],
    C: [],
}

UNIT_VALUE = {A: 12.0, B: None, C: 7.5}


def seed(Session, now=None):
    """Write the three factories and the OEM into an empty schema. Returns `now`."""
    now = now or datetime.utcnow()
    today = now.date()
    db = Session()
    tok = tenancy.set_current_tenant(None)
    try:
        for t in TENANTS:
            db.add(models.TenantConfig(tenant_code=t, unit_value_gbp=UNIT_VALUE[t]))
        db.flush()
        ids = {}
        for t in TENANTS:
            for name, status, line in MACHINES[t]:
                m = models.Machine(tenant_code=t, site="P1", name=name, status=status, line=line,
                                   utilization=0, downtime="0 min")
                db.add(m)
                db.flush()
                ids[(t, name)] = m.id
        for t in TENANTS:
            at = lambda days, hour=10: datetime.combine(today - timedelta(days=days), datetime.min.time()) \
                + timedelta(hours=hour)   # noqa: E731
            for mname, rows in PRODUCTION[t].items():
                for d, planned, runtime, ideal, total, good in rows:
                    db.add(models.ProductionRecord(
                        tenant_code=t, machine_id=ids[(t, mname)], planned_minutes=planned,
                        runtime_minutes=runtime, ideal_cycle_time_seconds=ideal, total_count=total,
                        good_count=good, rejected_count=total - good, created_at=at(d)))
            for mname, reason, minutes, d in DOWNTIME[t]:
                db.add(models.DowntimeLog(tenant_code=t, machine_id=ids[(t, mname)], reason=reason,
                                          duration=f"{minutes} min", created_at=at(d, 9)))
            for plan_no, mname, d, planned, actual, status in PLANS[t]:
                db.add(models.ProductionPlan(tenant_code=t, plan_no=plan_no, machine_id=ids[(t, mname)],
                                             planned_quantity=planned, actual_quantity=actual,
                                             plan_date=today - timedelta(days=d), shift_name="Day",
                                             status=status, created_at=at(d, 6)))
            name, target, actual, days = SHIFTS[t]
            for d in range(1, days + 1):
                db.add(models.ShiftData(tenant_code=t, shift_name=name, target_output=target,
                                        actual_output=actual, created_at=at(d, 18)))
            for i, (mname, inspected, passed, failed, defect) in enumerate(INSPECTIONS[t], 1):
                db.add(models.QualityInspection(
                    tenant_code=t, inspection_no=f"QI-{i:03d}", machine_id=ids[(t, mname)], inspector="QA",
                    inspected_quantity=inspected, passed_quantity=passed, failed_quantity=failed,
                    defect_category=defect, status="Failed" if failed else "Passed", created_at=at(i, 14)))
            for code, iname, stock, reorder, unit in STOCK[t]:
                db.add(models.InventoryItem(tenant_code=t, item_code=code, item_name=iname, category="Raw",
                                            current_stock=stock,
                                            reorder_level=reorder, unit=unit, supplier="Local"))
            for order_no, customer, qty, dispatched, due_in in ORDERS[t]:
                db.add(models.CustomerOrder(tenant_code=t, order_no=order_no, customer_name=customer,
                                            product_name="FG-001", order_quantity=qty, dispatched_quantity=dispatched,
                                            due_date=today + timedelta(days=due_in), status="Pending", created_at=at(6)))
            for task_no, mname, kind, due_in, status in MAINTENANCE[t]:
                db.add(models.MaintenanceTask(tenant_code=t, task_no=task_no, machine_id=ids[(t, mname)],
                                              task_type=kind, priority="High", assigned_to="Maintenance",
                                              planned_date=today + timedelta(days=due_in), status=status))
            for doc_no, title, review_in, approval in DOCUMENTS[t]:
                db.add(models.ComplianceDocument(tenant_code=t, document_no=doc_no, title=title,
                                                 document_type="SOP", department="Quality", owner="QA",
                                                 approval_status=approval,
                                                 review_due_date=today + timedelta(days=review_in)))
            for wo_no, status, state in WORK_ORDERS[t]:
                db.add(models.WorkOrder(tenant_code=t, work_order_no=wo_no, part_number="FG-001", batch_number="B1",
                                        machine_id=ids[(t, "CNC-01")], target_quantity=100, actual_quantity=0,
                                        status=status, material_state=state))
        db.add(models.OemOrganization(oem_code=OEM_CODE, name="Acme Industrial", is_active=True))
        db.flush()
        model = models.MachineModel(oem_code=OEM_CODE, family="CNC", model_code="AC-500", name="Acme AC-500",
                                    status="active")
        db.add(model)
        db.flush()
        db.add(models.MachineInstallation(oem_code=OEM_CODE, serial_number="ACME-SN-7731", model_id=model.id,
                                          factory_tenant_code=B, site="P1", machine_id=ids[(B, "CNC-01")],
                                          status="commissioned"))
        db.commit()
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()
    return now


# ── The oracle: expected figures from the seed spec, by the textbook formula ──

def _pooled_oee(rows):
    """OEE contract formula over summed minutes and counts (docs/engineering/OEE-CONTRACT.md)."""
    planned = sum(r[1] for r in rows)
    runtime = sum(r[2] for r in rows)
    ideal_work = sum(r[3] * r[4] for r in rows)
    total = sum(r[4] for r in rows)
    good = sum(r[5] for r in rows)
    a = runtime / planned
    p = min(1.0, ideal_work / (runtime * 60))
    q = good / total
    return a * p * q * 100, a * 100, p * 100, q * 100


def oracle(tenant):
    """{fact key: expected value} for every tool, for one factory."""
    rows = [r for rs in PRODUCTION[tenant].values() for r in rs]
    machines = MACHINES[tenant]
    out = {
        "machines.total": len(machines),
        "machines.running": sum(1 for _, s, _ in machines if s == "Running"),
        "machines.down": sum(1 for _, s, _ in machines if s == "Breakdown"),
        "machines.maintenance": sum(1 for _, s, _ in machines if s == "Maintenance"),
        "oee.machines_expected": len(machines),
        "oee.machines_reporting": len(PRODUCTION[tenant]),
        "downtime.events": len(DOWNTIME[tenant]),
        "downtime.minutes": sum(x[2] for x in DOWNTIME[tenant]),
        "production.runs": len(rows),
    }
    if rows:
        oee, a, p, q = _pooled_oee(rows)
        out.update({"oee.plant": oee, "oee.availability": a, "oee.performance": p, "oee.quality": q,
                    "production.total": sum(r[4] for r in rows), "production.good": sum(r[5] for r in rows)})
    reasons = {}
    for _m, reason, minutes, _d in DOWNTIME[tenant]:
        reasons[reason] = reasons.get(reason, 0) + minutes
    if reasons:
        out["downtime.top_reason"] = max(reasons, key=lambda r: (reasons[r], r))
    due = [p for p in PLANS[tenant]]
    if due:
        planned = sum(p[3] for p in due)
        actual = sum(p[4] for p in due)
        out.update({"plan.planned_units": planned, "plan.actual_units": actual,
                    "plan.attainment": actual / planned * 100,
                    "plan.behind": sum(1 for p in due if 0 < p[4] < p[3] and p[5] != "Completed"),
                    "plan.missed": sum(1 for p in due if p[4] == 0 and p[5] != "Completed")})
    name, target, actual, days = SHIFTS[tenant]
    if target:
        out["shift.attainment"] = actual / target * 100
    # A count of zero is a measured fact ("0 inspections recorded"); a RATE over
    # zero is not, so the rates are expected only where there is a denominator.
    insp = INSPECTIONS[tenant]
    out["quality.inspections"] = len(insp)
    if insp:
        inspected = sum(i[1] for i in insp)
        out.update({"quality.fail_rate": sum(i[3] for i in insp) / inspected * 100,
                    "quality.fpy": sum(i[2] for i in insp) / inspected * 100})
    stock = STOCK[tenant]
    out["stock.items"] = len(stock)
    if stock:
        out.update({"stock.out": sum(1 for s in stock if s[2] == 0),
                    "stock.at_risk": sum(1 for s in stock if s[2] <= s[3])})
    orders = ORDERS[tenant]
    out["orders.total"] = len(orders)
    out["orders.late"] = sum(1 for o in orders if o[3] < o[2] and o[4] < 0)
    out["maint.open"] = len(MAINTENANCE[tenant])
    out["maint.overdue"] = sum(1 for m in MAINTENANCE[tenant] if m[3] < 0)
    out["priced"] = UNIT_VALUE[tenant] is not None
    return out
