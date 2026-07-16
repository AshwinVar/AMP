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
    db.commit()

    reset_factory.rebuild_factory(db)   # with FK enforcement on, a missed table would raise here

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

    # ── modules light up ──────────────────────────────────────────────
    assert db.query(models.ProductionRecord).filter(models.ProductionRecord.tenant_code == "DEFAULT").count() > 0
    assert db.query(models.DowntimeLog).filter(models.DowntimeLog.tenant_code == "DEFAULT").count() > 0
    assert db.query(models.QualityInspection).filter(models.QualityInspection.tenant_code == "DEFAULT").count() == 10
    assert db.query(models.MaintenanceTask).filter(models.MaintenanceTask.tenant_code == "DEFAULT").count() == 2

    # ── idempotent: running again yields the same shape, no duplicates ─
    reset_factory.rebuild_factory(db)
    assert db.query(models.Machine).filter(models.Machine.tenant_code == "DEFAULT").count() == 8
    assert db.query(models.WorkOrder).filter(models.WorkOrder.tenant_code == "DEFAULT").count() == 10


if __name__ == "__main__":
    test_rebuild_creates_two_line_smt_ic_factory()
    print("RESET OK: DEFAULT -> SMT+IC (8 machines, 2 lines); 10 WOs (5 Bugatti/5 Mercedes) across "
          "RAW/SEMI/FIN; digital-twin zones; production/downtime/quality/maintenance seeded; "
          "DEFAULT-scoped + idempotent")
