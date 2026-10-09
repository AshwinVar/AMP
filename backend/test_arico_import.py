"""A year of a machine's own history, read off a USB stick without losing a shot.

BUILT AGAINST A REAL EXPORT. An ARICO AR1260 on a customer's floor wrote two
CSVs from its HOUR PROD. page: 365 days of hourly shots and hourly kWh, going
back fourteen months, in UTF-16-LE. The fixtures here are that file's shape and
its real numbers, because the things that break an importer -- the encoding, the
Day Total that disagrees with its own hours, the zero-filled night -- are
properties of the actual file and not of one I would have invented.

WHAT IS PINNED
  * UTF-16 is read. A naive open() sees every character spaced out and finds no
    rows at all, which looks like an empty file rather than a wrong encoding.
  * RE-IMPORTING IS SAFE. Every export carries the WHOLE history, so the second
    day's export re-delivers a year AMP already has. Twice imported must mean
    once stored.
  * An hour of zero is not written. A controller reports 0 for every hour of a
    day the machine was off; storing those asserts it ran and made nothing.
  * NULL energy is not zero energy. An hour nobody metered must come back as an
    absence, and a window where nothing was metered must make the whole series
    unavailable rather than a flat line on the axis.
  * The Day Total column is ignored, deliberately: it disagrees with the sum of
    its own 24 hours on most rows.

Run: DATABASE_URL="sqlite:///./ci_arico.db" python backend/test_arico_import.py
"""
import io
import os
import sys
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("DATABASE_URL", "sqlite:///./ci_arico.db")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import arico_import  # noqa: E402
import database      # noqa: E402
import models        # noqa: E402
from ai import plant_board  # noqa: E402

failures = []
T = "ARICO-TEST"
TMP = os.path.join(HERE, "_arico_fixture")

HEADER = ("yy/mm/dd," + ",".join(f"{h:02d} -> {h + 1:02d}" for h in range(24))
          + ",Day Total,Reserved,Reserved,Reserved,Reserved,")


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def section(t):
    print("\n" + "=" * 74 + f"\n{t}\n" + "=" * 74)


def write(name, rows, encoding="utf-16"):
    """One export file. `rows` is {date: [24 values]}; the Day Total is written
    DELIBERATELY WRONG so a reader that trusts it is caught."""
    os.makedirs(TMP, exist_ok=True)
    lines = [HEADER]
    for day, hours in rows.items():
        wrong_total = sum(hours) + 7          # the controller's own disagreement
        cells = [day] + [f"{v:.1f}" for v in hours] + [f"{wrong_total:.1f}"]
        lines.append(",".join(cells) + ",0.0,0.0,0.0,0.0,")
    path = os.path.join(TMP, name)
    io.open(path, "w", encoding=encoding, newline="\r\n").write("\n".join(lines) + "\n")
    return path


def wipe(db):
    for M in (models.ProductionRecord, models.Machine):
        db.query(M).filter(M.tenant_code == T).delete()
    db.commit()


