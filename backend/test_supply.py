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

    # plant reliability (completion basis): of the POs already due (2 received +
    # 1 late = 3 resolved), 2 were received in full -> round(2/3*100) = 67%.
    # On-track / at-risk POs aren't due yet and stay out of the denominator.
    assert s["resolved"] == 3
    assert s["reliability_rate"] == 67

    by = {x["supplier"]: x for x in s["by_supplier"]}
    assert by["Indium"]["pos"] == 2 and by["Indium"]["received"] == 1 and by["Indium"]["late"] == 1
    assert by["Kester"]["pos"] == 3 and by["Kester"]["at_risk"] == 1
    # worst-first: Indium (has a late PO) sorts before Kester
    assert s["by_supplier"][0]["supplier"] == "Indium"

    # per-supplier reliability, independently derived:
    #   Indium: 1 received of (1 received + 1 late) = 50%
    #   Kester: 1 received (PO-5) of (1 received + 0 late) = 100%; at-risk/on-track held out
    assert by["Indium"]["reliability_rate"] == 50
    assert by["Kester"]["reliability_rate"] == 100

    # RECONCILE: the summary's per-supplier reliability must equal the number the
    # supplier drill-down reports for that same supplier (same completion basis,
    # same PO set) — the two views can't disagree.
    for name in ("Indium", "Kester"):
        detail = supply.build_supplier_detail(db, "DEFAULT", name)
        assert by[name]["reliability_rate"] == detail["reliability_rate"], name
        assert by[name]["receipt_rate"] == detail["receipt_rate"], name

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
    # one late PO, none received: 0 of 1 resolved delivered in full -> 0% (a real 0).
    assert s["resolved"] == 1 and s["reliability_rate"] == 0

    # empty PO book -> zeros, no divide-by-zero (resolved 0 -> reliability 0, not a crash)
    empty = supply.build_supply_summary(_fresh_session(), "DEFAULT")
    assert empty["total"] == 0 and empty["receipt_rate"] == 0 and empty["chase"] == []
    assert empty["resolved"] == 0 and empty["reliability_rate"] == 0


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
    # reliability: of the due POs (1 received + 1 late), 1 delivered in full = 50%.
    # This is COMPLETION, not punctuality — purchase_orders carries no receipt
    # timestamp, so a PO received long after its expected date still counts as
    # received. Nothing may present this as an "on time" rate.
    assert d["reliability_rate"] == 50
    # `resolved` (POs already due) must be returned so a caller can tell a real
    # 50% from a floored 0%: 1 received + 1 late = 2 due; the at-risk PO isn't due.
    assert d["resolved"] == 2
    assert d["overdue_units"] == 80                         # 100 - 20 on the late PO
    # chase list: late (PO-2) first, then at-risk (PO-3); received excluded
    assert [o["po_no"] for o in d["chase"]] == ["PO-2", "PO-3"]
    # upcoming: PO-3 (due in 2 days, unreceived) lands; received/overdue don't
    assert len(d["upcoming"]) == 7 and sum(u["pos"] for u in d["upcoming"]) == 1
    # recent lists all three POs, each with its state
    assert len(d["recent"]) == 3
    assert {r["po_no"] for r in d["recent"]} == {"PO-1", "PO-2", "PO-3"}


def test_supplier_detail_exposes_resolved_so_no_due_reads_as_dash_not_zero():
    """A supplier with open POs but none yet due must NOT read as 0% reliable.

    reliability_rate is a completion rate over POs already due; with nothing due
    its denominator is empty and _pct floors it to 0. That 0 is a rendering
    default, not a measured failure, so the drill-down exposes `resolved` (the
    count of due POs) and the drawer shows "—" while resolved == 0 — otherwise a
    supplier we simply haven't waited on yet renders a red "0% received of due POs".
    """
    db = _fresh_session()
    _supplier(db, 1, "Indium")
    db.add_all([
        _po("PO-FUTURE", 1, 100, 0, 30),   # on_track — due in 30 days, nothing in yet
        _po("PO-SOON", 1, 100, 0, 2),      # at_risk — due in 2 days, still not due
    ])
    db.commit()

    d = supply.build_supplier_detail(db, "DEFAULT", "Indium")
    assert d["total"] == 2
    assert d["received"] == 0 and d["late"] == 0            # nothing has come due
    assert d["on_track"] == 1 and d["at_risk"] == 1
    # No PO is due yet -> resolved is 0 and reliability_rate is a floored 0, NOT a
    # real 0%. The distinguishing field the drawer gates on is `resolved`.
    assert d["resolved"] == 0
    assert d["reliability_rate"] == 0                       # floored empty denominator
    # A real 0% (below) has resolved > 0; this case has resolved == 0. The two are
    # only separable because `resolved` is exposed.
    db2 = _fresh_session()
    _supplier(db2, 1, "Indium")
    db2.add(_po("PO-LATE", 1, 100, 0, -1))                  # one PO due, none received
    db2.commit()
    real_zero = supply.build_supplier_detail(db2, "DEFAULT", "Indium")
    assert real_zero["resolved"] == 1 and real_zero["reliability_rate"] == 0


