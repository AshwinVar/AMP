"""What a shortage will actually stop — the measured link, at last (ADR-0030).

ADR-0026 names this gap in writing:

> A stock-out carries **no** units, because AMP has no measured link from a
> shortage to the units not made. That link is the smart-inventory change, and
> until it exists the radar says nothing about it.

This module is that link, and it is arithmetic rather than judgement:

    1. Which OPEN work orders still need this item? Through the tenant's OWN
       bill of materials (ADR-0013), never a shared recipe.
    2. How much does each of them still need? Outstanding units × the BOM's
       quantity per unit.
    3. How far does the stock on hand go? Allocated to the orders in DUE-DATE
       order — the rule a plant would use, stated rather than assumed.
    4. What is left unmade? Those are the units at risk, and they are the first
       figure AMP has ever had for "what will this shortage cost us".

WHAT IT REFUSES TO DO
---------------------
An at-risk item with **no BOM line** anywhere gets no units figure at all. AMP
has no measured link for it, and inventing one — "probably a day's output" —
would be exactly the fabrication the Radar declined to make. Those items are
listed separately, with the reason, so the list still reconciles to the
shortage list a buyer sees.

Money only where the company has set a unit value, as everywhere else.

NOTHING IS ORDERED. The demand figure is a recommendation that sits beside the
Reorder agent's existing draft (ai/agents.draft_reorder_on_inventory_low), which
a human still approves. This module writes nothing at all.
"""
from collections import defaultdict
from datetime import datetime

import bom as bom_module
import models
import work_order_status
from ai import evidence as ev
from ai.cost import build_cost_summary
from ai.inventory import build_inventory_summary
from currency import CURRENCY

name = "shortage"

TOP_N = 8
MAX_ORDERS_SHOWN = 5
ALLOCATION_RULE = ("Stock on hand is given to the open orders in due-date order, earliest first. "
                   "What is left over cannot be made until the item arrives.")
NO_LINK = ("AMP has no bill of materials linking this item to anything in production, so it cannot "
           "say what running out would stop.")


def _open_orders(db, tenant):
    """Open work orders with outstanding demand, earliest planned end first."""
    rows = (db.query(models.WorkOrder)
            .filter(models.WorkOrder.tenant_code == tenant, work_order_status.open_clause())
            .all())
    live = []
    for w in rows:
        outstanding = max((w.target_quantity or 0) - (w.actual_quantity or 0), 0)
        if outstanding:
            live.append((w, outstanding))
    # A missing planned_end sorts LAST: an order with no date cannot claim stock
    # ahead of one that has a date to miss.
    live.sort(key=lambda p: (p[0].planned_end is None, p[0].planned_end or datetime.max, p[0].id))
    return live


def _demand(db, tenant, orders):
    """{component_code: [(work_order, outstanding, per_unit, unit)]}, from the
    tenant's OWN boms. A part with no active BOM contributes nothing — it is not
    a zero, it is an absence, and the caller reports it as one."""
    per_part = {}
    out = defaultdict(list)
    for w, outstanding in orders:
        if w.part_number not in per_part:
            per_part[w.part_number] = bom_module.components_of(
                bom_module.resolve(db, tenant, w.part_number))
        for code, qty_per_unit, unit in per_part[w.part_number]:
            if qty_per_unit > 0:
                out[code].append((w, outstanding, float(qty_per_unit), unit))
    return out


