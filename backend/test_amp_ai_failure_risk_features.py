"""Failure-risk features: one implementation, half-open windows, None for "not measured".

WHAT IS ASSERTED
----------------
  1  BOUNDS       features.window_bounds(as_of, d) is exactly OeeWindow(days=d,
                  now=as_of) -- the repo's one half-open window rule -- and
                  history.date_bounds selects exactly the dates whose 00:00 lies
                  in [start, end) (brute force around midnight and mid-day)
  2  WORKED       a hand-built history with rows ON the boundaries, every
                  feature checked against a number computed by hand
  3  NOT ZERO     no production -> availability/performance/reject None, no
                  inspections -> None; never 0, which would claim a measurement
  4  EXCLUSION    a machine already in Breakdown at as_of is flagged (it cannot
                  start a NEW breakdown); a Breakdown event AT as_of is future
  5  NO FUTURE    appending rows at or after as_of changes no feature
  6  PARITY       features of a 400-day synthetic history equal the features of
                  the same history truncated to the lookback, at many dates
  7  PURE         extract() does not modify the history it is given

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_failure_risk_features.py
"""
import copy
import math
from datetime import date, datetime, timedelta

from amp_ai.failure_risk import features as F
from amp_ai.failure_risk import history as H
from amp_ai.failure_risk import synthetic as S
from oee_contract import OeeWindow

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def close(a, b, tol=1e-12):
    return a is not None and b is not None and math.isclose(a, b, rel_tol=0.0, abs_tol=tol)


AS_OF = datetime(2025, 6, 18, 10, 0)


def worked_history():
    return H.MachineHistory(
        7, "CNC-007",
        events=[
            (datetime(2025, 2, 1, 8, 0), "Idle", "Running", 70),         # before the lookback: state only
            (datetime(2025, 3, 1, 9, 0), "Running", "Breakdown", 0),     # lookback, not 30d
            (datetime(2025, 3, 1, 12, 0), "Breakdown", "Running", 75),
            (datetime(2025, 5, 25, 11, 0), "Running", "Breakdown", 0),   # 30d, not 7d
            (datetime(2025, 5, 25, 13, 30), "Breakdown", "Running", 80),
            (datetime(2025, 6, 11, 10, 0), "Running", "Breakdown", 0),   # EXACTLY as_of - 7d: inside 7d
            (datetime(2025, 6, 11, 12, 0), "Breakdown", "Running", 82),
            (datetime(2025, 6, 18, 10, 0), "Running", "Breakdown", 0),   # EXACTLY as_of: future
        ],
        downtime=[
            (datetime(2025, 5, 1, 9, 0), "Breakdown", "2 hrs"),           # lookback only
            (datetime(2025, 5, 25, 11, 0), "Breakdown", "2 hrs 30 min"),  # 150, 30d
            (datetime(2025, 6, 5, 8, 0), "Jam", "15 min"),                # 15, 30d
            (datetime(2025, 6, 11, 10, 0), "Breakdown", "2 hrs"),         # 120, 7d (boundary)
            (datetime(2025, 6, 15, 9, 0), "Changeover", "1.5 hrs"),       # 90, 7d
            (datetime(2025, 6, 18, 9, 59), "Jam", "5 min"),               # 5, 7d
            (datetime(2025, 6, 18, 10, 0), "Jam", "45 min"),              # future
        ],
        production=[
            (datetime(2025, 5, 5, 22, 0), 960, 700, 30, 1200, 1176, 24),   # lookback, before the last PM
            (datetime(2025, 5, 20, 22, 0), 960, 900, 30, 1800, 1764, 36),  # 30d
            (datetime(2025, 6, 10, 22, 0), 960, 800, 30, 1500, 1455, 45),  # 30d, 12 h before the 7d window
            (datetime(2025, 6, 12, 22, 0), 960, 600, 30, 1000, 950, 50),   # 7d
            (datetime(2025, 6, 17, 22, 0), 960, 840, 30, 1400, 1344, 56),  # 7d
            (datetime(2025, 6, 18, 22, 0), 960, 900, 30, 9999, 0, 9999),   # future
        ],
        inspections=[
            (datetime(2025, 6, 1, 14, 0), 50, 10),   # outside 14d
            (datetime(2025, 6, 4, 10, 0), 50, 5),    # EXACTLY as_of - 14d: inside
            (datetime(2025, 6, 11, 14, 0), 50, 2),
            (datetime(2025, 6, 18, 10, 0), 50, 50),  # future
        ],
        maintenance=[
            (date(2025, 1, 1), None, False, True),                  # open but planned before the lookback
            (date(2025, 3, 1), date(2025, 3, 1), True, False),      # reactive
            (date(2025, 5, 10), date(2025, 5, 12), False, False),   # last completed PM -> 2025-05-12 00:00
            (date(2025, 6, 1), None, False, True),                  # open, overdue
            (date(2025, 6, 10), date(2025, 6, 20), False, False),   # completed AFTER as_of: open at as_of, overdue
            (date(2025, 6, 11), date(2025, 6, 11), True, False),    # reactive, more recent: not "maintenance"
            (date(2025, 6, 18), None, False, True),                 # open, planned today: not yet overdue
        ],
    )


