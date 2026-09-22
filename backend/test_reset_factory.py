"""Reset-factory tests — the DEFAULT tenant becomes the SMT -> IC two-line plant.

Verifies the wipe is DEFAULT-scoped (other tenants untouched) and that the
rebuild produces exactly the two lines, the 10 work orders (5 per company)
across all three material states, the digital-twin zones, and live module data.

Run:  python backend/test_reset_factory.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import models
from database import Base
import reset_factory


def _fresh_session():
    """SQLite with foreign-key enforcement ON, so the reset's delete order is
    validated the same way Postgres (prod) would enforce it."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_con, _record):
        dbapi_con.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_rebuild_creates_two_line_smt_ic_factory():
    db = _fresh_session()
    # pre-existing DEFAULT data — including every table that FK-references a
    # machine — to prove the wipe is complete and FK-safe. GMATS is left alone.
    old = models.Machine(tenant_code="DEFAULT", name="CNC-OLD", status="Running", utilization=50)
    db.add(old)
    db.add(models.Machine(tenant_code="GMATS", name="GMATS-KEEP", status="Running", utilization=50))
    db.flush()
    db.add(models.WorkOrder(tenant_code="DEFAULT", work_order_no="WO-OLD", part_number="X",
                            batch_number="B", machine_id=old.id, target_quantity=10))
    db.add(models.OperatorJobExecution(tenant_code="DEFAULT", execution_no="OJ-OLD",
                                       operator_name="op", machine_id=old.id, job_status="Started"))
    dev = models.IndustrialDevice(tenant_code="DEFAULT", device_code="PLC-OLD",
                                  device_name="Old PLC", linked_machine_id=old.id)
    db.add(dev)
    db.flush()
    db.add(models.IndustrialSignal(tenant_code="DEFAULT", device_id=dev.id, machine_id=old.id,
                                   signal_name="temp", signal_value="42"))
    # a legacy AI recommendation whose tenant_code was never backfilled but still
    # references a DEFAULT machine — this is exactly what broke the reset on prod.
    db.add(models.AIRecommendation(tenant_code="LEGACY", recommendation_type="risk",
                                   title="t", message="m", related_machine_id=old.id))
    db.commit()

    reset_factory.rebuild_factory(db)   # with FK enforcement on, a missed reference would raise here

    # ── machines: 8, split 4 SMT / 4 IC; the old one is gone ──────────
    machines = db.query(models.Machine).filter(models.Machine.tenant_code == "DEFAULT").all()
    names = {m.name for m in machines}
    assert "CNC-OLD" not in names
    assert len(machines) == 8
    assert sum(1 for m in machines if m.line == "SMT") == 4
    assert sum(1 for m in machines if m.line == "IC") == 4
    assert "SMT-Printer-01" in names and "IC-FinalQC-01" in names
    # GMATS is left alone
    assert db.query(models.Machine).filter(models.Machine.tenant_code == "GMATS").count() == 1

    # ── work orders: 10, all old ones gone, all three states present ──
    wos = db.query(models.WorkOrder).filter(models.WorkOrder.tenant_code == "DEFAULT").all()
    assert len(wos) == 10 and "WO-OLD" not in {w.work_order_no for w in wos}
    assert {w.material_state for w in wos} == {"RAW", "SEMI", "FIN"}
    # RAW parts sit on an SMT machine, FIN parts at final QC (state -> line)
    by_id = {m.id: m for m in machines}
    for w in wos:
        if w.material_state == "RAW":
            assert by_id[w.machine_id].line == "SMT"
        else:
            assert by_id[w.machine_id].line == "IC"

    # ── customers: Bugatti + Mercedes, 5 orders each, linked to WOs ───
    cos = db.query(models.CustomerOrder).filter(models.CustomerOrder.tenant_code == "DEFAULT").all()
    assert len(cos) == 10
    assert sum(1 for c in cos if c.customer_name == "Bugatti") == 5
    assert sum(1 for c in cos if c.customer_name == "Mercedes") == 5
    assert all(c.linked_work_order_id is not None for c in cos)

    # ── digital twin: two zones ───────────────────────────────────────
    nodes = db.query(models.FactoryLayoutNode).filter(models.FactoryLayoutNode.tenant_code == "DEFAULT").all()
    assert len(nodes) == 8 and {n.zone for n in nodes} == {"SMT Line", "IC Line"}

    # ── the FK-referencing tables were handled cleanly ────────────────
    # operator jobs are factory data -> wiped; the PLC device/signal stay (the
    # connectivity layer) but are detached from the deleted machines.
    assert db.query(models.OperatorJobExecution).filter(models.OperatorJobExecution.tenant_code == "DEFAULT").count() == 0
    dev2 = db.query(models.IndustrialDevice).filter(models.IndustrialDevice.device_code == "PLC-OLD").first()
    assert dev2 is not None and dev2.linked_machine_id is None
    sig = db.query(models.IndustrialSignal).filter(models.IndustrialSignal.device_id == dev2.id).first()
    assert sig is not None and sig.machine_id is None
    # the mismatched-tenant recommendation was cleared by reference, not by tenant_code
    assert db.query(models.AIRecommendation).filter(models.AIRecommendation.tenant_code == "LEGACY").count() == 0

    # ── modules light up ──────────────────────────────────────────────
    assert db.query(models.ProductionRecord).filter(models.ProductionRecord.tenant_code == "DEFAULT").count() > 0
    assert db.query(models.DowntimeLog).filter(models.DowntimeLog.tenant_code == "DEFAULT").count() > 0
    assert db.query(models.QualityInspection).filter(models.QualityInspection.tenant_code == "DEFAULT").count() == 10
    assert db.query(models.MaintenanceTask).filter(models.MaintenanceTask.tenant_code == "DEFAULT").count() == 2

    # ── idempotent: running again yields the same shape, no duplicates ─
    reset_factory.rebuild_factory(db)
    assert db.query(models.Machine).filter(models.Machine.tenant_code == "DEFAULT").count() == 8
    assert db.query(models.WorkOrder).filter(models.WorkOrder.tenant_code == "DEFAULT").count() == 10


