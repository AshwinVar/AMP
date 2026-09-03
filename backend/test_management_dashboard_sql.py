"""/analytics/management — pool plant OEE + shift attainment in SQL (rule-4).

The management dashboard hydrated the whole (growing) production_records and
shift_data tables into Python just to sum them — the rule-4 antipattern already
retired on the sibling /analytics/summary rollup (#411/#419). This pins that the
endpoint now aggregates both tables with func.sum, that the number is byte-for-byte
the list-based build_management_summary result (so nothing about the displayed
summary changed), and that the edges (empty tables, zero shift target, an
hour-format downtime string) still behave.

Expected numbers are derived BY HAND here, independently of the engine, so a broken
pooling / attainment / loss valuation cannot pass the test.

Run:  cd backend && python test_management_dashboard_sql.py   (exit 0 = pass)
"""
import inspect

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import analytics_routes
import models
from analytics_engine import build_management_summary, downtime_aggregates
from database import Base


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed(db):
    # Two machines of very different volume, so the pooled OEE differs from a naive
    # per-record mean and a broken aggregation would show.
    db.add(models.Machine(id=1, name="Big", status="Running", utilization=80))
    db.add(models.Machine(id=2, name="Tiny", status="Breakdown", utilization=10))
    # Big run: planned 1000, runtime 500, ideal 30s, total 500, good 400, rej 100.
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=1000, runtime_minutes=500,
                                   ideal_cycle_time_seconds=30, total_count=500, good_count=400, rejected_count=100))
    # Tiny run: planned 10, runtime 10, ideal 60s, total 10, good 10, rej 0.
    db.add(models.ProductionRecord(machine_id=2, planned_minutes=10, runtime_minutes=10,
                                   ideal_cycle_time_seconds=60, total_count=10, good_count=10, rejected_count=0))
    # Downtime: an HOUR-FORMAT string on the Tiny machine (the exact class of bug the
    # shared duration parser exists to prevent) plus a plain-minutes one on Big.
    db.add(models.DowntimeLog(machine_id=2, reason="Breakdown", duration="2 hrs 15 min"))
    db.add(models.DowntimeLog(machine_id=1, reason="Tool Change", duration="30 min"))
    # Two shifts of very different target, so attainment is genuinely pooled.
    db.add(models.ShiftData(shift_name="A", target_output=1000, actual_output=900))
    db.add(models.ShiftData(shift_name="B", target_output=10, actual_output=10))
    db.commit()


def test_management_dashboard_matches_list_summary_and_pools_in_sql():
    db = _fresh_session()
    _seed(db)

    out = analytics_routes.get_management_dashboard(db=db, current_user={})

    # 1) Byte-for-byte parity with the list-based engine over the same rows. This is
    #    the honesty guarantee: the SQL aggregation changed nothing the user sees.
    machines = db.query(models.Machine).all()
    downtime = db.query(models.DowntimeLog).all()
    shifts = db.query(models.ShiftData).all()
    records = db.query(models.ProductionRecord).all()
    expected = build_management_summary(machines, downtime, shifts, records, unit_value_gbp=None)
    assert out == expected, (out, expected)

    # 2) Independently-derived numbers (not just "equal to the other code path").
    # Pooled sums: planned 1010, runtime 510, total 510, good 410,
    #   ideal_seconds = 30*500 + 60*10 = 15600.
    # availability = min(510/1010,1)      -> round(*100) = 50
    # performance  = min(15600/(510*60),1)-> round(*100) = 51
    # quality      = min(410/510,1)       -> round(*100) = 80
    # oee          = round(.50495*.5098*.80392*100) = 21
    assert out["avg_availability"] == 50, out
    assert out["avg_performance"] == 51, out
    assert out["avg_quality"] == 80, out
    assert out["avg_oee"] == 21, out
    # NOT the mean of the per-record OEEs (Big 20, Tiny 100 -> 60).
    assert out["avg_oee"] != 60, "plant OEE must pool by volume, not average per-record OEE"

    # Downtime totals via the shared free-text parser: "2 hrs 15 min"=135 + "30 min"=30.
    assert out["total_downtime_minutes"] == 165, out
    # Tiny lost the most time -> worst machine; loss reason ranks by MINUTES.
    assert out["worst_machine"] == "Tiny", out
    assert out["worst_machine_downtime"] == 135, out
    assert out["top_loss_reason"] == "Breakdown", out

    # Shift attainment pooled: (900+10)/(1000+10) = 910/1010 = round(90.099..) = 90.
    assert out["target_achievement"] == 90, out
    assert out["target_output"] == 1010 and out["actual_output"] == 910, out
    # NOT the mean of per-shift efficiency (90% and 100% -> 95%).
    assert out["target_achievement"] != 95, "attainment must pool, not average per-shift ratios"

    # Loss units at the observed run-rate good/runtime = 410/510 per minute over 165
    # downtime minutes = round(165 * 410/510) = round(132.6..) = 133. No £ rate
    # configured (DEFAULT tenant), so the legacy £8/min proxy: 165*8 = 1320.
    assert out["estimated_loss_units"] == 133, out
    assert out["estimated_loss_value"] == 1320, out

    assert out["breakdown_count"] == 1 and out["machine_count"] == 2, out

    # 3) Regression guard: the compute must aggregate in SQL, never re-hydrate the
    #    growing tables with a whole-table .all() scan (rule-4).
    src = inspect.getsource(analytics_routes.get_management_dashboard)
    assert "db.query(models.ProductionRecord).all()" not in src, \
        "management dashboard must pool production_records in SQL, not hydrate the whole table"
    assert "db.query(models.ShiftData).all()" not in src, \
        "management dashboard must sum shift_data in SQL, not hydrate the whole table"
    assert "func.sum(models.ProductionRecord" in src and "func.sum(models.ShiftData" in src, \
        "management dashboard should sum both tables with func.sum"
    print("PASS management dashboard: pooled in SQL (a50/p51/q80/oee21, attain90, dt165), parity with list path")


