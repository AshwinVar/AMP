"""Unit tests for predictive_engine — the maintenance risk scorer.

calculate_predictive_risk turns machine/downtime/production/event/work-order rows
into a 0-100 risk score per machine (with reasons + a recommendation). It drives
the /analytics/predictive-maintenance view and the maintenance agent, so the
weighting must not drift silently. Pure functions, stubbed rows, no DB.

Run:  python backend/test_predictive_engine.py     (exit 0 = pass)
"""
from types import SimpleNamespace

import predictive_engine as pe


def test_uses_shared_duration_parser():
    # predictive_engine used to carry a digit-concatenation parser that misread
    # hour formats ("1 hr" -> 1 minute), understating downtime in the risk score.
    # It now delegates to the shared, correct parser (full coverage in
    # test_duration); here we just confirm hours are parsed, not concatenated.
    import duration
    assert pe.parse_duration_to_minutes is duration.parse_duration_to_minutes
    assert pe.parse_duration_to_minutes("1 hr") == 60
    assert pe.parse_duration_to_minutes("2 hrs 15 min") == 135
    print("PASS predictive_engine uses the shared correct duration parser")


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


def test_null_utilization_scored_as_column_default_zero():
    # Machine.utilization is Column(Integer, default=0) WITHOUT nullable=False, so
    # a row written by raw SQL / a migration / a clearing update can be NULL. The
    # scorer used to do `None < 40` and raise TypeError, 500-ing the endpoint.
    # A missing utilization is treated as the column's own default (0), which is
    # < 40, so only the "low utilization" rule fires (+20) and nothing else.
    machines = [SimpleNamespace(id=1, name="M1", status="Running", utilization=None)]
    rows = pe.calculate_predictive_risk(machines, [], [], [], [])
    assert rows[0]["risk_score"] == 20, rows[0]
    assert rows[0]["utilization"] == 0                        # display matches what was scored
    assert "low utilization below 40%" in rows[0]["reasons"]
    assert rows[0]["risk_level"] == "Low"
    print("PASS calculate_predictive_risk treats NULL utilization as 0, no crash")


def test_null_actual_quantity_counts_as_zero():
    # WorkOrder.actual_quantity is Column(Integer, default=0) WITHOUT
    # nullable=False, so it can be NULL. `target - None` used to raise TypeError.
    # A missing actual is counted as 0, so pressure = 600 - 0 = 600 (>=500) -> +10.
    machines = [SimpleNamespace(id=1, name="M1", status="Running", utilization=55)]
    work_orders = [SimpleNamespace(machine_id=1, status="Running",
                                   target_quantity=600, actual_quantity=None)]
    rows = pe.calculate_predictive_risk(machines, [], [], [], work_orders)
    assert rows[0]["work_order_pressure"] == 600, rows[0]
    assert rows[0]["risk_score"] == 10, rows[0]
    assert "high active work-order load" in rows[0]["reasons"]
    print("PASS calculate_predictive_risk treats NULL actual_quantity as 0, no crash")


def test_null_production_counts_counted_as_zero():
    # ProductionRecord.total_count / rejected_count are declared nullable=False, but
    # that constraint isn't retro-applied to pre-existing rows, so a legacy / raw-SQL
    # / migration row can still hold NULL — exactly the rows _int exists for, and the
    # same columns analytics_engine.pooled_oee already coalesces with `or 0`. The
    # scorer summed them with `defaultdict(int) += None`, which raised TypeError and
    # 500-ed the predictive-maintenance endpoint. A missing count means no measured
    # production: it contributes 0, and total 0 leaves reject_rate at 0 (divide guard).
    machines = [SimpleNamespace(id=1, name="M1", status="Running", utilization=70)]
    records = [SimpleNamespace(machine_id=1, rejected_count=None, total_count=None)]
    rows = pe.calculate_predictive_risk(machines, [], records, [], [])
    assert rows[0]["reject_rate"] == 0, rows[0]
    assert rows[0]["risk_score"] == 0, rows[0]            # no rule fires on a clean 70% machine
    print("PASS calculate_predictive_risk treats NULL production counts as 0, no crash")


def test_null_production_counts_mixed_with_real_rows():
    # A NULL-count legacy row must not corrupt the reject_rate of REAL rows on the
    # same machine: only the good rows contribute. Two records — one all-NULL, one
    # real (8 rejects / 200 total = 4.0%, below the 5% band so no reject points).
    machines = [SimpleNamespace(id=1, name="M1", status="Running", utilization=70)]
    records = [
        SimpleNamespace(machine_id=1, rejected_count=None, total_count=None),
        SimpleNamespace(machine_id=1, rejected_count=8, total_count=200),
    ]
    rows = pe.calculate_predictive_risk(machines, [], records, [], [])
    # reject_rate = 8 / 200 = 4.0%  (the NULL row adds 0 to both sums)
    assert rows[0]["reject_rate"] == 4.0, rows[0]
    assert rows[0]["risk_score"] == 0, rows[0]            # 4.0% < 5% band, nothing fires
    print("PASS calculate_predictive_risk ignores NULL-count rows when pooling reject rate")


def test_null_target_quantity_counts_as_zero_pressure():
    # WorkOrder.target_quantity is nullable=False, but a legacy / raw-SQL / migration
    # row can hold NULL. `None - actual` raised TypeError on the same line that already
    # guards actual_quantity — 500-ing the endpoint. No known target = no known demand,
    # so pressure floors to 0 (max(0 - actual, 0)) rather than crashing or inventing one.
    machines = [SimpleNamespace(id=1, name="M1", status="Running", utilization=55)]
    work_orders = [SimpleNamespace(machine_id=1, status="Running",
                                   target_quantity=None, actual_quantity=50)]
    rows = pe.calculate_predictive_risk(machines, [], [], [], work_orders)
    assert rows[0]["work_order_pressure"] == 0, rows[0]
    assert rows[0]["risk_score"] == 0, rows[0]            # 0 pressure, no other rule fires
    assert "high active work-order load" not in rows[0]["reasons"]
    print("PASS calculate_predictive_risk treats NULL target_quantity as 0 pressure, no crash")


if __name__ == "__main__":
    test_uses_shared_duration_parser()
    test_classify_risk_boundaries()
    test_recommendation_tracks_bands()
    test_calculate_predictive_risk_scores_and_sorts()
    test_predictive_risk_reject_rate_guarded()
    test_null_utilization_scored_as_column_default_zero()
    test_null_actual_quantity_counts_as_zero()
    test_null_production_counts_counted_as_zero()
    test_null_production_counts_mixed_with_real_rows()
    test_null_target_quantity_counts_as_zero_pressure()
    print("ALL PREDICTIVE-ENGINE TESTS PASSED")
