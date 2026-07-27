"""Supplier performance read-model tests (ADR-0007).

`build_supplier_performance` scores each supplier over 90 days: pooled fill rate
(over-receipt capped per PO), open + overdue supply, and a GRN-timed on-time rate
(first receipt vs expected date; fulfilled-but-unreferenced POs are untimed, never
scored). These tests pin the numbers and the edges: the overdue subset, the
timing via first GRN, over-receipt capping, cancelled exclusion, unknown-supplier
labelling, worst-first ranking, empty safety, and the card contract.

Run:  python backend/test_supplier_performance.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import supplier_performance

NOW = datetime.utcnow()
TODAY = NOW.date()


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _suppliers(db):
    db.add(models.Supplier(id=1, supplier_code="SUP-A", supplier_name="Acme Metals"))
    db.add(models.Supplier(id=2, supplier_code="SUP-B", supplier_name="Bolt & Co"))


def _po(no, supplier_id, ordered, received, due_offset, status="Open", created=None):
    return models.PurchaseOrder(
        po_no=no, supplier_id=supplier_id, item_name="Widget", unit="pcs",
        order_quantity=ordered, received_quantity=received,
        expected_delivery_date=TODAY + timedelta(days=due_offset), status=status,
        created_at=created or NOW,
    )


def _grn(no, po_ref, day_offset):
    g = models.GoodsReceiptNote(grn_no=no, purchase_order_ref=po_ref,
                                supplier_name="x", received_by="buyer", status="Accepted")
    g.created_at = datetime.combine(TODAY + timedelta(days=day_offset), datetime.min.time())
    return g


def test_scorecard_fill_overdue_and_timing():
    db = _fresh_session()
    _suppliers(db)
    db.add_all([
        # Acme: P1 fulfilled on time (due +0 received -1 via GRN), P2 fulfilled late
        # (due -5, received -2 -> 3 days late), P3 open & overdue (due -4).
        _po("P1", 1, 100, 100, 0),
        _po("P2", 1, 50, 50, -5),
        _po("P3", 1, 80, 20, -4),
        # Bolt: P4 over-received (120/100, capped) untimed (no GRN), P5 open not yet due.
        _po("P4", 2, 100, 120, -1),
        _po("P5", 2, 40, 0, 6),
        # Cancelled: never counted anywhere.
        _po("P6", 1, 999, 0, -30, status="Cancelled"),
        _grn("G1", "P1", -1),
        _grn("G2", "P2", -2),
        _grn("G3", "P2", 0),      # later GRN for P2 — the FIRST (-2) must win
    ])
    db.commit()

    r = supplier_performance.build_supplier_performance(db, "DEFAULT")
    assert r["orders_considered"] == 5           # P6 cancelled -> excluded
    assert r["suppliers"] == 2
    assert r["overdue"] == 1                     # only P3 (P4 fulfilled, P5 not due)
    assert r["timed_deliveries"] == 2            # P1 + P2 (P4 has no GRN ref)
    assert r["plant_otd"] == 50                  # P1 on time, P2 late

    by = {s["supplier"]: s for s in r["by_supplier"]}
    acme, bolt = by["Acme Metals"], by["Bolt & Co"]

    # Acme: ordered 230, filled 100+50+20=170 -> 74%; 1 open, 1 overdue;
    # 2 fulfilled, both timed; on-time 1, late 1, avg 3.0 days late.
    assert acme["ordered_units"] == 230 and acme["filled_units"] == 170
    assert acme["fill_rate"] == 74
    assert acme["open"] == 1 and acme["overdue"] == 1
    assert acme["fulfilled"] == 2 and acme["timed_deliveries"] == 2
    assert acme["on_time"] == 1 and acme["late"] == 1 and acme["otd_rate"] == 50
    assert acme["avg_days_late"] == 3.0

    # Bolt: over-receipt capped (filled 100 of 140 ordered -> 71%), fulfilled but
    # untimed (no GRN reference), one open PO not yet due.
    assert bolt["ordered_units"] == 140 and bolt["filled_units"] == 100
    assert bolt["fill_rate"] == 71
    assert bolt["fulfilled"] == 1 and bolt["timed_deliveries"] == 0
    assert bolt["untimed_deliveries"] == 1 and bolt["otd_rate"] is None
    assert bolt["open"] == 1 and bolt["overdue"] == 0

    # Worst first: Acme (1 overdue) ranks above Bolt (0).
    assert r["by_supplier"][0]["supplier"] == "Acme Metals"
    assert r["worst"]["supplier"] == "Acme Metals"

    # Overdue chase list carries the honest gap.
    assert len(r["overdue_pos"]) == 1
    od = r["overdue_pos"][0]
    assert od["po_no"] == "P3" and od["days_overdue"] == 4
    assert od["ordered"] == 80 and od["received"] == 20

    assert r["tone"] == "bad"
    assert "1 PO overdue across 1 supplier — worst: Acme Metals (1)." == r["verdict"]


def test_unknown_supplier_and_none_quantities_are_safe():
    db = _fresh_session()
    db.add(_po("PX", None, 10, 0, -1))            # no supplier row at all
    p = _po("PY", None, 0, 0, 2)                  # zero-quantity PO
    p.received_quantity = None                    # NULL received
    db.add(p)
    db.commit()

    r = supplier_performance.build_supplier_performance(db, "DEFAULT")
    assert r["suppliers"] == 1                    # both pool under the None supplier
    row = r["by_supplier"][0]
    assert row["supplier"] == "Unknown supplier"
    assert row["orders"] == 2
    # PY: ordered 0 -> not fulfillable (never "fulfilled" at 0 >= 0 > 0 fails), open
    assert row["open"] == 2 and row["overdue"] == 1
    assert r["tone"] == "bad"


def test_empty_is_safe():
    db = _fresh_session()
    r = supplier_performance.build_supplier_performance(db, "DEFAULT")
    assert r["orders_considered"] == 0 and r["suppliers"] == 0
    assert r["overdue"] == 0 and r["plant_otd"] is None
    assert r["by_supplier"] == [] and r["overdue_pos"] == [] and r["worst"] is None
    assert r["verdict"] == "No purchase orders on record in the window."
    assert r["tone"] == "warn"


def test_all_on_time_reads_good():
    db = _fresh_session()
    _suppliers(db)
    db.add_all([_po("Q1", 1, 10, 10, 0), _grn("H1", "Q1", -1)])
    db.commit()

    r = supplier_performance.build_supplier_performance(db, "DEFAULT")
    assert r["overdue"] == 0 and r["plant_otd"] == 100
    assert r["tone"] == "good"
    assert "100% on time, nothing overdue" in r["verdict"]


def test_low_otd_without_overdue_reads_warn():
    db = _fresh_session()
    _suppliers(db)
    db.add_all([
        _po("R1", 1, 10, 10, -5), _grn("I1", "R1", -1),   # 4 days late
        _po("R2", 1, 10, 10, 0), _grn("I2", "R2", -1),    # on time
    ])
    db.commit()

    r = supplier_performance.build_supplier_performance(db, "DEFAULT")
    assert r["overdue"] == 0
    assert r["plant_otd"] == 50                    # below the 90% target
    assert r["tone"] == "warn"
    assert "below the 90% target" in r["verdict"]


def test_exposes_the_card_contract():
    db = _fresh_session()
    r = supplier_performance.build_supplier_performance(db, "DEFAULT")
    for k in ("days", "otd_target", "orders_considered", "suppliers", "overdue",
              "timed_deliveries", "plant_otd", "by_supplier", "overdue_pos",
              "worst", "verdict", "tone"):
        assert k in r, f"supplier-performance missing card key {k!r}"
    print("PASS supplier-performance exposes the card contract")


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
