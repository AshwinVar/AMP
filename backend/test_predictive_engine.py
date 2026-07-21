"""Unit tests for predictive_engine — the maintenance risk scorer.

calculate_predictive_risk turns machine/downtime/production/event/work-order rows
into a 0-100 risk score per machine (with reasons + a recommendation). It drives
the /analytics/predictive-maintenance view and the maintenance agent, so the
weighting must not drift silently. Pure functions, stubbed rows, no DB.

Run:  python backend/test_predictive_engine.py     (exit 0 = pass)
"""
from types import SimpleNamespace

import predictive_engine as pe


def test_parse_duration_digits_only():
    # This engine's parser is deliberately cruder than analytics_engine's: it
    # concatenates every digit, so "2h30m" reads as 230, not 150.
    assert pe.parse_duration_to_minutes("2h30m") == 230
    assert pe.parse_duration_to_minutes("90 min") == 90
    assert pe.parse_duration_to_minutes("") == 0
    assert pe.parse_duration_to_minutes(None) == 0
    print("PASS predictive parse_duration concatenates digits")


def test_classify_risk_boundaries():
    assert pe.classify_risk(75) == "Critical" and pe.classify_risk(74) == "High"
    assert pe.classify_risk(55) == "High" and pe.classify_risk(54) == "Medium"
    assert pe.classify_risk(35) == "Medium" and pe.classify_risk(34) == "Low"
    assert pe.classify_risk(0) == "Low" and pe.classify_risk(100) == "Critical"
    print("PASS classify_risk thresholds at 35/55/75")


def test_recommendation_tracks_bands():
    assert "Immediate" in pe.recommendation(80)
    assert "preventive" in pe.recommendation(60)
    assert "Monitor" in pe.recommendation(40)
    assert "stable" in pe.recommendation(10)
    print("PASS recommendation matches the risk band")


def test_calculate_predictive_risk_scores_and_sorts():
    machines = [
        SimpleNamespace(id=1, name="CNC-1", status="Breakdown", utilization=50),
        SimpleNamespace(id=2, name="CNC-2", status="Running", utilization=60),
    ]
    downtime = [
        SimpleNamespace(machine_id=1, reason="Breakdown", duration="60"),
        SimpleNamespace(machine_id=1, reason="Tool Change", duration="60"),
    ]
    records = [SimpleNamespace(machine_id=1, rejected_count=10, total_count=100)]
    events = [
        SimpleNamespace(machine_id=1, new_status="Breakdown"),
        SimpleNamespace(machine_id=1, new_status="Breakdown"),
    ]
    work_orders = [SimpleNamespace(machine_id=1, status="Running",
                                   target_quantity=600, actual_quantity=50)]
    rows = pe.calculate_predictive_risk(machines, downtime, records, events, work_orders)

    # Highest risk first.
    assert [r["machine_id"] for r in rows] == [1, 2], rows
    hot = rows[0]
    # 35 breakdown + 25 downtime(120) + 20 breakdown_events(3) + 20 reject(10%) +
    # 10 pressure(550) = 110 -> capped at 100.
    assert hot["risk_score"] == 100 and hot["risk_level"] == "Critical", hot
    assert hot["downtime_minutes"] == 120 and hot["reject_rate"] == 10.0
    assert hot["breakdown_events"] == 3 and hot["work_order_pressure"] == 550
    assert "Immediate" in hot["recommendation"]
    assert hot["reasons"] and "no major risk" not in " ".join(hot["reasons"])

    calm = rows[1]
    assert calm["risk_score"] == 0 and calm["risk_level"] == "Low"
    assert calm["reasons"] == ["no major risk indicators detected"]
    print("PASS calculate_predictive_risk scores, sorts, and explains")


def test_predictive_risk_reject_rate_guarded():
    # total_count 0 must not divide-by-zero.
    machines = [SimpleNamespace(id=1, name="M1", status="Running", utilization=70)]
    records = [SimpleNamespace(machine_id=1, rejected_count=0, total_count=0)]
    rows = pe.calculate_predictive_risk(machines, [], records, [], [])
    assert rows[0]["reject_rate"] == 0 and rows[0]["risk_score"] == 0
    print("PASS calculate_predictive_risk guards zero production")


if __name__ == "__main__":
    test_parse_duration_digits_only()
    test_classify_risk_boundaries()
    test_recommendation_tracks_bands()
    test_calculate_predictive_risk_scores_and_sorts()
    test_predictive_risk_reject_rate_guarded()
    print("ALL PREDICTIVE-ENGINE TESTS PASSED")
