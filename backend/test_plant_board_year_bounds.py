"""The plant-board endpoints 500'd on an out-of-range year/date instead of 400.

THE DEFECT
----------
Both board endpoints turn a requested calendar day/month into a UTC window by
subtracting the plant's IST offset (ai.plant_board):

    start = datetime(year, mon, 1) - IST_OFFSET
    end   = datetime(year + (mon == 12), (mon % 12) + 1, 1) - IST_OFFSET   # month
    start = datetime.combine(on, time.min) - IST_OFFSET                    # day

`GET /analytics/plant-board/month` takes `year` as a raw, UNBOUNDED int from the
query string, and `GET /analytics/plant-board` parses `on` with strptime, which
happily accepts `0001-01-01`. At the edges of datetime's representable range that
arithmetic raises, and the exception was uncaught — a bare 500 with a traceback,
where a bad `month` (not 1-12) or a malformed `on` already return a clean 400:

    * year < MINYEAR / year negative / year huge -> datetime(year, ...) ValueError
    * year == MINYEAR (1), month 1               -> datetime(1,1,1) - IST underflows
                                                     datetime.min  (OverflowError)
    * year == MAXYEAR (9999), December           -> datetime(9999+1, 1, 1) overflows
                                                     datetime.max  (ValueError)
    * on == 0001-01-01                           -> the day boundary underflows too

THE FIX (route boundary, not the ai/* read-model)
--------------------------------------------------
analytics_routes._require_board_year rejects a year outside [MINYEAR+1, MAXYEAR-1]
with a 400 before the read-model runs — the same boundary guard `month` 1-12 and
the `on` format check already apply. A real plant's operating dates are nowhere
near these limits, so nothing the board can actually report on is rejected.

WHY THESE NUMBERS, AND NOT A TAUTOLOGY
--------------------------------------
The test pins the bound to datetime's OWN limits and independently proves, with
the exact IST offset the read-model uses, that each rejected year is one the raw
arithmetic really raises on — so the 400s guard genuine 500s, and the accepted
boundary years (2 and 9998) are ones the arithmetic survives.

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_plant_board_year_bounds.py
"""
import os
import sys
from datetime import MAXYEAR, MINYEAR, datetime, time, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite:///./ci.db")

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import analytics_routes  # noqa: E402
import models  # noqa: E402
import tenancy as T  # noqa: E402
from ai.plant_board import IST_OFFSET  # noqa: E402
from database import Base  # noqa: E402

TENANT = "ACME"
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def _fresh_session():
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed(db):
    """One machine with a production record on 2026-10-07 (IST), so the valid
    day/month calls run the real read-model over actual rows."""
    tok = T.set_current_tenant(TENANT)
    try:
        m = models.Machine(name="Press-1", status="Running", utilization=70,
                            tenant_code=TENANT)
        db.add(m)
        db.commit()
        db.refresh(m)
        # 2026-10-07 12:00 IST == 06:30 UTC — squarely inside the IST day.
        db.add(models.ProductionRecord(
            machine_id=m.id, planned_minutes=60, runtime_minutes=55,
            total_count=100, good_count=95, rejected_count=5,
            ideal_cycle_time_seconds=30, tenant_code=TENANT,
            created_at=datetime(2026, 10, 7, 6, 30)))
        db.commit()
    finally:
        T.reset_current_tenant(tok)


CURRENT_USER = {"sub": "acme_admin", "role": "Admin", "tenant": TENANT}


def _call(handler, **kwargs):
    """Invoke a board handler as a bound request for TENANT. Returns the handler
    result, or the HTTPException it raised."""
    bound = T.set_current_tenant(TENANT)
    try:
        return handler(db=_DB, current_user=CURRENT_USER, **kwargs)
    except HTTPException as exc:
        return exc
    finally:
        T.reset_current_tenant(bound)


