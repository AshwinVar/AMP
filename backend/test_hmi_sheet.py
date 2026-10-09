"""Guards for figures transcribed off an HMI screen.

THE TWO DEFECTS THESE EXIST TO CATCH, both of which flatter the plant:

  1. A DAY COUNTED TWICE. The same day can reach AMP as a photograph of the
     MONTH page, a photograph of the HOUR PROD. page, and the controller's own
     USB export. Stored naively all three ADD, and a day of 5,000 shots becomes
     a day of 13,000. The direction matters: nobody questions a number that
     makes them look good.

  2. A DAY DRAWN AS AN HOUR. A MONTH-page figure has no hour in it. Stamped at
     midnight and bucketed by hour, a full day's output becomes one bar at 00:00
     followed by twenty-three empty hours -- a plant that ran three shifts shown
     as one that stopped before one o'clock.

Run: python backend/test_hmi_sheet.py
"""
import os
import sys
from datetime import date, datetime

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DATABASE_URL", "sqlite:///./ci_hmi_sheet.db")

import hmi_sheet  # noqa: E402
import models  # noqa: E402
from ai import plant_board  # noqa: E402
from database import Base, SessionLocal, engine  # noqa: E402

TENANT = "HMITEST"
FAILURES = []


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(label)


def fresh():
    """A workspace with two presses and nothing recorded."""
    db = SessionLocal()
    for model in (models.ProductionRecord, models.Machine):
        db.query(model).filter(model.tenant_code == TENANT).delete()
    db.commit()
    made = {}
    for name in ("IMM-01", "IMM-02"):
        m = models.Machine(tenant_code=TENANT, name=name, status="running")
        db.add(m)
        made[name] = m
    db.commit()
    return db, made


def write(tmp, name, text):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return path


def rows_for(db, machine_id):
    return (db.query(models.ProductionRecord)
              .filter(models.ProductionRecord.tenant_code == TENANT,
                      models.ProductionRecord.machine_id == machine_id)
              .all())


# ── The reader refuses rather than guesses ───────────────────────────────────

def case_reader(tmp):
    bad = [
        ("a missing column", "machine,date\nIMM-01,2026-10-09\n", "shots"),
        ("a date nobody can parse",
         "machine,date,shots\nIMM-01,Tuesday,5\n", "not a date"),
        ("shots that are not a number",
         "machine,date,shots\nIMM-01,2026-10-09,lots\n", "not a number"),
        ("a counter that went backwards",
         "machine,date,shots\nIMM-01,2026-10-09,-5\n", "does not go back"),
        ("a meter that went backwards",
         "machine,date,shots,kwh\nIMM-01,2026-10-09,5,-1\n", "does not go back"),
        ("a header and no rows", "machine,date,shots\n", "no rows"),
        ("a nameless machine",
         "machine,date,shots\n,2026-10-09,5\n", "machine is empty"),
    ]
    for label, text, wanted in bad:
        path = write(tmp, "bad.csv", text)
        try:
            hmi_sheet.read_sheet(path, "hmi-day")
            check(f"refuses {label}", False, "it was accepted")
        except hmi_sheet.HmiSheetError as exc:
            check(f"refuses {label}", wanted in str(exc), f"said {exc!r}")

    path = write(tmp, "h.csv", "machine,date,hour,shots\nIMM-01,2026-10-09,24,5\n")
    try:
        hmi_sheet.read_sheet(path, "hmi-hour")
        check("refuses hour 24", False, "it was accepted")
    except hmi_sheet.HmiSheetError as exc:
        check("refuses hour 24", "hours 0..23" in str(exc), f"said {exc!r}")


def case_blank_kwh_is_not_zero(tmp):
    """The defect this is the whole point of: a blank must survive as None.

    IMM-12 on the Shrinidhi floor prints an energy column of 0.0 for a day it
    made 3,658 shots, because its ENERGY PULSE KWh constant is 000.0. Its kWh
    cells are left blank here. If a blank became 0.0 the board would draw a bar
    saying the machine ran all day and drew no power.
    """
    path = write(tmp, "blank.csv",
                 "machine,date,shots,kwh\nIMM-01,2026-10-09,100,\n")
    rows = hmi_sheet.read_sheet(path, "hmi-day")
    check("a blank kwh reads as None, not 0.0",
          rows[0]["kwh"] is None, f"got {rows[0]['kwh']!r}")

    planned = hmi_sheet.plan(rows, "hmi-day")
    check("and survives planning as None",
          planned[0]["energy_kwh"] is None, f"got {planned[0]['energy_kwh']!r}")


