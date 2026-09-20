"""A shortage is sized from the tenant's own recipe, or not sized at all (ADR-0030).

ADR-0026 refused to put a number on a stock-out: *"AMP has no measured link from
a shortage to the units not made."* This is that link, so the first thing to pin
is that it is a LINK and not a guess.

  1. THE ARITHMETIC, computed independently here: outstanding units × the
     tenant's own quantity per unit, stock allocated in due-date order, and the
     remainder is what cannot be made.
  2. NO RECIPE, NO NUMBER. An item outside every bill of materials is listed
     with the reason and gets no units figure — the exact fabrication ADR-0026
     declined to make.
  3. THE ALLOCATION RULE IS THE ONE IT STATES: earliest due date first, and an
     order with no date claims stock last.
  4. PARTIAL STOCK IS PARTIAL. 90 units of a component that takes 2 each makes
     45 whole units, not 45.5 and not 0.
  5. MONEY ONLY WITH A UNIT VALUE, and never otherwise.
  6. THE TENANT'S OWN RECIPE. Two factories with the same part number and
     different quantities per unit get different answers — the ADR-0013 failure,
     checked from this side.
  7. IT WRITES NOTHING: no purchase order, no agent action.
  8. THE COPILOT says the same thing, grounded, and says what it could not size.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_shortage_impact.py
"""
import sys
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
from ai import grounding
from ai import shortage as sh
from ai.tools import registry as treg
from copilot_eval import fixtures as F
from database import Base

failures = []
A, B = "SHORT_A", "SHORT_B"
NOW = datetime(2026, 9, 20, 9, 0, 0)


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    return sessionmaker(bind=engine)


def within(Session, tenant, fn):
    db = Session()
    tok = tenancy.set_current_tenant(tenant)
    try:
        return fn(db)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def seed(Session, tenant, per_unit, stock, priced):
    """One part, one component, three open orders with different due dates."""
    def build(db):
        steel = models.InventoryItem(
            tenant_code=tenant, item_code="RM-STEEL", item_name="Steel bar", category="Raw",
            unit="kg", current_stock=stock, reorder_level=500)
        db.add(steel)
        db.flush()
        # Outbound movement, so ai.coverage can measure a burn rate and the Risk
        # Radar raises this item at all. Without it the radar has no days of
        # cover to judge and skips the item entirely — which is its own honest
        # behaviour, and not what section 10 is testing.
        for day in range(1, 8):
            db.add(models.InventoryTransaction(
                tenant_code=tenant, item_id=steel.id, transaction_type="Issue",
                quantity=30, created_at=NOW - timedelta(days=day)))
        # A second short item that appears in NO bill of materials.
        db.add(models.InventoryItem(
            tenant_code=tenant, item_code="RM-PAINT", item_name="Paint", category="Consumable",
            unit="L", current_stock=0, reorder_level=10))
        bom = models.BillOfMaterials(tenant_code=tenant, part_number="SHAFT-001",
                                     revision="A", active=True)
        db.add(bom)
        db.flush()
        db.add(models.BomComponent(tenant_code=tenant, bom_id=bom.id, component_code="RM-STEEL",
                                   quantity_per_unit=per_unit, unit="kg"))
        # A recipe line with NO quantity — a data error a real plant can have.
        # It must create no demand at all: "we do not know how much" is not
        # "one each", and a mutation that read it as one each survived until
        # this line existed.
        db.add(models.BomComponent(tenant_code=tenant, bom_id=bom.id, component_code="RM-PAINT",
                                   quantity_per_unit=0, unit="L"))
        for i, (no, target, actual, days) in enumerate([
                ("WO-EARLY", 100, 0, 2), ("WO-MID", 60, 10, 5), ("WO-LATE", 40, 0, 9)]):
            db.add(models.WorkOrder(
                tenant_code=tenant, work_order_no=no, part_number="SHAFT-001",
                batch_number=f"B{i}", target_quantity=target, actual_quantity=actual,
                status="In Progress", planned_end=NOW + timedelta(days=days)))
        # An order with NO due date: it must claim stock last.
        db.add(models.WorkOrder(
            tenant_code=tenant, work_order_no="WO-NODATE", part_number="SHAFT-001",
            batch_number="BX", target_quantity=25, actual_quantity=0, status="Planned"))
        # A CLOSED order must not create demand at all.
        db.add(models.WorkOrder(
            tenant_code=tenant, work_order_no="WO-DONE", part_number="SHAFT-001",
            batch_number="BD", target_quantity=999, actual_quantity=0, status="Completed"))
        if priced:
            # The ONE per-tenant money rate (tenancy.tenant_unit_value). Money
            # exists on this card only because the company set this, exactly as
            # everywhere else in AMP.
            db.add(models.TenantConfig(tenant_code=tenant, unit_value_gbp=12.0))
        db.commit()
    within(Session, tenant, build)


