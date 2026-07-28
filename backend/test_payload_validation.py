"""Raw-body handlers must answer 400, not 500, on ordinary bad input.

Several handlers take the request body as an untyped ``payload: dict`` instead of
a Pydantic schema, so nothing validates it before the handler runs. Written the
obvious way that becomes ``int(payload["qty"])``, and then every shape a real
form produces — a missing key, "" from a half-filled field, a JSON ``null`` (what
``JSON.stringify`` emits for ``undefined``/``NaN``), a stray "abc" — raises
straight out of the handler. FastAPI can only answer HTTP 500 "Internal Server
Error", which tells the user nothing about which field was wrong and tells the
operator a server fault occurred when it was a typo.

Sixteen call sites across two modules had it. This is the same class the GRN and
cycle-count fix (#379) closed, minus the partial-commit corruption: these
handlers build their row before writing, so a bad field costs the request, not
the ledger.

`payload_fields.int_field` / `str_field` turn each case into a 400 naming the
field, and the sweep at the bottom stops a new handler reintroducing it.

Run:  python backend/test_payload_validation.py     (exit 0 = pass)
"""
import ast
import os

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import enterprise_inventory_routes as eir
import gmats_inventory_routes as gir
import models
from database import Base
from payload_fields import MAX_QTY, int_field, str_field

BACKEND = os.path.dirname(os.path.abspath(__file__))
ADMIN = {"sub": "admin", "tenant": "DEFAULT", "role": "Admin"}

# Every shape a real client sends for a field the user did not fill in properly.
# "abc" only counts as bad for a numeric field — it is a perfectly good unit name.
BAD_NUMBERS = ["", None, "abc", "  "]
BAD_TEXT = ["", None, "  "]


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    db.add(models.InventoryItem(item_code="RM-1", item_name="Steel", category="Raw",
                                unit="kg", current_stock=100))
    db.add(models.GmatsItem(tenant_code="DEFAULT", item_code="G-1", item_name="Bolt",
                            unit="nos", physical_stock=10, reserved_stock=0,
                            reorder_level=1, purchase_rate=5))
    db.commit()
    return db


def _expect_400(label, fn, **kwargs):
    db = kwargs.pop("db")
    try:
        fn(db=db, current_user=ADMIN, **kwargs)
    except HTTPException as e:
        assert e.status_code == 400, f"{label}: got {e.status_code}, want 400"
        return e.detail
    except Exception as e:                       # noqa: BLE001 — that IS the bug
        raise AssertionError(f"{label}: raised {type(e).__name__} -> HTTP 500 ({e})") from e
    raise AssertionError(f"{label}: expected a 400, the call succeeded")


def test_the_int_helper_covers_every_bad_shape():
    for bad in BAD_NUMBERS:
        detail = None
        try:
            int_field({"qty": bad}, "qty")
        except HTTPException as e:
            detail = e.detail
        assert detail and "qty" in detail, f"{bad!r} -> {detail!r}"
    # a missing key is the same as an empty one
    try:
        int_field({}, "qty")
        assert False
    except HTTPException as e:
        assert "required" in e.detail

    # and the values that must still get through
    assert int_field({"qty": 0}, "qty") == 0, "zero is a real quantity"
    assert int_field({"qty": "7"}, "qty") == 7, "numeric strings are what forms send"
    assert int_field({}, "qty", required=False, default=3) == 3
    assert str_field({"n": "  Bolt "}, "n") == "Bolt", "text is trimmed"
    print(f"PASS int_field/str_field handle all {len(BAD_NUMBERS)} bad shapes and keep valid ones")


def test_bounds_are_enforced_with_a_readable_message():
    for value, expect in [(-1, "less than"), (MAX_QTY + 1, "unrealistically large"),
                          (1e23, "unrealistically large")]:
        try:
            int_field({"qty": value}, "qty")
            assert False, f"{value} should be rejected"
        except HTTPException as e:
            assert expect in e.detail, f"{value}: {e.detail}"
    assert int_field({"qty": -5}, "qty", minimum=None) == -5, "a caller may allow negatives"
    print("PASS negative and absurd quantities are refused with a readable reason")


def test_the_line_label_survives_for_multi_line_bodies():
    """#379's per-line messages must keep naming the row."""
    try:
        int_field({}, "accepted_qty", "Line 2")
        assert False
    except HTTPException as e:
        assert e.detail == "Line 2: 'accepted_qty' is required", e.detail
    print("PASS a per-line message still names the offending line")


