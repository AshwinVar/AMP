"""Copilot factory-context grounding tests.

The natural-language copilot grounds the model in a compact snapshot built by
`_build_factory_context`. The OEE line in that snapshot MUST be the same pooled
plant OEE (ratio of sums) the dashboards show — a mean of per-record OEE (mean
of ratios) over-weights tiny runs and would feed the model a number that
disagrees with every OEE card. This pins that line to the pooled definition and
covers the empty and zero-denominator edges.

Run:  python backend/test_ai_copilot_context.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import tenancy
from database import Base
import ai_copilot
from analytics_engine import pooled_oee


def _fresh_session():
    """A private database AND a known tenant binding.

    Binding the tenant is not decoration. Every row below is DEFAULT's, and
    `_build_factory_context` reads through the ADR-0002 scoping hook, so if an
    EARLIER suite in the same process left a different tenant bound and never
    reset it, every query here returns nothing and the OEE assertions fail.

    That is an order-dependent flake, and it was observed once in a full pytest
    run before being reproduced deterministically: bind FACTORY_Z first and
    this file fails; bind DEFAULT and it passes regardless of what ran before.
    A test that depends on the tenant its predecessor happened to leave behind
    is not testing the copilot.
    """
    tenancy.set_current_tenant("DEFAULT")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _oee_line(context: str):
    for line in context.splitlines():
        if "OEE" in line:
            return line
    return None


def test_context_oee_is_pooled_not_mean_of_ratios():
    # One big run at 20% OEE and one tiny perfect run at 100%. Mean of the two
    # per-record OEEs is 60%; the volume-weighted pooled OEE is far lower. The
    # copilot must ground the model in the pooled figure the dashboards report.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="Big", status="Running", utilization=80))
    db.add(models.Machine(id=2, name="Tiny", status="Running", utilization=80))
    # Big: A=500/1000=.5, P=(30*500)/(500*60)=.5, Q=400/500=.8 -> OEE 20%
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=1000, runtime_minutes=500,
                                   ideal_cycle_time_seconds=30, total_count=500, good_count=400, rejected_count=100))
    # Tiny: A=P=Q=1 -> OEE 100%
    db.add(models.ProductionRecord(machine_id=2, planned_minutes=10, runtime_minutes=10,
                                   ideal_cycle_time_seconds=60, total_count=10, good_count=10, rejected_count=0))
    db.commit()

    recs = db.query(models.ProductionRecord).all()
    # Independently derived pooled OEE: planned 1010, runtime 510, total 510,
    # good 410, ideal-seconds 30*500+60*10=15600.
    #   A=510/1010=.50495  P=15600/(510*60)=.50980  Q=410/510=.80392
    #   OEE = .50495*.50980*.80392 = .2069 -> 21%
    assert pooled_oee(recs)["oee"] == 21, pooled_oee(recs)["oee"]
    mean_of_ratios = round((20 + 100) / 2)          # = 60, the old wrong figure

    context = ai_copilot._build_factory_context(db, "DEFAULT")
    line = _oee_line(context)
    assert line is not None, context
    assert "21%" in line, line                       # the pooled, dashboard-consistent number
    assert f"{mean_of_ratios}%" not in line, line     # emphatically NOT the mean of ratios
    print(f"PASS copilot context OEE is pooled (21%), not the mean of ratios ({mean_of_ratios}%)")


def test_context_no_production_states_unmeasured_not_zero():
    """No production records -> say so, and do NOT report a number.

    This asserted "no OEE line at all". The property it protects is that the
    model is never handed a fabricated 0% or a NaN, and silence achieved that.
    Under ADR-0014 the line is present and says "no production recorded — not
    zero, unmeasured", which protects the same property more strongly: silence
    lets the model infer whatever it likes about a factory it was told nothing
    about, whereas this states the distinction the whole contract is built on.

    What must still hold, and is asserted below: no percentage appears.
    """
    db = _fresh_session()
    db.add(models.Machine(id=1, name="Idle", status="Running", utilization=80))
    db.commit()
    context = ai_copilot._build_factory_context(db, "DEFAULT")
    line = _oee_line(context)
    assert line is not None, context
    assert "not zero, unmeasured" in line, line
    # The point: no fabricated figure of any kind.
    assert "0%" not in line and "%" not in line.split("):")[-1], line
    print("PASS copilot context reports OEE as unmeasured, never as a number")


def test_context_with_no_factory_at_all_stays_silent():
    """A workspace with no machines emits no OEE line.

    CONTROL for the test above: the "unmeasured" line is gated on a factory
    existing. Without that gate the empty-workspace placeholder
    ("No factory data available yet.") becomes unreachable, because the context
    always contains at least the OEE line.
    """
    db = _fresh_session()
    context = ai_copilot._build_factory_context(db, "DEFAULT")
    assert _oee_line(context) is None, context
    print("PASS an empty workspace emits no OEE line at all")


def test_context_zero_denominator_record_does_not_crash():
    """A record with every divisor zero must not raise — and must not report 0%.

    This asserted `"0%" in line`. The property worth protecting is that a
    zero-denominator record does not raise; the 0% was the fabrication ADR-0014
    removes. Nothing was scheduled and nothing was made, so availability,
    performance and quality are all UNDEFINED, and OEE is a product of three
    undefined things. Reporting 0% tells a manager their plant ran badly when in
    fact it did not run at all.
    """
    db = _fresh_session()
    db.add(models.Machine(id=1, name="Cold", status="Running", utilization=0))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=0, runtime_minutes=0,
                                   ideal_cycle_time_seconds=0, total_count=0, good_count=0, rejected_count=0))
    db.commit()
    context = ai_copilot._build_factory_context(db, "DEFAULT")   # must not raise
    line = _oee_line(context)
    assert line is not None, context
    assert "not zero, unmeasured" in line, line
    assert "0%" not in line, line
    print("PASS a zero-denominator record reads as unmeasured, not as 0%")


def _low_stock_lines(context: str):
    """The item lines under the LOW STOCK heading (each starts with '- ')."""
    out, in_block = [], False
    for line in context.splitlines():
        if line.startswith("LOW STOCK:"):
            in_block = True
            continue
        if in_block:
            if line.startswith("- "):
                out.append(line)
            else:
                break
    return out


def test_context_null_stock_does_not_crash_and_excludes_the_null_item():
    # current_stock / reorder_level are nullable Integer columns (default=0, not
    # NOT NULL). A raw-SQL / migration / cleared write can store NULL, and the old
    # `None <= None` raised TypeError. Because _build_factory_context runs OUTSIDE
    # the /ai/ask try/except, that would be an unhandled 500 — not the honest rules
    # fallback. The context must build; the un-scorable NULL row is excluded (can't
    # say if it's low), while a genuinely-low, fully-populated item still surfaces.
    from sqlalchemy import text
    db = _fresh_session()
    db.add(models.InventoryItem(item_code="A", item_name="Bolt", category="Fastener",
                                unit="pcs", current_stock=2, reorder_level=10))          # genuinely low
    db.add(models.InventoryItem(item_code="B", item_name="Nut", category="Fastener",
                                unit="pcs", current_stock=1, reorder_level=10))
    db.add(models.InventoryItem(item_code="C", item_name="Washer", category="Fastener",
                                unit="pcs", current_stock=5, reorder_level=1))
    db.add(models.InventoryItem(item_code="D", item_name="Screw", category="Fastener",
                                unit="pcs", current_stock=500, reorder_level=10))         # healthy
    db.commit()
    # The ORM `default=0` coerces a constructor None to 0, so force real NULLs the
    # way a raw-SQL / migration write would: Nut has NULL stock, Washer NULL level.
    db.execute(text("UPDATE inventory_items SET current_stock=NULL WHERE item_code='B'"))
    db.execute(text("UPDATE inventory_items SET reorder_level=NULL WHERE item_code='C'"))
    db.commit()
    db.expire_all()

    context = ai_copilot._build_factory_context(db, "DEFAULT")   # must not raise
    lines = _low_stock_lines(context)
    assert len(lines) == 1, lines            # only the genuinely-low, non-NULL item
    assert "Bolt" in lines[0], lines
    assert "Nut" not in context and "Washer" not in context, context
    print("PASS copilot context survives NULL stock/level and excludes the un-scorable rows")


def test_context_gmats_null_stock_does_not_crash():
    # Same hazard on the GMATS 4-bucket path: physical_stock / reserved_stock /
    # reorder_level are nullable; `None - None` / `None <= None` 500'd the copilot.
    from sqlalchemy import text
    db = _fresh_session()
    db.add(models.GmatsItem(tenant_code="GMATS", item_code="G1", item_name="Gasket",
                            unit="Nos", physical_stock=3, reserved_stock=1, reorder_level=10))   # available 2 <= 10 -> low
    db.add(models.GmatsItem(tenant_code="GMATS", item_code="G2", item_name="Seal",
                            unit="Nos", physical_stock=1, reserved_stock=0, reorder_level=10))
    db.add(models.GmatsItem(tenant_code="GMATS", item_code="G3", item_name="Valve",
                            unit="Nos", physical_stock=200, reserved_stock=0, reorder_level=10))  # healthy
    db.commit()
    # Force a real NULL physical_stock on Seal (ORM default=0 would mask it).
    db.execute(text("UPDATE gmats_items SET physical_stock=NULL WHERE item_code='G2'"))
    db.commit()
    db.expire_all()

    context = ai_copilot._build_factory_context(db, "GMATS")     # must not raise
    lines = _low_stock_lines(context)
    assert len(lines) == 1, lines
    assert "Gasket" in lines[0], lines
    assert "Seal" not in context, context
    print("PASS copilot GMATS context survives NULL stock and excludes the un-scorable row")


if __name__ == "__main__":
    test_context_oee_is_pooled_not_mean_of_ratios()
    test_context_no_production_states_unmeasured_not_zero()
    test_context_with_no_factory_at_all_stays_silent()
    test_context_zero_denominator_record_does_not_crash()
    test_context_null_stock_does_not_crash_and_excludes_the_null_item()
    test_context_gmats_null_stock_does_not_crash()
    print("ALL COPILOT CONTEXT TESTS PASSED")
