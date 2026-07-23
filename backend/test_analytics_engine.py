"""Unit tests for analytics_engine — the pure compute core.

These functions turn raw ORM rows into the OEE / KPI / alert numbers that every
dashboard, report, agent and read-model depends on. They are pure (plain args in,
dicts/lists out), so a regression in the math is both easy to introduce and
invisible until a customer sees a wrong number. This locks the arithmetic down
with lightweight stub rows (SimpleNamespace) — no DB needed.

Run:  python backend/test_analytics_engine.py     (exit 0 = pass)
"""
from types import SimpleNamespace

import analytics_engine as ae


def _rec(**kw):
    kw.setdefault("machine", None)
    return SimpleNamespace(**kw)


def test_parse_duration_to_minutes():
    assert ae.parse_duration_to_minutes("2h 30m") == 150
    assert ae.parse_duration_to_minutes("1h30m") == 90
    assert ae.parse_duration_to_minutes("45m") == 45
    assert ae.parse_duration_to_minutes("3h") == 180
    assert ae.parse_duration_to_minutes("90") == 90          # bare number = minutes
    assert ae.parse_duration_to_minutes("") == 0
    assert ae.parse_duration_to_minutes(None) == 0
    print("PASS parse_duration_to_minutes handles h/m/bare/empty")


def test_calculate_oee_known_values():
    # availability 400/480=.8333, performance (30*700)/(400*60)=.875,
    # quality 690/700=.9857 -> oee .71875 -> 72.
    oee = ae.calculate_oee_from_record(_rec(
        runtime_minutes=400, planned_minutes=480,
        ideal_cycle_time_seconds=30, total_count=700, good_count=690,
    ))
    assert oee == {"availability": 83, "performance": 88, "quality": 99, "oee": 72}, oee
    print("PASS calculate_oee_from_record computes the OEE components")


def test_calculate_oee_zero_guards_and_perf_cap():
    # Every divisor is guarded — no ZeroDivisionError, components fall to 0.
    z = ae.calculate_oee_from_record(_rec(
        runtime_minutes=0, planned_minutes=0,
        ideal_cycle_time_seconds=0, total_count=0, good_count=0,
    ))
    assert z == {"availability": 0, "performance": 0, "quality": 0, "oee": 0}, z
    # Performance is capped at 100% even when the ideal cycle time implies >1.
    fast = ae.calculate_oee_from_record(_rec(
        runtime_minutes=100, planned_minutes=100,
        ideal_cycle_time_seconds=600, total_count=100, good_count=100,
    ))
    assert fast["performance"] == 100 and fast["oee"] == 100, fast
    print("PASS calculate_oee_from_record guards divide-by-zero + caps performance")


def test_build_shift_kpis():
    rows = ae.build_shift_kpis([
        SimpleNamespace(shift_name="A", target_output=100, actual_output=90),
        SimpleNamespace(shift_name="B", target_output=0, actual_output=0),
    ])
    assert rows[0] == {"shift_name": "A", "target_output": 100, "actual_output": 90,
                       "efficiency": 90, "gap": 10}, rows[0]
    assert rows[1]["efficiency"] == 0, rows[1]  # zero target -> 0, not a crash
    print("PASS build_shift_kpis computes efficiency + gap (zero target guarded)")


def test_build_oee_trends_indexes_and_names_machines():
    r1 = _rec(machine_id=1, machine=SimpleNamespace(name="CNC-1"),
              runtime_minutes=400, planned_minutes=480, ideal_cycle_time_seconds=30,
              total_count=700, good_count=690, rejected_count=10)
    r2 = _rec(machine_id=2, machine=None,
              runtime_minutes=100, planned_minutes=100, ideal_cycle_time_seconds=30,
              total_count=100, good_count=100, rejected_count=0)
    rows = ae.build_oee_trends([r1, r2])
    assert rows[0]["record"] == 1 and rows[0]["machine_name"] == "CNC-1"
    assert rows[1]["record"] == 2 and rows[1]["machine_name"] == "Machine 2"  # fallback name
    assert rows[0]["oee"] == 72
    print("PASS build_oee_trends indexes records and falls back on missing machine name")