def case_zero_shots_are_not_stored(tmp):
    path = write(tmp, "z.csv", "machine,date,hour,shots\n"
                 "IMM-01,2026-10-09,0,0\nIMM-01,2026-10-09,1,50\n")
    planned = hmi_sheet.plan(hmi_sheet.read_sheet(path, "hmi-hour"), "hmi-hour")
    check("an hour that made nothing is not written as a record of zero",
          len(planned) == 1 and planned[0]["total_count"] == 50,
          f"planned {len(planned)} rows")


# ── Precedence: one machine-day has exactly one source ───────────────────────

def case_a_better_source_replaces_a_weaker_one(tmp):
    db, made = fresh()
    day_path = write(tmp, "d.csv",
                     "machine,date,shots,kwh\nIMM-01,2026-10-09,5000,90.0\n")
    hmi_sheet.import_sheet(db, TENANT, made, day_path, "hmi-day")
    check("the MONTH page lands", len(rows_for(db, made["IMM-01"].id)) == 1)

    hour_path = write(tmp, "h.csv", "machine,date,hour,shots,kwh\n"
                      + "".join(f"IMM-01,2026-10-09,{h},200,3.5\n" for h in range(24)))
    out = hmi_sheet.import_sheet(db, TENANT, made, hour_path, "hmi-hour")

    rows = rows_for(db, made["IMM-01"].id)
    check("the HOUR PROD. page replaces it rather than adding to it",
          len(rows) == 24, f"{len(rows)} rows remain")
    check("and says how many it replaced", out["rows_replaced"] == 1,
          f"reported {out['rows_replaced']}")
    check("so the day is counted once",
          sum(r.total_count for r in rows) == 4800,
          f"day totals {sum(r.total_count for r in rows)}")
    db.close()


def case_a_weaker_source_never_overwrites_a_better_one(tmp):
    db, made = fresh()
    hour_path = write(tmp, "h.csv", "machine,date,hour,shots\n"
                      + "".join(f"IMM-01,2026-10-09,{h},200\n" for h in range(24)))
    hmi_sheet.import_sheet(db, TENANT, made, hour_path, "hmi-hour")

    day_path = write(tmp, "d.csv",
                     "machine,date,shots\nIMM-01,2026-10-09,9999\n")
    out = hmi_sheet.import_sheet(db, TENANT, made, day_path, "hmi-day")

    rows = rows_for(db, made["IMM-01"].id)
    check("the MONTH page does not overwrite the hours",
          len(rows) == 24 and sum(r.total_count for r in rows) == 4800,
          f"{len(rows)} rows totalling {sum(r.total_count for r in rows)}")
    check("and the person is told which days were kept",
          out["days_kept_from_a_better_source"].get("IMM-01") == ["2026/10/09"],
          f"reported {out['days_kept_from_a_better_source']!r}")
    check("and the summary counts only what was stored", out["shots"] == 0,
          f"claimed {out['shots']} shots")
    db.close()


def case_a_transcription_never_deletes_the_controllers_own_export(tmp):
    """The mutant that found this deleted UPWARDS: `weaker = everything else`.

    Nothing in the suite imported a photograph over an existing USB export, so
    a supersede that wiped the export instead of being blocked by it survived.
    An export is the best source there is; a photograph arriving afterwards
    must leave it entirely alone.
    """
    db, made = fresh()
    machine = made["IMM-01"]
    for hour in range(24):
        db.add(models.ProductionRecord(
            tenant_code=TENANT, machine_id=machine.id,
            source_record_id=f"arico:IMM-01:2026/10/09:{hour:02d}",
            planned_minutes=60, runtime_minutes=60,
            ideal_cycle_time_seconds=15, total_count=250, good_count=250,
            rejected_count=0, energy_kwh=4.0,
            created_at=datetime(2026, 10, 9, hour)))
    db.commit()

    hour_path = write(tmp, "h.csv", "machine,date,hour,shots\n"
                      + "".join(f"IMM-01,2026-10-09,{h},999\n" for h in range(24)))
    out = hmi_sheet.import_sheet(db, TENANT, made, hour_path, "hmi-hour")

    rows = rows_for(db, machine.id)
    check("a photographed hour does not delete the exported one",
          len(rows) == 24, f"{len(rows)} rows remain")
    check("and does not overwrite its figures",
          sum(r.total_count for r in rows) == 6000,
          f"totals {sum(r.total_count for r in rows)}")
    check("the export is still the source of every row",
          all((r.source_record_id or "").startswith("arico:") for r in rows))
    check("and the person is told the day was kept",
          out["days_kept_from_a_better_source"].get("IMM-01") == ["2026/10/09"],
          f"reported {out['days_kept_from_a_better_source']!r}")
    db.close()