def oracle(per_unit, stock):
    """The same arithmetic, written out independently: due-date order, whole units."""
    orders = [("WO-EARLY", 100), ("WO-MID", 50), ("WO-LATE", 40), ("WO-NODATE", 25)]
    left, rows, at_risk = float(stock), [], 0
    for no, outstanding in orders:
        can_make = min(outstanding, int(left // per_unit))
        left -= can_make * per_unit
        rows.append((no, outstanding - can_make))
        at_risk += outstanding - can_make
    required = sum(o for _n, o in orders) * per_unit
    return rows, at_risk, required


def main_():
    Session = session()
    # 91kg on hand, 2kg a unit: 45 WHOLE units and 1kg stranded. The odd kilo is
    # the point — with 90 the division is exact, and a mutation that allowed
    # fractional units survived because 45.0 looks like 45.
    seed(Session, A, per_unit=2.0, stock=91, priced=True)
    # The same part number, a DIFFERENT recipe, and no unit value.
    seed(Session, B, per_unit=5.0, stock=91, priced=False)

    s = within(Session, A, lambda db: sh.build_shortage_impact(db, A, now=NOW))
    want_rows, want_at_risk, want_required = oracle(2.0, 91)

    print("\n1. The arithmetic, against an independent calculation")
    steel = next(x for x in s["shortages"] if x["item_code"] == "RM-STEEL")
    check("the open orders' requirement is outstanding x quantity per unit",
          steel["required_units"] == want_required, f"{steel['required_units']} != {want_required}")
    check(f"the units that cannot be made ({want_at_risk})",
          steel["units_at_risk"] == want_at_risk, f"{steel['units_at_risk']} != {want_at_risk}")
    check("the shortfall is what must be bought",
          steel["shortfall_units"] == round(want_required - 91, 2), str(steel["shortfall_units"]))
    check("a closed work order creates no demand",
          all(r["work_order_no"] != "WO-DONE" for r in steel["orders"]),
          str([r["work_order_no"] for r in steel["orders"]]))
    check("an order already part-made counts only what is outstanding",
          next(r for r in steel["orders"] if r["work_order_no"] == "WO-MID")["outstanding"] == 50)

    print("\n2. Partial stock makes whole units, not fractions")
    early = next(r for r in steel["orders"] if r["work_order_no"] == "WO-EARLY")
    check("91kg at 2kg a unit makes 45 WHOLE units, not 45.5", early["units_at_risk"] == 100 - 45,
          str(early["units_at_risk"]))
    check("...and the stranded kilo does not become half a unit",
          isinstance(steel["units_at_risk"], int) and steel["units_at_risk"] == want_at_risk,
          f"{steel['units_at_risk']!r}")
    check("...and every figure on the row is a whole number of units",
          all(float(r["units_at_risk"]).is_integer() for r in steel["orders"]))

    print("\n3. The allocation rule is the one it states")
    order_of = [r["work_order_no"] for r in steel["orders"]]
    check("the earliest due date claims stock first", order_of[0] == "WO-EARLY", str(order_of))
    check("and an order with no due date claims it last", order_of[-1] == "WO-NODATE", str(order_of))
    for no, want_short in want_rows:
        got = next(r for r in steel["orders"] if r["work_order_no"] == no)["units_at_risk"]
        check(f"{no}: {want_short} units at risk", got == want_short, f"{got} != {want_short}")
    check("the rule is stated on the payload", "due-date order" in s["note"], s["note"])

    print("\n4. No recipe, no number")
    unlinked = {u["item_code"] for u in s["unlinked"]}
    check("the item in no bill of materials is listed as unlinked", unlinked == {"RM-PAINT"},
          str(unlinked))
    check("...with the reason, in words",
          all("no bill of materials" in u["why"] for u in s["unlinked"]))
    check("...and NOT_CONFIGURED as its state",
          all(u["state"] == "NOT CONFIGURED" for u in s["unlinked"]))
    check("it is given no units figure at all",
          all("units_at_risk" not in u for u in s["unlinked"]), str(s["unlinked"]))
    check("and it is not silently dropped from the payload", len(s["unlinked"]) == 1)
    check("the whole result says PARTIAL DATA when something could not be sized",
          s["state"] == "PARTIAL DATA", s["state"])

    print("\n4b. A recipe line with no quantity creates no demand")
    # RM-PAINT is IN the bill of materials, at a quantity of zero. AMP must
    # treat that as "we do not know how much", not as one each — so it stays in
    # the unlinked list rather than acquiring an invented figure.
    paint = next((x for x in s["shortages"] if x["item_code"] == "RM-PAINT"), None)
    check("an item whose only recipe line has no quantity is not sized", paint is None,
          str(paint and paint["units_at_risk"]))
    check("...and it is still reported, with the reason",
          any(u["item_code"] == "RM-PAINT" for u in s["unlinked"]))

    print("\n5. Money only where a unit value is set")
    check("A is priced, so the units at risk carry a value",
          steel["money_at_risk"] == want_at_risk * 12, str(steel["money_at_risk"]))
    check("...with the currency beside it", steel["currency"] is not None)
    b = within(Session, B, lambda db: sh.build_shortage_impact(db, B, now=NOW))
    b_steel = next(x for x in b["shortages"] if x["item_code"] == "RM-STEEL")
    check("B has no unit value, so no money figure", b_steel["money_at_risk"] is None)
    check("...and none at the top level either", b["money_at_risk"] is None)
    check("...and no currency symbol is claimed", b["currency"] is None)

    print("\n6. Each tenant's OWN recipe (the ADR-0013 failure, from this side)")
    b_rows, b_at_risk, b_required = oracle(5.0, 91)
    check("the same part number with a different quantity per unit gives a different answer",
          b_steel["units_at_risk"] == b_at_risk and b_steel["units_at_risk"] != steel["units_at_risk"],
          f"{b_steel['units_at_risk']} vs {steel['units_at_risk']}")
    check("B's requirement uses B's own quantity per unit",
          b_steel["required_units"] == b_required, str(b_steel["required_units"]))
    check("A's payload never mentions B", B not in repr(s))
    check("B's payload never mentions A", A not in repr(b))

    print("\n6b. An empty workspace is not a healthy one")
    # TWO DIFFERENT FACTS WEAR THE SAME EMPTY LIST. A workspace with stock, none
    # of it low, is genuinely fine. A workspace with NO stock records has not
    # been looked at, and calling that OK with zero units at risk is the "empty
    # stock is healthy" defect the evaluation caught once already. A brand-new
    # workspace is the first thing a prospect sees.
    empty = within(Session, "TENANT_EMPTY",
                   lambda db: sh.build_shortage_impact(db, "TENANT_EMPTY", now=NOW))
    check("a workspace with no stock items is NOT_CONFIGURED, not OK",
          empty["state"] == "NOT CONFIGURED", empty["state"])
    check("...and is given NO units figure, not a zero",
          empty["units_at_risk"] is None, str(empty["units_at_risk"]))
    check("...and says so, rather than reporting stock as fine",
          "no stock items are set up" in empty["headline"].lower(), empty["headline"])
    check("...and denies being a health report in the same breath",
          "not a report that stock is healthy" in empty["headline"].lower(), empty["headline"])
    # CONTROL: stock that exists and is not low still reads as the clean OK it
    # is, so the branch above cannot be "return NOT_CONFIGURED for everything".
    # One item, well above its reorder level, and nothing else -- the point is
    # the difference between an empty list and an empty WORKSPACE.
    within(Session, "TENANT_FULL", lambda db: (
        db.add(models.InventoryItem(
            tenant_code="TENANT_FULL", item_code="RM-PLENTY", item_name="Plenty",
            category="Raw", unit="kg", current_stock=9999.0, reorder_level=10)),
        db.commit()))
    stocked = within(Session, "TENANT_FULL",
                     lambda db: sh.build_shortage_impact(db, "TENANT_FULL", now=NOW))
    check("CONTROL: stock that exists and is not low is still OK with a zero",
          stocked["state"] == "OK" and stocked["units_at_risk"] == 0,
          f"{stocked['state']} {stocked['units_at_risk']}")
    check("...and says nothing is low, which is a claim it has earned",
          "at or below its reorder level" in stocked["headline"], stocked["headline"])

    print("\n7. It writes nothing")
    before = within(Session, A, lambda db: (
        db.query(models.PurchaseOrder).count(), db.query(models.AgentAction).count()))
    within(Session, A, lambda db: sh.build_shortage_impact(db, A, now=NOW))
    after = within(Session, A, lambda db: (
        db.query(models.PurchaseOrder).count(), db.query(models.AgentAction).count()))
    check("no purchase order and no agent action is created by a read", before == after,
          f"{before} -> {after}")
    check("the suggested order is a recommendation, stated in units",
          steel["suggested_order_units"] >= steel["shortfall_units"],
          str(steel["suggested_order_units"]))

    print("\n8. The Copilot says the same thing, grounded")
    r = within(Session, A, lambda db: treg.run_tool(
        db, treg.Principal(tenant=A, role="Admin"), "get_shortage_risk"))
    check("the tool answers", r.state in ("OK", "PARTIAL DATA", "NOT CONFIGURED"),
          f"{r.state}: {r.summary}")
    check("it states what it could not size", "no recipe" in r.summary, r.summary)
    g = grounding.check(r.summary, r.to_dict()["facts"], question="what will the shortage stop?")
    check("every figure in the sentence is in its evidence", g.passed,
          f"{r.summary} :: {g.ungrounded_numbers} {g.unknown_identifiers}")
    unlinked_fact = [f for f in r.facts if f.key == "shortage.items_unlinked"]
    check("the count it could not size is itself a fact", len(unlinked_fact) == 1)
    check("...and it is the real number", unlinked_fact and unlinked_fact[0].value == 1)
    # The unpriced workspace, through the TOOL. Asking only in a priced one left
    # the "no unit value" branch untested, and a mutation writing a 0 money fact
    # survived.
    rb = within(Session, B, lambda db: treg.run_tool(
        db, treg.Principal(tenant=B, role="Admin"), "get_shortage_risk"))
    money_fact = [f for f in rb.facts if f.key == "shortage.money_at_risk"]
    check("an unpriced workspace gets a money fact of UNKNOWN", len(money_fact) == 1
          and money_fact[0].provenance == "UNKNOWN", str([f.provenance for f in money_fact]))
    check("...with no value at all, not a zero", money_fact and money_fact[0].value is None,
          str(money_fact and money_fact[0].value))
    check("...and the reason is stated", money_fact and "no unit value is set" in money_fact[0].detail)
    check("no currency symbol appears in an unpriced answer", "\u00a3" not in rb.summary, rb.summary)

    print("\n9. The three-factory fixture: the honest answer is 'no link'")
    # None of the three factories has a bill of materials, so every short item
    # must come back unlinked. A number here would be the invented one.
    FS = session()
    F.seed(FS)
    for tenant in F.TENANTS:
        out = within(FS, tenant, lambda db, t=tenant: sh.build_shortage_impact(db, t, now=NOW))
        check(f"{tenant}: no shortage is given a units figure", not out["shortages"],
              str([x['item_code'] for x in out['shortages']]))
        check(f"{tenant}: and nothing claims money", out["money_at_risk"] is None)
        for other in F.TENANTS:
            if other != tenant:
                check(f"{tenant}: does not name {other}", other not in repr(out))

    print("\n10. The Risk Radar now sizes a stock-out, where a recipe exists")
    # ADR-0026 shipped the stock rule with no size and said why: AMP had no
    # measured link. The link exists now, so the radar must carry it — and must
    # still carry nothing for an item outside every recipe.
    from ai.risk_radar import build_risk_radar
    radar = within(Session, A, lambda db: build_risk_radar(db, A, now=NOW))
    stock_risks = [r for r in radar["risks"] if r["key"].startswith("stock.")]
    sized = [r for r in stock_risks if r["impact_units"] is not None]
    check("the radar raised a stock risk at all", bool(stock_risks),
          str([r["key"] for r in radar["risks"]]))
    if stock_risks:
        steel_risk = next((r for r in stock_risks if "RM-STEEL" in r["key"]), None)
        check("the steel shortage now carries the units it would stop",
              steel_risk is not None and steel_risk["impact_units"] == want_at_risk,
              str(steel_risk and steel_risk["impact_units"]))
        check("...and money, because this workspace has a unit value",
              steel_risk is not None and steel_risk["impact_money"] == want_at_risk * 12,
              str(steel_risk and steel_risk["impact_money"]))
        paint_risk = next((r for r in stock_risks if "RM-PAINT" in r["key"]), None)
        if paint_risk is not None:
            check("the item in no recipe is STILL unsized on the radar",
                  paint_risk["impact_units"] is None, str(paint_risk["impact_units"]))
    check("every sized stock risk still states its rule",
          all(r["rule"] for r in sized))
    # And in a workspace with no recipes at all, the radar is exactly as it was.
    b_radar = within(Session, B, lambda db: build_risk_radar(db, B, now=NOW))
    b_stock = [r for r in b_radar["risks"] if r["key"].startswith("stock.")]
    check("B has a recipe too, so its stock risk is sized with B's own numbers",
          all(r["impact_units"] is not None or "RM-PAINT" in r["key"] for r in b_stock),
          str([(r["key"], r["impact_units"]) for r in b_stock]))
    check("B has no unit value, so no stock risk claims money",
          all(r["impact_money"] is None for r in b_stock),
          str([r["impact_money"] for r in b_stock]))

    print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'All checks passed'}")
    for f in failures:
        print("  -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main_())
