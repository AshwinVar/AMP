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


def test_calculate_oee_caps_availability_and_quality():
    # A machine that ran PAST its planned minutes (runtime 500 > planned 480)
    # must not report availability above 100% — availability 500/480 = 1.0417
    # clamps to 1.0 -> 100 (was 104). performance (30*700)/(500*60) = .70 -> 70;
    # quality 690/700 = .9857 -> 99; oee = 1.0 * .70 * .9857 = .69 -> 69.
    over_run = ae.calculate_oee_from_record(_rec(
        runtime_minutes=500, planned_minutes=480,
        ideal_cycle_time_seconds=30, total_count=700, good_count=690,
    ))
    assert over_run["availability"] == 100, over_run
    assert over_run["oee"] == 69, over_run
    # good_count 110 > total_count 100 (a data-entry slip): quality 1.10 clamps
    # to 1.0 -> 100 (was 110). availability 100/100 = 1.0 -> 100; performance
    # (30*100)/(100*60) = .50 -> 50; oee = 1.0 * .50 * 1.0 = .50 -> 50.
    over_good = ae.calculate_oee_from_record(_rec(
        runtime_minutes=100, planned_minutes=100,
        ideal_cycle_time_seconds=30, total_count=100, good_count=110,
    ))
    assert over_good["quality"] == 100, over_good
    assert over_good["oee"] == 50, over_good
    print("PASS calculate_oee_from_record caps availability + quality at 100%")


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


def test_build_smart_alerts_downtime_window_is_recent_not_caller_order():
    """The 'accumulated downtime recently' alert must reflect the MOST-RECENT
    logs no matter how the caller ordered the list. /alerts/smart passes them
    newest-first (id desc, limit 100); a positional [-50:] slice on that read the
    OLDEST 50 and missed recent downtime, while the report export (.all(),
    oldest-first) read the newest 50 — the same helper gave opposite windows.
    Sorting by id inside the helper makes the window order-independent."""
    machine = SimpleNamespace(id=1, name="CNC-1", status="Running", utilization=95)
    # 50 recent logs (ids 51..100), 1 min each = 50 min recent -> BELOW the 60 min
    # threshold, so no alert. 10 OLD logs (ids 1..10), 100 min each = 1000 min,
    # which WOULD fire if (wrongly) counted. Independently derived: recent = 50.
    recent = [SimpleNamespace(id=i, machine_id=1, reason="Minor stop", duration="1m")
              for i in range(51, 101)]
    old = [SimpleNamespace(id=i, machine_id=1, reason="Breakdown", duration="100m")
           for i in range(1, 11)]
    pool = recent + old
    for label, logs in [
        ("newest-first", sorted(pool, key=lambda l: l.id, reverse=True)),  # like /alerts/smart
        ("oldest-first", sorted(pool, key=lambda l: l.id)),                # like the report export
    ]:
        alerts = ae.build_smart_alerts([machine], [], logs)
        fired = any(a["type"] == "Downtime Escalation" for a in alerts)
        assert not fired, f"{label}: recent 50 logs = 50 min (<60) must NOT fire; the 1000 min is stale"

    # And when the RECENT logs themselves exceed 60 min, it fires in either order:
    # 50 x 2 min = 100 min recent.
    heavy = [SimpleNamespace(id=i, machine_id=1, reason="Breakdown", duration="2m")
             for i in range(51, 101)]
    for logs in (sorted(heavy, key=lambda l: l.id, reverse=True),
                 sorted(heavy, key=lambda l: l.id)):
        alerts = ae.build_smart_alerts([machine], [], logs)
        assert any(a["type"] == "Downtime Escalation" for a in alerts), "recent 100 min must fire"
    print("PASS build_smart_alerts downtime window = most-recent 50 logs, order-independent")


def test_build_smart_alerts_empty_downtime_is_safe():
    machine = SimpleNamespace(id=1, name="CNC-1", status="Running", utilization=95)
    alerts = ae.build_smart_alerts([machine], [], [])
    assert not any(a["type"] == "Downtime Escalation" for a in alerts)
    print("PASS build_smart_alerts is safe on empty downtime")


def test_build_smart_alerts_null_utilization_is_safe_and_honest():
    """utilization is Column(Integer, default=0) WITHOUT nullable=False, so a row
    written by raw SQL / a cleared update can be NULL. `None < 40` used to raise
    TypeError and 500 /alerts/smart. A NULL means "no reading", which is NOT a
    measured 0%: the engine must skip the utilization alert (not fabricate a
    'critically low at 0%' from the column default), while still firing the
    status-based alerts. Expected counts are derived by hand, not read back."""
    machines = [
        SimpleNamespace(id=1, name="No-Reading", status="Running", utilization=None),
        SimpleNamespace(id=2, name="Breakdown-No-Reading", status="Breakdown", utilization=None),
        SimpleNamespace(id=3, name="Genuinely-Low", status="Running", utilization=30),
    ]
    alerts = ae.build_smart_alerts(machines, [], [])          # no records, no downtime
    kinds = {(a["machine"], a["type"]) for a in alerts}

    # NULL-utilization machines raise NO "Low Utilization" alert (not a 0% one).
    assert ("No-Reading", "Low Utilization") not in kinds, kinds
    assert ("Breakdown-No-Reading", "Low Utilization") not in kinds, kinds
    # status-based alerts are unaffected by the missing reading.
    assert ("Breakdown-No-Reading", "Breakdown") in kinds, kinds
    # a real low reading still fires (util 30 < 40 -> High).
    assert ("Genuinely-Low", "Low Utilization") in kinds, kinds
    # exactly two alerts total: the breakdown + the one genuine low-utilization.
    assert len(alerts) == 2, alerts
    # no fabricated "0%" leaked into any message.
    assert not any("at 0%" in a["message"] for a in alerts), alerts
    print("PASS build_smart_alerts skips the utilization alert on a NULL reading (no crash, no fake 0%)")


