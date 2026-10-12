"""Read figures transcribed off an HMI screen into AMP's production records.

WHY THIS EXISTS. Of thirteen presses on the Shrinidhi floor, four can write
their history to a USB stick and the rest cannot. The rest still SHOW it: the
ARICO HOUR PROD. page draws twenty-four hourly bars with the numbers printed
underneath, and its MONTH page prints a day's shots and kWh for every day of the
month. A photograph of those two screens carries a machine's real output. It is
the only way to get the older machines into AMP before a gateway exists, and it
costs a walk round the floor.

TWO SHAPES, AND THEY ARE NOT INTERCHANGEABLE.

    hour   one row per machine-hour   from the HOUR PROD. page
    day    one row per machine-day    from the MONTH page

An hour row is the same thing a gateway publishes and is stored the same way. A
DAY row is not an hour and must never be drawn as one: a day's 5,050 shots
stamped at midnight would draw a single bar at 00:00 claiming the plant made a
day's work in an hour and nothing afterwards. So a day row is written with
planned_minutes=MINUTES_IN_DAY, and the read-models skip anything that is not an
hour when they build an hourly series. The month view, which groups by day,
reads it correctly.

PRECEDENCE, BECAUSE THE SAME DAY CAN ARRIVE TWICE. A machine photographed this
morning and exported to USB this evening delivers the same day twice, in two
shapes. Adding both would double that day's output -- silently, and in the
direction that flatters the plant. So each machine-day has exactly one source,
and a better one replaces a weaker one:

    arico     the controller's own USB export      (best)
    hmi-hour  the HOUR PROD. page, photographed
    hmi-day   the MONTH page, photographed         (weakest)

Importing a stronger source DELETES the weaker rows for the days it covers.
Importing a weaker one over a stronger one writes nothing and says so. This is
not a tidiness rule: it is the only thing standing between a transcription and a
double-counted day.

WHAT A TRANSCRIPTION IS NOT. A figure read off a photograph is the machine's own
measurement, but a human copied the digits. It is kept in its own id namespace
so it can always be told apart from a figure that arrived by wire, and so that
every one of these rows can be deleted the day a gateway replaces them.
"""
import csv
import os
from datetime import datetime, timedelta

import models

MINUTES_IN_HOUR = 60
MINUTES_IN_DAY = 24 * 60

#: Best first. A source may replace anything that appears AFTER it in this list.
PRECEDENCE = ("arico", "hmi-hour", "hmi-day")

HOUR_FIELDS = ("machine", "date", "hour", "shots")
DAY_FIELDS = ("machine", "date", "shots")


class HmiSheetError(Exception):
    """A refusal with a sentence a person can act on."""


def rank(kind):
    """Position in PRECEDENCE. Lower is stronger. Unknown kinds are weakest."""
    return PRECEDENCE.index(kind) if kind in PRECEDENCE else len(PRECEDENCE)


def source_id(kind, machine_name, day, hour=None):
    """The idempotency key for one machine-hour, or one machine-day.

    Deterministic, so re-importing the same sheet updates rows instead of
    adding them. The kind leads so a row's origin is readable from its key and
    so every row from one source can be found with a LIKE.
    """
    stem = f"{kind}:{machine_name}:{day}"
    return stem if hour is None else f"{stem}:{hour:02d}"


def day_prefix(kind, machine_name, day):
    """The key prefix shared by every row of one kind, machine and day."""
    return f"{kind}:{machine_name}:{day}"


def _parse_date(value, where):
    text = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y/%m/%d")
        except ValueError:
            continue
    raise HmiSheetError(
        f"{where}: {text!r} is not a date. Write it as 2026-10-09.")


def _parse_number(value, where, field, allow_blank=False):
    text = (value or "").strip()
    if not text:
        if allow_blank:
            return None
        raise HmiSheetError(f"{where}: {field} is empty.")
    try:
        return float(text)
    except ValueError:
        raise HmiSheetError(
            f"{where}: {field} is {text!r}, which is not a number. A screen "
            f"that was too dark to read is left blank, not guessed.")


