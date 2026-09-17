"""The Purchasing list returned a 500 for every tenant whose reorder agent had fired.

THE DEFECT
----------
The reorder agent drafts a purchase order with no supplier:

    po = models.PurchaseOrder(..., supplier_id=None, ...)        # ai/agents.py

which the database accepts — `PurchaseOrder.supplier_id` is a nullable foreign
key, and "no supplier chosen yet" is a true fact about an agent's draft. But the
response model the list route serialises through said otherwise:

    class PurchaseOrderResponse(BaseModel):
        supplier_id: int                                          # schemas.py

so `GET /purchase-orders` (response_model=List[PurchaseOrderResponse]) raised a
validation error on the first agent PO in the list and answered 500 for the
WHOLE list — every purchase order the tenant has, not just the agent's. Under
the default policy the reorder agent auto-approves, so this is not a corner of
the approval flow: any tenant whose stock crossed a reorder level lost its
Purchasing screen. The dashboard loads with allSettled, so it showed an empty
section rather than an error, which is how it went unnoticed.

WHY A CLASS TEST, NOT A ONE-FIELD TEST
--------------------------------------
Agents create rows through a different constructor from the one humans use (the
POST routes validate against the Create schemas, which require a supplier). So
the question is not "is supplier_id optional" but "does every row an AGENT can
create survive the response model of the list that displays it". This drives
every agent creation path that exists — reorder PO, maintenance/quality/yield
task, repeated-downtime escalation, briefing escalation — and validates the
resulting rows exactly as FastAPI does for the list route. A future agent that
writes None into a field a response model requires fails here, whichever field.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_agent_items_serialize.py
"""
import os
import sys
from datetime import datetime, timedelta
from typing import List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite:///./ci.db")

from pydantic import TypeAdapter  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import models  # noqa: E402
import schemas  # noqa: E402
import tenancy  # noqa: E402
from ai import agents  # noqa: E402
from database import Base  # noqa: E402
from events import DowntimeStarted, InventoryLow  # noqa: E402

TENANT = "ACME"
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def _db():
    tenancy.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def as_fastapi_would(response_model, rows):
    """Serialise rows the way FastAPI serialises a route's response_model.

    httpx is not a project dependency, so the app cannot be driven through a
    TestClient here. FastAPI validates the handler's return value against the
    response field in python mode with from_attributes — this is that call.
    Returns (ok, error_text)."""
    try:
        TypeAdapter(response_model).validate_python(rows, from_attributes=True)
        return True, ""
    except Exception as e:  # pydantic.ValidationError, reported verbatim
        return False, str(e).splitlines()[0] + " :: " + " | ".join(
            line.strip() for line in str(e).splitlines()[1:4])


def main():
    db = _db()
    token = tenancy.set_current_tenant(TENANT)
    try:
        db.add(models.Machine(name="PRESS-01", status="Running", utilization=80,
                              line="L1", tenant_code=TENANT))
        db.add(models.InventoryItem(item_code="ACME-BOLT", item_name="M8 bolt",
                                    category="Fasteners", current_stock=2,
                                    reorder_level=10, unit="pcs", tenant_code=TENANT))
        db.commit()
        machine = db.query(models.Machine).first()
        item = db.query(models.InventoryItem).first()

        print("=" * 74)
        print("1. EVERY AGENT CREATION PATH RUNS FOR REAL")
        print("=" * 74)
        # Reorder agent: the path that broke the Purchasing list.
        agents.draft_reorder_on_inventory_low(InventoryLow(
            tenant_code=TENANT, item_id=item.id, item_code=item.item_code,
            item_name=item.item_name, current_stock=2, reorder_level=10), db)

        # The single constructor every task-proposing agent uses
        # (maintenance, quality, yield).
        agents._propose_task(db, TENANT, "maintenance", task_no="AUTO-MAINT-T",
                             machine_id=machine.id, task_type=agents.AUTO_TASK_TYPE,
                             priority="Critical", summary="probe", notes="probe",
                             severity="Critical")

        # Repeated-downtime escalation: needs the threshold of downtime logs.
        now = datetime.utcnow()
        for i in range(agents.ESCALATION_THRESHOLD):
            db.add(models.DowntimeLog(machine_id=machine.id,
                                      reason="Breakdown", duration="30 min",
                                      created_at=now - timedelta(days=i),
                                      tenant_code=TENANT))
        db.commit()
        agents.escalate_on_repeated_downtime(DowntimeStarted(
            tenant_code=TENANT, machine_id=machine.id,
            reason="Breakdown", duration="30 min"), db)
        db.commit()

        pos = db.query(models.PurchaseOrder).all()
        tasks = db.query(models.MaintenanceTask).all()
        escs = db.query(models.Escalation).all()
        check("the reorder agent created a purchase order", len(pos) == 1, repr(pos))
        check("...with no supplier, which the database accepts",
              pos and pos[0].supplier_id is None, repr(pos and pos[0].supplier_id))
        check("the task constructor created a maintenance task", len(tasks) == 1, repr(tasks))
        check("the escalation agent created an escalation", len(escs) >= 1, repr(escs))

        print()
        print("=" * 74)
        print("2. EACH ROW SURVIVES THE RESPONSE MODEL OF THE LIST THAT SHOWS IT")
        print("=" * 74)
        for label, model, rows in (
                ("GET /purchase-orders", List[schemas.PurchaseOrderResponse], pos),
                ("GET /maintenance/tasks", List[schemas.MaintenanceTaskResponse], tasks),
                ("GET /escalations", List[schemas.EscalationResponse], escs)):
            ok, err = as_fastapi_would(model, rows)
            check(f"{label} serialises every agent-created row (no 500)", ok, err)

        # The failure mode is the WHOLE list, not the one row: a human PO alongside
        # the agent's draft must still reach the screen.
        supplier = models.Supplier(supplier_code="SUP-1", supplier_name="Real Supplier",
                                   tenant_code=TENANT)
        db.add(supplier)
        db.commit()
        db.add(models.PurchaseOrder(po_no="PO-HUMAN-1", supplier_id=supplier.id,
                                    item_id=item.id, item_name=item.item_name,
                                    order_quantity=5, unit="pcs",
                                    expected_delivery_date=now.date(), status="Open",
                                    tenant_code=TENANT))
        db.commit()
        ok, err = as_fastapi_would(List[schemas.PurchaseOrderResponse],
                                   db.query(models.PurchaseOrder).all())
        check("a human PO next to the agent's draft still reaches the list", ok, err)

        print()
        print("=" * 74)
        print("3. THE BRIEFING ESCALATION PATH, WHEN IT CAN FIRE")
        print("=" * 74)
        # escalate_from_briefing composes the whole briefing; on this minimal plant
        # it may legitimately find nothing to escalate. Assert on what it DID
        # create rather than assuming it creates something.
        before = db.query(models.Escalation).count()
        try:
            agents.escalate_from_briefing(db, TENANT)
            db.commit()
        except Exception as e:  # a crash here is itself a finding
            check("escalate_from_briefing runs without raising", False, repr(e))
        created = db.query(models.Escalation).all()
        ok, err = as_fastapi_would(List[schemas.EscalationResponse], created)
        check(f"escalations from every path serialise ({len(created) - before} from briefing)",
              ok, err)
    finally:
        tenancy.reset_current_tenant(token)

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILURE(S)")
        for f in failures:
            print("  -", f)
        return 1
    print("AGENT ITEMS SERIALISE OK: every row an agent creates reaches the list that shows it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
