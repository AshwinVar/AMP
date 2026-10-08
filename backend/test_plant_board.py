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
DAY = date(2026, 10, 7)


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def section(t):
    print("\n" + "=" * 74 + f"\n{t}\n" + "=" * 74)


def wipe(db):
    for M in (models.ProductionRecord, models.ToolAsset, models.PartSpec, models.Machine):
        db.query(M).filter(M.tenant_code == T).delete()
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

        wipe(db)
    finally:
        db.close()


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