# --------------------------------------------------------------------------- 1
def section_bounds():
    print("\n1. Window bounds are the repo's half-open rule")
    ok = True
    for as_of in (AS_OF, datetime(2025, 6, 18), datetime(2024, 2, 29, 23, 59, 59, 999999)):
        for d in (7, 14, 30, 120):
            w = OeeWindow(days=d, now=as_of)
            if F.window_bounds(as_of, d) != (w.start, w.end):
                ok = False
    check("features.window_bounds(as_of, d) == (OeeWindow.start, OeeWindow.end)", ok)
    check("features.window_bounds IS history.window_bounds (one implementation)", F.window_bounds is H.window_bounds)

    ok = True
    detail = ""
    for start in (datetime(2025, 3, 1), datetime(2025, 3, 1, 10, 0), datetime(2025, 2, 28, 23, 59, 59)):
        for end in (datetime(2025, 3, 5), datetime(2025, 3, 5, 0, 0, 1), datetime(2025, 3, 5, 10, 0)):
            lo, hi = H.date_bounds(start, end)
            for offset in range(-3, 10):
                day = date(2025, 3, 1) + timedelta(days=offset)
                expected = start <= H.date_instant(day) < end
                if (lo <= day < hi) != expected:
                    ok, detail = False, f"{start} {end} {day}"
    check("date_bounds keeps exactly the dates whose 00:00 is in [start, end)", ok, detail)


# --------------------------------------------------------------------------- 2
def section_worked():
    print("\n2. Hand-computed features on a history with rows on the boundaries")
    f = F.extract(worked_history(), AS_OF)
    check("extract returns exactly FEATURE_NAMES", tuple(f) == F.FEATURE_NAMES, repr(tuple(f)))
    expected = {
        "breakdowns_7d": 1.0,                    # 06-11 10:00 is as_of - 7d exactly; 06-18 10:00 is as_of
        "breakdowns_30d": 2.0,                   # 05-25, 06-11
        "days_since_breakdown": 7.0,
        "never_broken_in_lookback": 0.0,
        "downtime_min_7d": 215.0,                # 120 + 90 + 5
        "downtime_min_30d": 380.0,               # 150 + 15 + 215
        "stoppages_7d": 3.0,
        "stoppage_accel": 3.0 / (5 * 7 / 30 + 1),
        "availability_7d": 1440 / 1920,
        "performance_7d": ((1000 + 1400) * 30 / 60) / 1440,
        "performance_drift": ((1000 + 1400) * 30 / 60) / 1440 - ((1800 + 1500 + 1000 + 1400) * 30 / 60) / 3140,
        "reject_rate_7d": 106 / 2400,
        "reject_rate_drift": 106 / 2400 - 187 / 5700,
        "inspection_fail_rate_14d": 7 / 100,     # 06-04 10:00 is as_of - 14d exactly
        "runtime_h_since_maint": 3140 / 60,      # records at or after 2025-05-12 00:00
        "no_maint_in_lookback": 0.0,
        "days_since_maint": 37 + 10 / 24,        # 2025-05-12 00:00 -> 2025-06-18 10:00
        "overdue_maint_open": 2.0,               # 06-01 open; 06-10 completed only on 06-20
    }
    for name, value in expected.items():
        check(f"{name} = {value:.6g}", close(f[name], value), f"got {f[name]!r}")
    check("every value is a float or None", all(v is None or type(v) is float for v in f.values()))

    view = H.truncate(worked_history(), AS_OF)
    check("the Breakdown AT as_of is not in the truncated events", all(e[0] < AS_OF for e in view.events))
    check("state at window start comes from the event before the lookback", view.state_at_window_start == ("Running", 70))
    check("a completion dated after as_of is shown as open with no completion date",
          (date(2025, 6, 10), None, False, True) in view.maintenance)
    check("an open task planned before the lookback is not in the window",
          all(m[0] != date(2025, 1, 1) for m in view.maintenance))

    h = worked_history()
    h.maintenance.append((date(2025, 6, 17), date(2025, 6, 18), False, False))
    f2 = F.extract(h, AS_OF)
    check("a PM completed on as_of's date (read as 00:00) counts as done before a 10:00 as_of",
          close(f2["days_since_maint"], 10 / 24) and f2["runtime_h_since_maint"] == 0.0,
          f"{f2['days_since_maint']} {f2['runtime_h_since_maint']}")
    check("... and removes nothing from the overdue count it was never in", f2["overdue_maint_open"] == 2.0)

    h = worked_history()
    h.maintenance = [m for m in h.maintenance if m[2]]      # reactive tasks only
    f3 = F.extract(h, AS_OF)
    check("reactive (corrective) work is not maintenance: no_maint flag set, days capped at the lookback",
          f3["no_maint_in_lookback"] == 1.0 and f3["days_since_maint"] == float(H.LOOKBACK_DAYS))
    check("with no maintenance, runtime counts over the whole lookback",
          close(f3["runtime_h_since_maint"], (700 + 3140) / 60))


