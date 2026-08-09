"""One unassigned work order must not 500 the whole list.

`WorkOrder.machine_id` is `Column(Integer, ForeignKey("machines.id"))` — NULLABLE,
and legitimately so: an order is planned before a machine is assigned to it, which
is the ordinary sequence in every scheduling workflow AMP supports.

`WorkOrderResponse.machine_id` was typed `int`. FastAPI validates the whole
response list, so a SINGLE work order with no machine raised

    ResponseValidationError: {'type': 'int_type', 'loc': ('response', 0,
    'machine_id'), 'msg': 'Input should be a valid integer', 'input': None}

and `GET /work-orders` returned 500 — for every row, not just the unassigned one.
The scheduling board, the order list and every consumer of that endpoint went
blank together.

Found by the OEM adversarial audit, whose fixture created work orders the plain
way (no machine). It is the same NULL-response class already healed on
ProductionPlanResponse.planned_quantity and MachineResponse.utilization (#375);
this model was missed.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_work_order_response_null_safe.py
"""
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import schemas
from database import Base

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SessionLocal = sessionmaker(bind=engine)

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def test_an_unassigned_work_order_serialises():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    machine = models.Machine(tenant_code="DEFAULT", site="", name="CNC-01",
                             status="Running", utilization=70)
    db.add(machine)
    db.flush()
    # The ordinary sequence: plan the order, assign the machine later.
    db.add(models.WorkOrder(tenant_code="DEFAULT", work_order_no="WO-UNASSIGNED",
                            part_number="P-1", batch_number="B1",
                            machine_id=None, target_quantity=10, status="Planned"))
    db.add(models.WorkOrder(tenant_code="DEFAULT", work_order_no="WO-ASSIGNED",
                            part_number="P-2", batch_number="B2",
                            machine_id=machine.id, target_quantity=20,
                            status="In Progress"))
    db.commit()

    rows = db.query(models.WorkOrder).order_by(models.WorkOrder.id).all()
    check("the fixture really does store a NULL machine_id",
          rows[0].machine_id is None, str(rows[0].machine_id))

    try:
        out = [schemas.WorkOrderResponse.model_validate(r, from_attributes=True)
               for r in rows]
        check("the whole list serialises", len(out) == 2, str(len(out)))
        check("...the unassigned order reports machine_id None, not 0",
              out[0].machine_id is None, str(out[0].machine_id))
        check("...and the assigned one keeps its real machine",
              out[1].machine_id == machine.id, str(out[1].machine_id))
    except Exception as e:
        check("the whole list serialises", False, f"{type(e).__name__}: {e}")

    # CONTROL: a genuinely required field is still required, so this fix did not
    # simply make the model permissive.
    #
    # NOT target_quantity — that one is deliberately HEALED to 0 by an existing
    # validator (a NULL count is reported as zero rather than 500ing the list),
    # so asserting it is refused would assert the opposite of the design. The
    # first draft of this control did exactly that and failed, correctly.
    # `work_order_no` has no healer: it is the order's identity, and there is no
    # honest default for it.
    try:
        schemas.WorkOrderResponse.model_validate(
            {"id": 1, "work_order_no": None, "part_number": "P",
             "batch_number": "B", "machine_id": None, "target_quantity": 5,
             "actual_quantity": 0, "status": "Planned"})
        check("CONTROL: a NULL work_order_no is still refused", False, "accepted")
    except Exception:
        check("CONTROL: a NULL work_order_no is still refused", True)
    db.close()


if __name__ == "__main__":
    test_an_unassigned_work_order_serialises()
    print()
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        sys.exit(1)
    print("AN UNASSIGNED WORK ORDER NO LONGER 500s THE LIST")
