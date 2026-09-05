"""A production record written at this instant belongs to the window ending now.

THE DEFECT
----------
`OeeWindow` is half-open, `[start, end)`, and the module docstring justifies that
carefully: adjacent windows must tile without overlap so a week-over-week
comparison cannot double-count a record on the boundary. That rule is right and
this change does not touch it.

The bug was the DEFAULT end. It was `datetime.utcnow()` — the current instant —
against a filter of `created_at < end`. A ProductionRecord's `created_at`
defaults to `datetime.utcnow()` too, so a record written in the same clock tick
as the query carries EXACTLY the window's end and is excluded from its own
window. Captured from a live repro:

    stored created_at   2026-09-05 02:19:59.037681
    window `end` param  2026-09-05 02:19:59.037681      <- identical
    filter              created_at < end                <- so, excluded

    raw SQL, same row, `created_at < end`                : 1 row
    plant_oee(...)["has_data"]                           : False

WHY IT LOOKED LIKE A FLAKY TEST
-------------------------------
`datetime.utcnow()` has ~15.6 ms granularity on Windows, so two calls in the
same tick return the IDENTICAL value and the collision is common:
`test_ai_copilot_context.py` failed 3 runs in 8 on this machine, on master, with
no change in the tree. On Linux the clock is microsecond-grained, collisions are
vanishingly rare, and CI stayed green — which is exactly why this survived. The
platform difference was the tell, not the cause.

It cost a real misdiagnosis: the flake was first pinned on an unrelated change
by stashing files one at a time and reading pass/fail. Those were coin flips.
Eight consecutive runs of an unmodified tree is what settled it.

THE FIX, AND WHY IT IS NOT `<=`
--------------------------------
Widening the filter to `created_at <= end` would break the tiling invariant the
window exists to provide: `[d-14, d-7)` and `[d-7, d)` would share any record
landing exactly on `d-7`.

Instead the DEFAULT end becomes the next representable instant after now, so
"everything that has already happened" is inside a window that ends now, and the
comparison stays strictly exclusive. An EXPLICIT `now=` is passed through
untouched — that is the argument callers use to build adjacent windows, and
section 3 pins that it still tiles exactly.

`_now()` exists as a seam so this is testable at all. Asserting the old
behaviour by racing the clock would be a test that fails ~35% of the time on one
platform and never on CI — the same non-evidence the bug hid behind.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_oee_window_boundary.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import oee_contract
import tenancy
from database import Base

T = "WINDOWEDGE"
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def seed(created_at):
    """One machine, one measurable record, written AT a chosen instant."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    db = sessionmaker(bind=engine)()
    tok = tenancy.set_current_tenant(None)
    db.add(models.TenantConfig(tenant_code=T))
    db.add(models.Machine(tenant_code=T, name="PRESS-01", site="P1",
                          status="Running", utilization=80, downtime="0 min"))
    db.flush()
    db.add(models.ProductionRecord(
        tenant_code=T, machine_id=1, planned_minutes=1000, runtime_minutes=500,
        ideal_cycle_time_seconds=30, total_count=500, good_count=400,
        rejected_count=100, created_at=created_at))
    db.commit()
    tenancy.reset_current_tenant(tok)
    return db