# ── 1. the raw arithmetic really raises on the rejected years (not a tautology) ──
def test_boundary_years_really_overflow():
    # year below the accepted floor: the IST-offset day boundary underflows datetime.min
    try:
        datetime(MINYEAR, 1, 1) - IST_OFFSET
        check("datetime(MINYEAR,1,1) - IST raises", False, "did not raise")
    except (OverflowError, ValueError):
        check("datetime(MINYEAR,1,1) - IST raises", True)
    # the month view's December boundary at MAXYEAR rolls the year to 10000
    try:
        datetime(MAXYEAR + 1, 1, 1)
        check("datetime(MAXYEAR+1,1,1) raises", False, "did not raise")
    except (OverflowError, ValueError):
        check("datetime(MAXYEAR+1,1,1) raises", True)
    # a negative / huge year is a plain out-of-range datetime
    for bad in (-5, 100000):
        try:
            datetime(bad, 6, 1)
            check(f"datetime(year={bad}) raises", False, "did not raise")
        except (OverflowError, ValueError):
            check(f"datetime(year={bad}) raises", True)
    # the accepted boundary years DON'T raise — so the guard isn't over-tight
    for ok in (analytics_routes._MIN_BOARD_YEAR, analytics_routes._MAX_BOARD_YEAR):
        try:
            datetime(ok, 12, 1) - IST_OFFSET            # month-view Dec start
            datetime(ok + (ok == analytics_routes._MAX_BOARD_YEAR), 1, 1)  # never 10000
            datetime.combine(datetime(ok, 1, 2).date(), time.min) - IST_OFFSET
            check(f"accepted year {ok} survives the arithmetic", True)
        except (OverflowError, ValueError) as exc:
            check(f"accepted year {ok} survives the arithmetic", False, repr(exc))


# ── 2. _require_board_year: exact inclusive bounds ──
def test_require_board_year_bounds():
    check("bounds are [2, 9998]",
          (analytics_routes._MIN_BOARD_YEAR, analytics_routes._MAX_BOARD_YEAR) == (2, 9998),
          f"{analytics_routes._MIN_BOARD_YEAR}..{analytics_routes._MAX_BOARD_YEAR}")
    # inside the band -> no error
    for y in (2, 9998, 2026):
        try:
            analytics_routes._require_board_year(y)
            check(f"year {y} accepted", True)
        except HTTPException as exc:
            check(f"year {y} accepted", False, f"status {exc.status_code}")
    # outside the band -> 400 (the exact years that overflow)
    for y in (1, 9999, 0 - 5, 100000):
        exc = None
        try:
            analytics_routes._require_board_year(y)
        except HTTPException as e:
            exc = e
        check(f"year {y} -> 400", exc is not None and exc.status_code == 400,
              "no HTTPException" if exc is None else f"status {exc.status_code}")


# ── 3. the month endpoint: 400 on a bad year, 200 on a real one ──
def test_month_endpoint():
    for y in (-5, 0 - 1, 100000, MINYEAR, MAXYEAR):
        res = _call(analytics_routes.plant_board_month, year=y, month=12)
        check(f"/plant-board/month?year={y} -> 400 not 500",
              isinstance(res, HTTPException) and res.status_code == 400,
              type(res).__name__)
    ok = _call(analytics_routes.plant_board_month, year=2026, month=10)
    check("/plant-board/month?year=2026&month=10 -> dict (no raise)",
          isinstance(ok, dict), type(ok).__name__)
    check("month payload echoes the requested period",
          isinstance(ok, dict) and ok.get("year") == 2026 and ok.get("month") == 10,
          ok if not isinstance(ok, dict) else {k: ok.get(k) for k in ("year", "month")})


# ── 4. the day endpoint: 400 on 0001-01-01, 200 on a real day with the seeded output ──
def test_day_endpoint():
    res = _call(analytics_routes.plant_board_day, on="0001-01-01")
    check("/plant-board?on=0001-01-01 -> 400 not 500",
          isinstance(res, HTTPException) and res.status_code == 400,
          type(res).__name__)
    # a malformed date is still a 400 (unchanged behaviour), not swallowed
    bad = _call(analytics_routes.plant_board_day, on="not-a-date")
    check("/plant-board?on=not-a-date -> 400",
          isinstance(bad, HTTPException) and bad.status_code == 400, type(bad).__name__)
    ok = _call(analytics_routes.plant_board_day, on="2026-10-07")
    check("/plant-board?on=2026-10-07 -> dict (no raise)",
          isinstance(ok, dict), type(ok).__name__)


_DB = None


if __name__ == "__main__":
    _DB = _fresh_session()
    _seed(_DB)
    test_boundary_years_really_overflow()
    test_require_board_year_bounds()
    test_month_endpoint()
    test_day_endpoint()
    if failures:
        print(f"\n{len(failures)} FAILED:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("\nAll plant-board year-bound checks passed.")
