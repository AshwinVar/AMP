"""shift_attainment never prints a percentage the data can't support.

THE DEFECT
----------
analytics_engine.shift_attainment(actual, target) is THE one formula for a
shift's efficiency (ai/shift.py, build_shift_kpis, the Executive OEE shift chart
and /reports/shifts.csv all route through it). It used to be:

    return round(actual / target * 100) if target else None

`if target` returns None for a 0 / None target (an unplanned shift, correct), but
it defended neither direction against a CORRUPT row:

  * a NEGATIVE target is truthy, so it slipped through to a nonsensical ratio;
  * a NEGATIVE actual produced a sub-zero attainment (e.g. -50%);
  * a NULL actual raised TypeError (`None / target`) for any direct caller that
    had not already coalesced it.

The write path (machines_routes.create_shift) rejects negative target/actual, so
the trigger is a legacy / raw-SQL / migration row — the same threat model the
engines defend everywhere else. Its sibling quality_fail_rate already coalesces
NULLs, guards a non-positive denominator (`inspected <= 0`) and floors the result;
calculate_oee_from_record / pooled_oee apply the same symmetric floor to good/total.
shift_attainment was the one member of that family without the floor.

THE RULE
--------
  * target <= 0  -> None            (no measurable target; was only 0 / None before)
  * actual < 0   -> floored to 0    (a shift cannot produce negative units)
  * NULL actual / target coalesced to 0 first (no TypeError)
  * NOT capped at 100 — a shift can beat its plan (over-attainment is real output),
    unlike a fail rate that cannot exceed the units inspected.

On every valid row (target > 0, actual >= 0) the change is a STRICT NO-OP: the
floor never fires and the ratio is identical, including over-attainment > 100%.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_shift_attainment_floor.py
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import analytics_engine
import models
from analytics_engine import build_shift_kpis, shift_attainment
from database import Base

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def _session(shifts):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    for name, target, actual in shifts:
        db.add(models.ShiftData(shift_name=name, target_output=target, actual_output=actual))
    db.commit()
    return db


def main():
    section("1. VALID ROWS: STRICT NO-OP (ratio unchanged, over-attainment uncapped)")
    # Expected values derived by hand: round(actual / target * 100).
    check("50 of 100 -> 50%", shift_attainment(50, 100) == 50, repr(shift_attainment(50, 100)))
    check("900 of 1000 -> 90% (regression guard, matches existing contract)",
          shift_attainment(900, 1000) == 90, repr(shift_attainment(900, 1000)))
    check("1500 of 1000 -> 150% (over-attainment is NOT capped)",
          shift_attainment(1500, 1000) == 150, repr(shift_attainment(1500, 1000)))
    check("0 of 100 -> 0% (a planned shift that produced nothing is a real 0)",
          shift_attainment(0, 100) == 0, repr(shift_attainment(0, 100)))
    # The no-op holds across a battery of valid pairs: identical to the raw ratio.
    for actual, target in [(1, 3), (7, 7), (250, 480), (999, 1000), (3000, 1000)]:
        expected = round(actual / target * 100)
        check(f"no-op {actual}/{target} == round ratio {expected}",
              shift_attainment(actual, target) == expected,
              repr(shift_attainment(actual, target)))

    section("2. NO TARGET: None (unchanged for 0 / None, NEW for a corrupt negative)")
    check("target 0 -> None (unplanned shift, existing contract)",
          shift_attainment(0, 0) is None, repr(shift_attainment(0, 0)))
    check("target 0 with output -> None (existing contract)",
          shift_attainment(50, 0) is None, repr(shift_attainment(50, 0)))
    check("NEGATIVE target -> None, not a bogus percentage [the fix]",
          shift_attainment(50, -100) is None, repr(shift_attainment(50, -100)))
    check("None target -> None, no crash",
          shift_attainment(50, None) is None, repr(shift_attainment(50, None)))

    section("3. NEGATIVE / NULL ACTUAL: floored to 0, never sub-zero, never a crash")
    check("NEGATIVE actual -> 0%, not -50% [the fix]",
          shift_attainment(-50, 100) == 0, repr(shift_attainment(-50, 100)))
    check("actual more negative than target -> still 0%",
          shift_attainment(-500, 100) == 0, repr(shift_attainment(-500, 100)))
    check("NULL actual with a real target -> 0%, no TypeError [the fix]",
          shift_attainment(None, 100) == 0, repr(shift_attainment(None, 100)))
    check("NULL actual AND NULL target -> None, no crash",
          shift_attainment(None, None) is None, repr(shift_attainment(None, None)))

    section("4. END-TO-END: build_shift_kpis floors a corrupt row AND reconciles")
    # A corrupt legacy row the API would have rejected, read back from the table.
    db = _session([
        ("Morning", 1000, 900),      # valid: 90%
        ("Over", 1000, 1200),        # over-attainment: 120%, uncapped
        ("NegActual", 100, -50),     # corrupt: floored to 0%, NOT -50%
        ("NegTarget", -100, 50),     # corrupt: no measurable target -> None
        ("Unplanned", 0, 50),        # unplanned: None
    ])
    rows = {r["shift_name"]: r for r in build_shift_kpis(db.query(models.ShiftData).all())}
    db.close()

    eff = {name: r["efficiency"] for name, r in rows.items()}
    check("efficiencies: 90 / 120 / 0 / None / None",
          eff == {"Morning": 90, "Over": 120, "NegActual": 0,
                  "NegTarget": None, "Unplanned": None}, str(eff))
    check("no shift reads a NEGATIVE efficiency",
          all(v is None or v >= 0 for v in eff.values()), str(eff))

    # Reconciliation (rule-3): the per-shift figure IS shift_attainment of that
    # row's own coalesced target/actual — one basis, no second formula.
    reconciles = all(
        r["efficiency"] == shift_attainment(r["actual_output"], r["target_output"])
        for r in rows.values()
    )
    check("each row's efficiency == shift_attainment(actual, target)", reconciles)

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED - shift_attainment floor")
    print("=" * 74)
    return 1 if failures else 0


def test_shift_attainment_floor():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