def read_sheet(path, kind):
    """Parse a transcription sheet. Pure: touches no database.

    Columns, by kind:
        hmi-hour   machine,date,hour,shots[,kwh]
        hmi-day    machine,date,shots[,kwh]

    `kwh` is optional everywhere and blank means the screen showed no energy --
    which is NOT zero. A machine whose energy column reads 0.0 because its pulse
    constant was never set has not measured zero kilowatt-hours; it has measured
    nothing, and a zero here would be drawn as a bar.
    """
    if kind not in ("hmi-hour", "hmi-day"):
        raise HmiSheetError(
            f"{kind!r} is not a sheet kind. Use 'hmi-hour' or 'hmi-day'.")
    required = HOUR_FIELDS if kind == "hmi-hour" else DAY_FIELDS

    if not os.path.exists(path):
        raise HmiSheetError(f"{path} does not exist.")
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header = [h.strip().lower() for h in (reader.fieldnames or [])]
        missing = [f for f in required if f not in header]
        if missing:
            raise HmiSheetError(
                f"{os.path.basename(path)} is missing the column(s) "
                f"{', '.join(missing)}. It needs {', '.join(required)}.")

        rows = []
        for n, raw in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): v for k, v in raw.items()}
            where = f"{os.path.basename(path)} line {n}"
            machine = (row.get("machine") or "").strip()
            if not machine:
                raise HmiSheetError(f"{where}: machine is empty.")
            day = _parse_date(row.get("date"), where)
            shots = _parse_number(row.get("shots"), where, "shots")
            if shots < 0:
                raise HmiSheetError(
                    f"{where}: shots is {shots:g}. A counter does not go back.")
            kwh = _parse_number(row.get("kwh"), where, "kwh", allow_blank=True)
            if kwh is not None and kwh < 0:
                raise HmiSheetError(
                    f"{where}: kwh is {kwh:g}. A meter does not go back.")

            entry = {"machine": machine, "date": day,
                     "shots": int(shots), "kwh": kwh}
            if kind == "hmi-hour":
                hour = _parse_number(row.get("hour"), where, "hour")
                if hour != int(hour) or not 0 <= hour <= 23:
                    raise HmiSheetError(
                        f"{where}: hour is {hour:g}. The page has hours 0..23.")
                entry["hour"] = int(hour)
            rows.append(entry)

    if not rows:
        raise HmiSheetError(
            f"{os.path.basename(path)} has a header and no rows.")
    return rows


def plan(rows, kind):
    """Every row worth writing, as production-record fields. Pure.

    An entry of zero shots is dropped. The controller prints 0 for an hour the
    machine did not run and for every hour of a day it stood idle; storing those
    would assert the machine ran and made nothing, which is a different claim
    from "it was not running" and drags every average down.
    """
    out = []
    for row in rows:
        if row["shots"] <= 0:
            continue
        if kind == "hmi-hour":
            stamp = (datetime.strptime(row["date"], "%Y/%m/%d")
                     + timedelta(hours=row["hour"]))
            minutes = MINUTES_IN_HOUR
            sid = source_id(kind, row["machine"], row["date"], row["hour"])
        else:
            stamp = datetime.strptime(row["date"], "%Y/%m/%d")
            minutes = MINUTES_IN_DAY
            sid = source_id(kind, row["machine"], row["date"])
        out.append({
            "source_record_id": sid,
            "machine": row["machine"],
            "date": row["date"],
            "created_at": stamp,
            "planned_minutes": minutes,
            "runtime_minutes": minutes,
            "total_count": row["shots"],
            # These pages carry no good/reject split -- the Q.C.1 page has
            # TOTAL/PACK/FAIL but it is a running counter, not a per-day one.
            # good == total states that quality was not measured in this window,
            # not that nothing was rejected.
            "good_count": row["shots"],
            "rejected_count": 0,
            "energy_kwh": row["kwh"],
        })
    return out