def case_rank_puts_an_unknown_source_last(tmp):
    """`rank` is the whole precedence order; an unknown kind must be weakest.

    A mutant made it strongest (-1), which would let any typo in a `kind`
    delete every real row it touched. Nothing reached it through import_sheet,
    because that validates the kind first -- so the contract is pinned here,
    where it is actually stated.
    """
    check("the export outranks both photographs",
          hmi_sheet.rank("arico") < hmi_sheet.rank("hmi-hour")
          < hmi_sheet.rank("hmi-day"))
    check("an unknown kind outranks nothing",
          hmi_sheet.rank("nonsense") > hmi_sheet.rank("hmi-day"))

    db, made = fresh()
    day_path = write(tmp, "d.csv", "machine,date,shots\nIMM-01,2026-10-09,5000\n")
    hmi_sheet.import_sheet(db, TENANT, made, day_path, "hmi-day")
    removed = hmi_sheet.supersede(db, TENANT, "IMM-01", ["2026/10/09"], "nonsense")
    db.commit()
    check("so a source nobody recognises deletes nothing",
          removed == 0 and len(rows_for(db, made["IMM-01"].id)) == 1,
          f"removed {removed}")
    db.close()


def case_precedence_only_touches_the_same_machine_and_day(tmp):
    db, made = fresh()
    day_path = write(tmp, "d.csv", "machine,date,shots\n"
                     "IMM-01,2026-10-08,4000\nIMM-02,2026-10-09,4000\n")
    hmi_sheet.import_sheet(db, TENANT, made, day_path, "hmi-day")

    hour_path = write(tmp, "h.csv", "machine,date,hour,shots\n"
                      + "".join(f"IMM-01,2026-10-09,{h},100\n" for h in range(24)))
    hmi_sheet.import_sheet(db, TENANT, made, hour_path, "hmi-hour")

    check("another day on the same machine is left alone",
          any(r.planned_minutes == hmi_sheet.MINUTES_IN_DAY
              for r in rows_for(db, made["IMM-01"].id)))
    check("another machine on the same day is left alone",
          len(rows_for(db, made["IMM-02"].id)) == 1)
    db.close()


def case_reimporting_the_same_sheet_changes_nothing(tmp):
    db, made = fresh()
    path = write(tmp, "d.csv", "machine,date,shots,kwh\n"
                 "IMM-01,2026-10-09,5000,90.0\nIMM-01,2026-10-08,4000,80.0\n")
    first = hmi_sheet.import_sheet(db, TENANT, made, path, "hmi-day")
    second = hmi_sheet.import_sheet(db, TENANT, made, path, "hmi-day")

    rows = rows_for(db, made["IMM-01"].id)
    check("a re-import creates nothing",
          second["rows_written"] == 0 and first["rows_written"] == 2,
          f"wrote {second['rows_written']} the second time")
    check("it updates in place instead", second["rows_updated"] == 2)
    check("and the plant's output has not doubled",
          sum(r.total_count for r in rows) == 9000,
          f"totals {sum(r.total_count for r in rows)}")
    db.close()


