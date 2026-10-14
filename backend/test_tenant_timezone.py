"""The board buckets a day and its hours in the PLANT's local time, not UTC.

WHY THIS EXISTS. ProductionRecord.created_at is stored in UTC. A plant in
Asia/Kolkata (UTC+5:30) that ran a shot at 05:37 local saw it land in HOUR 0 of
the board -- 12:00 on the loading screen -- because the hour was read straight
off the UTC timestamp (00:07). The board now reads TenantConfig.timezone and
buckets BOTH the day window and each hour in local time, so 05:37 local shows in
hour 5, and a shot just after local midnight counts on the right day.

A tenant with NO timezone set keeps the old behaviour exactly: tenant_zone()
falls back to UTC, so every existing board and test is unchanged. A garbage
timezone must fall back too, never 500 the board.

Run: DATABASE_URL="sqlite:///./ci_tz.db" python backend/test_tenant_timezone.py
"""
import os
import sys
from datetime import date, datetime

os.environ.setdefault("DATABASE_URL", "sqlite:///./ci_tz.db")

import database  # noqa: E402
import models    # noqa: E402
from ai import plant_board  # noqa: E402

failures = []
T = "TZ-TEST"


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def _wipe(db):
    for M in (models.ProductionRecord, models.Machine):
        db.query(M).filter(M.tenant_code == T).delete()
    db.query(models.TenantConfig).filter(models.TenantConfig.tenant_code == T).delete()
    db.commit()


def _tenant(db, tz):
    db.add(models.TenantConfig(tenant_code=T, timezone=tz))
    m = models.Machine(tenant_code=T, site="", name="IMM-TZ", status="Running")
    db.add(m)
    db.commit()
    return m


def _record(db, m, when_utc, shots):
    db.add(models.ProductionRecord(
        tenant_code=T, machine_id=m.id, total_count=shots,
        good_count=shots, rejected_count=0,
        planned_minutes=60, runtime_minutes=55, ideal_cycle_time_seconds=14,
        created_at=when_utc))
    db.commit()


def _hours(board):
    """hour -> shots, from the first machine's hourly points."""
    return {p["hour"]: p["shots"] for p in board["production"][0]["points"]}


def test_hours_bucket_in_local_time():
    db = database.SessionLocal()
    try:
        _wipe(db)
        m = _tenant(db, "Asia/Kolkata")
        # 00:07 UTC == 05:37 IST. The reported bug put this in hour 0 (12:00).
        _record(db, m, datetime(2026, 10, 7, 0, 7, 0), 100)
        hours = _hours(plant_board.day(db, T, date(2026, 10, 7)))
        check("a 05:37-local shot buckets into local hour 5, not UTC hour 0",
              hours.get(5) == 100 and hours.get(0, 0) == 0, str(hours))
    finally:
        _wipe(db)
        db.close()


def test_day_window_is_the_local_day():
    db = database.SessionLocal()
    try:
        _wipe(db)
        m = _tenant(db, "Asia/Kolkata")
        # 2026-10-06 20:00 UTC == 2026-10-07 01:30 IST -> belongs to IST Oct 7.
        _record(db, m, datetime(2026, 10, 6, 20, 0, 0), 50)
        on7 = _hours(plant_board.day(db, T, date(2026, 10, 7)))
        on6 = _hours(plant_board.day(db, T, date(2026, 10, 6)))
        check("a 20:00-UTC shot counts on the local day it falls in (IST Oct 7, hour 1)",
              on7.get(1) == 50, str(on7))
        check("...and NOT on the previous local day (IST Oct 6)",
              sum(on6.values()) == 0, str(on6))
    finally:
        _wipe(db)
        db.close()


def test_no_timezone_falls_back_to_utc():
    db = database.SessionLocal()
    try:
        _wipe(db)
        m = _tenant(db, None)                       # NULL timezone
        _record(db, m, datetime(2026, 10, 7, 0, 7, 0), 100)
        hours = _hours(plant_board.day(db, T, date(2026, 10, 7)))
        check("no timezone set -> a 00:07-UTC shot stays in UTC hour 0 (unchanged)",
              hours.get(0) == 100 and hours.get(5, 0) == 0, str(hours))
    finally:
        _wipe(db)
        db.close()


def test_an_unknown_timezone_is_not_fatal():
    db = database.SessionLocal()
    try:
        _wipe(db)
        m = _tenant(db, "Mars/Olympus")             # not a real zone
        _record(db, m, datetime(2026, 10, 7, 0, 7, 0), 100)
        # Must not raise; falls back to UTC.
        hours = _hours(plant_board.day(db, T, date(2026, 10, 7)))
        check("a garbage timezone falls back to UTC rather than 500ing the board",
              hours.get(0) == 100, str(hours))
    finally:
        _wipe(db)
        db.close()


def main():
    models.Base.metadata.create_all(bind=database.engine)
    test_hours_bucket_in_local_time()
    test_day_window_is_the_local_day()
    test_no_timezone_falls_back_to_utc()
    test_an_unknown_timezone_is_not_fatal()
    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}")
        sys.exit(1)
    print("tenant-timezone OK")


if __name__ == "__main__":
    main()