def test_supplier_detail_is_empty_safe_for_unknown_supplier():
    db = _fresh_session()
    _supplier(db, 1, "Indium")
    db.add(_po("PO-1", 1, 100, 100, 5))
    db.commit()
    d = supply.build_supplier_detail(db, "DEFAULT", "Nonexistent")
    assert d["total"] == 0
    assert d["resolved"] == 0                                      # nothing due -> "—" in the drawer
    assert d["receipt_rate"] == 0 and d["reliability_rate"] == 0   # no divide-by-zero
    assert d["chase"] == [] and d["recent"] == []
    assert d["category"] is None and d["supplier_status"] is None


def test_supply_classifies_pos_at_classification_boundaries():
    """Boundary-value coverage for the receipt-state thresholds (AT_RISK_DAYS=3).

    The existing tests only exercise interior offsets (-2, 2, 30), so an
    off-by-one in _state would still pass them. These pin the exact edges:
      - exactly one day overdue (days == -1)        -> late   (days < 0)
      - exactly due today (days == 0)               -> at_risk (0 <= AT_RISK_DAYS)
      - exactly AT_RISK_DAYS out (days == 3)        -> at_risk (inclusive top edge)
      - one day past that (days == AT_RISK_DAYS + 1) -> on_track (window excludes above)
    A late branch of `days <= 0`, an at-risk branch of `days < AT_RISK_DAYS`,
    or an over-wide `days <= AT_RISK_DAYS + 1` each shift one of these counts.
    """
    assert supply.AT_RISK_DAYS == 3   # boundaries below assume the shipped threshold
    db = _fresh_session()
    _supplier(db, 1, "Indium")
    db.add_all([
        _po("PO-OVERDUE-1D", 1, 100, 0, -1),                        # one day overdue -> late
        _po("PO-DUE-TODAY", 1, 100, 0, 0),                          # due today       -> at_risk
        _po("PO-ATRISK-EDGE", 1, 100, 0, supply.AT_RISK_DAYS),      # exactly 3 days  -> at_risk
        _po("PO-ONTRACK-EDGE", 1, 100, 0, supply.AT_RISK_DAYS + 1), # exactly 4 days  -> on_track
    ])
    db.commit()

    s = supply.build_supply_summary(db, "DEFAULT")
    assert s["total"] == 4
    # one-day-overdue is the only late; due-today AND the 3-day edge are at_risk;
    # the 4-day PO tips over into on_track.
    assert s["received"] == 0
    assert s["late"] == 1
    assert s["at_risk"] == 2
    assert s["on_track"] == 1

    # Per-PO state + days_to_due, read back from the chase list (which carries
    # both). on_track POs are never chased, so PO-ONTRACK-EDGE must be absent.
    state_by_po = {o["po_no"]: (o["state"], o["days_to_due"]) for o in s["chase"]}
    assert state_by_po["PO-OVERDUE-1D"] == ("late", -1)
    assert state_by_po["PO-DUE-TODAY"] == ("at_risk", 0)
    assert state_by_po["PO-ATRISK-EDGE"] == ("at_risk", 3)
    assert "PO-ONTRACK-EDGE" not in state_by_po
    # chase order: late (-1) first, then at_risk by soonest due (0 before 3).
    assert [o["po_no"] for o in s["chase"]] == ["PO-OVERDUE-1D", "PO-DUE-TODAY", "PO-ATRISK-EDGE"]
    print("PASS PO classification at the AT_RISK_DAYS boundaries (late/at-risk/on-track edges)")


if __name__ == "__main__":
    test_supply_classifies_pos_and_rolls_up_by_supplier()
    test_supply_classifies_pos_at_classification_boundaries()
    test_supply_honours_overdue_status_and_is_empty_safe()
    test_supplier_detail_scopes_and_scores_one_supplier()
    test_supplier_detail_exposes_resolved_so_no_due_reads_as_dash_not_zero()
    test_supplier_detail_is_empty_safe_for_unknown_supplier()
    print("SUPPLY OK: POs classified received/on-track/at-risk/late; unit receipt rate; "
          "plant + per-supplier reliability (completion basis, reconciled with the drill-down); "
          "per-supplier rollup (worst first); chase list (late then at-risk); "
          "overdue-status wins; empty-safe; supplier drill-down scopes to one supplier "
          "(receipt rate, reliability, overdue units, chase, upcoming, recent) and is empty-safe")