def test_calculate_oee_floors_components_at_zero():
    # The ingest paths reject negative counts/minutes, but a legacy / raw-SQL row
    # can still hold one, and the ratio then goes NEGATIVE. The clamp is symmetric
    # (floor 0, cap 1) so a metric never prints below the floor the data supports.
    #
    # Negative good_count: quality -50/100 = -0.5 -> floored to 0. availability
    # 90/100 = .9 -> 90; performance (30*100)/(90*60)=3000/5400=.5556 -> 56;
    # oee = .9 * .5556 * 0 = 0 (was quality -50, oee -25 before the floor).
    neg_good = ae.calculate_oee_from_record(_rec(
        runtime_minutes=90, planned_minutes=100,
        ideal_cycle_time_seconds=30, total_count=100, good_count=-50,
    ))
    assert neg_good == {"availability": 90, "performance": 56,
                        "quality": 0, "oee": 0}, neg_good

    # Negative runtime_minutes is the subtle one: availability -100/100 = -1.0 and
    # performance 3000/-6000 = -0.5 are BOTH negative, and their product with
    # quality .9 is round(-1.0 * -0.5 * .9 * 100) = +45 — a fabricated positive OEE
    # from two negatives. Flooring each component at 0 collapses it to an honest 0.
    neg_runtime = ae.calculate_oee_from_record(_rec(
        runtime_minutes=-100, planned_minutes=100,
        ideal_cycle_time_seconds=30, total_count=100, good_count=90,
    ))
    assert neg_runtime == {"availability": 0, "performance": 0,
                           "quality": 90, "oee": 0}, neg_runtime
    print("PASS calculate_oee_from_record floors components at 0 (no negative/fake-positive OEE)")


def test_pooled_oee_from_sums_floors_negative_at_zero():
    # A negative good in the pooled sums: quality -50/100 = -0.5 -> floored to 0.
    # availability 100/100 = 1.0 -> 100; performance 3000/(100*60)=.5 -> 50;
    # oee = 1.0 * .5 * 0 = 0 (was quality -50, oee -25 before the floor).
    p = ae.pooled_oee_from_sums(planned=100, runtime=100, total=100,
                                good=-50, ideal_seconds=3000, has_data=True)
    assert p == {"oee": 0, "availability": 100, "performance": 50,
                 "quality": 0, "has_data": True}, p
    print("PASS pooled_oee_from_sums floors a negative component at 0")


def test_per_record_and_pooled_reconcile_on_negative():
    # Rule-3 reconciliation: the per-record view and the pooled view of the SAME
    # single record must agree component-for-component — including on the floored
    # negative — so a drill-down can't disagree with its own headline.
    rec = _rec(runtime_minutes=100, planned_minutes=100,
               ideal_cycle_time_seconds=30, total_count=100, good_count=-50)
    per = ae.calculate_oee_from_record(rec)
    pooled = ae.pooled_oee([rec])
    for key in ("availability", "performance", "quality", "oee"):
        assert per[key] == pooled[key], (key, per, pooled)
        assert per[key] >= 0, (key, per)     # never below the floor
    print("PASS per-record and pooled OEE reconcile (and stay >= 0) on a negative row")


def test_calculate_fallback_oee_monotonic():
    assert ae.calculate_fallback_oee(80) == round((80 / 100) * 0.9 * 0.95 * 100)
    assert ae.calculate_fallback_oee(90) > ae.calculate_fallback_oee(50)
    assert ae.calculate_fallback_oee(0) == 0                  # zero utilization = 0
    assert ae.calculate_fallback_oee(-30) == 0               # negative reading floored, not a negative estimate
    print("PASS calculate_fallback_oee follows utilization monotonically (floored at 0)")


if __name__ == "__main__":
    test_parse_duration_to_minutes()
    test_calculate_oee_known_values()
    test_calculate_oee_zero_guards_and_perf_cap()
    test_calculate_oee_caps_availability_and_quality()
    test_build_shift_kpis()
    test_build_oee_trends_indexes_and_names_machines()
    test_build_management_summary()
    test_management_summary_uses_pooled_not_averaged_oee()
    test_estimated_loss_value_uses_unit_rate_when_given()
    test_oee_direction_is_one_shared_definition_with_a_dead_band()
    test_build_management_summary_empty_is_safe()
    test_build_smart_alerts_severity_and_dedup()
    test_build_smart_alerts_downtime_window_is_recent_not_caller_order()
    test_build_smart_alerts_empty_downtime_is_safe()
    test_build_smart_alerts_null_utilization_is_safe_and_honest()
    test_calculate_oee_floors_components_at_zero()
    test_pooled_oee_from_sums_floors_negative_at_zero()
    test_per_record_and_pooled_reconcile_on_negative()
    test_calculate_fallback_oee_monotonic()
    print("ALL ANALYTICS-ENGINE TESTS PASSED")