def case_a_machine_is_never_invented(tmp):
    db, made = fresh()
    path = write(tmp, "d.csv", "machine,date,shots\nIMM-99,2026-10-09,100\n")
    try:
        hmi_sheet.import_sheet(db, TENANT, made, path, "hmi-day")
        check("a sheet naming an unknown machine is refused", False,
              "it was accepted")
    except hmi_sheet.HmiSheetError as exc:
        check("a sheet naming an unknown machine is refused",
              "IMM-99" in str(exc), f"said {exc!r}")
    check("and no machine was created for it",
          db.query(models.Machine).filter(
              models.Machine.tenant_code == TENANT).count() == 2)
    db.close()


# ── A day is never drawn as an hour ──────────────────────────────────────────

def case_a_day_total_is_not_drawn_at_midnight(tmp):
    db, made = fresh()
    day_path = write(tmp, "d.csv",
                     "machine,date,shots,kwh\nIMM-01,2026-10-09,5000,90.0\n")
    hmi_sheet.import_sheet(db, TENANT, made, day_path, "hmi-day")

    board = plant_board.day(db, TENANT, date(2026, 10, 9))
    row = next(p for p in board["production"] if p["machine"] == "IMM-01")

    midnight = next(p for p in row["points"] if p["hour"] == 0)
    check("the day's shots do not appear as a bar at 00:00",
          midnight["parts"] == 0, f"drew {midnight['parts']} at midnight")
    check("no hour carries the day",
          max(p["parts"] for p in row["points"]) == 0,
          f"peak hour has {max(p['parts'] for p in row['points'])}")
    check("but the day's real total is still reported",
          row["total"] == 5000, f"reported {row['total']}")
    check("and the board says the hours are not known",
          row["hours_known"] is False, f"claimed hours_known={row['hours_known']}")

    power = board["power"]
    check("the day's kWh is in the total", power.get("total") == 90.0,
          f"reported {power.get('total')}")
    check("and the power chart admits it has no hours",
          power.get("hours_known") is False or not power.get("available"),
          f"claimed hours_known={power.get('hours_known')}")
    # The mutant that found this fed the DAY record to _energy_hours. The total
    # stayed right and hours_known stayed False, so every assertion above still
    # passed -- while the chart drew 90 kWh at midnight. The points are the
    # thing being lied about, so the points are what has to be checked.
    drawn = power.get("points") or []
    check("and draws no kWh at midnight either",
          all((p["kwh"] or 0) == 0 for p in drawn),
          f"drew {[p for p in drawn if (p['kwh'] or 0)][:2]}")
    db.close()


def case_an_hourly_day_still_draws_and_adds_up(tmp):
    db, made = fresh()
    hour_path = write(tmp, "h.csv", "machine,date,hour,shots,kwh\n"
                      + "".join(f"IMM-01,2026-10-09,{h},200,3.5\n" for h in range(24)))
    hmi_sheet.import_sheet(db, TENANT, made, hour_path, "hmi-hour")

    board = plant_board.day(db, TENANT, date(2026, 10, 9))
    row = next(p for p in board["production"] if p["machine"] == "IMM-01")
    check("an hourly day draws every hour",
          all(p["parts"] == 200 for p in row["points"]),
          f"points {[p['parts'] for p in row['points']][:3]}...")
    check("its total matches its hours", row["total"] == 4800)
    check("and it does not claim its hours are unknown",
          row["hours_known"] is True)
    check("the power chart draws, and its total matches",
          board["power"]["available"] and board["power"]["total"] == 84.0,
          f"reported {board['power'].get('total')}")
    db.close()


def case_a_mixed_plant_keeps_both_honest(tmp):
    """One machine reports hours, the other only a day. Both must be right."""
    db, made = fresh()
    hour_path = write(tmp, "h.csv", "machine,date,hour,shots\n"
                      + "".join(f"IMM-01,2026-10-09,{h},100\n" for h in range(24)))
    hmi_sheet.import_sheet(db, TENANT, made, hour_path, "hmi-hour")
    day_path = write(tmp, "d.csv", "machine,date,shots\nIMM-02,2026-10-09,7000\n")
    hmi_sheet.import_sheet(db, TENANT, made, day_path, "hmi-day")

    board = plant_board.day(db, TENANT, date(2026, 10, 9))
    one = next(p for p in board["production"] if p["machine"] == "IMM-01")
    two = next(p for p in board["production"] if p["machine"] == "IMM-02")
    check("the metered machine keeps its hours",
          one["hours_known"] is True and one["total"] == 2400)
    check("the photographed machine keeps its day",
          two["hours_known"] is False and two["total"] == 7000)
    check("and the day machine contributes nothing to any hour",
          all(p["parts"] == 0 for p in two["points"]))
    db.close()


