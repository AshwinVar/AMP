"""The plant board: shots become parts, kilograms and rupees — or honestly nothing.

BUILT AGAINST A REAL CUSTOMER'S NUMBERS. The Ele clip from Shrinidhi Plastics'
own part table: PP H 110, 0.33 g, 56 cavities with 56 active, 14 s cycle,
Rs 0.09 a piece. Round numbers hide the arithmetic errors that matter; 0.33 g
and 0.09 do not.

WHAT IS PINNED:
  * ProductionRecord.total_count IS A SHOT COUNT. arico_import and hmi_sheet
    both write a machine's own cycle counter into it, and the edge gateway
    publishes cycles. The board's job is to MULTIPLY it by the active cavities;
    it used to echo it, so every parts, kilogram and rupee figure was out by the
    cavity count — and because ideal_parts_per_hour did multiply, the target
    line sat 56x above the bars and rated healthy hours "low". The fixtures
    below therefore speak in shots and the expectations in parts
  * a machine with NO spec has no cavity count, so its figure stays in shots and
    carries counts_are_parts=False — two units on one axis is worse than either
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


def produce(db, machine, hour, shots, good=None):
    """One hour of output, IN SHOTS.

    The parameter is named `shots` because that is what the column holds:
    arico_import.py and hmi_sheet.py both write a machine's cycle count into
    total_count, and the edge gateway publishes cycles. It was previously named
    `parts`, and that single word was the whole defect -- it made every
    expectation below read as though the board's job were to echo the column
    rather than multiply it by the cavities.
    """
    db.add(models.ProductionRecord(
        tenant_code=T, machine_id=machine.id, total_count=shots,
        good_count=shots if good is None else good,
        rejected_count=0 if good is None else shots - good,
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
        # The ideal is 14,400 PARTS/hr, so the shot counts below are that
        # target divided by the 56 active cavities. Shots, not parts, because
        # shots are what a machine's counter actually produces.
        m = setup(db)
        produce(db, m, 9, 257)              # 257 x 56 = 14,392 — 99.9% of ideal
        produce(db, m, 10, 196)             # 10,976 parts = 76% — below the line
        produce(db, m, 11, 214)             # 11,984 parts = 83% — acceptable
        pts = {p["hour"]: p for p in plant_board.day(db, T, DAY)["production"][0]["points"]}
        check("an hour at the ideal is ok", pts[9]["status"] == "ok", pts[9]["status"])
        check("76% of ideal is low", pts[10]["status"] == "low", pts[10]["status"])
        check("83% of ideal is ok", pts[11]["status"] == "ok", pts[11]["status"])
        check("an hour with no output is low, not unrated",
              pts[0]["status"] == "low", pts[0]["status"])

        m = setup(db, cycle=0)              # no ideal declared
        produce(db, m, 9, 250)
        pts = {p["hour"]: p for p in plant_board.day(db, T, DAY)["production"][0]["points"]}
        check("with NO declared cycle every hour is UNRATED, never green",
              {p["status"] for p in pts.values()} == {"unrated"},
              str({p["status"] for p in pts.values()}))

        section("4. SHOTS BECOME PARTS, AND ONLY THEN KILOGRAMS AND RUPEES")
        m = setup(db)
        produce(db, m, 9, 1000)             # 1,000 SHOTS
        board = plant_board.day(db, T, DAY)
        prod = board["production"][0]
        # THE CONVERSION ITSELF, pinned before anything derived from it:
        # 1,000 shots on a 56-active-cavity mould is 56,000 pieces.
        check("1,000 shots on 56 active cavities is 56,000 parts",
              prod["total"] == 56000, str(prod["total"]))
        check("...and the board says these numbers ARE parts",
              prod["counts_are_parts"] is True, str(prod["counts_are_parts"]))
        # THE SHOT COUNT IS CARRIED TOO, unconverted. It is the only figure on
        # the board a customer can check against their own HMI counter, so it
        # must survive the conversion rather than be replaced by it.
        check("the raw shot count is carried beside the parts",
              prod["shots"] == 1000, str(prod.get("shots")))
        check("...and on every hour, so a rate chart can be drawn in shots",
              [x["shots"] for x in prod["points"] if x["shots"]] == [1000],
              str([(x["hour"], x["shots"]) for x in prod["points"] if x["shots"]]))
        rm = board["rm_status"][0]
        # 56,000 x 0.33 g = 18,480 g = 18.48 kg
        check("56,000 parts at 0.33 g is 18.48 kg",
              rm["kg_total"] == 18.48, str(rm["kg_total"]))
        shift = board["shift_rate"][0]["points"][1]       # hours 8-16
        # 56,000 x 0.09 = Rs 5,040 over 8 hours = Rs 630/hr
        check("shift-hour rate is revenue over the 8-hour block",
              shift["revenue"] == 5040.0 and shift["rate_per_hour"] == 630.0,
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
        produce(db, m, 9, 1000)
        shift = plant_board.day(db, T, DAY)["shift_rate"][0]["points"][1]
        check("a price rise dated in the future does not restate today",
              shift["revenue"] == 5040.0,
              f"got {shift['revenue']}; the Dec price would give 28000.0")

        section("7. COUNTED WITHOUT A SPEC — AS SHOTS, AND LABELLED AS SHOTS")
        m = setup(db, fit_tool=False)       # output, but no tool and so no part
        produce(db, m, 9, 1000)
        board = plant_board.day(db, T, DAY)
        prod, rm = board["production"][0], board["rm_status"][0]
        # NO SPEC MEANS NO CAVITY COUNT, so there is nothing to multiply by and
        # the figure stays the raw shot count. The flag is what stops the
        # screen calling it parts and plotting it on one axis beside a machine
        # whose numbers WERE converted -- two units on one chart is worse than
        # either unit alone.
        check("with no tool fitted the count stays in SHOTS, unconverted",
              prod["total"] == 1000, str(prod["total"]))
        check("...and shots equals total, because there is nothing to multiply by",
              prod["shots"] == prod["total"], f"{prod['shots']} vs {prod['total']}")
        check("...and the board says so, so the screen cannot call them parts",
              prod["counts_are_parts"] is False, str(prod["counts_are_parts"]))
        check("...but no part is claimed", prod["part"] is None, str(prod["part"]))
        check("...no kilograms are invented", rm["kg_total"] == 0.0, str(rm["kg_total"]))
        check("...and the shift is marked NOT priced",
              board["shift_rate"][0]["priced"] is False)

        m = setup(db, part_code=None)       # a tool that names no part
        produce(db, m, 9, 1000)
        board = plant_board.day(db, T, DAY)
        check("a tool naming no part counts but does not price",
              board["production"][0]["total"] == 1000
              and board["shift_rate"][0]["priced"] is False)

        section("8. THE MONTHLY TABLES")
        m = setup(db)
        for h in (9, 10, 11):
            produce(db, m, h, 1000, good=980)
        mon = plant_board.month(db, T, DAY.year, DAY.month)
        # 3,000 shots x 56 = 168,000 parts; 2,940 good shots x 56 = 164,640.
        check("item-wise production totals the month, in parts",
              mon["itemwise_production"][0]["part"] == "Ele clip"
              and mon["itemwise_production"][0]["total"] == 168000,
              str(mon["itemwise_production"][:1]))
        # The good count is in shots too, so it scales by the same cavity
        # count. Converting one and not the other would put a yield of 1/56 on
        # a line that scrapped 2%.
        check("...and carries good separately, scaled by the same cavities",
              mon["itemwise_production"][0]["good"] == 164640,
              str(mon["itemwise_production"][0]))
        check("material consumption is 168,000 x 0.33 g = 55.44 kg",
              mon["rm_consumption"][0]["kg"] == 55.44, str(mon["rm_consumption"]))
        check("revenue is 168,000 x Rs 0.09 = Rs 15,120",
              mon["shift_rate_by_machine"][0]["revenue"] == 15120.0,
              str(mon["shift_rate_by_machine"]))
        check("monthly power and packing are unavailable too",
              mon["power"]["available"] is False and mon["packing"]["available"] is False)


        section("14. MATERIAL RECEIVED: ITS OWN UNIT, NEVER ADDED TO A WEIGHT")
        # Their sheet asks for RM addition beside RM consumption. Consumption is
        # derived and always kilograms; a receipt is whatever the storekeeper
        # booked it in. Adding bags to kilograms prints a number that is not a
        # weight, so the two are only ever totalled where both are weights.
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
                                        created_at=datetime(DAY.year, DAY.month, 6, 9, 0))
        draft = models.GoodsReceiptNote(tenant_code=T, grn_no="G-2", supplier_name="S",
                                        received_by="store", status="Draft",
                                        created_at=datetime(DAY.year, DAY.month, 6, 9, 0))
        db.add_all([taken, draft])
        db.flush()
        db.add(models.GRNItem(tenant_code=T, grn_id=taken.id, item_id=resin.id,
                              ordered_qty=500, received_qty=500, accepted_qty=500))
        db.add(models.GRNItem(tenant_code=T, grn_id=taken.id, item_id=bags.id,
                              ordered_qty=4, received_qty=4, accepted_qty=4))
        db.add(models.GRNItem(tenant_code=T, grn_id=draft.id, item_id=resin.id,
                              ordered_qty=9999, received_qty=9999, accepted_qty=9999))
        db.commit()
        produce(db, m, 9, 10000)

        mo = plant_board.month(db, T, DAY.year, DAY.month)
        added = {a["material"]: a for a in mo["rm_added"]}
        check("a receipt in kilograms carries a kilogram figure",
              added["PP H 110"]["kg"] == 500.0 and added["PP H 110"]["unit"] == "kg",
              str(added.get("PP H 110")))
        check("a receipt in BAGS keeps its count and has NO kilogram figure",
              added["Masterbatch"]["quantity"] == 4.0
              and added["Masterbatch"]["kg"] is None,
              str(added.get("Masterbatch")))
        check("a DRAFT receipt is not on the floor and is not counted",
              added["PP H 110"]["quantity"] == 500.0,
              f"got {added['PP H 110']['quantity']}; the draft would make it 10499")
        check("the daily series carries the weights only",
              mo["rm_added_daily"] == [{"day": date(DAY.year, DAY.month, 6).isoformat(),
                                        "kg": 500.0}],
              str(mo["rm_added_daily"]))

        section("15. USED AGAINST RECEIVED, AND HONEST ABOUT THE JOIN")
        balance = {b["material"]: b for b in mo["rm_balance"]}
        # 10,000 shots x 56 cavities x 0.33 g = 184.8 kg used against 500 received.
        check("both sides sit on one row for the same material",
              balance["PP H 110"]["consumed_kg"] == mo["rm_consumption"][0]["kg"]
              and balance["PP H 110"]["added_kg"] == 500.0,
              str(balance.get("PP H 110")))
        check("a material received but never consumed says None, not 0",
              balance["Masterbatch"]["consumed_kg"] is None
              and balance["Masterbatch"]["added_qty"] == 4.0,
              str(balance.get("Masterbatch")))
        # "Nothing was received" and "received under a different name" are
        # different facts, and this join cannot tell them apart.
        wipe_grn = db.query(models.GRNItem).filter(models.GRNItem.tenant_code == T)
        wipe_grn.delete()
        db.commit()
        mo2 = plant_board.month(db, T, DAY.year, DAY.month)
        check("a material consumed but never received says None, not 0",
              mo2["rm_balance"][0]["added_kg"] is None
              and mo2["rm_balance"][0]["consumed_kg"] > 0,
              str(mo2["rm_balance"][:1]))
        check("...and nothing received leaves an empty list, not a zero row",
              mo2["rm_added"] == [] and mo2["rm_added_daily"] == [],
              str(mo2["rm_added"]))

        section("16. ANOTHER WORKSPACE'S RECEIPTS ARE NOT ON THIS BOARD")
        other = models.InventoryItem(tenant_code=OTHER, item_code="PP", item_name="PP H 110",
                                     category="Raw", unit="kg", current_stock=0,
                                     reorder_level=0, supplier="S")
        db.add(other)
        db.flush()
        og = models.GoodsReceiptNote(tenant_code=OTHER, grn_no="G-X", supplier_name="S",
                                     received_by="store", status="Accepted",
                                     created_at=datetime(DAY.year, DAY.month, 6, 9, 0))
        db.add(og)
        db.flush()
        db.add(models.GRNItem(tenant_code=OTHER, grn_id=og.id, item_id=other.id,
                              ordered_qty=77777, received_qty=77777, accepted_qty=77777))
        db.commit()
        mine = plant_board.month(db, T, DAY.year, DAY.month)
        check("the other workspace's 77,777 kg is nowhere in this month",
              all(a["quantity"] != 77777.0 for a in mine["rm_added"]),
              str(mine["rm_added"]))

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
