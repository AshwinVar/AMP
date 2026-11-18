"""Shift ingest rejects impossible rows at the boundary.

Shift data (shift_data) feeds every shift metric: build_shift_kpis' per-shift
efficiency is actual_output / target_output, and the pooled shift attainment on
/analytics/summary, /analytics/executive-oee and build_management_summary sums
these same two columns. target_output / actual_output are plain ints in the
schema (no ge=0), so nothing but the handler stops a negative from being stored.
This was the ONE counted ingest still missing the non-negative guard its siblings
already have (production-record #266, quality #324, order/PO #346,
production-plan #351, work-order #354, operator #374).

A negative actual or target would make a hand-rolled efficiency read below zero
or invert its sign — a rate the data cannot support. The ingest guard rejects such
a row at the door (the primary defence, pinned below). shift_attainment is ALSO
floored (target <= 0 -> None, a negative actual floored to 0), so a legacy /
raw-SQL / migration row that predates the guard still reads an honest figure rather
than a negative one — the defence-in-depth pinned below. A target of 0 is a
legitimate UNPLANNED shift and must still be accepted.

Run:  python backend/test_shift_ingest_nonnegative.py     (exit 0 = pass)
"""
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import schemas
import machines_routes
from analytics_engine import build_shift_kpis
from database import Base

_ADMIN = {"sub": "a", "role": "Admin", "tenant": "DEFAULT"}


def _sess():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _shift(**kw):
    base = dict(shift_name="A", target_output=200, actual_output=150)
    base.update(kw)
    return schemas.ShiftCreate(**base)


def test_display_layer_floors_a_corrupt_negative_row():
    # Defence-in-depth: the ingest guard below rejects a negative row on create, but
    # a legacy / raw-SQL / migration row that predates it must still read honestly.
    # build_shift_kpis routes every per-shift figure through analytics_engine
    # .shift_attainment, which is floored — so a corrupt negative no longer prints a
    # sub-zero efficiency (test_shift_attainment_floor.py pins the helper itself).
    #
    # A negative ACTUAL: target=200, actual=-50. A hand-rolled round((-50/200)*100)
    # would be -25%; the floor reads it as 0% — a shift cannot produce negative units.
    kpi = build_shift_kpis([models.ShiftData(shift_name="A", target_output=200, actual_output=-50)])[0]
    assert kpi["efficiency"] == 0, kpi
    # A negative TARGET: target=-100, actual=50. A hand-rolled round((50/-100)*100)
    # would be -50%; a planned output below zero is no measurable target -> None.
    kpi = build_shift_kpis([models.ShiftData(shift_name="B", target_output=-100, actual_output=50)])[0]
    assert kpi["efficiency"] is None, kpi
    # A clean row of the same shape reads an honest, bounded rate (strict no-op).
    kpi = build_shift_kpis([models.ShiftData(shift_name="C", target_output=200, actual_output=150)])[0]
    assert kpi["efficiency"] == 75, kpi
    print("PASS a corrupt negative row reads 0%/None, never a sub-zero efficiency")


def test_create_rejects_negative_outputs():
    db = _sess()
    for bad in (dict(target_output=-1, actual_output=150),
                dict(target_output=200, actual_output=-1)):
        try:
            machines_routes.create_shift(_shift(**bad), db=db, current_user=_ADMIN)
            assert False, f"negative output should 400: {bad}"
        except HTTPException as e:
            assert e.status_code == 400, e.status_code
    # nothing impossible was persisted
    assert db.query(models.ShiftData).count() == 0, "rejected rows must not persist"
    # a valid row still succeeds and stores the exact outputs
    r = machines_routes.create_shift(_shift(), db=db, current_user=_ADMIN)
    assert r.target_output == 200 and r.actual_output == 150, (r.target_output, r.actual_output)
    print("PASS create rejects negative target/actual; a valid row still stores exactly")


def test_create_accepts_zero_target_unplanned_shift():
    # A shift with target 0 is a legitimate UNPLANNED shift, not an impossible row —
    # the pooled efficiency treats it as such (adds real output, no fabricated 0%).
    # Only negatives are rejected, so this must go through, and its per-shift
    # efficiency is None -- nothing to measure against, never a crash, and not a
    # 0% (test_no_target_no_shift_efficiency.py).
    db = _sess()
    r = machines_routes.create_shift(_shift(target_output=0, actual_output=0),
                                     db=db, current_user=_ADMIN)
    assert r.target_output == 0 and r.actual_output == 0, (r.target_output, r.actual_output)
    kpi = build_shift_kpis([r])[0]
    assert kpi["efficiency"] is None and kpi["gap"] == 0, kpi
    print("PASS create accepts a zero-target unplanned shift (no efficiency, no crash)")


if __name__ == "__main__":
    test_display_layer_floors_a_corrupt_negative_row()
    test_create_rejects_negative_outputs()
    test_create_accepts_zero_target_unplanned_shift()
    print("SHIFT INGEST OK: negatives rejected on create; zero-target unplanned shift accepted")
