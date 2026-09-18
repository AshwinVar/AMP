"""Statement periods and covered hours: anchored, half-open, in the contract's zone.

WHAT THIS PINS
--------------
A statement covers one whole contract period: [local midnight on the 1st,
local midnight on the 1st one or three months later), converted to UTC. Periods
tile the contract exactly — no gap, no overlap — and a termination or an end is
always on a boundary, so no period is ever partial and no fee needs prorating.

These periods are ANCHORED. They deliberately do not use oee_contract.OeeWindow,
which is ROLLING ([now - days, now)); reusing a rolling window against an
anchored period is the mistake a whole 17-finding audit came from, so a
structural check below fails if this module ever imports it.

Covered hours are expanded per LOCAL date, with excluded local dates removed,
and converted to UTC. A local time that does not exist (the spring-forward gap)
moves forward to the first instant that does; an ambiguous local time
(fall-back) takes its first occurrence.

Run: DATABASE_URL="sqlite:///./ci.db" python test_contract_periods.py
"""
import ast
import io
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import contract_periods as cp  # noqa: E402
import contract_terms as ct  # noqa: E402
from contract_periods import Period, PeriodError  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def terms(timezone="Asia/Kolkata", period_months=1, term_months=12, coverage=None):
    doc = {
        "schema": 1, "currency": "INR", "period_months": period_months,
        "period_fee": "40000.00", "timezone": timezone, "term_months": term_months,
        "coverage": coverage or {"mode": "24x7"},
        "sla_target_pct": "97.00", "credit_tiers": [], "min_measured_pct": "90.00",
        "trusted_sources": ["mqtt"],
        "status_defaults": {"Breakdown": "OEM", "Maintenance": "FACTORY",
                            "Offline": "DISPUTED"},
        "reason_map": {}, "generic_reasons": ["breakdown", "unknown"],
        "reason_lead_seconds": 600, "termination_notice_days": 30,
        "covered_installations": [{"installation_id": 1, "serial_number": "S1"}],
    }
    return ct.parse(doc)


def _refused(fn, *args):
    try:
        fn(*args)
    except PeriodError as e:
        return str(e)
    return None


IST_JAN = datetime(2025, 12, 31, 18, 30)       # 2026-01-01 00:00 Asia/Kolkata
IST_NEXT_JAN = datetime(2026, 12, 31, 18, 30)  # 2027-01-01 00:00 Asia/Kolkata


# --------------------------------------------------------------------------
# 1. periods
# --------------------------------------------------------------------------

def test_ist_month_boundaries_convert_to_utc_and_tile():
    t = terms()
    ps = cp.periods(t, IST_JAN, IST_NEXT_JAN)
    assert len(ps) == 12, len(ps)
    assert ps[0] == Period(datetime(2025, 12, 31, 18, 30), datetime(2026, 1, 31, 18, 30))
    assert ps[1].start == datetime(2026, 1, 31, 18, 30)
    assert ps[-1].end == IST_NEXT_JAN
    for a, b in zip(ps, ps[1:]):
        assert a.end == b.start, (a, b)       # no gap, no overlap
    assert all(p.start < p.end for p in ps)
    assert all(p.start.tzinfo is None and p.start.microsecond == 0 for p in ps)
    assert sum(((p.end - p.start) for p in ps), timedelta()) == timedelta(days=365)
    print("PASS 12 IST months: 18:30Z boundaries, tile [start, end) with no gap or overlap")


def test_quarters():
    t = terms(period_months=3)
    ps = cp.periods(t, IST_JAN, IST_NEXT_JAN)
    assert [p.start for p in ps] == [datetime(2025, 12, 31, 18, 30),
                                     datetime(2026, 3, 31, 18, 30),
                                     datetime(2026, 6, 30, 18, 30),
                                     datetime(2026, 9, 30, 18, 30)]
    assert ps[-1].end == IST_NEXT_JAN
    print("PASS quarterly periods start on local Jan/Apr/Jul/Oct 1")


