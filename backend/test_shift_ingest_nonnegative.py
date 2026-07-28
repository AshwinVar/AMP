"""Shift ingest rejects impossible rows at the boundary.

Shift data (shift_data) feeds every shift metric: build_shift_kpis' per-shift
efficiency is actual_output / target_output, and the pooled shift attainment on
/analytics/summary, /analytics/executive-oee and build_management_summary sums
these same two columns. target_output / actual_output are plain ints in the
schema (no ge=0), so nothing but the handler stops a negative from being stored.
This was the ONE counted ingest still missing the non-negative guard its siblings
already have (production-record #266, quality #324, order/PO #346,
production-plan #351, work-order #354, operator #374).

A negative target INVERTS the efficiency into a negative percentage; a negative
actual prints a below-zero efficiency — a rate the data cannot support. Both are
pinned below with independently-derived numbers. A target of 0 is a legitimate
UNPLANNED shift and must still be accepted.

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


def test_why_it_matters_negative_output_breaks_efficiency_honesty():
    # build_shift_kpis' own efficiency definition, fed the values the OLD ingest
    # would have stored.
    #
    # A negative ACTUAL: target=200, actual=-50 -> round((-50/200)*100) = -25%.
    # Efficiency cannot be negative — the data does not support it.
    kpi = build_shift_kpis([models.ShiftData(shift_name="A", target_output=200, actual_output=-50)])[0]
    assert kpi["efficiency"] == -25, kpi
    # A negative TARGET: target=-100, actual=50 -> round((50/-100)*100) = -50%.
    # A negative denominator inverts the sign of the whole rate.
    kpi = build_shift_kpis([models.ShiftData(shift_name="B", target_output=-100, actual_output=50)])[0]
    assert kpi["efficiency"] == -50, kpi
    # A clean row of the same shape reads an honest, bounded rate.
    kpi = build_shift_kpis([models.ShiftData(shift_name="C", target_output=200, actual_output=150)])[0]
    assert kpi["efficiency"] == 75, kpi
    print("PASS a negative output would drive shift efficiency below zero (dishonest)")


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
    # efficiency is the guarded 0 (0/0 -> 0), never a crash.
    db = _sess()
    r = machines_routes.create_shift(_shift(target_output=0, actual_output=0),
                                     db=db, current_user=_ADMIN)
    assert r.target_output == 0 and r.actual_output == 0, (r.target_output, r.actual_output)
    kpi = build_shift_kpis([r])[0]
    assert kpi["efficiency"] == 0 and kpi["gap"] == 0, kpi
    print("PASS create accepts a zero-target unplanned shift (efficiency 0, no crash)")


if __name__ == "__main__":
    test_why_it_matters_negative_output_breaks_efficiency_honesty()
    test_create_rejects_negative_outputs()
    test_create_accepts_zero_target_unplanned_shift()
    print("SHIFT INGEST OK: negatives rejected on create; zero-target unplanned shift accepted")
