"""Malformed-input hardening for the remnant / issue-slip create paths.

enterprise_inventory_routes takes a raw ``payload: dict`` on these endpoints (no
Pydantic schema), so nothing validates the body before it reaches ``int(...)``.
#379 fixed exactly this on the GRN / cycle-count handlers — routing every
client quantity through the shared ``_int_field`` so a missing key, "", null or
"abc" becomes a 400 naming the field instead of a bare 500 — but the sibling
remnant and issue-slip create/update paths still called ``int(payload["item_id"])``
directly. This pins the follow-up: the same malformed inputs now raise a clean
HTTP 400, and — the point of it — a rejected create leaves NO row behind (the
"phantom header" class of bug #379 called out), while every well-formed request
still stores the exact numbers it was given.

Handlers are called directly with an in-memory SQLite session and a fake
current_user (the pattern test_gmats_inventory_routes / test_event_bus use), so
the test is deterministic and needs no running server.

Run:  python backend/test_enterprise_inventory_ingest_400.py     (exit 0 = pass)
Also collectable by pytest.
"""
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import enterprise_inventory_routes as eir
import models
from database import Base

_USER = {"tenant": "DEFAULT", "role": "Admin", "sub": "tester"}


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _expect_400(fn, *args):
    """Call a handler that should reject its input; return the raised status."""
    try:
        fn(*args)
    except HTTPException as e:
        assert e.status_code == 400, f"expected 400, got {e.status_code}: {e.detail}"
        return e.detail
    raise AssertionError(f"{getattr(fn, '__name__', fn)} did not raise on bad input")


# ── create_remnant ────────────────────────────────────────────────

def test_create_remnant_happy_path_stores_exact_values():
    db = _db()
    out = eir.create_remnant(
        {"item_id": 7, "original_qty": 100, "remaining_qty": 40, "unit": "kg"},
        db, _USER,
    )
    r = db.query(models.Remnant).filter_by(id=out["id"]).first()
    # Independently-derived: the fields are stored verbatim, none coerced.
    assert r.item_id == 7 and r.original_qty == 100 and r.remaining_qty == 40
    assert r.unit == "kg" and r.status == "Available"
    assert out["tag_no"] == "REM-1001"          # first remnant -> count 0 -> 1000+0+1
    print("PASS create_remnant happy path stores exact values")


def test_create_remnant_rejects_malformed_and_leaves_no_row():
    db = _db()
    base = {"item_id": 1, "original_qty": 10, "remaining_qty": 5, "unit": "pcs"}
    cases = [
        ("missing item_id",     {k: v for k, v in base.items() if k != "item_id"}),
        ("missing unit",        {k: v for k, v in base.items() if k != "unit"}),
        ("blank original_qty",  {**base, "original_qty": ""}),
        ("null remaining_qty",  {**base, "remaining_qty": None}),
        ("text remaining_qty",  {**base, "remaining_qty": "abc"}),
        ("negative qty",        {**base, "original_qty": -5}),
        ("blank unit",          {**base, "unit": "   "}),
        ("overflow item_id",    {**base, "item_id": 1e23}),   # JS Number(...) -> 1e+23
    ]
    for label, payload in cases:
        _expect_400(eir.create_remnant, payload, db, _USER)
        # The regression that matters: a rejected create committed NOTHING.
        assert db.query(models.Remnant).count() == 0, f"{label}: phantom remnant persisted"
    print(f"PASS create_remnant returns 400 (no phantom row) for {len(cases)} malformed bodies")


# ── update_remnant_status ─────────────────────────────────────────

def _seed_remnant(db):
    r = models.Remnant(tag_no="REM-1", item_id=1, original_qty=50,
                        remaining_qty=50, unit="kg", status="Available")
    db.add(r); db.commit(); db.refresh(r)
    return r


def test_update_remnant_status_absent_qty_keeps_current():
    db = _db()
    r = _seed_remnant(db)
    eir.update_remnant_status(r.id, {"status": "In Use"}, db, _USER)   # no remaining_qty
    db.refresh(r)
    assert r.status == "In Use" and r.remaining_qty == 50, r.remaining_qty
    print("PASS update_remnant_status keeps remaining_qty when the body omits it")


def test_update_remnant_status_rejects_text_qty():
    db = _db()
    r = _seed_remnant(db)
    _expect_400(eir.update_remnant_status, r.id, {"remaining_qty": "abc"}, db, _USER)
    db.refresh(r)
    assert r.remaining_qty == 50, "a rejected update must not mutate the row"
    print("PASS update_remnant_status returns 400 on a non-numeric remaining_qty")


# ── create_issue_slip ─────────────────────────────────────────────

def test_create_issue_slip_happy_path_optional_remnant():
    db = _db()
    out = eir.create_issue_slip(
        {"item_id": 3, "requested_qty": 25}, db, _USER,   # remnant_id omitted -> None
    )
    s = db.query(models.MaterialIssueSlip).filter_by(id=out["id"]).first()
    assert s.item_id == 3 and s.requested_qty == 25 and s.remnant_id is None
    assert s.status == "Pending" and out["slip_no"] == "MIS-5001"
    print("PASS create_issue_slip happy path stores exact values, remnant optional")


def test_create_issue_slip_rejects_malformed_and_leaves_no_row():
    db = _db()
    base = {"item_id": 1, "requested_qty": 5}
    cases = [
        ("missing requested_qty", {"item_id": 1}),
        ("missing item_id",       {"requested_qty": 5}),
        ("text item_id",          {**base, "item_id": "abc"}),
        ("negative qty",          {**base, "requested_qty": -1}),
        ("text remnant_id",       {**base, "remnant_id": "abc"}),
    ]
    for label, payload in cases:
        _expect_400(eir.create_issue_slip, payload, db, _USER)
        assert db.query(models.MaterialIssueSlip).count() == 0, f"{label}: phantom slip persisted"
    print(f"PASS create_issue_slip returns 400 (no phantom row) for {len(cases)} malformed bodies")


if __name__ == "__main__":
    test_create_remnant_happy_path_stores_exact_values()
    test_create_remnant_rejects_malformed_and_leaves_no_row()
    test_update_remnant_status_absent_qty_keeps_current()
    test_update_remnant_status_rejects_text_qty()
    test_create_issue_slip_happy_path_optional_remnant()
    test_create_issue_slip_rejects_malformed_and_leaves_no_row()
    print("ALL ENTERPRISE-INVENTORY INGEST-400 TESTS PASSED")