def test_boundaries_are_enforced():
    t = terms()
    assert _refused(cp.periods, t, datetime(2026, 1, 1), IST_NEXT_JAN), \
        "UTC midnight is not an IST month start"
    assert _refused(cp.periods, t, IST_JAN, IST_NEXT_JAN + timedelta(days=1)), \
        "an end off a boundary"
    assert _refused(cp.periods, t, IST_JAN, datetime(2026, 2, 15)), "mid-month end"
    assert cp.periods(t, IST_JAN, IST_JAN) == []
    # A termination effective at a boundary cuts the list there, whole.
    assert len(cp.periods(t, IST_JAN, datetime(2026, 3, 31, 18, 30))) == 3
    assert cp.month_start_utc(t, 2026, 1) == IST_JAN
    assert cp.month_start_utc(t, 2025, 13) == IST_JAN          # month carries
    assert cp.month_start_utc(t, 2027, 1) == IST_NEXT_JAN
    assert cp.is_month_start(t, cp.month_start_utc(t, 2026, 7))
    assert _refused(cp.month_start_utc, t, "2026", 1)
    assert cp.is_month_start(t, IST_JAN)
    assert not cp.is_month_start(t, datetime(2026, 1, 1))
    assert not cp.is_month_start(t, IST_JAN + timedelta(seconds=1))
    assert _refused(cp.periods, t, IST_JAN + timedelta(microseconds=1), IST_NEXT_JAN)
    print("PASS start and end must be period boundaries; a boundary termination is whole")


def test_period_starting_and_effective_end():
    t = terms()
    contract = SimpleNamespace(starts_at=IST_JAN, ends_at=IST_NEXT_JAN,
                               termination_effective_at=None)
    assert cp.contract_effective_end(contract) == IST_NEXT_JAN
    p = cp.period_starting(contract, t, datetime(2026, 2, 28, 18, 30))
    assert p == Period(datetime(2026, 2, 28, 18, 30), datetime(2026, 3, 31, 18, 30))
    assert cp.period_starting(contract, t, datetime(2026, 3, 1)) is None
    assert cp.period_starting(contract, t, IST_NEXT_JAN) is None
    assert cp.period_starting(contract, t, datetime(2025, 11, 30, 18, 30)) is None
    terminated = SimpleNamespace(starts_at=IST_JAN, ends_at=IST_NEXT_JAN,
                                 termination_effective_at=datetime(2026, 3, 31, 18, 30))
    assert cp.contract_effective_end(terminated) == datetime(2026, 3, 31, 18, 30)
    assert cp.period_starting(terminated, t, datetime(2026, 3, 31, 18, 30)) is None
    assert cp.period_starting(terminated, t, datetime(2026, 2, 28, 18, 30)) is not None
    print("PASS period_starting finds whole periods inside [starts_at, effective_end)")


def test_boundary_helpers_for_amendments_and_termination():
    t = terms()
    assert cp.is_boundary(t, IST_JAN, IST_JAN)
    assert cp.is_boundary(t, IST_JAN, datetime(2026, 5, 31, 18, 30))
    assert not cp.is_boundary(t, IST_JAN, datetime(2026, 6, 1))
    assert not cp.is_boundary(t, IST_JAN, datetime(2025, 11, 30, 18, 30))  # before start
    # Notice ends mid-month: effective at the next boundary at or after it.
    assert cp.boundary_at_or_after(t, IST_JAN, datetime(2026, 3, 10, 9, 0)) == \
        datetime(2026, 3, 31, 18, 30)
    assert cp.boundary_at_or_after(t, IST_JAN, datetime(2026, 3, 31, 18, 30)) == \
        datetime(2026, 3, 31, 18, 30)
    assert cp.boundary_at_or_after(t, IST_JAN, datetime(2020, 1, 1)) == IST_JAN
    q = terms(period_months=3)
    assert cp.boundary_at_or_after(q, IST_JAN, datetime(2026, 1, 5)) == \
        datetime(2026, 3, 31, 18, 30)
    assert not cp.is_boundary(q, IST_JAN, datetime(2026, 1, 31, 18, 30))
    print("PASS is_boundary / boundary_at_or_after follow the contract's period grid")


def test_a_dst_month_is_shorter_and_still_whole():
    t = terms(timezone="Europe/London")
    march = datetime(2026, 3, 1)                   # 00:00 GMT
    ps = cp.periods(t, march, datetime(2026, 4, 30, 23, 0))
    assert ps[0] == Period(march, datetime(2026, 3, 31, 23, 0))   # Apr 1 00:00 BST
    assert ps[0].end - ps[0].start == timedelta(days=31, hours=-1)
    assert ps[1] == Period(datetime(2026, 3, 31, 23, 0), datetime(2026, 4, 30, 23, 0))
    print("PASS Europe/London March is 31 days minus the lost hour, still one whole period")


