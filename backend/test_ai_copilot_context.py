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
from database import Base
import ai_copilot
from analytics_engine import pooled_oee


def _fresh_session():
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


def test_context_no_production_has_no_oee_line():
    # No production records -> no OEE line at all (no fabricated 0%/NaN, no crash).
    db = _fresh_session()
    db.add(models.Machine(id=1, name="Idle", status="Running", utilization=80))
    db.commit()
    context = ai_copilot._build_factory_context(db, "DEFAULT")
    assert _oee_line(context) is None, context
    print("PASS copilot context omits OEE when there is no production")


def test_context_zero_denominator_record_does_not_crash():
    # A record with zero planned/runtime/total (all divisors zero) must pool to a
    # real 0% OEE, not raise. Pooling guards every denominator.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="Cold", status="Running", utilization=0))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=0, runtime_minutes=0,
                                   ideal_cycle_time_seconds=0, total_count=0, good_count=0, rejected_count=0))
    db.commit()
    context = ai_copilot._build_factory_context(db, "DEFAULT")
    line = _oee_line(context)
    assert line is not None and "0%" in line, line
    print("PASS copilot context handles a zero-denominator record (pooled 0%)")


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
    test_context_no_production_has_no_oee_line()
    test_context_zero_denominator_record_does_not_crash()
    test_context_null_stock_does_not_crash_and_excludes_the_null_item()
    test_context_gmats_null_stock_does_not_crash()
    print("ALL COPILOT CONTEXT TESTS PASSED")