def test_build_management_summary():
    machines = [SimpleNamespace(id=1, name="CNC-1", status="Breakdown"),
                SimpleNamespace(id=2, name="CNC-2", status="Running")]
    downtime = [SimpleNamespace(machine_id=1, reason="Tool Change", duration="1h"),
                SimpleNamespace(machine_id=1, reason="Breakdown", duration="90m"),
                SimpleNamespace(machine_id=2, reason="Tool Change", duration="30m")]
    shifts = [SimpleNamespace(target_output=100, actual_output=80)]
    recs = [_rec(runtime_minutes=400, planned_minutes=480, ideal_cycle_time_seconds=30,
                 total_count=700, good_count=690)]
    s = ae.build_management_summary(machines, downtime, shifts, recs)
    assert s["total_downtime_minutes"] == 180                     # 60 + 90 + 30
    assert s["estimated_loss_value"] == 180 * 8                   # £8/min model
    assert s["top_loss_reason"] == "Tool Change"                 # 60+30=90 > 90 breakdown? tie -> first max
    assert s["worst_machine"] == "CNC-1" and s["worst_machine_downtime"] == 150
    assert s["breakdown_count"] == 1 and s["machine_count"] == 2
    assert s["target_achievement"] == 80 and s["avg_oee"] == 72
    print("PASS build_management_summary rolls up downtime, loss, worst machine, OEE")


def test_management_summary_uses_pooled_not_averaged_oee():
    # A tiny perfect run + a large poor run. Averaging per-record OEE would give
    # (100 + 20) / 2 = 60; pooling (ratio of sums) weights by volume and gives 21.
    # This pins the standardised pooled aggregation.
    recs = [
        _rec(planned_minutes=10, runtime_minutes=10, ideal_cycle_time_seconds=60,
             total_count=10, good_count=10),
        _rec(planned_minutes=1000, runtime_minutes=500, ideal_cycle_time_seconds=30,
             total_count=500, good_count=400),
    ]
    s = ae.build_management_summary([], [], [], recs)
    assert s["avg_oee"] == ae.pooled_oee(recs)["oee"], s["avg_oee"]
    assert s["avg_oee"] == 21 and s["avg_oee"] != 60, s["avg_oee"]
    print("PASS build_management_summary pools OEE (volume-weighted), not averages")


def test_estimated_loss_value_uses_unit_rate_when_given():
    machines = [SimpleNamespace(id=1, name="M1", status="Running")]
    downtime = [SimpleNamespace(machine_id=1, reason="Breakdown", duration="2 hrs")]  # 120 min
    shifts = [SimpleNamespace(target_output=100, actual_output=90)]
    recs = [_rec(runtime_minutes=400, planned_minutes=480, ideal_cycle_time_seconds=30,
                 total_count=700, good_count=690)]
    # run-rate = 690 good / 400 min = 1.725 units/min; 120 min downtime -> 207 lost units.
    no_rate = ae.build_management_summary(machines, downtime, shifts, recs)
    assert no_rate["estimated_loss_units"] == 207
    assert no_rate["estimated_loss_value"] == 120 * 8       # legacy £8/min when no rate
    assert no_rate["unit_value_gbp"] is None

    priced = ae.build_management_summary(machines, downtime, shifts, recs, unit_value_gbp=4.50)
    assert priced["estimated_loss_units"] == 207
    assert priced["estimated_loss_value"] == round(207 * 4.50)   # lost units x tenant rate
    assert priced["unit_value_gbp"] == 4.50

    # A configured rate of 0 is a real £0 margin -> £0, NOT the £8/min proxy. Using
    # truthiness (`if unit_value_gbp`) fabricated 120*8 = £960 for a £0 tenant.
    zero = ae.build_management_summary(machines, downtime, shifts, recs, unit_value_gbp=0)
    assert zero["estimated_loss_units"] == 207
    assert zero["estimated_loss_value"] == 0                # 207 lost units x £0, not 120*8
    assert zero["unit_value_gbp"] == 0
    print("PASS estimated_loss_value = lost units x rate; £0 rate -> £0 (not the £8/min proxy)")


