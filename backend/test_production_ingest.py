"""Production-record ingest rejects impossible rows at the boundary.

create_production_record stores rows the OEE/cost math later trusts. Negative
minutes/counts are nonsensical and silently corrupt every window that includes
them; the plain-int schema doesn't stop them, so the handler must. runtime >
planned is NOT rejected — a job can run over its plan (ADR-0010 / #228).

Run:  python backend/test_production_ingest.py
"""
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import schemas
import machines_routes
from database import Base

_ADMIN = {"sub": "a", "role": "Admin", "tenant": "DEFAULT"}


def _sess():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=80))
    db.commit()
    return db


def _rec(**kw):
    base = dict(machine_id=1, planned_minutes=480, runtime_minutes=440,
                ideal_cycle_time_seconds=30, total_count=100, good_count=90, rejected_count=10)
    base.update(kw)
    return schemas.ProductionCreate(**base)


def test_production_ingest_rejects_negative_counts():
    db = _sess()
    # -5 + 15 == 10 satisfies the sum check, yet good_count is negative and would
    # drag good-rate / OEE. The negative guard must catch it.
    try:
        machines_routes.create_production_record(
            _rec(total_count=10, good_count=-5, rejected_count=15), db=db, current_user=_ADMIN)
        assert False, "negative good_count should 400"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
    # a valid record still succeeds
    r = machines_routes.create_production_record(_rec(), db=db, current_user=_ADMIN)
    assert r.good_count == 90
    print("PASS production ingest rejects negative counts (the sum check alone would pass -5+15==10)")


def test_production_ingest_still_allows_runtime_over_planned():
    # A job running over its plan is real, ingestible data; the OEE/cost math
    # handles it (availability caps at 100%, cost floors downtime per record).
    db = _sess()
    r = machines_routes.create_production_record(
        _rec(planned_minutes=100, runtime_minutes=120, total_count=100, good_count=100, rejected_count=0),
        db=db, current_user=_ADMIN)
    assert r.runtime_minutes == 120 and r.planned_minutes == 100
    print("PASS runtime>planned is still accepted (over-run is legitimate, not rejected)")


if __name__ == "__main__":
    test_production_ingest_rejects_negative_counts()
    test_production_ingest_still_allows_runtime_over_planned()
    print("PRODUCTION INGEST OK: negatives rejected; runtime>planned still allowed")