def _fingerprint(db):
    """Every figure a demo would be rehearsed on. If two rebuilds differ here,
    the numbers you practised are not the numbers in the meeting."""
    rows = []
    for r in db.query(models.ProductionRecord).order_by(models.ProductionRecord.id).all():
        rows.append((r.machine_id, r.runtime_minutes, r.total_count, r.good_count, r.rejected_count))
    for d in db.query(models.DowntimeLog).order_by(models.DowntimeLog.id).all():
        rows.append((d.machine_id, d.reason, d.duration))
    for q in db.query(models.QualityInspection).order_by(models.QualityInspection.id).all():
        rows.append((q.inspection_no, q.inspected_quantity, q.failed_quantity, q.defect_category))
    for w in db.query(models.WorkOrder).order_by(models.WorkOrder.id).all():
        rows.append((w.work_order_no, w.target_quantity, w.actual_quantity, w.status))
    for c in db.query(models.CustomerOrder).order_by(models.CustomerOrder.id).all():
        rows.append((c.order_no, c.order_quantity, c.due_date, c.priority))
    return rows


def test_the_demo_is_reproducible():
    """The whole point of a demo factory: two resets give the same plant.

    This file imported `random` and never seeded it, so production, downtime,
    reject rates, order dates and work-order progress were redrawn on every
    reset. "The biggest measured loss" was a different sentence each time, and
    no runbook could quote a figure."""
    a, b = _fresh_session(), _fresh_session()
    reset_factory.rebuild_factory(a)
    reset_factory.rebuild_factory(b)
    fa, fb = _fingerprint(a), _fingerprint(b)
    assert fa, "the fingerprint read nothing"
    assert fa == fb, next((f"{x} != {y}" for x, y in zip(fa, fb) if x != y), "lengths differ")

    # A different seed gives a different (but equally repeatable) plant, so the
    # determinism is the seed's doing and not an accident of fixed literals.
    c, d = _fresh_session(), _fresh_session()
    reset_factory.rebuild_factory(c, seed=999)
    reset_factory.rebuild_factory(d, seed=999)
    assert _fingerprint(c) == _fingerprint(d)
    assert _fingerprint(c) != fa