# --------------------------------------------------------------------------
# 2. local time -> UTC
# --------------------------------------------------------------------------

def test_local_to_utc_gap_and_fold():
    london = terms(timezone="Europe/London").zone
    # Spring forward 2026-03-29 01:00 GMT -> 02:00 BST: 01:30 does not exist.
    assert cp.local_to_utc(datetime(2026, 3, 29, 1, 30), london) == datetime(2026, 3, 29, 1, 0)
    assert cp.local_to_utc(datetime(2026, 3, 29, 1, 0), london) == datetime(2026, 3, 29, 1, 0)
    assert cp.local_to_utc(datetime(2026, 3, 29, 1, 59, 59), london) == \
        datetime(2026, 3, 29, 1, 0)
    assert cp.local_to_utc(datetime(2026, 3, 29, 2, 0), london) == datetime(2026, 3, 29, 1, 0)
    assert cp.local_to_utc(datetime(2026, 3, 29, 0, 59), london) == datetime(2026, 3, 29, 0, 59)
    # Fall back 2026-10-25 02:00 BST -> 01:00 GMT: 01:30 happens twice; first one.
    assert cp.local_to_utc(datetime(2026, 10, 25, 1, 30), london) == datetime(2026, 10, 25, 0, 30)
    ist = terms().zone
    assert cp.local_to_utc(datetime(2026, 1, 1), ist) == IST_JAN
    print("PASS a non-existent local time moves forward to the transition; a repeated one "
          "takes its first occurrence")


# --------------------------------------------------------------------------
# 3. covered intervals
# --------------------------------------------------------------------------

def test_24x7_covers_the_period_clipped_to_the_active_range():
    t = terms()
    p = Period(IST_JAN, datetime(2026, 1, 31, 18, 30))
    assert cp.covered_intervals(p, t, IST_JAN, IST_NEXT_JAN) == [(p.start, p.end)]
    mid = datetime(2026, 1, 15, 6, 0)
    assert cp.covered_intervals(p, t, IST_JAN, mid) == [(p.start, mid)]
    assert cp.covered_intervals(p, t, mid, IST_NEXT_JAN) == [(mid, p.end)]
    assert cp.covered_intervals(p, t, p.end, IST_NEXT_JAN) == []
    print("PASS 24x7 coverage is the period intersected with the active range")


def test_weekly_windows_in_ist_with_an_excluded_date():
    t = terms(coverage={"mode": "weekly",
                        "windows": [{"days": [0, 1, 2, 3, 4, 5], "start": "08:00",
                                     "end": "20:00"}],
                        "excluded_dates": ["2026-10-20"]})
    october = Period(datetime(2026, 9, 30, 18, 30), datetime(2026, 10, 31, 18, 30))
    got = cp.covered_intervals(october, t, IST_JAN, IST_NEXT_JAN)
    # Oct 2026: 31 days, 4 Sundays, 1 excluded Tuesday -> 26 x 12 h.
    assert len(got) == 26, len(got)
    assert sum(((e - s) for s, e in got), timedelta()) == timedelta(hours=26 * 12)
    assert got[0] == (datetime(2026, 10, 1, 2, 30), datetime(2026, 10, 1, 14, 30))
    starts = {s.date() for s, _ in got}
    assert datetime(2026, 10, 20).date() not in starts
    assert datetime(2026, 10, 4).date() not in starts        # a Sunday
    assert all(a[1] < b[0] for a, b in zip(got, got[1:]))
    print("PASS Mon-Sat 08:00-20:00 IST in Oct 2026: 26 windows, Sundays and 20 Oct excluded")