def test_management_dashboard_sums_path_parity_is_exact_unit():
    # Same guarantee at the engine level, independent of the DB: feeding
    # build_management_summary the pre-aggregated sums must produce the identical dict
    # as feeding it the rows. Pins that the sums entry-point is a pure equivalent.
    db = _fresh_session()
    _seed(db)
    machines = db.query(models.Machine).all()
    downtime = db.query(models.DowntimeLog).all()
    shifts = db.query(models.ShiftData).all()
    records = db.query(models.ProductionRecord).all()

    via_rows = build_management_summary(machines, downtime, shifts, records, unit_value_gbp=2.5)
    prod_sums = (
        sum(r.planned_minutes for r in records),
        sum(r.runtime_minutes for r in records),
        sum(r.total_count for r in records),
        sum(r.good_count for r in records),
        sum(r.ideal_cycle_time_seconds * r.total_count for r in records),
        len(records),
    )
    shift_sums = (sum(s.target_output for s in shifts), sum(s.actual_output for s in shifts))
    via_sums = build_management_summary(
        machines, downtime, [], [], unit_value_gbp=2.5,
        production_sums=prod_sums, shift_sums=shift_sums)
    assert via_rows == via_sums, (via_rows, via_sums)
    # And the £ path honoured the configured rate: 133 loss units * £2.5 = round(332.5)=332.
    assert via_sums["estimated_loss_value"] == round(133 * 2.5), via_sums
    print("PASS build_management_summary: sums entry-point == rows entry-point (incl. configured £ rate)")


def test_management_dashboard_downtime_aggregate_parity():
    """The downtime rows are the LAST growing table this endpoint hydrates.

    `/analytics/management` scans downtime_logs whole, on a table that grows with
    TIME rather than with the size of the factory — the same defect #531 fixed on
    the two polled endpoints, left behind there only because this caller hands
    the row LIST to build_management_summary rather than consuming it locally.

    The fix is the pattern this file already establishes for production and
    shifts: a pre-aggregated entry point that must produce a byte-identical
    dict. `downtime_aggregates` does the tally in SQL with a GROUP BY, which is
    exact rather than approximate — so this is an equivalence, not a tolerance.
    """
    db = _fresh_session()
    _seed(db)
    machines = db.query(models.Machine).all()
    downtime = db.query(models.DowntimeLog).all()
    shifts = db.query(models.ShiftData).all()
    records = db.query(models.ProductionRecord).all()

    via_rows = build_management_summary(machines, downtime, shifts, records,
                                        unit_value_gbp=2.5)
    via_agg = build_management_summary(machines, [], shifts, records,
                                       unit_value_gbp=2.5,
                                       downtime_agg=downtime_aggregates(db))
    assert via_rows == via_agg, (via_rows, via_agg)
    # Non-vacuous: the fixture must actually carry downtime, or an empty list
    # would equal an empty aggregate and this would prove nothing.
    assert via_rows["total_downtime_minutes"] > 0, via_rows
    assert via_rows["top_loss_reason"] != "No data", via_rows
    print("PASS build_management_summary: downtime aggregate == downtime rows")


def test_management_dashboard_empty_tables_are_zero_not_a_crash():
    # No production, no shifts, no downtime: the SQL COALESCE(SUM,0)/COUNT drives the
    # empty path (has_data False), so every headline is a clean 0 and nothing divides
    # by an empty aggregate.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="M", status="Idle", utilization=0))
    db.commit()
    out = analytics_routes.get_management_dashboard(db=db, current_user={})
    assert out["avg_oee"] == 0 and out["avg_availability"] == 0, out
    assert out["total_downtime_minutes"] == 0, out
    assert out["target_achievement"] == 0, out          # 0 target -> 0, not a crash
    assert out["estimated_loss_units"] == 0 and out["estimated_loss_value"] == 0, out
    assert out["worst_machine"] == "No data" and out["top_loss_reason"] == "No data", out
    assert out["machine_count"] == 1 and out["breakdown_count"] == 0, out
    print("PASS management dashboard: empty tables -> all-zero summary, no divide-by-empty crash")


def test_management_dashboard_zero_target_shift_is_zero_not_a_crash():
    # A shift with a 0 target (unplanned) must not divide-by-zero the attainment: the
    # pooled denominator is the summed target, so a single 0-target shift with real
    # output contributes output to the numerator and 0 to the denominator.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="M", status="Running", utilization=50))
    db.add(models.ShiftData(shift_name="Unplanned", target_output=0, actual_output=40))
    db.commit()
    out = analytics_routes.get_management_dashboard(db=db, current_user={})
    # Only shift has target 0 -> total target 0 -> attainment 0 (honest, not a crash).
    assert out["target_output"] == 0 and out["actual_output"] == 40, out
    assert out["target_achievement"] == 0, out
    print("PASS management dashboard: zero-target shift -> attainment 0 (no divide-by-zero)")


if __name__ == "__main__":
    test_management_dashboard_matches_list_summary_and_pools_in_sql()
    test_management_dashboard_sums_path_parity_is_exact_unit()
    test_management_dashboard_downtime_aggregate_parity()
    test_management_dashboard_empty_tables_are_zero_not_a_crash()
    test_management_dashboard_zero_target_shift_is_zero_not_a_crash()
    print("\nAll management-dashboard SQL-aggregate tests passed.")