# --------------------------------------------------------------------------- 3
def section_not_zero():
    print("\n3. Nothing measured -> None, never 0")
    h = worked_history()
    h.production = []
    h.inspections = []
    f = F.extract(h, AS_OF)
    for name in ("availability_7d", "performance_7d", "performance_drift", "reject_rate_7d",
                 "reject_rate_drift", "inspection_fail_rate_14d"):
        check(f"{name} is None without records", f[name] is None, repr(f[name]))
    check("runtime since maintenance is 0.0 hours (a count of nothing), not None", f["runtime_h_since_maint"] == 0.0)

    h = worked_history()
    h.production = [(datetime(2025, 6, 15, 22, 0), 0, 0, 30, 0, 0, 0)]
    f = F.extract(h, AS_OF)
    check("planned 0 -> availability None; runtime 0 -> performance None; total 0 -> reject rate None",
          f["availability_7d"] is None and f["performance_7d"] is None and f["reject_rate_7d"] is None)

    h = worked_history()
    h.production = [(datetime(2025, 6, 15, 22, 0), 960, 0, 30, 0, 0, 0)]
    f = F.extract(h, AS_OF)
    check("a planned day with no runtime is MEASURED availability 0.0", f["availability_7d"] == 0.0)

    h = worked_history()
    h.production.append((datetime(2025, 5, 30, 22, 0), 960, 900, 30, 1000, 1000, 0))
    h.production.sort(key=lambda r: r[0])
    h.production = [r for r in h.production if not datetime(2025, 6, 11, 10) <= r[0] < AS_OF]
    f = F.extract(h, AS_OF)
    check("drift is None when the 7-day side is unmeasured", f["performance_drift"] is None and f["reject_rate_drift"] is None)

    quiet = H.MachineHistory(1, "Q", events=[(datetime(2025, 2, 10), "Idle", "Breakdown", 0),   # 128 days before as_of
                                            (datetime(2025, 2, 10, 5), "Breakdown", "Running", 60)])
    f = F.extract(quiet, AS_OF)
    check("a breakdown older than the lookback is not seen: never_broken flag, days capped at 120",
          f["never_broken_in_lookback"] == 1.0 and f["days_since_breakdown"] == 120.0 and f["breakdowns_30d"] == 0.0)


# --------------------------------------------------------------------------- 4
def section_exclusion():
    print("\n4. Machines already in Breakdown are excluded")
    h = worked_history()
    check("an event Breakdown AT as_of is the future: not already broken", not H.in_breakdown_at(h, AS_OF))
    check("one microsecond later it is already broken", H.in_breakdown_at(h, AS_OF + timedelta(microseconds=1)))
    check("status_at reports the last event strictly before as_of", H.status_at(h, AS_OF) == ("Running", 82))
    loaded = H.MachineHistory(9, "L", state_at_window_start=("Breakdown", 0), window_end=AS_OF)
    check("with no event in the lookback, the loader's state at window start decides", H.in_breakdown_at(loaded, AS_OF))
    unknown = H.MachineHistory(9, "U")
    check("no information at all is not 'broken'", not H.in_breakdown_at(unknown, AS_OF) and H.status_at(unknown, AS_OF) is None)
    check("labels refuse a truncated history (it has no future)",
          _raises(ValueError, H.breakdown_in_horizon, H.truncate(h, AS_OF), AS_OF))
    check("the label window is [as_of, as_of + 7d): the Breakdown AT as_of is a positive",
          H.breakdown_in_horizon(worked_history(), AS_OF) == 1)
    later = worked_history()
    later.events = [e for e in later.events if e[0] < AS_OF] + [(AS_OF + timedelta(days=7), "Running", "Breakdown", 0)]
    check("a Breakdown exactly 7 days after as_of is outside the label window", H.breakdown_in_horizon(later, AS_OF) == 0)
    check("re-truncating a view for a different as_of is refused",
          _raises(ValueError, H.truncate, H.truncate(h, AS_OF), AS_OF - timedelta(days=1)))


