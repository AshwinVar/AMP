"""Regression tests for the predictive-maintenance risk WEIGHTING matrix.

test_predictive_engine.py already pins classify_risk's thresholds, the sort, and
the NULL-column guards, and it exercises ONE dense "hot" machine that trips five
rules at once (breakdown + high downtime + breakdown events + high reject +
pressure). But its own docstring warns "the weighting must not drift silently",
and several individual bands are never isolated, so a single weight could change
by a few points and no test would notice:

  * the Maintenance-status contribution (+15),
  * the high-utilization band (>90 -> +12) and its boundary,
  * the MODERATE accumulated-downtime band (60-119 min -> +15, vs the >=120 -> +25
    high band the hot case already covers),
  * the frequent-downtime-events band (>=5 events -> +15), isolated from minutes,
  * the moderate reject band (5-7.9% -> +10) and the 8% boundary into the high band,
  * breakdown_events counted from a downtime log whose reason is "breakdown"
    (case-insensitively), not only from MachineEvent rows.

Each expected score below is derived by hand from predictive_engine's rule table
(the point values are spelled out in the assertions), never read back from the
function — so a weight change breaks the test.

Durations are the real free-text formats operators type. The moderate/high
downtime bands are asserted through HOUR and DECIMAL-HOUR fixtures ("1 hr 30 min"
-> 90, "2 hrs" -> 120, "1.5 hrs" -> 90) so this also guards the exact class of
bug the shared parser was created for: predictive_engine's old digit-concatenation
parser read "1 hr" as 1 minute, silently understating the downtime score. A misparse
here would land the machine in the wrong band (or none), so the band assertions
pin the parser AT THE SCORING LEVEL, not just in isolation.

Run:  cd backend && python test_predictive_engine_weights.py   (exit 0 = pass)
"""
from types import SimpleNamespace

import predictive_engine as pe


def _machine(status="Running", utilization=70, mid=1, name="M1"):
    # Utilization 70 is deliberately "neutral": it is neither < 40 (low band) nor
    # > 90 (high band), so a machine built this way scores 0 until a rule is added.
    return SimpleNamespace(id=mid, name=name, status=status, utilization=utilization)


def _down(reason="Tool Change", duration="1 min", mid=1):
    return SimpleNamespace(machine_id=mid, reason=reason, duration=duration)


def _score(machines, downtime=None, records=None, events=None, work_orders=None):
    return pe.calculate_predictive_risk(
        machines, downtime or [], records or [], events or [], work_orders or []
    )


def test_neutral_machine_scores_zero():
    # The baseline every isolated band builds on: a Running machine at 70%
    # utilization with no history trips no rule.
    row = _score([_machine()])[0]
    assert row["risk_score"] == 0, row
    assert row["risk_level"] == "Low"
    assert row["reasons"] == ["no major risk indicators detected"], row
    print("PASS neutral Running/70% machine scores 0 (isolation baseline)")


def test_maintenance_status_band():
    # Maintenance status alone contributes +15 (and nothing else fires).
    row = _score([_machine(status="Maintenance")])[0]
    assert row["risk_score"] == 15, row
    assert "machine currently in maintenance" in row["reasons"]
    assert row["risk_level"] == "Low"          # 15 < 35
    print("PASS Maintenance status band = +15")


def test_high_utilization_band_and_boundary():
    # utilization > 90 contributes +12; the check is strict (> 90), so 90 itself
    # is not "high" and 40 is not "low" — both boundaries score 0.
    hot = _score([_machine(utilization=95)])[0]
    assert hot["risk_score"] == 12, hot
    assert "high utilization above 90%" in hot["reasons"]

    assert _score([_machine(utilization=90)])[0]["risk_score"] == 0     # 90 is not > 90
    assert _score([_machine(utilization=40)])[0]["risk_score"] == 0     # 40 is not < 40
    print("PASS high-utilization band = +12; 90 and 40 are non-scoring boundaries")


def test_moderate_downtime_band_hour_format():
    # "1 hr 30 min" = 90 minutes -> the MODERATE band (60-119) -> +15, NOT the
    # >=120 high band (+25). A digit-concatenation parser would read this as 130
    # or 1, landing in the wrong band; pinning the score pins the parse.
    row = _score([_machine()], downtime=[_down(duration="1 hr 30 min")])[0]
    assert row["downtime_minutes"] == 90, row          # hours parsed, not concatenated
    assert row["risk_score"] == 15, row                # moderate band, not 25
    assert "moderate accumulated downtime" in row["reasons"]
    assert "high accumulated downtime" not in row["reasons"]
    print("PASS moderate downtime band via '1 hr 30 min' -> 90 min -> +15")


def test_high_downtime_band_hour_format():
    # "2 hrs" = 120 minutes -> the >=120 high band -> +25.
    row = _score([_machine()], downtime=[_down(duration="2 hrs")])[0]
    assert row["downtime_minutes"] == 120, row
    assert row["risk_score"] == 25, row                # high band
    assert "high accumulated downtime" in row["reasons"]
    print("PASS high downtime band via '2 hrs' -> 120 min -> +25")


