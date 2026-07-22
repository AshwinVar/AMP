"""Inbound supply outlook read-model tests (ADR-0007).

Classifies purchase orders into received / on-track / at-risk / late from what's
received vs expected, rolls up per supplier (worst first), and lists the inbound
POs to chase. Run:  python backend/test_supply.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import supply


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _supplier(db, id_, name):
    db.add(models.Supplier(id=id_, supplier_code=f"S{id_}", supplier_name=name))


def _po(no, supplier_id, qty, received, due_offset_days, status="Open", item="Solder Paste"):
    return models.PurchaseOrder(
        po_no=no, supplier_id=supplier_id, item_name=item,
        order_quantity=qty, received_quantity=received, unit="kg", status=status,
        expected_delivery_date=(datetime.utcnow().date() + timedelta(days=due_offset_days)),
    )


def test_supply_classifies_pos_and_rolls_up_by_supplier():
    db = _fresh_session()
    _supplier(db, 1, "Indium")
    _supplier(db, 2, "Kester")
    db.add_all([
        # Indium: one received (full receipt), one late (overdue, short)
        _po("PO-1", 1, 100, 100, 5),                       # received (full quantity in)
        _po("PO-2", 1, 100, 20, -2),                       # late (2 days overdue)
        # Kester: one at-risk (due in 2 days), one on-track (due in 30), one received by status
        _po("PO-3", 2, 100, 0, 2),                         # at_risk (<= 3 days)
        _po("PO-4", 2, 100, 50, 30),                       # on_track
        _po("PO-5", 2, 100, 100, -10, status="Received"),  # received by status though overdue date
    ])
    db.commit()

    s = supply.build_supply_summary(db, "DEFAULT")
    assert s["total"] == 5
    assert s["received"] == 2 and s["late"] == 1 and s["at_risk"] == 1 and s["on_track"] == 1
    # unit receipt: received 270 of 500 ordered = 54%
    assert s["receipt_rate"] == 54

    by = {x["supplier"]: x for x in s["by_supplier"]}
    assert by["Indium"]["pos"] == 2 and by["Indium"]["received"] == 1 and by["Indium"]["late"] == 1
    assert by["Kester"]["pos"] == 3 and by["Kester"]["at_risk"] == 1
    # worst-first: Indium (has a late PO) sorts before Kester
    assert s["by_supplier"][0]["supplier"] == "Indium"

    # chase list: late first (PO-2), then at-risk (PO-3); received/on-track excluded
    chase = s["chase"]
    assert [o["po_no"] for o in chase] == ["PO-2", "PO-3"]
    assert chase[0]["state"] == "late" and chase[0]["days_to_due"] == -2

    # upcoming inbound load: 7 forward days; only PO-3 (due in 2 days, unreceived) lands
    assert len(s["upcoming"]) == 7
    assert sum(u["pos"] for u in s["upcoming"]) == 1 and s["upcoming"][2]["pos"] == 1


def test_supply_honours_overdue_status_and_is_empty_safe():
    db = _fresh_session()
    _supplier(db, 1, "Indium")
    # "Overdue" status with a future expected date is still late (status wins).
    db.add(_po("PO-9", 1, 100, 10, 5, status="Overdue"))
    db.commit()
    s = supply.build_supply_summary(db, "DEFAULT")
    assert s["late"] == 1 and s["chase"][0]["po_no"] == "PO-9"

    # empty PO book -> zeros, no divide-by-zero
    empty = supply.build_supply_summary(_fresh_session(), "DEFAULT")
    assert empty["total"] == 0 and empty["receipt_rate"] == 0 and empty["chase"] == []


def test_supplier_detail_scopes_and_scores_one_supplier():
    db = _fresh_session()
    _supplier(db, 1, "Indium")
    _supplier(db, 2, "Kester")
    db.add_all([
        # Indium: one received-in-full, one overdue-and-short, one at-risk.
        _po("PO-1", 1, 100, 100, 5),    # received
        _po("PO-2", 1, 100, 20, -2),    # late (80 units still owed)
        _po("PO-3", 1, 100, 0, 2),      # at_risk (due in 2 days)
        # Kester's PO must not bleed into Indium's drill-down.
        _po("PO-9", 2, 100, 0, 1),
    ])
    db.commit()

    d = supply.build_supplier_detail(db, "DEFAULT", "Indium")
    assert d["supplier"] == "Indium"
    assert d["total"] == 3                                  # PO-9 (Kester) excluded
    assert d["received"] == 1 and d["late"] == 1 and d["at_risk"] == 1 and d["on_track"] == 0
    # unit receipt: 120 of 300 ordered = 40%
    assert d["receipt_rate"] == 40
    # reliability: of the due POs (1 received + 1 late), 1 delivered in full = 50%
    assert d["reliability_rate"] == 50
    assert d["overdue_units"] == 80                         # 100 - 20 on the late PO
    # chase list: late (PO-2) first, then at-risk (PO-3); received excluded
    assert [o["po_no"] for o in d["chase"]] == ["PO-2", "PO-3"]
    # upcoming: PO-3 (due in 2 days, unreceived) lands; received/overdue don't
    assert len(d["upcoming"]) == 7 and sum(u["pos"] for u in d["upcoming"]) == 1
    # recent lists all three POs, each with its state
    assert len(d["recent"]) == 3
    assert {r["po_no"] for r in d["recent"]} == {"PO-1", "PO-2", "PO-3"}


def test_supplier_detail_is_empty_safe_for_unknown_supplier():
    db = _fresh_session()
    _supplier(db, 1, "Indium")
    db.add(_po("PO-1", 1, 100, 100, 5))
    db.commit()
    d = supply.build_supplier_detail(db, "DEFAULT", "Nonexistent")
    assert d["total"] == 0
    assert d["receipt_rate"] == 0 and d["reliability_rate"] == 0   # no divide-by-zero
    assert d["chase"] == [] and d["recent"] == []
    assert d["category"] is None and d["supplier_status"] is None


if __name__ == "__main__":
    test_supply_classifies_pos_and_rolls_up_by_supplier()
    test_supply_honours_overdue_status_and_is_empty_safe()
    test_supplier_detail_scopes_and_scores_one_supplier()
    test_supplier_detail_is_empty_safe_for_unknown_supplier()
    print("SUPPLY OK: POs classified received/on-track/at-risk/late; unit receipt rate; "
          "per-supplier rollup (worst first); chase list (late then at-risk); "
          "overdue-status wins; empty-safe; supplier drill-down scopes to one supplier "
          "(receipt rate, reliability, overdue units, chase, upcoming, recent) and is empty-safe")