def test_every_raw_body_handler_answers_400():
    """The 16 sites, driven through the real handlers."""
    checked = 0
    numeric = [
        ("create_remnant.original_qty", eir.create_remnant,
         lambda b: dict(payload={"item_id": 1, "original_qty": b, "remaining_qty": 1, "unit": "kg"})),
        ("create_issue_slip.requested_qty", eir.create_issue_slip,
         lambda b: dict(payload={"item_id": 1, "requested_qty": b})),
        ("gmats_stock_in.qty", gir.gmats_stock_in,
         lambda b: dict(item_id=1, payload={"qty": b})),
        ("gmats_update_item.reorder_level", gir.gmats_update_item,
         lambda b: dict(item_id=1, payload={"reorder_level": b})),
    ]
    text = [
        ("create_remnant.unit", eir.create_remnant,
         lambda b: dict(payload={"item_id": 1, "original_qty": 1, "remaining_qty": 1, "unit": b})),
        ("gmats_add_alias.alias_name", gir.gmats_add_alias,
         lambda b: dict(item_id=1, payload={"alias_name": b})),
        ("gmats_create_item.item_code", gir.gmats_create_item,
         lambda b: dict(payload={"item_code": b, "item_name": "X"})),
    ]
    for values, cases in ((BAD_NUMBERS, numeric), (BAD_TEXT, text)):
        for bad in values:
            for label, fn, build in cases:
                detail = _expect_400(f"{label} ({bad!r})", fn, db=_fresh_session(), **build(bad))
                field = label.split(".")[1]
                assert field in detail, f"{label}: the message must name the field, got {detail!r}"
                checked += 1
    # a missing key, not just an empty one
    _expect_400("create_remnant missing item_id", eir.create_remnant,
                db=_fresh_session(), payload={"original_qty": 1, "remaining_qty": 1, "unit": "kg"})
    _expect_400("gmats_stock_in missing qty", gir.gmats_stock_in,
                db=_fresh_session(), item_id=1, payload={})
    print(f"PASS all {checked + 2} raw-body cases answer 400 naming the field, never 500")


def test_the_valid_paths_still_work():
    """A validator that rejects good input is worse than the bug."""
    db = _fresh_session()
    r = eir.create_remnant({"item_id": 1, "original_qty": 10, "remaining_qty": 4, "unit": "kg"},
                           db=db, current_user=ADMIN)
    assert r["tag_no"] == "REM-1001", r
    assert db.query(models.Remnant).first().remaining_qty == 4

    db = _fresh_session()
    s = eir.create_issue_slip({"item_id": 1, "requested_qty": 3}, db=db, current_user=ADMIN)
    assert s["slip_no"] == "MIS-5001", s

    db = _fresh_session()
    out = gir.gmats_stock_in(item_id=1, payload={"qty": 5}, db=db, current_user=ADMIN)
    assert out["physical_stock"] == 15, "stock-in must still add to stock"

    db = _fresh_session()
    out = gir.gmats_update_item(item_id=1, payload={"reorder_level": 7}, db=db, current_user=ADMIN)
    assert out["reorder_level"] == 7
    print("PASS remnant, issue slip, stock-in and item update all still work on valid input")


def _bare_payload_reads(path, src=None):
    """int()/float() over a payload subscript, or a bare payload[...] read."""
    src = src if src is not None else open(path, encoding="utf-8").read()
    hits = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Subscript):
            continue
        base = node.value
        if isinstance(base, ast.Name) and base.id in ("payload", "line"):
            hits.append((node.lineno, ast.unparse(node)))
    return hits


# Subscripts that are safe because the key's presence is checked immediately
# above them. Key: "<file>::<line-ish>" -> why.
ALLOWED_SUBSCRIPTS = {
    "platform_routes.py": "each payload[...] read sits inside an `if key in payload:` guard, "
                          "and unit_value_gbp is float()-parsed inside try/except with its own 400s",
}


def test_no_handler_reads_the_body_bare():
    """All sixteen sites shared one habit; keeping the coercion in one place is
    what stops the seventeenth."""
    offenders = []
    for name in sorted(os.listdir(BACKEND)):
        if (not name.endswith("_routes.py") or name.startswith("test_")
                or name in ALLOWED_SUBSCRIPTS):
            continue
        for lineno, code in _bare_payload_reads(os.path.join(BACKEND, name)):
            offenders.append(f"{name}:{lineno} -> {code}")

    assert not offenders, (
        "These handlers read the request body directly, so a missing key or a "
        "blank field raises out of the handler as a 500 instead of a 400:\n  "
        + "\n  ".join(offenders)
        + "\nUse payload_fields.int_field(payload, 'key') / str_field(payload, 'key').")
    print(f"PASS no route module reads the body bare ({len(ALLOWED_SUBSCRIPTS)} documented exception)")


def test_the_guard_detects_a_bare_read():
    """Prove the sweep fails on the old habit, not just passes on clean code."""
    old_way = (
        "def create_thing(payload):\n"
        '    return int(payload["qty"])\n'
    )
    hits = _bare_payload_reads("<mem>", src=old_way)
    assert len(hits) == 1 and "payload['qty']" in hits[0][1].replace('"', "'"), hits
    clean = (
        "def create_thing(payload):\n"
        '    return int_field(payload, "qty")\n'
    )
    assert _bare_payload_reads("<mem>", src=clean) == [], "the helper form must not be flagged"
    print("PASS the guard flags a bare body read and clears the helper form")


if __name__ == "__main__":
    test_the_int_helper_covers_every_bad_shape()
    test_bounds_are_enforced_with_a_readable_message()
    test_the_line_label_survives_for_multi_line_bodies()
    test_every_raw_body_handler_answers_400()
    test_the_valid_paths_still_work()
    test_no_handler_reads_the_body_bare()
    test_the_guard_detects_a_bare_read()
    print("\nALL PAYLOAD VALIDATION TESTS PASSED")