def _raises(exc, fn, *args):
    try:
        fn(*args)
    except exc:
        return True
    return False


# --------------------------------------------------------------------------- 5
def section_no_future():
    print("\n5. Rows at or after as_of change nothing")
    base = F.extract(worked_history(), AS_OF)
    h = worked_history()
    for k in range(0, 30, 3):
        ts = AS_OF + timedelta(hours=k)
        h.events.append((ts + timedelta(days=1), "Running", "Breakdown", 0))
        h.downtime.append((ts, "Breakdown", "9 hrs"))
        h.production.append((ts, 960, 10, 30, 5, 0, 5))
        h.inspections.append((ts, 50, 49))
    h.maintenance.append((date(2025, 6, 25), None, False, True))                 # planned in the future
    h.maintenance.append((date(2025, 7, 2), date(2025, 7, 2), False, False))     # planned and done in the future
    h.maintenance.sort(key=lambda m: m[0])
    check("features ignore every future row", F.extract(h, AS_OF) == base)

    # A task planned before as_of WAS open at as_of; only its completion date is
    # future information. Knowing it will be completed on 06-19 must change nothing.
    open_task = worked_history()
    open_task.maintenance.append((date(2025, 6, 17), None, False, True))
    done_later = worked_history()
    done_later.maintenance.append((date(2025, 6, 17), date(2025, 6, 19), False, False))
    check("a completion dated after as_of reads exactly like a still-open task",
          F.extract(done_later, AS_OF) == F.extract(open_task, AS_OF)
          and F.extract(open_task, AS_OF)["overdue_maint_open"] == base["overdue_maint_open"] + 1)


# --------------------------------------------------------------------------- 6
def section_truncation_parity():
    print("\n6. A 400-day history and its truncation give identical features")
    fleet = S.generate_fleet(40, 400, seed=31)
    mismatches = 0
    values = []
    for mh in fleet.histories:
        for day in range(H.LOOKBACK_DAYS, 394, 21):
            as_of = S.as_of_for_day(day)
            full = F.extract(mh, as_of)
            short = F.extract(H.truncate(mh, as_of), as_of)
            values.append(full)
            if full != short:
                mismatches += 1
    check(f"{len(values)} (machine, date) pairs compared, no mismatch", len(values) > 400 and mismatches == 0,
          f"{mismatches} mismatches")
    for name in F.FEATURE_NAMES:
        present = [v[name] for v in values if v[name] is not None]
        check(f"synthetic data exercises {name} (not constant)", len(set(present)) > 1, repr(set(present))[:60])
    check("caps hold: days_since_* <= 120, runtime_h_since_maint <= 2000",
          all(v["days_since_breakdown"] <= 120 and v["days_since_maint"] <= 120
              and v["runtime_h_since_maint"] <= F.RUNTIME_H_CAP for v in values))


# --------------------------------------------------------------------------- 7
def section_pure():
    print("\n7. extract() leaves its input untouched")
    h = worked_history()
    snapshot = copy.deepcopy((h.events, h.downtime, h.production, h.inspections, h.maintenance,
                              h.state_at_window_start, h.window_end))
    F.extract(h, AS_OF)
    check("history unchanged after extract", snapshot == (h.events, h.downtime, h.production, h.inspections,
                                                          h.maintenance, h.state_at_window_start, h.window_end))


def main():
    print("=" * 74)
    print("AMP-native AI failure risk: features")
    print("=" * 74)
    section_bounds()
    section_worked()
    section_not_zero()
    section_exclusion()
    section_no_future()
    section_truncation_parity()
    section_pure()
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


def test_amp_ai_failure_risk_features():
    """pytest entry point; CI runs this file as a standalone script."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