def case_hourly_only_is_what_does_it():
    """The helper itself, so a caller that forgets it is the only way to regress.

    THE SHIFT CASE IS THE IMPORTANT ONE, and it caught a real regression. This
    filter was first written as `<= 60`, which looks obviously right and is not:
    most of AMP writes planned_minutes=480 -- the factory simulator,
    reset_factory, onboard_tenant, the copilot eval fixtures -- so a 60-minute
    rule silently drops nearly every record AMP has ever stored and blanks the
    plant board for every seeded tenant. test_copilot_eval.py failed with board
    figures of 0.0 and that is how it was found.

    The defect being fixed is narrower and provable: a record carrying an ENTIRE
    DAY, which has no hour in it at all. Shift records keep the behaviour they
    have always had.
    """
    class R:
        def __init__(self, minutes):
            self.planned_minutes = minutes
    check("an hour is kept",
          len(plant_board.hourly_only([R(hmi_sheet.MINUTES_IN_HOUR)])) == 1)
    check("a shift is kept -- 480 is what most of AMP writes",
          len(plant_board.hourly_only([R(480)])) == 1)
    check("so is anything short of a whole day",
          len(plant_board.hourly_only([R(plant_board.DAY_MINUTES - 1)])) == 1)
    check("a whole day is dropped",
          plant_board.hourly_only([R(hmi_sheet.MINUTES_IN_DAY)]) == [])
    check("and so is anything longer",
          plant_board.hourly_only([R(plant_board.DAY_MINUTES * 2)]) == [])
    check("a record with no window at all is kept, not silently dropped",
          len(plant_board.hourly_only([R(None)])) == 1)


def case_the_remaining_refusals(tmp):
    """The refusal paths the happy-path cases never walk.

    Each one is a sentence somebody on a shop floor has to be able to act on,
    so each is asserted by its wording rather than by its exception type.
    """
    try:
        hmi_sheet.read_sheet(write(tmp, "k.csv", "machine,date,shots\n"),
                             "hmi-week")
        check("an unknown sheet kind is refused", False, "it was accepted")
    except hmi_sheet.HmiSheetError as exc:
        check("an unknown sheet kind is refused",
              "not a sheet kind" in str(exc), f"said {exc!r}")

    try:
        hmi_sheet.read_sheet(os.path.join(tmp, "nope.csv"), "hmi-day")
        check("a missing file is refused", False, "it was accepted")
    except hmi_sheet.HmiSheetError as exc:
        check("a missing file is refused",
              "does not exist" in str(exc), f"said {exc!r}")

    try:
        hmi_sheet.read_sheet(
            write(tmp, "e.csv",
                  "machine,date,hour,shots\nIMM-01,2026-10-09,,5\n"),
            "hmi-hour")
        check("an empty hour is refused, not read as midnight", False,
              "it was accepted")
    except hmi_sheet.HmiSheetError as exc:
        check("an empty hour is refused, not read as midnight",
              "is empty" in str(exc), f"said {exc!r}")

    db, made = fresh()
    stranger = models.Machine(tenant_code="SOMEONE-ELSE", name="IMM-01",
                              status="running")
    db.add(stranger)
    db.commit()
    path = write(tmp, "x.csv", "machine,date,shots\nIMM-01,2026-10-09,100\n")
    try:
        hmi_sheet.import_sheet(db, TENANT, {"IMM-01": stranger}, path,
                               "hmi-day")
        check("a sheet cannot be imported into another workspace", False,
              "it was accepted")
    except hmi_sheet.HmiSheetError as exc:
        check("a sheet cannot be imported into another workspace",
              "workspace" in str(exc), f"said {exc!r}")
    db.query(models.Machine).filter(
        models.Machine.tenant_code == "SOMEONE-ELSE").delete()
    db.commit()
    db.close()


