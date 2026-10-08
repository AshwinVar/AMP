"""The plant board: shots become parts, kilograms and rupees — or honestly nothing.

BUILT AGAINST A REAL CUSTOMER'S NUMBERS. The Ele clip from Shrinidhi Plastics'
own part table: PP H 110, 0.33 g, 56 cavities with 56 active, 14 s cycle,
Rs 0.09 a piece. Round numbers hide the arithmetic errors that matter; 0.33 g
and 0.09 do not.

WHAT IS PINNED:
  * active cavities, not cavities, convert shots to parts — a 56-cavity tool run
    with 4 blocked makes 52 a shot, and collapsing the two overstates output by
    8% forever
  * the ideal rate comes from the declared cycle, and an hour with no declared
    ideal is UNRATED rather than green — green would claim a standard nobody set
  * power and packing report NO SOURCE, never zero. Zeros are indistinguishable
    from a plant that consumed nothing, and a chart drawn from them is a lie
    with axes on it
  * the spec in force is the one for the day read, not today's — pricing last
    month's output at this month's price restates a month already reported
  * a machine with no tool, or a tool naming no part, still COUNTS parts; it
    just cannot price them

Run: DATABASE_URL="sqlite:///./ci_board.db" python backend/test_plant_board.py
"""
import os
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("DATABASE_URL", "sqlite:///./ci_board.db")

import database  # noqa: E402
import models    # noqa: E402
from ai import plant_board  # noqa: E402

failures = []
T = "BOARD-TEST"
#: A second workspace, so a receipt belonging to somebody else can be shown
#: not to reach this board.
OTHER = "BOARD-OTHER"
DAY = date(2026, 10, 7)


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def section(t):
    print("\n" + "=" * 74 + f"\n{t}\n" + "=" * 74)


def wipe(db):
    # GRN lines before their notes and items: they carry the foreign keys.
    for M in (models.GRNItem, models.GoodsReceiptNote, models.InventoryItem,
              models.ProductionRecord, models.ToolAsset, models.PartSpec, models.Machine):
        for tenant in (T, OTHER):
            db.query(M).filter(M.tenant_code == tenant).delete()
    db.commit()


def setup(db, *, active_cavities=56, price=0.09, weight=0.33, cycle=14.0,
          effective=date(2026, 1, 1), part_code="ELE-CLIP", fit_tool=True):
    wipe(db)
    m = models.Machine(tenant_code=T, site="", name="IMM-01", status="Running")
    db.add(m)
    db.flush()
    db.add(models.PartSpec(
        tenant_code=T, part_code="ELE-CLIP", part_name="Ele clip", material="PP H 110",
        part_weight_g=weight, cavities=56, active_cavities=active_cavities,
        ideal_cycle_time_s=cycle, price_per_piece=price, effective_from=effective))
    if fit_tool:
        db.add(models.ToolAsset(tenant_code=T, tool_no="MLD-ELE", name="Ele clip mould",
                                machine_id=m.id, cavities=active_cavities,
                                part_code=part_code, status="Active"))
    db.commit()
    return m


def produce(db, machine, hour, parts, good=None):
    db.add(models.ProductionRecord(
        tenant_code=T, machine_id=machine.id, total_count=parts,
        good_count=parts if good is None else good,
        rejected_count=0 if good is None else parts - good,
        planned_minutes=60, runtime_minutes=55, ideal_cycle_time_seconds=14,
        created_at=datetime.combine(DAY, datetime.min.time()) + timedelta(hours=hour)))
    db.commit()