def test_every_planted_problem_is_discoverable():
    """AMP must find each planted problem through its OWN rules.

    Nothing here tells the intelligence layer what to look for: the seed writes
    ordinary rows and the read-models are asked what they see. Before this the
    demo could plant exactly ONE of the seven problem kinds the Command Centre
    can discover (a machine down); four were structurally impossible because
    the seed created no inventory, no production plans, no past-due orders and
    no overdue maintenance."""
    import tenancy
    from ai.command_centre import build_command_centre
    from ai.delivery import build_delivery_summary
    from ai.inventory import build_inventory_summary
    from ai.maintenance import build_maintenance_summary

    db = _fresh_session()
    tenancy.install_scoping()
    tok = tenancy.set_current_tenant(None)
    reset_factory.rebuild_factory(db)
    tenancy.reset_current_tenant(tok)

    tok = tenancy.set_current_tenant("DEFAULT")
    try:
        # 1-2. a machine down now, and one downtime reason that dominates
        cc = build_command_centre(db, "DEFAULT")
        titles = " | ".join(p["title"] for p in cc["problems"])
        assert "machine down right now" in titles, titles
        assert reset_factory.PROBLEM_DOWNTIME_REASON in titles, titles

        # 3. the plan shortfall, sized and ranked. The EXACT shortfall, not just
        # "something is behind": the seed plants two short plans and flattening
        # either one must fail here.
        plan_rows = [p for p in cc["problems"] if "is behind" in p["title"]]
        assert plan_rows, titles
        assert plan_rows[0]["impact_units"] == 330, plan_rows[0]
        behind = db.query(models.ProductionPlan).filter(
            models.ProductionPlan.tenant_code == "DEFAULT",
            models.ProductionPlan.status == "Behind").all()
        assert sorted(p.planned_quantity - p.actual_quantity for p in behind) == [160, 330], \
            [(p.plan_no, p.planned_quantity, p.actual_quantity) for p in behind]

        # 4. the quality spike, on the machine AND the defect it was planted on
        quality = [p for p in cc["problems"] if "failed inspection" in p["title"]]
        assert quality, titles
        assert reset_factory.PROBLEM_QUALITY_DEFECT in quality[0]["title"], quality[0]["title"]
        assert reset_factory.PROBLEM_QUALITY_MACHINE in str(quality[0]["detail"]), quality[0]["detail"]

        # 5. the material shortage
        assert build_inventory_summary(db, "DEFAULT")["at_risk"] == 3

        # 6. the late order
        assert build_delivery_summary(db, "DEFAULT")["late"] == 1

        # 7. the overdue maintenance
        assert build_maintenance_summary(db, "DEFAULT")["overdue"] == 1

        # ...and money, which no seeder used to configure, so every loss read as
        # a unit count and the demo's most persuasive surface was off by default.
        assert cc["cost"]["priced"] is True
        assert any(p["impact_money"] for p in cc["problems"]), "nothing was priced"

        # THE RUNBOOK'S OWN FIGURES. docs/sales/FACTORY-DEMO-RUNBOOK.md quotes
        # these so a demo can be rehearsed, which only works while they are
        # stable. Changing DEMO_SEED (or any planted quantity) changes them and
        # must fail here rather than silently leaving the runbook wrong — that
        # document is read aloud to a prospect.
        biggest = cc["problems"][1]
        assert reset_factory.PROBLEM_DOWNTIME_REASON in biggest["title"], biggest["title"]
        assert (biggest["impact_units"], biggest["impact_money"]) == (778, 9725), biggest
    finally:
        tenancy.reset_current_tenant(tok)


def test_the_new_seeders_touch_only_default():
    """Stock, plans, shifts and the unit value are DEFAULT's alone.

    `_seed_inventory` upserts rather than wiping (inventory hangs off movements,
    POs and slips — `reseed_inventory.py`'s territory), so it is the one seeder
    that reads existing rows before writing. It must still never read or write
    another tenant's."""
    db = _fresh_session()
    db.add(models.InventoryItem(tenant_code="OTHER", item_code="RM-PASTE-01",
                                item_name="Someone else's paste", category="Raw Material",
                                unit="kg", current_stock=999, reorder_level=1))
    db.add(models.ShiftData(tenant_code="OTHER", shift_name="Day", target_output=1, actual_output=1))
    db.add(models.TenantConfig(tenant_code="OTHER", unit_value_gbp=99.0))
    db.commit()

    reset_factory.rebuild_factory(db)

    other = db.query(models.InventoryItem).filter(models.InventoryItem.tenant_code == "OTHER").one()
    assert other.current_stock == 999 and other.item_name == "Someone else's paste"
    assert db.query(models.ShiftData).filter(models.ShiftData.tenant_code == "OTHER").count() == 1
    assert db.query(models.TenantConfig).filter(
        models.TenantConfig.tenant_code == "OTHER").one().unit_value_gbp == 99.0
    assert db.query(models.TenantConfig).filter(
        models.TenantConfig.tenant_code == "DEFAULT").one().unit_value_gbp == reset_factory.DEMO_UNIT_VALUE_GBP


if __name__ == "__main__":
    test_rebuild_creates_two_line_smt_ic_factory()
    test_the_demo_is_reproducible()
    test_every_planted_problem_is_discoverable()
    test_the_new_seeders_touch_only_default()
    print("RESET OK: DEFAULT -> SMT+IC (8 machines, 2 lines); 10 WOs (5 Bugatti/5 Mercedes) across "
          "RAW/SEMI/FIN; digital-twin zones; production/downtime/quality/maintenance seeded; "
          "DEFAULT-scoped + idempotent; REPRODUCIBLE; all seven planted problems discoverable")