def main():
    real_now = oee_contract._now

    print("=" * 74)
    print("1. A RECORD WRITTEN AT THIS INSTANT IS IN THE WINDOW ENDING NOW")
    print("=" * 74)
    # THE DEFECT, deterministic. The clock is pinned, so the record's created_at
    # and the window's end are the same value by construction rather than by a
    # 15 ms coincidence — this fails on every platform before the fix and passes
    # on every platform after it.
    frozen = datetime(2026, 9, 5, 2, 19, 59, 37681)
    oee_contract._now = lambda: frozen
    try:
        db = seed(frozen)
        tok = tenancy.set_current_tenant(T)
        result = oee_contract.plant_oee(db, T)
        check("the record is measured, not dropped on the boundary",
              result["has_data"] is True,
              f"has_data={result['has_data']} — the record carries exactly the "
              f"window's end and was excluded from its own window")
        # Derived here from the seeded row, not copied from another suite:
        #   A = 500/1000 = .5   P = (30*500)/(500*60) = .5   Q = 400/500 = .8
        #   OEE = .5 * .5 * .8 = .20 -> 20%
        # plant_oee returns RATIOS; percentages are a presentation-boundary
        # conversion (as_percentages), which is where the single rounding lives.
        check("...so the plant reports the OEE the row implies (20%)",
              oee_contract.as_percentages(result)["oee"] == 20,
              str(oee_contract.as_percentages(result)["oee"]))
        check("...and coverage sees the machine reporting",
              result["coverage"]["machines_reporting"] == 1,
              str(result["coverage"]))
        tenancy.reset_current_tenant(tok)
        db.close()

        print()
        print("=" * 74)
        print("2. A RECORD FROM THE FUTURE IS STILL EXCLUDED")
        print("=" * 74)
        # The fix moves the boundary by one microsecond, not by an hour. A row
        # dated after the window must still be out, or "end" would stop meaning
        # anything — and clock skew on an ingest gateway makes future-dated rows
        # a real thing rather than a hypothetical.
        db = seed(frozen + timedelta(seconds=1))
        tok = tenancy.set_current_tenant(T)
        result = oee_contract.plant_oee(db, T)
        check("a record one second in the future is not measured",
              result["has_data"] is False, str(result["oee"]))
        tenancy.reset_current_tenant(tok)
        db.close()

        print()
        print("=" * 74)
        print("3. AN EXPLICIT now= IS UNTOUCHED, SO ADJACENT WINDOWS STILL TILE")
        print("=" * 74)
        # The invariant the half-open rule exists for. If the nudge leaked into
        # explicitly-constructed windows, [d-14, d-7) and [d-7, d) would BOTH
        # contain a record landing exactly on d-7 and a week-over-week
        # comparison would double-count it.
        d = datetime(2026, 9, 5, 12, 0, 0)
        this_week = oee_contract.OeeWindow(days=7, now=d)
        last_week = oee_contract.OeeWindow(days=7, now=d - timedelta(days=7))
        check("an explicit end is used verbatim, with no nudge",
              this_week.end == d, str(this_week.end))
        check("the two windows abut exactly", last_week.end == this_week.start,
              f"{last_week.end} vs {this_week.start}")

        boundary = this_week.start
        db = seed(boundary)
        tok = tenancy.set_current_tenant(T)
        this_r = oee_contract.plant_oee(db, T, this_week)
        last_r = oee_contract.plant_oee(db, T, last_week)
        in_this = this_r["coverage"]["machines_reporting"]
        in_last = last_r["coverage"]["machines_reporting"]
        check("a record exactly on the shared boundary is in exactly ONE window",
              in_this + in_last == 1, f"this={in_this} last={in_last}")
        check("...and it belongs to the LATER one (start inclusive, end exclusive)",
              in_this == 1 and in_last == 0, f"this={in_this} last={in_last}")
        # ...and the same for the SUMS, not just the coverage count.
        #
        # `coverage()` and `_sums()` apply the boundary rule in two separate
        # SQL filters. Asserting only on coverage left a mutation alive:
        # widening `_sums` alone to `created_at <= end` — the tempting "fix" for
        # the boundary bug — kept coverage honest while double-counting the
        # boundary record's MINUTES in both windows, which is precisely the
        # week-over-week distortion half-open exists to prevent.
        measured = [w for w, r in (("this", this_r), ("last", last_r)) if r["has_data"]]
        check("...and the record's NUMBERS land in exactly one window too",
              measured == ["this"], f"measured by: {measured}")
        tenancy.reset_current_tenant(tok)
        db.close()
    finally:
        oee_contract._now = real_now

    print()
    print("=" * 74)
    print("4. THE DEFAULT WINDOW USES THE REAL CLOCK")
    print("=" * 74)
    # The seam must not have become the behaviour. Without this, replacing
    # _now() with a constant would satisfy everything above.
    before = datetime.utcnow()
    window = oee_contract.OeeWindow(days=7)
    after = datetime.utcnow() + timedelta(seconds=1)
    check("the default end tracks the real clock", before <= window.end <= after,
          f"{before} <= {window.end} <= {after}")
    check("...and is strictly after an instant that had ALREADY passed",
          window.end > before, f"end={window.end} sampled-before={before}")
    check("the window still spans the requested number of days",
          window.end - window.start == timedelta(days=7),
          str(window.end - window.start))

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