def supersede(db, tenant, machine_name, days, kind):
    """Delete rows for `days` that a source of `kind` outranks. Returns the count.

    Called before writing, so the stronger source replaces the weaker one rather
    than adding to it. A source never deletes its own rows -- those are updated
    in place -- and never touches a source that outranks it.
    """
    weaker = [k for k in PRECEDENCE if rank(k) > rank(kind)]
    if not weaker or not days:
        return 0
    removed = 0
    for day in days:
        for k in weaker:
            q = db.query(models.ProductionRecord).filter(
                models.ProductionRecord.tenant_code == tenant,
                models.ProductionRecord.source_record_id.like(
                    day_prefix(k, machine_name, day) + "%"))
            for row in q.all():
                db.delete(row)
                removed += 1
    return removed


def blocked_days(db, tenant, machine_name, days, kind):
    """The days already held by a source that OUTRANKS `kind`.

    A photograph must not overwrite the controller's own export. Rather than
    silently dropping those days, the caller reports them: a person who
    photographed a machine they had already exported deserves to be told that
    the better figures were kept.
    """
    stronger = [k for k in PRECEDENCE if rank(k) < rank(kind)]
    held = []
    for day in days:
        for k in stronger:
            if db.query(models.ProductionRecord).filter(
                    models.ProductionRecord.tenant_code == tenant,
                    models.ProductionRecord.source_record_id.like(
                        day_prefix(k, machine_name, day) + "%")).first():
                held.append(day)
                break
    return held


def import_sheet(db, tenant, machines, path, kind, ideal_cycle_time_seconds=0):
    """Write a transcription sheet into production_records. Returns a summary.

    `machines` maps a machine name to a models.Machine that already belongs to
    `tenant`. This never creates one: inventing a machine from a spreadsheet
    cell is how a workspace ends up with two of everything.
    """
    rows = plan(read_sheet(path, kind), kind)

    unknown = sorted({r["machine"] for r in rows} - set(machines))
    if unknown:
        raise HmiSheetError(
            f"{', '.join(unknown)} is not a machine in {tenant}. Add the "
            f"machine first, or correct the name in the sheet.")
    for name, machine in machines.items():
        if machine.tenant_code != tenant:
            raise HmiSheetError(
                f"machine {name!r} belongs to {machine.tenant_code!r}, not "
                f"{tenant!r}. A sheet is imported into its own workspace.")

    by_machine = {}
    for row in rows:
        by_machine.setdefault(row["machine"], []).append(row)

    created = updated = replaced = 0
    skipped = {}
    written = []
    for name, machine_rows in sorted(by_machine.items()):
        machine = machines[name]
        days = sorted({r["date"] for r in machine_rows})

        held = blocked_days(db, tenant, name, days, kind)
        if held:
            skipped[name] = held
            machine_rows = [r for r in machine_rows if r["date"] not in held]
            days = [d for d in days if d not in held]
        if not machine_rows:
            continue
        written.extend(machine_rows)

        replaced += supersede(db, tenant, name, days, kind)

        existing = {r.source_record_id: r for r in
                    db.query(models.ProductionRecord).filter(
                        models.ProductionRecord.tenant_code == tenant,
                        models.ProductionRecord.source_record_id.like(
                            f"{kind}:{name}:%")).all()}
        for row in machine_rows:
            found = existing.get(row["source_record_id"])
            if found is not None:
                found.total_count = row["total_count"]
                found.good_count = row["good_count"]
                found.rejected_count = row["rejected_count"]
                found.energy_kwh = row["energy_kwh"]
                found.created_at = row["created_at"]
                found.planned_minutes = row["planned_minutes"]
                found.runtime_minutes = row["runtime_minutes"]
                updated += 1
                continue
            db.add(models.ProductionRecord(
                tenant_code=tenant, machine_id=machine.id,
                source_record_id=row["source_record_id"],
                planned_minutes=row["planned_minutes"],
                runtime_minutes=row["runtime_minutes"],
                ideal_cycle_time_seconds=int(ideal_cycle_time_seconds or 0),
                total_count=row["total_count"], good_count=row["good_count"],
                rejected_count=row["rejected_count"],
                energy_kwh=row["energy_kwh"], created_at=row["created_at"]))
            created += 1
    db.commit()

    # Counted over the rows that were actually stored, NOT over the sheet. A
    # day held back by a better source contributes nothing here: reporting the
    # sheet's own totals would tell a person their plant made shots that AMP
    # deliberately did not record.
    return {
        "kind": kind,
        "machines": sorted(by_machine),
        "rows_written": created,
        "rows_updated": updated,
        "rows_replaced": replaced,
        "shots": sum(r["total_count"] for r in written),
        "kwh": round(sum(r["energy_kwh"] or 0 for r in written), 1),
        "rows_without_energy": sum(1 for r in written
                                   if r["energy_kwh"] is None),
        "days_kept_from_a_better_source": skipped,
    }