def _allocate(on_hand, lines):
    """Walk the orders in due-date order, spending the stock. Returns
    (rows, units_at_risk, required_total)."""
    left = float(on_hand)
    rows, at_risk, required = [], 0, 0.0
    for w, outstanding, per_unit, unit in lines[:MAX_ORDERS_SHOWN + 5]:
        need = outstanding * per_unit
        required += need
        # How many whole units this order can still make from what is left.
        can_make = min(outstanding, int(left // per_unit)) if per_unit > 0 else outstanding
        left = max(0.0, left - can_make * per_unit)
        short = outstanding - can_make
        at_risk += short
        rows.append({
            "work_order_no": w.work_order_no,
            "part_number": w.part_number,
            "outstanding": outstanding,
            "per_unit": round(per_unit, 4),
            "component_unit": unit,
            "units_at_risk": short,
            "planned_end": w.planned_end.isoformat() if w.planned_end else None,
        })
    return rows, at_risk, required


def _fact(key, label, value, prov, unit="", source="", window="now", detail=""):
    return ev.Fact(key=key, label=label, value=value, provenance=prov, unit=unit,
                   source=source, window=window, detail=detail).to_dict(key)


def _shortage(item, rows, at_risk, required, unit_value):
    on_hand = item["current_stock"]
    shortfall = max(0.0, required - on_hand)
    money = None
    if unit_value is not None and at_risk:
        money = round(at_risk * unit_value)
    facts = [
        _fact(f"short.{item['item_code']}.on_hand", f"{item['item_name']} on hand", on_hand,
              ev.MEASURED, item["unit"], "inventory_items"),
        _fact(f"short.{item['item_code']}.required", f"{item['item_name']} the open orders need",
              round(required, 2), ev.DERIVED, item["unit"],
              "work_orders x bills_of_materials",
              detail="outstanding units on open orders, times this tenant's own quantity per unit"),
        _fact(f"short.{item['item_code']}.at_risk", "Units that cannot be made", at_risk,
              ev.DERIVED, "units", "work_orders x bills_of_materials", detail=ALLOCATION_RULE),
    ]
    if shortfall > 0:
        facts.append(_fact(f"short.{item['item_code']}.shortfall", "Short by", round(shortfall, 2),
                           ev.DERIVED, item["unit"], "the two figures above"))
    if money is not None:
        facts.append(_fact(f"short.{item['item_code']}.money", "Value of the units at risk", money,
                           ev.DERIVED, CURRENCY, "cost model (ADR-0010)",
                           detail="the units at risk at the company's own configured unit value"))
    return {
        "item_code": item["item_code"],
        "item_name": item["item_name"],
        "unit": item["unit"],
        "on_hand": on_hand,
        "reorder_level": item["reorder_level"],
        "required_units": round(required, 2),
        "shortfall_units": round(shortfall, 2),
        "units_at_risk": at_risk,
        "money_at_risk": money,
        "currency": CURRENCY if money is not None else None,
        "orders": rows[:MAX_ORDERS_SHOWN],
        "orders_affected": len([r for r in rows if r["units_at_risk"] > 0]),
        # What a buyer should order to clear the demand AMP can see. A
        # recommendation, next to the Reorder agent's policy-based draft — this
        # module orders nothing and writes nothing.
        "suggested_order_units": int(shortfall) + (1 if shortfall % 1 else 0),
        "basis": ALLOCATION_RULE,
        "facts": facts,
        "state": ev.OK,
    }


def build_shortage_impact(db, tenant: str, now=None) -> dict:
    """For every item at or below its reorder level, the production it will stop."""
    at = now or datetime.utcnow()
    inv = build_inventory_summary(db, tenant)
    cost = build_cost_summary(db, tenant)
    unit_value = cost["unit_value_gbp"] if cost["priced"] else None

    items = inv["items"]
    if not items:
        return {"generated_at": at.isoformat(), "state": ev.OK,
                "headline": "Nothing is at or below its reorder level.",
                "shortages": [], "unlinked": [], "units_at_risk": 0, "money_at_risk": None,
                "currency": None, "priced": bool(unit_value is not None),
                "note": ALLOCATION_RULE}

    orders = _open_orders(db, tenant)
    demand = _demand(db, tenant, orders)

    shortages, unlinked = [], []
    for item in items[:TOP_N]:
        lines = demand.get(item["item_code"], [])
        if not lines:
            # NO MEASURED LINK. Stated, never guessed — this is the fabrication
            # ADR-0026 refused to make, and refusing it is still the right answer
            # for an item nobody has put in a recipe.
            unlinked.append({"item_code": item["item_code"], "item_name": item["item_name"],
                             "on_hand": item["current_stock"], "unit": item["unit"],
                             "state": ev.NOT_CONFIGURED, "why": NO_LINK})
            continue
        rows, at_risk, required = _allocate(item["current_stock"], lines)
        shortages.append(_shortage(item, rows, at_risk, required, unit_value))

    shortages.sort(key=lambda s: (-(s["money_at_risk"] or 0), -s["units_at_risk"], s["item_code"]))
    total_units = sum(s["units_at_risk"] for s in shortages)
    total_money = (sum(s["money_at_risk"] or 0 for s in shortages)
                   if unit_value is not None and total_units else None)

    if not shortages and unlinked:
        state = ev.NOT_CONFIGURED
        headline = (f"{len(unlinked)} item{'s' if len(unlinked) != 1 else ''} at or below the reorder "
                    "level, and none of them appears in a bill of materials, so AMP cannot say what "
                    "running out would stop.")
    elif not total_units:
        state = ev.OK
        headline = ("Everything at or below its reorder level is still covered: the stock on hand "
                    "reaches every open order that needs it.")
    else:
        state = ev.PARTIAL_DATA if unlinked else ev.OK
        worst = shortages[0]
        headline = (f"{total_units:,} units of production cannot be made from the stock on hand. "
                    f"The biggest is {worst['item_name']}: {worst['units_at_risk']:,} units across "
                    f"{worst['orders_affected']} open order{'s' if worst['orders_affected'] != 1 else ''}.")
    return {
        "generated_at": at.isoformat(),
        "state": state,
        "headline": headline,
        "units_at_risk": total_units,
        "money_at_risk": total_money,
        "currency": CURRENCY if total_money is not None else None,
        "priced": unit_value is not None,
        "shortages": shortages,
        # Kept in the payload, not dropped: the list a buyer sees must still
        # reconcile with the list AMP could size.
        "unlinked": unlinked,
        "note": ALLOCATION_RULE,
    }


def say_shortage(s) -> tuple:
    """One sentence for the Copilot, with the unmeasurable part attached."""
    text = s["headline"]
    if s["unlinked"]:
        text += (f" {len(s['unlinked'])} other item"
                 f"{'s are' if len(s['unlinked']) != 1 else ' is'} short with no recipe linking "
                 "them to production, so AMP cannot size those.")
    return text, "inventory"