def test_decimal_hours_downtime_band():
    # "1.5 hrs" = 90 (not 300) -> moderate band -> +15. Guards the decimal-hour
    # bug (an integer-only parser read "1.5 h" as 5 hours) at the scoring level.
    row = _score([_machine()], downtime=[_down(duration="1.5 hrs")])[0]
    assert row["downtime_minutes"] == 90, row
    assert row["risk_score"] == 15, row
    print("PASS decimal-hour downtime via '1.5 hrs' -> 90 min -> +15 (not 300 -> +25)")


def test_frequent_downtime_events_isolated_from_minutes():
    # Five 1-minute logs: total downtime is only 5 minutes (below the 60-min
    # band, so NO minute points), but the event COUNT is 5 (>=5) -> +15. This
    # isolates the events band from the minutes band.
    logs = [_down(duration="1 min") for _ in range(5)]
    row = _score([_machine()], downtime=logs)[0]
    assert row["downtime_minutes"] == 5, row           # below 60 -> no minute points
    assert row["downtime_events"] == 5, row
    assert row["risk_score"] == 15, row                # frequent-events band only
    assert "frequent downtime events" in row["reasons"]
    assert "moderate accumulated downtime" not in row["reasons"]
    print("PASS frequent-downtime-events band = +15, isolated from downtime minutes")


def test_moderate_reject_band_and_high_boundary():
    # reject_rate 6% (6/100) -> moderate band (5-7.9%) -> +10.
    mod = _score([_machine()],
                 records=[SimpleNamespace(machine_id=1, rejected_count=6, total_count=100)])[0]
    assert mod["reject_rate"] == 6.0, mod
    assert mod["risk_score"] == 10, mod
    assert "moderate reject rate" in mod["reasons"]

    # 8% is the boundary INTO the high band -> +20 (not +10).
    high = _score([_machine()],
                  records=[SimpleNamespace(machine_id=1, rejected_count=8, total_count=100)])[0]
    assert high["reject_rate"] == 8.0, high
    assert high["risk_score"] == 20, high
    assert "high reject rate" in high["reasons"]
    print("PASS reject bands: 6% -> +10 (moderate), 8% -> +20 (high boundary)")


def test_breakdown_events_counted_from_downtime_reason_case_insensitive():
    # breakdown_events aggregates BOTH MachineEvent(new_status="Breakdown") AND a
    # DowntimeLog whose reason is "breakdown" (matched case-insensitively). Three
    # lowercase-"breakdown" 1-minute logs give breakdown_events=3 -> +20, with no
    # minute points (3 min) and no frequent-events points (3 < 5).
    logs = [_down(reason="breakdown", duration="1 min") for _ in range(3)]
    row = _score([_machine()], downtime=logs)[0]
    assert row["breakdown_events"] == 3, row
    assert row["downtime_events"] == 3, row            # < 5, no frequent-events points
    assert row["downtime_minutes"] == 3, row           # < 60, no minute points
    assert row["risk_score"] == 20, row                # repeated-breakdown band only
    assert "repeated breakdown transitions" in row["reasons"]
    print("PASS breakdown_events counts a 'breakdown'-reason log (case-insensitive) = +20")


def test_bands_are_additive_into_the_medium_level():
    # Maintenance (+15) + utilization 95 (+12) + "1 hr 10 min" = 70 min moderate
    # downtime (+15) = 42 -> classify_risk 42 is Medium (>=35, <55).
    row = _score([_machine(status="Maintenance", utilization=95)],
                 downtime=[_down(duration="1 hr 10 min")])[0]
    assert row["downtime_minutes"] == 70, row
    assert row["risk_score"] == 42, row                # 15 + 12 + 15
    assert row["risk_level"] == "Medium"
    for reason in ("machine currently in maintenance",
                   "high utilization above 90%",
                   "moderate accumulated downtime"):
        assert reason in row["reasons"], row
    print("PASS bands add: Maintenance+high-util+moderate-downtime = 42 (Medium)")


def test_empty_machine_list_returns_empty():
    # No machines -> no rows, no crash (empty-dataset edge; sorted([]) is []).
    assert pe.calculate_predictive_risk([], [], [], [], []) == []
    print("PASS empty machine list -> [] (no crash)")


if __name__ == "__main__":
    test_neutral_machine_scores_zero()
    test_maintenance_status_band()
    test_high_utilization_band_and_boundary()
    test_moderate_downtime_band_hour_format()
    test_high_downtime_band_hour_format()
    test_decimal_hours_downtime_band()
    test_frequent_downtime_events_isolated_from_minutes()
    test_moderate_reject_band_and_high_boundary()
    test_breakdown_events_counted_from_downtime_reason_case_insensitive()
    test_bands_are_additive_into_the_medium_level()
    test_empty_machine_list_returns_empty()
    print("ALL PREDICTIVE-ENGINE WEIGHTING TESTS PASSED")