def main():
    models.Base.metadata.create_all(bind=database.engine)
    db = database.SessionLocal()
    try:
        section("1. THE CUSTOMER'S OWN PART: 56 CAVITIES, 14s, 0.33g, Rs 0.09")
        m = setup(db)
        board = plant_board.day(db, T, DAY)
        prod = board["production"][0]
        # 3600/14 = 257.14 shots/hr x 56 = 14,400 parts/hr
        check("ideal rate is derived from cycle and ACTIVE cavities",
              prod["ideal_per_hour"] == 14400, str(prod["ideal_per_hour"]))
        check("the part and material are named from the fitted tool",
              prod["part"] == "Ele clip" and board["rm_status"][0]["material"] == "PP H 110",
              f"{prod['part']} / {board['rm_status'][0]['material']}")

        section("2. ACTIVE CAVITIES, NOT CAVITIES")
        setup(db, active_cavities=52)       # 4 blocked, tool still has 56
        prod = plant_board.day(db, T, DAY)["production"][0]
        check("4 blocked cavities lower the ideal rate by exactly that fraction",
              prod["ideal_per_hour"] == 13371,
              f"got {prod['ideal_per_hour']}; using all 56 would give 14400")

        section("3. GREEN, RED, AND UNRATED")
        m = setup(db)
        produce(db, m, 9, 14400)            # exactly ideal
        produce(db, m, 10, 11000)           # 76% — below the 80% line
        produce(db, m, 11, 12000)           # 83% — acceptable
        pts = {p["hour"]: p for p in plant_board.day(db, T, DAY)["production"][0]["points"]}
        check("an hour at the ideal is ok", pts[9]["status"] == "ok", pts[9]["status"])
        check("76% of ideal is low", pts[10]["status"] == "low", pts[10]["status"])
        check("83% of ideal is ok", pts[11]["status"] == "ok", pts[11]["status"])
        check("an hour with no output is low, not unrated",
              pts[0]["status"] == "low", pts[0]["status"])

        m = setup(db, cycle=0)              # no ideal declared
        produce(db, m, 9, 5000)
        pts = {p["hour"]: p for p in plant_board.day(db, T, DAY)["production"][0]["points"]}
        check("with NO declared cycle every hour is UNRATED, never green",
              {p["status"] for p in pts.values()} == {"unrated"},
              str({p["status"] for p in pts.values()}))

        section("4. KILOGRAMS AND RUPEES")
        m = setup(db)
        produce(db, m, 9, 10000)
        board = plant_board.day(db, T, DAY)
        rm = board["rm_status"][0]
        # 10,000 x 0.33 g = 3,300 g = 3.3 kg
        check("10,000 parts at 0.33 g is 3.3 kg", rm["kg_total"] == 3.3, str(rm["kg_total"]))
        shift = board["shift_rate"][0]["points"][1]       # hours 8-16
        # 10,000 x 0.09 = Rs 900 over 8 hours = Rs 112.50/hr
        check("shift-hour rate is revenue over the 8-hour block",
              shift["revenue"] == 900.0 and shift["rate_per_hour"] == 112.5,
              f"{shift['revenue']} / {shift['rate_per_hour']}")
        check("...and the shift is marked priced", board["shift_rate"][0]["priced"])

        section("5. POWER AND PACKING REPORT NO SOURCE, NEVER ZERO")
        for key in ("power", "packing"):
            series = board[key]
            check(f"{key} is marked unavailable", series["available"] is False)
            check(f"...{key} carries no points that could be drawn as zero",
                  series["points"] == [], str(series["points"]))
            check(f"...{key} names the reason and the fix",
                  bool(series["reason"]) and bool(series["fix"]),
                  f"{series.get('reason')} / {series.get('fix')}")

        section("6. THE SPEC IN FORCE IS THE ONE FOR THE DAY READ")
        m = setup(db, price=0.09, effective=date(2026, 1, 1))
        db.add(models.PartSpec(
            tenant_code=T, part_code="ELE-CLIP", part_name="Ele clip", material="PP H 110",
            part_weight_g=0.33, cavities=56, active_cavities=56, ideal_cycle_time_s=14.0,
            price_per_piece=0.50, effective_from=date(2026, 12, 1)))   # a FUTURE rise
        db.commit()
        produce(db, m, 9, 10000)
        shift = plant_board.day(db, T, DAY)["shift_rate"][0]["points"][1]
        check("a price rise dated in the future does not restate today",
              shift["revenue"] == 900.0,
              f"got {shift['revenue']}; the Dec price would give 5000.0")

        section("7. COUNTED WITHOUT A SPEC, NEVER PRICED WRONGLY")
        m = setup(db, fit_tool=False)       # output, but no tool and so no part
        produce(db, m, 9, 10000)
        board = plant_board.day(db, T, DAY)
        prod, rm = board["production"][0], board["rm_status"][0]
        check("parts are still counted with no tool fitted",
              prod["total"] == 10000, str(prod["total"]))
        check("...but no part is claimed", prod["part"] is None, str(prod["part"]))
        check("...no kilograms are invented", rm["kg_total"] == 0.0, str(rm["kg_total"]))
        check("...and the shift is marked NOT priced",
              board["shift_rate"][0]["priced"] is False)

        m = setup(db, part_code=None)       # a tool that names no part
        produce(db, m, 9, 10000)
        board = plant_board.day(db, T, DAY)
        check("a tool naming no part counts but does not price",
              board["production"][0]["total"] == 10000
              and board["shift_rate"][0]["priced"] is False)

        section("8. THE MONTHLY TABLES")
        m = setup(db)
        for h in (9, 10, 11):
            produce(db, m, h, 10000, good=9800)
        mon = plant_board.month(db, T, DAY.year, DAY.month)
        check("item-wise production totals the month",
              mon["itemwise_production"][0]["part"] == "Ele clip"
              and mon["itemwise_production"][0]["total"] == 30000,
              str(mon["itemwise_production"][:1]))
        check("...and carries good separately from total",
              mon["itemwise_production"][0]["good"] == 29400,
              str(mon["itemwise_production"][0]))
        check("material consumption is 30,000 x 0.33 g = 9.9 kg",
              mon["rm_consumption"][0]["kg"] == 9.9, str(mon["rm_consumption"]))
        check("revenue is 30,000 x Rs 0.09 = Rs 2,700",
              mon["shift_rate_by_machine"][0]["revenue"] == 2700.0,
              str(mon["shift_rate_by_machine"]))
        check("monthly power and packing are unavailable too",
              mon["power"]["available"] is False and mon["packing"]["available"] is False)


        section("9. ANY WINDOW, BUCKETED BY ITS SPAN")
        m = setup(db)
        for d in (5, 6, 7):
            db.add(models.ProductionRecord(
                tenant_code=T, machine_id=m.id, total_count=10000, good_count=9800,
                rejected_count=200, planned_minutes=60, runtime_minutes=55,
                ideal_cycle_time_seconds=14,
                created_at=datetime(2026, 10, d, 9, 0)))
        db.commit()

        one = plant_board.period(db, T, datetime(2026, 10, 7), datetime(2026, 10, 8))
        check("one day is read in hours", one["bucket"] == "hour" and len(one["series"]) == 24,
              f"{one['bucket']} / {len(one['series'])}")
        check("...and the hour that ran is the one with the parts",
              [s["parts"] for s in one["series"]].index(10000) == 9,
              str([s["parts"] for s in one["series"] if s["parts"]]))

        three = plant_board.period(db, T, datetime(2026, 10, 5), datetime(2026, 10, 8))
        check("three days are read in days", three["bucket"] == "day" and len(three["series"]) == 3,
              f"{three['bucket']} / {len(three['series'])}")
        check("...every day carries its own parts and kilograms",
              [s["parts"] for s in three["series"]] == [10000, 10000, 10000]
              and [s["kg"] for s in three["series"]] == [3.3, 3.3, 3.3],
              str(three["series"]))

        year = plant_board.period(db, T, datetime(2026, 1, 1), datetime(2027, 1, 1))
        check("a year is read in months", year["bucket"] == "month" and len(year["series"]) == 12,
              f"{year['bucket']} / {len(year['series'])}")
        check("...and October holds all of it",
              year["series"][9]["parts"] == 30000, str(year["series"][9]))

        forced = plant_board.period(db, T, datetime(2026, 10, 5), datetime(2026, 10, 8), "hour")
        check("an explicit bucket overrides the span", forced["bucket"] == "hour"
              and len(forced["series"]) == 72, f"{forced['bucket']} / {len(forced['series'])}")

        # The window is the denominator of the rate, so it must be the window
        # asked for and not a calendar month.
        check("the shift-hour rate divides by the hours in THIS window",
              three["hours"] == 72.0 and one["hours"] == 24.0,
              f"{three['hours']} / {one['hours']}")

        section("10. AN EMPTY BUCKET IS A ZERO; AN UNDERIVABLE ONE IS NOT")
        check("a bucket with no production says 0 parts, which is what happened",
              one["series"][0]["parts"] == 0, str(one["series"][0]))
        check("...and 0.0 kg, because the weight IS known for this part",
              one["series"][0]["kg"] == 0.0, str(one["series"][0]["kg"]))
        setup(db, fit_tool=False)        # output, but no spec anywhere
        db.add(models.ProductionRecord(
            tenant_code=T, machine_id=db.query(models.Machine).filter(
                models.Machine.tenant_code == T).first().id,
            total_count=500, good_count=500, rejected_count=0, planned_minutes=60,
            runtime_minutes=55, ideal_cycle_time_seconds=14,
            created_at=datetime(2026, 10, 7, 9, 0)))
        db.commit()
        bare = plant_board.period(db, T, datetime(2026, 10, 7), datetime(2026, 10, 8))
        check("with no spec in the window every bucket's kg is UNKNOWN, not 0",
              all(s["kg"] is None for s in bare["series"]),
              str([s["kg"] for s in bare["series"][:3]]))
        check("...and every bucket's revenue is UNKNOWN, not 0",
              all(s["revenue"] is None for s in bare["series"]))
        check("...while the parts are still counted",
              sum(s["parts"] for s in bare["series"]) == 500)
        check("...and named as unconverted",
              sum(s["unassigned"] for s in bare["series"]) == 500)

        section("11. MATERIAL RECEIVED: ITS OWN UNIT, AND NEVER ADDED TO A WEIGHT")
        m = setup(db)
        resin = models.InventoryItem(tenant_code=T, item_code="PP", item_name="PP H 110",
                                     category="Raw", unit="kg", current_stock=0,
                                     reorder_level=0, supplier="S")
        bags = models.InventoryItem(tenant_code=T, item_code="MB", item_name="Masterbatch",
                                    category="Raw", unit="bags", current_stock=0,
                                    reorder_level=0, supplier="S")
        db.add_all([resin, bags])
        db.flush()
        taken = models.GoodsReceiptNote(tenant_code=T, grn_no="G-1", supplier_name="S",
                                        received_by="store", status="Accepted",
                                        created_at=datetime(2026, 10, 6, 9, 0))
        draft = models.GoodsReceiptNote(tenant_code=T, grn_no="G-2", supplier_name="S",
                                        received_by="store", status="Draft",
                                        created_at=datetime(2026, 10, 6, 9, 0))
        db.add_all([taken, draft])
        db.flush()
        db.add(models.GRNItem(tenant_code=T, grn_id=taken.id, item_id=resin.id,
                              ordered_qty=500, received_qty=500, accepted_qty=500))
        db.add(models.GRNItem(tenant_code=T, grn_id=taken.id, item_id=bags.id,
                              ordered_qty=4, received_qty=4, accepted_qty=4))
        db.add(models.GRNItem(tenant_code=T, grn_id=draft.id, item_id=resin.id,
                              ordered_qty=9999, received_qty=9999, accepted_qty=9999))
        db.commit()
        db.add(models.ProductionRecord(
            tenant_code=T, machine_id=m.id, total_count=10000, good_count=10000,
            rejected_count=0, planned_minutes=60, runtime_minutes=55,
            ideal_cycle_time_seconds=14, created_at=datetime(2026, 10, 6, 9, 0)))
        db.commit()

        w = plant_board.period(db, T, datetime(2026, 10, 6), datetime(2026, 10, 7))
        added = {a["material"]: a for a in w["rm_added"]}
        check("a receipt in kilograms carries a kilogram figure",
              added["PP H 110"]["kg"] == 500.0 and added["PP H 110"]["unit"] == "kg",
              str(added.get("PP H 110")))
        check("a receipt in BAGS keeps its count and has NO kilogram figure",
              added["Masterbatch"]["quantity"] == 4.0
              and added["Masterbatch"]["kg"] is None
              and added["Masterbatch"]["unit"] == "bags",
              str(added.get("Masterbatch")))
        check("a DRAFT receipt is not on the floor and is not counted",
              added["PP H 110"]["quantity"] == 500.0,
              f"got {added['PP H 110']['quantity']}; the draft would make it 10499")

        balance = {b["material"]: b for b in w["rm_balance"]}
        # 10,000 x 0.33 g = 3.3 kg used against 500 kg received.
        check("used and received sit on one row for the same material",
              balance["PP H 110"]["consumed_kg"] == 3.3
              and balance["PP H 110"]["added_kg"] == 500.0,
              str(balance.get("PP H 110")))
        check("a material received but never consumed says so with None, not 0",
              balance["Masterbatch"]["consumed_kg"] is None
              and balance["Masterbatch"]["added_qty"] == 4.0,
              str(balance.get("Masterbatch")))

        outside = plant_board.period(db, T, datetime(2026, 10, 7), datetime(2026, 10, 8))
        check("a receipt outside the window is not in it", outside["rm_added"] == [],
              str(outside["rm_added"]))

        section("12. ANOTHER WORKSPACE'S RECEIPTS ARE NOT ON THIS BOARD")
        other = models.InventoryItem(tenant_code=OTHER, item_code="PP", item_name="PP H 110",
                                     category="Raw", unit="kg", current_stock=0,
                                     reorder_level=0, supplier="S")
        db.add(other)
        db.flush()
        og = models.GoodsReceiptNote(tenant_code=OTHER, grn_no="G-X", supplier_name="S",
                                     received_by="store", status="Accepted",
                                     created_at=datetime(2026, 10, 6, 9, 0))
        db.add(og)
        db.flush()
        db.add(models.GRNItem(tenant_code=OTHER, grn_id=og.id, item_id=other.id,
                              ordered_qty=77777, received_qty=77777, accepted_qty=77777))
        db.commit()
        mine = plant_board.period(db, T, datetime(2026, 10, 6), datetime(2026, 10, 7))
        check("the other workspace's 77,777 kg is nowhere in this tenant's window",
              all(a["quantity"] != 77777.0 for a in mine["rm_added"]),
              str(mine["rm_added"]))

        section("13. month() IS STILL THE MONTH, AND STILL SAYS SO")
        mo = plant_board.month(db, T, 2026, 10)
        check("it keeps year and month in the payload",
              mo["year"] == 2026 and mo["month"] == 10, str((mo.get("year"), mo.get("month"))))
        check("...is bucketed by day", mo["bucket"] == "day", mo["bucket"])
        check("...and still reports power and packing as unavailable",
              mo["power"]["available"] is False and mo["packing"]["available"] is False)

        wipe(db)
    finally:
        db.close()

# ── Collected by pytest as well as run as a script ────────────────────
#
# CI's per-file runner (`python test_plant_board.py`) is the contract
# and is unchanged. The separate coverage job collects every suite into ONE
# pytest process to compute the branch-coverage floor, and pytest only collects
# module-level `test_*` functions -- this file had none, so every line it
# exercises counted as untested. conftest.py describes the intended shape.
def test_everything():
    """The whole suite as one case, failing with whatever it recorded."""
    code = None
    try:
        code = main()
    except SystemExit as exc:          # several suites exit from inside main()
        code = exc.code
    assert not failures, "\n  " + "\n  ".join(str(f) for f in failures)
    assert code in (0, None), f"the suite exited with {code}"


if __name__ == "__main__":
    main()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED - plant board")
