"""Read an ARICO HOUR PROD. export into AMP's production records.

WHAT THIS READS. The controller's own HOUR PROD. page has an EXPORT DATA button
that writes two CSVs to a USB stick, named <YYMMDD>Q1.CSV and <YYMMDD>Q2.CSV:

    Q1   shots  per hour
    Q2   kWh    per hour

Both have the same shape -- one row per day, 24 hourly columns, a Day Total and
four Reserved columns -- and both carry the controller's WHOLE history, not just
the day in the filename. A single export off one machine produced 365 days:
1,316,137 shots and 25,133 kWh, hour by hour, going back fourteen months.

UTF-16-LE, which is why a naive open() sees every character spaced out. The
header is detected rather than assumed, so a firmware that writes UTF-8 or adds
a BOM still parses.

WHY THE EXPORT AND NOT A LIVE READ. This controller has no Ethernet, and whether
its serial port speaks a protocol anyone else can read is still unknown. The USB
export needs none of that: it already exists, it is already complete, and it
costs two button presses. A plant gets its history on day one instead of waiting
for a gateway, and the live feed -- when it arrives -- writes the same records.

RE-IMPORTING IS SAFE, AND THAT IS NOT OPTIONAL. Every export contains the whole
year, so the second day's export re-delivers 365 days AMP already has. Each hour
is keyed by a deterministic source_record_id and protected by the existing
UNIQUE (tenant_code, source_record_id) that production_records already carries
for gateway retries -- the same guard, for the same reason. An hour already
stored is UPDATED, never inserted again, so a day exported at noon and again at
six o'clock ends up with the evening's fuller numbers rather than two rows.

AN HOUR OF ZERO IS NOT WRITTEN. A controller reports 0 for an hour the machine
did not run and for every hour of a day it was switched off. Storing those would
fill the database with records asserting a machine ran and made nothing, which
is a different claim from "it wasn't running" and would drag every average down.

WHAT IT CANNOT KNOW. These files carry no good/reject split -- the Q.C. page has
TOTAL/PACK/FAIL but the hourly export does not -- so good_count is set equal to
total_count and rejected_count to 0. That is a statement that quality was not
measured per hour, not a claim that nothing was rejected; the machine's own FAIL
counter is a day-level figure and belongs to a different import.
"""
import io
import os
import re
from datetime import datetime, timedelta

import models

# One row per day: a date, 24 hourly buckets, the day's total, then padding.
HOURS = 24
HEADER_MARK = "yy/mm/dd"
DATE = re.compile(r"^\d{4}/\d{2}/\d{2}$")

# The encodings seen in the field, most likely first.
ENCODINGS = ("utf-16", "utf-16-le", "utf-8-sig", "utf-8")


class AricoImportError(Exception):
    """A refusal with a sentence a person can act on."""


def _decode(path):
    """The file's text, whatever the firmware wrote it in."""
    raw = io.open(path, "rb").read()
    if not raw:
        raise AricoImportError(f"{os.path.basename(path)} is empty.")
    for encoding in ENCODINGS:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
        if HEADER_MARK in text:
            return text
    raise AricoImportError(
        f"{os.path.basename(path)} does not look like an ARICO HOUR PROD. "
        f"export: no '{HEADER_MARK}' header in any encoding tried "
        f"({', '.join(ENCODINGS)}).")


def read_export(path):
    """{'2026/10/09': [24 hourly floats]} from one exported CSV.

    The Day Total column is deliberately ignored. It disagrees with the sum of
    the 24 hours on most rows -- by a median of one shot, worst five out of four
    thousand -- because a cycle straddling an hour boundary lands in one and not
    the other. The hourly buckets are what AMP stores, so they are what AMP
    reads; taking both would mean choosing which to believe on every row.
    """
    days = {}
    for line in _decode(path).splitlines():
        cells = [c.strip() for c in line.split(",")]
        if not cells or not DATE.match(cells[0]):
            continue
        hours = cells[1:1 + HOURS]
        if len(hours) < HOURS:
            raise AricoImportError(
                f"{os.path.basename(path)}: the row for {cells[0]} has "
                f"{len(hours)} hourly columns, not {HOURS}.")
        try:
            days[cells[0]] = [float(h or 0) for h in hours]
        except ValueError as exc:
            raise AricoImportError(
                f"{os.path.basename(path)}: the row for {cells[0]} has a value "
                f"that is not a number ({exc}).")
    if not days:
        raise AricoImportError(
            f"{os.path.basename(path)} has a header and no day rows.")
    return days