def main(argv=None, db=None):
    """Import a sheet from the command line, into any workspace.

    Deliberately takes the tenant as an argument and hardcodes none: AMP is a
    multi-tenant platform and a plant that happens to be the first to need this
    is still just one tenant.

        python backend/hmi_sheet.py --tenant SHRINIDHI --kind hmi-day \
            --file backend/data/shrinidhi-2026-10-day.csv

    `db` is for the caller that already has a session -- a test, or a script
    importing several sheets in one transaction. Left None it opens its own and
    closes it, which is what the command line does.

    WHY IT IS A PARAMETER AT ALL, and the answer is not "for tidiness".

    This used to resolve `database.SessionLocal` itself, at call time. Under
    `python test_hmi_sheet.py` that is fine. In the coverage job, where every
    backend suite shares one process, it reported "HMITEST has no machines" from
    a query run moments after two were committed.

    The cause is that SEVERAL SUITES PERMANENTLY REBIND `database.SessionLocal`
    to a sessionmaker on their own engine and never put it back --
    test_connected_equipment.py and test_demo_reset_repeatable.py among them,
    both of which sort before test_hmi_sheet.py. A module that captured the
    factory by value at import time (`from database import SessionLocal`) keeps
    the real one; a module that looks up `database.SessionLocal` when it runs
    gets whichever suite last hijacked it. So the test created machines in one
    database and this function queried another.

    Taking the session as a parameter removes the lookup, and with it the
    question of which database a caller meant. Everything this function is
    actually responsible for -- the arguments, the refusals, the exit code, the
    summary -- is testable directly. Any module that resolves
    `database.SessionLocal` at call time has the same exposure.
    """
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tenant", required=True, help="workspace code")
    ap.add_argument("--kind", required=True, choices=("hmi-day", "hmi-hour"),
                    help="which screen the figures were read off")
    ap.add_argument("--file", required=True, help="the transcription sheet")
    ap.add_argument("--cycle-seconds", type=int, default=0,
                    help="ideal cycle time, if the controller showed one")
    args = ap.parse_args(argv)

    owned = db is None
    if owned:
        import database
        db = database.SessionLocal()
    try:
        machines = {m.name: m for m in db.query(models.Machine)
                    .filter(models.Machine.tenant_code == args.tenant).all()}
        if not machines:
            print(f"{args.tenant} has no machines. Seed the workspace first.")
            return 1
        out = import_sheet(db, args.tenant, machines, args.file, args.kind,
                           ideal_cycle_time_seconds=args.cycle_seconds)
    except HmiSheetError as exc:
        print(f"refused: {exc}")
        return 1
    finally:
        if owned:
            db.close()

    print(f"{out['kind']}: {', '.join(out['machines'])}")
    print(f"  wrote {out['rows_written']}, updated {out['rows_updated']}, "
          f"replaced {out['rows_replaced']}")
    print(f"  {out['shots']:,} shots, {out['kwh']:,} kWh, "
          f"{out['rows_without_energy']} rows with no energy measured")
    for machine, days in sorted(out["days_kept_from_a_better_source"].items()):
        print(f"  {machine}: {len(days)} day(s) kept from a better source "
              f"-- the controller's own export already covers them")
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())