def test_windows_merge_and_clip():
    t = terms(timezone="UTC", coverage={
        "mode": "weekly",
        "windows": [{"days": [3], "start": "08:00", "end": "12:00"},
                    {"days": [3], "start": "11:00", "end": "14:00"},
                    {"days": [3], "start": "14:00", "end": "16:00"},
                    {"days": [4], "start": "00:00", "end": "24:00"},
                    {"days": [2], "start": "22:00", "end": "24:00"}],
        "excluded_dates": []})
    jan = Period(datetime(2026, 1, 1), datetime(2026, 2, 1))   # Thu 1 Jan
    got = cp.covered_intervals(jan, t, datetime(2026, 1, 1, 9), datetime(2026, 1, 8))
    assert got[0] == (datetime(2026, 1, 1, 9), datetime(2026, 1, 1, 16)), got[:2]
    assert got[1] == (datetime(2026, 1, 2), datetime(2026, 1, 3)), got[:3]
    # Wednesday 22:00-24:00 touches Thursday 08:00? No: a 8h gap keeps them apart.
    assert got[2] == (datetime(2026, 1, 7, 22), datetime(2026, 1, 8)), got
    assert len(got) == 3, got
    # Touching intervals merge: Wed 22-24 + Thu 00-24 (on a Thu/Fri schedule).
    t2 = terms(timezone="UTC", coverage={
        "mode": "weekly", "windows": [{"days": [2], "start": "22:00", "end": "24:00"},
                                      {"days": [3], "start": "00:00", "end": "02:00"}],
        "excluded_dates": []})
    got2 = cp.covered_intervals(jan, t2, jan.start, jan.end)
    assert (datetime(2026, 1, 7, 22), datetime(2026, 1, 8, 2)) in got2, got2
    print("PASS overlapping and touching windows merge; clipped to the active range")


def test_windows_across_a_dst_gap():
    t = terms(timezone="Europe/London", coverage={
        "mode": "weekly",
        "windows": [{"days": [6], "start": "01:30", "end": "03:00"},
                    {"days": [5], "start": "01:15", "end": "01:45"}],
        "excluded_dates": []})
    march = Period(datetime(2026, 3, 1), datetime(2026, 3, 31, 23, 0))
    got = cp.covered_intervals(march, t, march.start, march.end)
    # Sunday 29 March: 01:30 does not exist -> starts 02:00 BST (01:00Z) to 03:00 BST.
    assert (datetime(2026, 3, 29, 1, 0), datetime(2026, 3, 29, 2, 0)) in got, got
    # A normal Sunday: 01:30-03:00 GMT.
    assert (datetime(2026, 3, 22, 1, 30), datetime(2026, 3, 22, 3, 0)) in got, got
    # Saturday 28 March is before the change: a normal 30 minutes.
    assert (datetime(2026, 3, 28, 1, 15), datetime(2026, 3, 28, 1, 45)) in got, got
    print("PASS a window starting inside the spring-forward gap starts at the transition")


def test_a_window_entirely_inside_the_gap_covers_nothing():
    t = terms(timezone="Europe/London", coverage={
        "mode": "weekly", "windows": [{"days": [6], "start": "01:15", "end": "01:45"}],
        "excluded_dates": []})
    march = Period(datetime(2026, 3, 1), datetime(2026, 3, 31, 23, 0))
    got = cp.covered_intervals(march, t, march.start, march.end)
    assert all(s.date() != datetime(2026, 3, 29).date() for s, _ in got), got
    assert len(got) == 4, got      # 1, 8, 15, 22 March
    print("PASS a window wholly inside a DST gap covers no time that day")


# --------------------------------------------------------------------------
# 4. structural: anchored periods never use the rolling OEE window
# --------------------------------------------------------------------------

def test_contract_periods_never_uses_the_rolling_oee_window():
    path = os.path.join(HERE, "contract_periods.py")
    source = io.open(path, encoding="utf-8").read()
    tree = ast.parse(source)
    functions = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for required in ("periods", "covered_intervals", "local_to_utc", "period_starting"):
        assert required in functions, f"structural scan did not find {required}"
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported |= {a.name for a in node.names}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
        {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for forbidden in ("oee_contract", "OeeWindow"):
        assert forbidden not in imported, f"contract_periods imports {forbidden}"
        assert forbidden not in names, f"contract_periods references {forbidden}"
    bad = [n.lineno for n in ast.walk(tree)
           if (isinstance(n, ast.Constant) and type(n.value) is float)
           or (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "float")]
    assert not bad, f"float at lines {bad}"
    print(f"PASS contract_periods.py: {len(functions)} functions; no oee_contract/OeeWindow, "
          "no float")


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"ALL {len(tests)} CONTRACT PERIOD TESTS PASSED")