def source_id(machine_name, day, hour):
    """The idempotency key for one machine-hour.

    Deterministic on purpose: the same hour re-exported tomorrow produces the
    same key and updates the row rather than adding one. Prefixed so a figure
    that came off a USB stick can always be told from one a gateway published.
    """
    return f"arico:{machine_name}:{day}:{hour:02d}"


def _as_datetime(day, hour):
    """The hour's START, as a naive local datetime.

    The bucket labelled '09 -> 10' is stamped 09:00. AMP's read-models bucket by
    the hour of created_at, so stamping the start puts it back in the hour it
    came from; stamping the end would shift the whole day forward by one.
    """
    return datetime.strptime(day, "%Y/%m/%d") + timedelta(hours=hour)


def plan(production, energy, machine_name):
    """Every hour worth writing, as plain dicts. Pure: touches no database.

    Hours with no production are skipped (see the module docstring). An hour
    with energy but no shots is skipped too -- that is a heater warming an idle
    machine, which is real but is not production, and writing it as a production
    record of zero would say the machine ran and made nothing.
    """
    rows = []
    for day in sorted(production):
        hours = production[day]
        kwh = energy.get(day) or []
        for hour in range(HOURS):
            made = int(hours[hour])
            if made <= 0:
                continue
            rows.append({
                "source_record_id": source_id(machine_name, day, hour),
                "created_at": _as_datetime(day, hour),
                "total_count": made,
                # No per-hour quality in this export. good == total states that
                # quality was not measured here, not that nothing failed.
                "good_count": made,
                "rejected_count": 0,
                "energy_kwh": (float(kwh[hour]) if hour < len(kwh) else None),
            })
    return rows


def import_export(db, tenant, machine, production_path, energy_path=None,
                  ideal_cycle_time_seconds=0):
    """Write the export into production_records. Returns a summary.

    `machine` is a models.Machine that already belongs to `tenant` -- this never
    creates one, because inventing a machine from a filename is how a workspace
    ends up with two of everything.
    """
    if machine.tenant_code != tenant:
        raise AricoImportError(
            f"machine {machine.name!r} belongs to {machine.tenant_code!r}, not "
            f"{tenant!r}. An export is imported into its own workspace.")

    production = read_export(production_path)
    energy = read_export(energy_path) if energy_path else {}

    missing = sorted(set(production) - set(energy)) if energy else []
    rows = plan(production, energy, machine.name)

    existing = {r.source_record_id: r for r in db.query(models.ProductionRecord)
                .filter(models.ProductionRecord.tenant_code == tenant,
                        models.ProductionRecord.source_record_id.like(
                            f"arico:{machine.name}:%")).all()}

    created = updated = 0
    for row in rows:
        found = existing.get(row["source_record_id"])
        if found is not None:
            found.total_count = row["total_count"]
            found.good_count = row["good_count"]
            found.rejected_count = row["rejected_count"]
            found.energy_kwh = row["energy_kwh"]
            found.created_at = row["created_at"]
            updated += 1
            continue
        db.add(models.ProductionRecord(
            tenant_code=tenant, machine_id=machine.id,
            source_record_id=row["source_record_id"],
            planned_minutes=60, runtime_minutes=60,
            ideal_cycle_time_seconds=int(ideal_cycle_time_seconds or 0),
            total_count=row["total_count"], good_count=row["good_count"],
            rejected_count=row["rejected_count"],
            energy_kwh=row["energy_kwh"], created_at=row["created_at"]))
        created += 1
    db.commit()

    days = sorted(production)
    return {
        "machine": machine.name,
        "days_in_file": len(days),
        "first_day": days[0], "last_day": days[-1],
        "hours_written": created, "hours_updated": updated,
        "shots": sum(r["total_count"] for r in rows),
        "kwh": round(sum(r["energy_kwh"] or 0 for r in rows), 1),
        "days_without_energy": missing,
    }