def main():
    models.Base.metadata.create_all(bind=database.engine)
    db = database.SessionLocal()
    try:
        wipe(db)
        m = models.Machine(tenant_code=T, site="", name="IMM-01", status="Idle")
        db.add(m)
        db.commit()

        # A worked day, a day the machine was off, and a half day.
        full = [206.0, 204.0, 210.0] + [208.0] * 21
        off = [0.0] * 24
        half = [180.0] * 6 + [0.0] * 18
        prod = write("Q1.CSV", {"2026/10/07": full, "2026/10/08": off,
                                "2026/10/09": half})
        kwh = write("Q2.CSV", {"2026/10/07": [3.8] * 24, "2026/10/08": [0.0] * 24,
                               "2026/10/09": [3.6] * 6 + [0.0] * 18})

        section("1. THE FILE IS UTF-16, AND IT IS READ")
        days = arico_import.read_export(prod)
        check("all three day rows are found", sorted(days) ==
              ["2026/10/07", "2026/10/08", "2026/10/09"], str(sorted(days)))
        check("...with 24 hourly columns each",
              all(len(v) == 24 for v in days.values()),
              str([len(v) for v in days.values()]))
        check("a plain UTF-8 export reads too (firmware differs)",
              len(arico_import.read_export(
                  write("U8.CSV", {"2026/10/07": full}, encoding="utf-8"))) == 1)

        section("2. THE DAY TOTAL IS IGNORED, BECAUSE IT DISAGREES WITH ITS OWN HOURS")
        # The fixture writes a Day Total 7 higher than the hours sum. On the real
        # export the two disagree on 255 of 365 rows.
        rows = arico_import.plan(days, {}, "IMM-01")
        day7 = sum(r["total_count"] for r in rows if r["created_at"].day == 7)
        check("the hours are summed, not the stated total",
              day7 == int(sum(full)), f"{day7} vs hours {int(sum(full))} "
                                      f"vs stated {int(sum(full)) + 7}")

        section("3. AN HOUR OF ZERO IS NOT A RECORD")
        check("the day the machine was off writes nothing",
              not [r for r in rows if r["created_at"].day == 8],
              str([r["created_at"] for r in rows if r["created_at"].day == 8]))
        check("...and a half day writes only the hours that ran",
              len([r for r in rows if r["created_at"].day == 9]) == 6,
              str(len([r for r in rows if r["created_at"].day == 9])))

        section("4. THE HOUR IS STAMPED AT ITS START")
        first = min(rows, key=lambda r: r["created_at"])
        check("the '00 -> 01' bucket is stamped 00:00, not 01:00",
              first["created_at"] == datetime(2026, 10, 7, 0, 0),
              str(first["created_at"]))

        section("5. IMPORT, THEN IMPORT AGAIN")
        out = arico_import.import_export(db, T, m, prod, kwh, ideal_cycle_time_seconds=20)
        check("every producing hour is written",
              out["hours_written"] == 24 + 6, str(out["hours_written"]))
        check("...and the shots are the sum of the hours",
              out["shots"] == int(sum(full)) + int(sum(half)), str(out["shots"]))
        check("...and the kWh come from the second file",
              out["kwh"] == round(3.8 * 24 + 3.6 * 6, 1), str(out["kwh"]))

        before = db.query(models.ProductionRecord).filter(
            models.ProductionRecord.tenant_code == T).count()
        again = arico_import.import_export(db, T, m, prod, kwh)
        after = db.query(models.ProductionRecord).filter(
            models.ProductionRecord.tenant_code == T).count()
        check("A SECOND IMPORT OF THE SAME YEAR ADDS NOTHING",
              after == before, f"{before} -> {after}")
        check("...it updates instead", again["hours_updated"] == before
              and again["hours_written"] == 0, str(again))

        section("6. A LATER EXPORT OF THE SAME DAY CORRECTS IT")
        fuller = write("Q1b.CSV", {"2026/10/09": [180.0] * 12 + [0.0] * 12})
        arico_import.import_export(db, T, m, fuller, None)
        ninth = (db.query(models.ProductionRecord)
                   .filter(models.ProductionRecord.tenant_code == T,
                           models.ProductionRecord.created_at >= datetime(2026, 10, 9))
                   .count())
        check("the day grows from 6 hours to 12, not to 18", ninth == 12, str(ninth))

        section("7. NULL ENERGY IS NOT ZERO ENERGY")
        b = plant_board.day(db, T, date(2026, 10, 7))
        check("a metered day draws a real power series", b["power"]["available"])
        check("...with the hours it measured",
              b["power"]["total"] == round(3.8 * 24, 2), str(b["power"]["total"]))

        # Now a machine whose export carried no energy file at all.
        wipe(db)
        m2 = models.Machine(tenant_code=T, site="", name="IMM-02", status="Idle")
        db.add(m2)
        db.commit()
        arico_import.import_export(db, T, m2, prod, None)      # production only
        b2 = plant_board.day(db, T, date(2026, 10, 7))
        check("a machine that measured NO energy reports unavailable",
              b2["power"]["available"] is False, str(b2["power"]))
        check("...and carries no points that could be drawn as zero",
              b2["power"]["points"] == [], str(b2["power"]["points"]))
        check("...while its production is still counted",
              b2["production"][0]["total"] == int(sum(full)),
              str(b2["production"][0]["total"]))
        check("...and it names the reason and the fix",
              bool(b2["power"]["reason"]) and bool(b2["power"]["fix"]))

        section("8. REFUSALS")
        bad = os.path.join(TMP, "notanexport.CSV")
        io.open(bad, "w", encoding="utf-8").write("hello,world\n1,2\n")
        try:
            arico_import.read_export(bad)
            check("a file that is not an export is refused", False, "accepted")
        except arico_import.AricoImportError as e:
            check("a file that is not an export is refused",
                  "HOUR PROD" in str(e), str(e))
        empty = os.path.join(TMP, "empty.CSV")
        io.open(empty, "w", encoding="utf-8").write("")
        try:
            arico_import.read_export(empty)
            check("an empty file is refused", False, "accepted")
        except arico_import.AricoImportError as e:
            check("an empty file is refused", "empty" in str(e).lower(), str(e))

        other = models.Machine(tenant_code="SOMEONE-ELSE", site="", name="THEIRS",
                               status="Idle")
        db.add(other)
        db.commit()
        try:
            arico_import.import_export(db, T, other, prod, None)
            check("an export cannot be imported into another workspace", False, "accepted")
        except arico_import.AricoImportError as e:
            check("an export cannot be imported into another workspace",
                  "workspace" in str(e), str(e))
        db.query(models.Machine).filter(
            models.Machine.tenant_code == "SOMEONE-ELSE").delete()
        db.commit()

        section("9. AN UNSCALED METER IS NOT A MEASUREMENT OF ZERO")
        # FROM THE FLOOR, NOT INVENTED. IMM-12's own USB export covers 365
        # days, 1,834,320 shots and exactly 0.0 kWh -- not one non-zero hour in
        # fourteen months, from a press that never stopped. Its MASTER 1 page
        # shows ENERGY PULSE KWh = 000.0: the controller counts meter pulses
        # and multiplies them by nothing. Stored as measured zeros, those rows
        # draw a flat line along the axis of the power chart and tell a moulder
        # his busiest machine runs for free.
        wipe(db)
        m12 = models.Machine(tenant_code=T, site="", name="IMM-12", status="Idle")
        db.add(m12)
        db.commit()
        unscaled = write("Z1.CSV", {"2026/10/09": [257.0] * 15 + [0.0] * 9})
        zeros = write("Z2.CSV", {"2026/10/09": [0.0] * 24})
        out = arico_import.import_export(db, T, m12, unscaled, zeros)

        rows = db.query(models.ProductionRecord).filter(
            models.ProductionRecord.tenant_code == T,
            models.ProductionRecord.machine_id == m12.id).all()
        check("the shots are imported", len(rows) == 15, f"{len(rows)} rows")
        check("and not one hour claims a measured zero",
              all(r.energy_kwh is None for r in rows),
              f"{sum(1 for r in rows if r.energy_kwh == 0)} hours stored 0.0")
        check("the summary says nothing was metered",
              out["hours_metered"] == 0 and out["hours_unmetered"] == 15,
              f"metered={out['hours_metered']} unmetered={out['hours_unmetered']}")
        check("even though the energy file covered every day",
              out["days_without_energy"] == [],
              f"reported {out['days_without_energy']}")

        # A machine that IS scaled must be unaffected -- the rule is about
        # zeros, not about energy files.
        wipe(db)
        m11 = models.Machine(tenant_code=T, site="", name="IMM-11", status="Idle")
        db.add(m11)
        db.commit()
        ran = write("Y1.CSV", {"2026/10/09": [225.0] * 16 + [0.0] * 8})
        drew = write("Y2.CSV", {"2026/10/09": [3.3] * 16 + [0.0] * 8})
        out = arico_import.import_export(db, T, m11, ran, drew)
        rows = db.query(models.ProductionRecord).filter(
            models.ProductionRecord.tenant_code == T,
            models.ProductionRecord.machine_id == m11.id).all()
        check("a metered machine keeps every one of its readings",
              out["hours_metered"] == 16 and out["hours_unmetered"] == 0,
              f"metered={out['hours_metered']}")
        check("and its kWh survive unchanged",
              sorted({round(r.energy_kwh, 1) for r in rows}) == [3.3],
              f"stored {sorted({r.energy_kwh for r in rows})}")

        section("10. THE EXPORT REPLACES THE PHOTOGRAPH OF THE SAME DAY")
        # A machine photographed in the morning and exported in the evening
        # delivers the same day twice, in two shapes. Stored together they ADD,
        # and the error runs upward -- nobody audits the number that says they
        # had a good day. The export is the better source, so it wins outright.
        wipe(db)
        m = models.Machine(tenant_code=T, site="", name="IMM-11", status="Idle")
        db.add(m)
        db.commit()
        for hour in range(16):
            db.add(models.ProductionRecord(
                tenant_code=T, machine_id=m.id,
                source_record_id=f"hmi-hour:IMM-11:2026/10/09:{hour:02d}",
                planned_minutes=60, runtime_minutes=60,
                ideal_cycle_time_seconds=16, total_count=222, good_count=222,
                rejected_count=0, energy_kwh=3.4,
                created_at=datetime(2026, 10, 9, hour)))
        db.commit()
        before = db.query(models.ProductionRecord).filter(
            models.ProductionRecord.tenant_code == T).count()
        check("the transcription is in place first", before == 16, f"{before} rows")

        p1 = write("X1.CSV", {"2026/10/09": [225.0] * 16 + [0.0] * 8})
        p2 = write("X2.CSV", {"2026/10/09": [3.3] * 16 + [0.0] * 8})
        out = arico_import.import_export(db, T, m, p1, p2)

        rows = db.query(models.ProductionRecord).filter(
            models.ProductionRecord.tenant_code == T).all()
        check("the day is not counted twice", len(rows) == 16, f"{len(rows)} rows")
        check("every surviving row came from the export",
              all((r.source_record_id or "").startswith("arico:") for r in rows),
              f"{[r.source_record_id for r in rows if not (r.source_record_id or '').startswith('arico:')][:2]}")
        check("and the import says how many it replaced",
              out["transcribed_rows_replaced"] == 16,
              f"reported {out['transcribed_rows_replaced']}")
        check("so the day totals the exported figure, not the sum of both",
              sum(r.total_count for r in rows) == 3600,
              f"totals {sum(r.total_count for r in rows)}")

        wipe(db)
    finally:
        db.close()
        for name in os.listdir(TMP) if os.path.isdir(TMP) else []:
            os.remove(os.path.join(TMP, name))
        if os.path.isdir(TMP):
            os.rmdir(TMP)


# ── Collected by pytest as well as run as a script ────────────────────
#
# CI's per-file runner (`python test_arico_import.py`) is the contract and is
# unchanged. The coverage job collects every suite into ONE pytest process, and
# pytest only collects module-level `test_*` functions -- without this the code
# this file exercises would count as untested. See docs/TESTING.md.
def test_everything():
    """The whole suite as one case, failing with whatever it recorded."""
    code = None
    try:
        code = main()
    except SystemExit as exc:
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
    print("ALL CHECKS PASSED - ARICO hourly export import")