def test_oee_direction_is_one_shared_definition_with_a_dead_band():
    """The single week-over-week OEE direction (shared by the recovery trend badge
    and the scorecard OEE delta). A sub-dead-band move is 'flat' so it can't read
    'down'/red on one exec surface and 'flat' on the other."""
    assert ae.OEE_TREND_DEAD_BAND == 2
    assert ae.oee_direction(69, 70) == "flat"     # -1: inside the dead-band
    assert ae.oee_direction(71, 70) == "flat"     # +1: inside the dead-band
    assert ae.oee_direction(68, 70) == "down"     # -2: at the edge -> moving
    assert ae.oee_direction(72, 70) == "up"       # +2: at the edge -> moving
    assert ae.oee_direction(70, None) is None     # no prior week
    print("PASS oee_direction: one shared WoW definition with a single dead-band")


def test_build_management_summary_empty_is_safe():
    s = ae.build_management_summary([], [], [], [])
    assert s["top_loss_reason"] == "No data" and s["worst_machine"] == "No data"
    assert s["total_downtime_minutes"] == 0 and s["avg_oee"] == 0
    print("PASS build_management_summary returns safe defaults on empty input")


def test_build_smart_alerts_severity_and_dedup():
    machines = [SimpleNamespace(id=1, name="CNC-1", status="Breakdown", utilization=30),
                SimpleNamespace(id=2, name="CNC-2", status="Running", utilization=95)]
    # CNC-1: low OEE + high reject on its latest record.
    recs = [_rec(id=10, machine_id=1, machine=machines[0],
                 runtime_minutes=100, planned_minutes=480, ideal_cycle_time_seconds=5,
                 total_count=100, good_count=80, rejected_count=20)]
    downtime = [SimpleNamespace(machine_id=1, reason="Breakdown", duration="90m")]
    alerts = ae.build_smart_alerts(machines, recs, downtime)
    kinds = {(a["machine"], a["type"]) for a in alerts}
    assert ("CNC-1", "Breakdown") in kinds
    assert ("CNC-1", "Low Utilization") in kinds        # util 30 < 40 -> High
    assert any(a["type"] in ("OEE Degradation", "Low OEE") for a in alerts)
    assert ("CNC-1", "Quality Escalation") in kinds     # reject 20% > 8%
    assert ("CNC-1", "Downtime Escalation") in kinds    # 90 min > 60
    # dedup: no (machine, type) pair appears twice
    pairs = [(a["machine"], a["type"]) for a in alerts]
    assert len(pairs) == len(set(pairs)), pairs
    print(f"PASS build_smart_alerts raises the right severities and dedups ({len(alerts)} alerts)")


def test_calculate_fallback_oee_monotonic():
    assert ae.calculate_fallback_oee(80) == round((80 / 100) * 0.9 * 0.95 * 100)
    assert ae.calculate_fallback_oee(90) > ae.calculate_fallback_oee(50)
    print("PASS calculate_fallback_oee follows utilization monotonically")


if __name__ == "__main__":
    test_parse_duration_to_minutes()
    test_calculate_oee_known_values()
    test_calculate_oee_zero_guards_and_perf_cap()
    test_build_shift_kpis()
    test_build_oee_trends_indexes_and_names_machines()
    test_build_management_summary()
    test_management_summary_uses_pooled_not_averaged_oee()
    test_estimated_loss_value_uses_unit_rate_when_given()
    test_oee_direction_is_one_shared_definition_with_a_dead_band()
    test_build_management_summary_empty_is_safe()
    test_build_smart_alerts_severity_and_dedup()
    test_calculate_fallback_oee_monotonic()
    print("ALL ANALYTICS-ENGINE TESTS PASSED")
