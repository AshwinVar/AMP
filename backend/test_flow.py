"""WIP flow read-model tests (ADR-0007).

Work orders grouped by material state (RAW -> SEMI -> FIN) with counts and
quantities for the two-line pipeline; WIP = not-yet-finished, finished = FIN.

Run:  python backend/test_flow.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import flow


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _wo(no, state, target, actual):
    return models.WorkOrder(work_order_no=no, part_number="P", batch_number="B",
                            machine_id=None, target_quantity=target, actual_quantity=actual,
                            material_state=state)


def test_flow_groups_work_orders_by_state():
    db = _fresh_session()
    db.add_all([
        _wo("WO-1", "RAW", 100, 0),
        _wo("WO-2", "RAW", 200, 0),
        _wo("WO-3", "SEMI", 150, 90),
        _wo("WO-4", "FIN", 120, 120),
        _wo("WO-5", None, 50, 0),      # no state -> treated as RAW
    ])
    db.commit()

    s = flow.build_flow_summary(db, "DEFAULT")
    assert s["total"] == 5
    by = {st["key"]: st for st in s["stages"]}
    assert [st["key"] for st in s["stages"]] == ["RAW", "SEMI", "FIN"]   # flow order
    # RAW: WO-1 + WO-2 + WO-5(None) = 3, target 100+200+50 = 350
    assert by["RAW"]["count"] == 3 and by["RAW"]["target"] == 350
    assert by["SEMI"]["count"] == 1 and by["SEMI"]["actual"] == 90
    assert by["FIN"]["count"] == 1 and by["FIN"]["target"] == 120
    # each stage carries its processing line
    assert by["RAW"]["line"] == "SMT" and by["SEMI"]["line"] == "IC"
    assert s["wip"] == 4          # RAW(3) + SEMI(1), not finished
    assert s["finished"] == 1     # FIN

    # empty -> zeros, all three stages present
    empty = flow.build_flow_summary(_fresh_session(), "DEFAULT")
    assert empty["total"] == 0 and empty["wip"] == 0 and len(empty["stages"]) == 3


if __name__ == "__main__":
    test_flow_groups_work_orders_by_state()
    print("FLOW OK: work orders grouped RAW/SEMI/FIN with counts + quantities; WIP vs finished; stage->line")