def case_the_command_line(tmp):
    """The CLI is a real entry point, with a refusal path people will hit.

    It takes the tenant as an argument and hardcodes none -- AMP is a
    multi-tenant platform, and the plant that happened to need this first is
    still just one tenant. Pinned here so a later convenience cannot quietly
    bake one in.

    THE SESSION IS PASSED IN, and that is the point of `db` being a parameter.
    This case used to let main() open its own and trust it to see machines
    another session had just committed. It held standalone and failed the
    coverage job -- "HMITEST has no machines" from a query moments after two
    were committed, in a process where a thousand tests share one SQLite file.
    That interaction belongs to the harness; everything main() is responsible
    for is exercised here regardless of it.
    """
    db, made = fresh()
    machine_id = made["IMM-01"].id
    path = write(tmp, "cli.csv",
                 "machine,date,shots,kwh\nIMM-01,2026-10-09,5000,90.0\n")

    rc = hmi_sheet.main(["--tenant", TENANT, "--kind", "hmi-day",
                         "--file", path], db=db)
    check("the CLI imports a sheet", rc == 0, f"exited {rc}")

    rows = rows_for(db, machine_id)
    check("and the row is actually there",
          len(rows) == 1 and rows[0].total_count == 5000, f"{len(rows)} rows")

    rc = hmi_sheet.main(["--tenant", "NO-SUCH-TENANT", "--kind", "hmi-day",
                         "--file", path], db=db)
    check("a workspace with no machines is refused, not invented", rc == 1,
          f"exited {rc}")

    bad = write(tmp, "bad-cli.csv", "machine,date\nIMM-01,2026-10-09\n")
    rc = hmi_sheet.main(["--tenant", TENANT, "--kind", "hmi-day",
                         "--file", bad], db=db)
    check("a malformed sheet makes the CLI exit non-zero", rc == 1,
          f"exited {rc}")

    # The session was LENT, not surrendered: a caller that passes one keeps it.
    check("a borrowed session is left open for its owner",
          db.is_active and rows_for(db, machine_id) is not None)
    db.close()


def main():
    Base.metadata.create_all(bind=engine)
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "_hmi_sheet_tmp")
    os.makedirs(tmp, exist_ok=True)
    try:
        print("the reader refuses rather than guesses")
        case_reader(tmp)
        case_blank_kwh_is_not_zero(tmp)
        case_zero_shots_are_not_stored(tmp)
        print("\none machine-day has exactly one source")
        case_a_better_source_replaces_a_weaker_one(tmp)
        case_a_transcription_never_deletes_the_controllers_own_export(tmp)
        case_rank_puts_an_unknown_source_last(tmp)
        case_a_weaker_source_never_overwrites_a_better_one(tmp)
        case_precedence_only_touches_the_same_machine_and_day(tmp)
        case_reimporting_the_same_sheet_changes_nothing(tmp)
        case_a_machine_is_never_invented(tmp)
        print("\na day is never drawn as an hour")
        case_a_day_total_is_not_drawn_at_midnight(tmp)
        case_an_hourly_day_still_draws_and_adds_up(tmp)
        case_a_mixed_plant_keeps_both_honest(tmp)
        case_hourly_only_is_what_does_it()
        print("\nthe refusals, and the command line")
        case_the_remaining_refusals(tmp)
        case_the_command_line(tmp)
    finally:
        for f in os.listdir(tmp):
            os.remove(os.path.join(tmp, f))
        os.rmdir(tmp)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("all checks passed")
    return 0




# ── Collected by pytest as well as run as a script ────────────────────
#
# EVERY CASE ABOVE IS NAMED case_*, NOT test_*, AND ON PURPOSE. They take a
# `tmp` argument, and pytest resolves a test function's arguments as FIXTURES --
# so named test_* they collect as 13 errors reading "fixture 'tmp' not found"
# while `python test_hmi_sheet.py` passes all 44 checks. The suite looked green
# locally and failed the coverage job, which runs every backend suite in one
# pytest process. See docs/TESTING.md and test_arico_import.py, which is laid
# out the same way for the same reason.
def test_everything():
    """The whole suite as one case, failing with whatever it recorded."""
    code = None
    try:
        code = main()
    except SystemExit as exc:
        code = exc.code
    assert not FAILURES, "\n  " + "\n  ".join(str(f) for f in FAILURES)
    assert code in (0, None), f"the suite exited with {code}"


if __name__ == "__main__":
    sys.exit(main())
