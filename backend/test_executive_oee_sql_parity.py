"""Reference oracle: /analytics/executive-oee must not change its numbers.

The endpoint hydrated FIVE whole tables into Python on every call - and the
dashboard polls it. At a year of a twenty-machine plant (7,300 production
records) that measured 265 ms per call, of which 94 ms was hydrating the
production rows alone; the equivalent SQL GROUP BY over the same rows took
5.4 ms.

Moving the arithmetic into SQL is only safe if the numbers come out identical,
so this file keeps the PRE-CHANGE implementation verbatim and asserts the new
one agrees with it field for field, on data built to hit every branch: a
machine with no production at all (falls back to utilisation / status
constants), zero planned minutes, good_count > total_count (must clamp),
runtime > planned (must clamp), NULL quality columns, a quality row with no
machine, and an empty database.

Same approach as #380, where the pre-fix implementation was kept as the oracle
for the GRN/cycle-count paging rewrite.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./ci.db")

from duration import parse_duration_to_minutes
from analytics_engine import pooled_oee
import models


def reference_executive_oee(db):
    machines = db.query(models.Machine).all()
    downtime_logs = db.query(models.DowntimeLog).all()
    production_records = db.query(models.ProductionRecord).all()
    shifts = db.query(models.ShiftData).all()
    quality_rows = db.query(models.QualityInspection).all()

    machine_map = {machine.id: machine.name for machine in machines}

    production_by_machine = {}
    for record in production_records:
        production_by_machine.setdefault(record.machine_id, []).append(record)

    downtime_by_machine = {}
    reason_counts = {}

    for log in downtime_logs:
        minutes = parse_duration_to_minutes(log.duration)
        downtime_by_machine[log.machine_id] = downtime_by_machine.get(log.machine_id, 0) + minutes
        reason_counts[log.reason] = reason_counts.get(log.reason, 0) + minutes

    quality_by_machine = {}
    for row in quality_rows:
        if not row.machine_id:
            continue
        bucket = quality_by_machine.setdefault(
            row.machine_id,
            {"inspected": 0, "passed": 0, "failed": 0, "scrap": 0, "rework": 0},
        )
        # passed / failed / scrap / rework_quantity are nullable Integers (default=0,
        # no nullable=False) — coalesce to 0 so a real SQL NULL can't 500 the exec
        # rollup via int + None (inspected_quantity is nullable=False).
        bucket["inspected"] += row.inspected_quantity
        bucket["passed"] += row.passed_quantity or 0
        bucket["failed"] += row.failed_quantity or 0
        bucket["scrap"] += row.scrap_quantity or 0
        bucket["rework"] += row.rework_quantity or 0

    machine_rows = []

    for machine in machines:
        records = production_by_machine.get(machine.id, [])
        planned_minutes = sum(record.planned_minutes for record in records)
        runtime_minutes = sum(record.runtime_minutes for record in records)
        ideal_cycle_total = sum(
            record.ideal_cycle_time_seconds * record.total_count
            for record in records
        )
        total_count = sum(record.total_count for record in records)
        good_count = sum(record.good_count for record in records)
        rejected_count = sum(record.rejected_count for record in records)

        if planned_minutes > 0:
            # Cap at 100% like pooled_oee / calculate_oee_from_record cap every
            # component: the HTTP ingest (machines_routes.create_production_record)
            # rejects negatives and enforces good+rejected==total, but it does NOT
            # require runtime_minutes <= planned_minutes, so a machine that ran past
            # its planned window (runtime > planned) computed availability > 100%.
            # An availability the data can't support (>100%) then inflated THIS
            # machine's OEE above the physical bound and disagreed with the capped
            # pooled plant rollup below — the exact honesty/reconciliation rule the
            # shared OEE definition already follows (performance is clamped the same
            # way three lines down). min(ratio, 1) before rounding matches pooled_oee.
            availability = round(min(runtime_minutes / planned_minutes, 1) * 100)
        else:
            # No production for this machine — fall back to its utilization as a
            # rough availability. utilization is nullable, so treat an unset reading
            # as 0 (max() also floors any stray negative), never `max(None, 0)`.
            availability = max(machine.utilization or 0, 0)

        runtime_seconds = runtime_minutes * 60
        if runtime_seconds > 0:
            performance = round(min((ideal_cycle_total / runtime_seconds), 1) * 100)
        else:
            performance = 90 if machine.status == "Running" else 60

        if total_count > 0:
            # Clamp to 100% too (same shared OEE definition): the main write paths
            # enforce good <= total, but a data-entry slip / raw-SQL write can store
            # good_count > total_count, and an uncapped good/total would print a
            # quality above 100% — a figure the data can't support. min(ratio, 1)
            # keeps every normal record unchanged (good <= total -> ratio <= 1).
            quality = round(min(good_count / total_count, 1) * 100)
        else:
            q = quality_by_machine.get(machine.id)
            if q and q["inspected"] > 0:
                quality = round((q["passed"] / q["inspected"]) * 100)
            else:
                quality = 95

        # FLOOR every component at 0 as well as capping at 100 — mirror of the real
        # endpoint's symmetric clamp (kept verbatim so this stays a faithful oracle).
        # A no-op on every well-formed machine; only a negative-count legacy/raw-SQL
        # row (SUM goes negative) or an over-100 inspection fallback is affected.
        availability = max(0, min(100, availability))
        performance = max(0, min(100, performance))
        quality = max(0, min(100, quality))

        oee = round((availability / 100) * (performance / 100) * (quality / 100) * 100)

        machine_rows.append(
            {
                "machine_id": machine.id,
                "machine_name": machine.name,
                "status": machine.status,
                "availability": availability,
                "performance": performance,
                "quality": quality,
                "oee": oee,
                "downtime_minutes": downtime_by_machine.get(machine.id, 0),
                "total_count": total_count,
                "good_count": good_count,
                "rejected_count": rejected_count,
                "utilization": machine.utilization,
            }
        )

    machine_rows.sort(key=lambda row: row["oee"], reverse=True)

    # Plant rollup is POOLED (ratio of sums) — the single standardised OEE
    # definition (analytics_engine.pooled_oee), so /analytics/executive-oee agrees
    # with /oee-summary and every other surface. Averaging per-machine OEE was a
    # mean of ratios that over-weighted small runs AND folded in the no-data
    # fallback constants (utilization / 90-or-60 / 95), giving the exec home a
    # plant OEE that contradicted the pooled one shown elsewhere.
    plant = pooled_oee([rec for recs in production_by_machine.values() for rec in recs])
    plant_availability = plant["availability"]
    plant_performance = plant["performance"]
    plant_quality = plant["quality"]
    plant_oee = plant["oee"]

    total_target = sum(shift.target_output for shift in shifts)
    total_actual = sum(shift.actual_output for shift in shifts)
    plan_achievement = round((total_actual / total_target) * 100) if total_target else 0

    downtime_pareto = [
        {"reason": reason, "minutes": minutes}
        for reason, minutes in sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)
    ]

    shift_rows = []
    for shift in shifts:
        efficiency = round((shift.actual_output / shift.target_output) * 100) if shift.target_output else 0
        shift_rows.append(
            {
                "shift_name": shift.shift_name,
                "target_output": shift.target_output,
                "actual_output": shift.actual_output,
                "efficiency": efficiency,
            }
        )

    quality_defects = {}
    for row in quality_rows:
        key = row.defect_category or "No Defect"
        quality_defects[key] = quality_defects.get(key, 0) + (row.failed_quantity or 0)

    quality_trend = [
        {"defect": defect, "failed_quantity": qty}
        for defect, qty in sorted(quality_defects.items(), key=lambda item: item[1], reverse=True)
    ]

    return {
        "plant_availability": plant_availability,
        "plant_performance": plant_performance,
        "plant_quality": plant_quality,
        "plant_oee": plant_oee,
        "machine_ranking": machine_rows,
        "downtime_pareto": downtime_pareto,
        "shift_oee": shift_rows,
        "quality_trend": quality_trend,
        "production_target": total_target,
        "production_actual": total_actual,
        "production_achievement": plan_achievement,
        "running_machines": len([machine for machine in machines if machine.status == "Running"]),
        "breakdown_machines": len([machine for machine in machines if machine.status == "Breakdown"]),
        # Added when Offline stopped being a status that no rollup counted
        # (test_machine_status_buckets.py). This reference exists to be compared
        # key-for-key against the endpoint, so it has to grow with it — and the
        # comparison is exactly what caught the omission when it did not.
        "offline_machines": len([machine for machine in machines if machine.status == "Offline"]),
    }


# ---------------------------------------------------------------- the parity test

import json
import tempfile

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _fresh_db():
    path = os.path.join(tempfile.mkdtemp(), "parity.db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _machine(db, name, status="Running", utilization=70):
    m = models.Machine(name=name, status=status, utilization=utilization,
                       downtime="0 min", tenant_code="DEFAULT")
    db.add(m)
    db.flush()
    return m


def _seed_every_branch(db):
    """One machine per branch the function can take."""
    normal = _machine(db, "Normal")
    over = _machine(db, "RanPastPlan")
    slip = _machine(db, "DataEntrySlip")
    zero = _machine(db, "ZeroPlanned")
    idle_no_prod = _machine(db, "NoProductionIdle", status="Idle", utilization=41)
    run_no_prod = _machine(db, "NoProductionRunning", status="Running", utilization=0)
    qual_only = _machine(db, "QualityOnly", status="Breakdown", utilization=12)

    def prod(machine, planned, runtime, ideal, total, good, rejected):
        db.add(models.ProductionRecord(
            machine_id=machine.id, planned_minutes=planned, runtime_minutes=runtime,
            ideal_cycle_time_seconds=ideal, total_count=total, good_count=good,
            rejected_count=rejected, tenant_code="DEFAULT"))

    prod(normal, 480, 400, 30, 600, 560, 40)
    prod(normal, 480, 380, 30, 590, 570, 20)
    # runtime > planned: availability must clamp to 100, not 104
    prod(over, 480, 500, 30, 700, 690, 10)
    # good > total: quality must clamp to 100
    prod(slip, 480, 400, 30, 100, 140, 0)
    # planned = 0 -> availability falls back to utilization; runtime = 0 ->
    # performance falls back to the status constant
    prod(zero, 0, 0, 30, 0, 0, 0)

    # A machine with no production at all, but inspections: quality comes from
    # the inspection ratio rather than the 95 constant.
    db.add(models.QualityInspection(inspection_no="Q1", machine_id=qual_only.id, inspector="QA",
        inspected_quantity=200, passed_quantity=150, failed_quantity=50,
        defect_category="Porosity", rework_quantity=None, scrap_quantity=None,
        status="Closed", tenant_code="DEFAULT"))
    # NULL passed_quantity must coalesce to 0, not blow up.
    db.add(models.QualityInspection(inspection_no="Q2", machine_id=qual_only.id, inspector="QA",
        inspected_quantity=100, passed_quantity=None, failed_quantity=None,
        defect_category=None, rework_quantity=0, scrap_quantity=0,
        status="Open", tenant_code="DEFAULT"))
    # Two categories with the SAME failed total, added in a known order. The
    # trend sort is stable, so ties keep first-appearance order - a GROUP BY that
    # returned its groups in any other order would silently reorder the chart.
    db.add(models.QualityInspection(inspection_no="Q-tieA", machine_id=qual_only.id, inspector="QA",
        inspected_quantity=60, passed_quantity=30, failed_quantity=30,
        defect_category="Burr", status="Closed", tenant_code="DEFAULT"))
    db.add(models.QualityInspection(inspection_no="Q-tieB", machine_id=qual_only.id, inspector="QA",
        inspected_quantity=60, passed_quantity=30, failed_quantity=30,
        defect_category="Warp", status="Closed", tenant_code="DEFAULT"))

    # An inspection with NO machine: counts toward the defect trend, not any machine.
    db.add(models.QualityInspection(inspection_no="Q3", machine_id=None, inspector="QA",
        inspected_quantity=50, passed_quantity=40, failed_quantity=10,
        defect_category="Scratch", status="Closed", tenant_code="DEFAULT"))

    for reason, duration in (("Breakdown", "2 hrs 15 min"), ("Changeover", "45 min"),
                             ("Breakdown", "1.5 hrs"), ("Material", "90")):
        db.add(models.DowntimeLog(machine_id=normal.id, reason=reason,
                                  duration=duration, tenant_code="DEFAULT"))
    db.add(models.DowntimeLog(machine_id=idle_no_prod.id, reason="Breakdown",
                              duration="30 min", tenant_code="DEFAULT"))

    db.add(models.ShiftData(shift_name="A", target_output=1000, actual_output=900, tenant_code="DEFAULT"))
    db.add(models.ShiftData(shift_name="B", target_output=0, actual_output=0, tenant_code="DEFAULT"))
    db.add(models.ShiftData(shift_name="C", target_output=800, actual_output=850, tenant_code="DEFAULT"))
    db.commit()
    assert run_no_prod and idle_no_prod  # referenced for clarity


def _compare(db, label):
    import analytics_routes
    expected = reference_executive_oee(db)
    actual = analytics_routes.get_executive_oee(
        db=db, current_user={"username": "t", "role": "Admin", "tenant": "DEFAULT"})
    if actual != expected:
        # Show the first differing key rather than two walls of JSON.
        for key in expected:
            if actual.get(key) != expected[key]:
                raise AssertionError(
                    f"{label}: '{key}' changed\n  was: {json.dumps(expected[key], default=str)[:400]}"
                    f"\n  now: {json.dumps(actual.get(key), default=str)[:400]}")
        raise AssertionError(f"{label}: keys differ {sorted(expected)} vs {sorted(actual)}")
    return expected


def test_matches_reference_on_every_branch():
    db = _fresh_db()
    _seed_every_branch(db)
    out = _compare(db, "every-branch")
    # Guard the guard: if the fixture stopped exercising the clamps, the parity
    # assertion above would pass while proving much less.
    ranking = {row["machine_name"]: row for row in out["machine_ranking"]}
    assert ranking["RanPastPlan"]["availability"] == 100, "fixture no longer hits the availability clamp"
    assert ranking["DataEntrySlip"]["quality"] == 100, "fixture no longer hits the quality clamp"
    assert ranking["NoProductionIdle"]["performance"] == 60, "fixture no longer hits the status fallback"
    assert ranking["QualityOnly"]["quality"] == 50, "fixture no longer hits the inspection fallback"
    tied = [row["defect"] for row in out["quality_trend"] if row["failed_quantity"] == 30]
    assert tied == ["Burr", "Warp"], f"fixture no longer exercises a defect-trend tie: {out['quality_trend']}"
    print("  every-branch parity OK,", len(out["machine_ranking"]), "machines")
    db.close()


def test_plant_pooling_clamps_when_every_run_slipped():
    """The PLANT pooling has its own clamp, separate from the per-machine one.

    The every-branch fixture has one machine with good > total, but the plant is
    a ratio of SUMS, so the other machines drown it out and the pooled clamp
    never fires. Mutation-testing caught that: removing the clamp from
    pooled_oee_from_sums left the parity test green. Here every run slipped, so
    the plant ratio itself exceeds 1 and the clamp is the only thing keeping
    plant_quality at a figure the data can support.
    """
    db = _fresh_db()
    m = _machine(db, "AllSlipped")
    for _ in range(3):
        db.add(models.ProductionRecord(
            machine_id=m.id, planned_minutes=480, runtime_minutes=600,
            ideal_cycle_time_seconds=3600, total_count=100, good_count=150,
            rejected_count=0, tenant_code="DEFAULT"))
    db.commit()

    out = _compare(db, "plant-clamp")
    assert out["plant_quality"] == 100, out["plant_quality"]
    assert out["plant_availability"] == 100, out["plant_availability"]
    assert out["plant_performance"] == 100, out["plant_performance"]
    assert out["plant_oee"] == 100, out["plant_oee"]
    print("  plant-clamp parity OK")
    db.close()


def test_matches_reference_on_an_empty_database():
    db = _fresh_db()
    out = _compare(db, "empty")
    assert out["plant_oee"] == 0 and out["machine_ranking"] == []
    print("  empty-database parity OK")
    db.close()


def test_matches_reference_with_machines_but_no_activity():
    db = _fresh_db()
    _machine(db, "Bare", status="Maintenance", utilization=33)
    db.commit()
    out = _compare(db, "machines-only")
    assert len(out["machine_ranking"]) == 1
    print("  machines-only parity OK")
    db.close()


def test_per_machine_components_floor_at_zero():
    """A negative-count legacy/raw-SQL row must not print a negative per-machine OEE.

    The HTTP ingest rejects negative counts/minutes, but that constraint is not
    retro-applied to rows written by raw SQL / a migration / a cleared update — the
    exact premise the shared OEE helper floors against (#414). The per-machine block
    capped each component at 100% but never floored at 0, so such a row printed a
    NEGATIVE availability / performance / quality (and an OEE below zero) for its
    machine, while the pooled plant rollup on the same response floored the identical
    sums to 0 — the parts contradicting the whole (rule-3). Every expected number
    below is derived by hand from the row, NOT read back from the implementation.

    Calls the endpoint DIRECTLY (not via the reference oracle) so a floor removed
    from the real code fails this test even if the oracle lost it too.
    """
    import analytics_routes
    db = _fresh_db()

    neg_runtime = _machine(db, "NegRuntime", status="Running", utilization=70)
    neg_counts = _machine(db, "NegCounts", status="Running", utilization=55)
    insp_neg = _machine(db, "InspNeg", status="Breakdown", utilization=12)
    insp_over = _machine(db, "InspOver", status="Running", utilization=80)

    def prod(machine, planned, runtime, ideal, total, good, rejected):
        db.add(models.ProductionRecord(
            machine_id=machine.id, planned_minutes=planned, runtime_minutes=runtime,
            ideal_cycle_time_seconds=ideal, total_count=total, good_count=good,
            rejected_count=rejected, tenant_code="DEFAULT"))

    # Negative runtime: availability = round(min(-50/480, 1)*100) = -10 -> floored to 0.
    #   runtime<=0 -> performance falls back to the Running constant 90.
    #   quality = round(min(90/100, 1)*100) = 90.
    prod(neg_runtime, 480, -50, 30, 100, 90, 10)

    # Positive runtime but a negative ideal_cycle and negative good_count:
    #   availability = round(min(400/480, 1)*100) = 83.
    #   performance = round(min(-3000/24000, 1)*100) = -12/-13 -> floored to 0.
    #   quality     = round(min(-50/100, 1)*100)   = -50      -> floored to 0.
    prod(neg_counts, 480, 400, -30, 100, -50, 150)

    # No production; the inspection-fallback quality was uncapped in BOTH directions.
    #   InspNeg : passed=-20 of 100 -> round(-20) = -20 -> floored to 0.
    #   InspOver: passed=150 of 100 -> round(150)       -> capped at 100.
    db.add(models.QualityInspection(inspection_no="QN", machine_id=insp_neg.id, inspector="QA",
        inspected_quantity=100, passed_quantity=-20, failed_quantity=0,
        status="Closed", tenant_code="DEFAULT"))
    db.add(models.QualityInspection(inspection_no="QO", machine_id=insp_over.id, inspector="QA",
        inspected_quantity=100, passed_quantity=150, failed_quantity=0,
        status="Closed", tenant_code="DEFAULT"))
    db.commit()

    out = analytics_routes.get_executive_oee(
        db=db, current_user={"username": "t", "role": "Admin", "tenant": "DEFAULT"})
    rows = {row["machine_name"]: row for row in out["machine_ranking"]}

    # No per-machine component or OEE may fall outside the bound the data supports.
    for name, row in rows.items():
        for field in ("availability", "performance", "quality", "oee"):
            assert 0 <= row[field] <= 100, f"{name}.{field} = {row[field]} out of [0,100]"

    # Independently-derived exact values (see the fixture comments above).
    assert rows["NegRuntime"]["availability"] == 0, rows["NegRuntime"]
    assert rows["NegRuntime"]["performance"] == 90, rows["NegRuntime"]
    assert rows["NegRuntime"]["quality"] == 90, rows["NegRuntime"]
    assert rows["NegRuntime"]["oee"] == 0, rows["NegRuntime"]

    assert rows["NegCounts"]["availability"] == 83, rows["NegCounts"]
    assert rows["NegCounts"]["performance"] == 0, rows["NegCounts"]
    assert rows["NegCounts"]["quality"] == 0, rows["NegCounts"]
    assert rows["NegCounts"]["oee"] == 0, rows["NegCounts"]

    assert rows["InspNeg"]["quality"] == 0, rows["InspNeg"]
    assert rows["InspOver"]["quality"] == 100, rows["InspOver"]

    # Reconciliation: the pooled plant rollup already floors, so it too stays in bound.
    for field in ("plant_availability", "plant_performance", "plant_quality", "plant_oee"):
        assert 0 <= out[field] <= 100, f"{field} = {out[field]} out of [0,100]"

    print("  per-machine component floor OK")
    db.close()


if __name__ == "__main__":
    test_matches_reference_on_every_branch()
    test_plant_pooling_clamps_when_every_run_slipped()
    test_matches_reference_on_an_empty_database()
    test_matches_reference_with_machines_but_no_activity()
    test_per_machine_components_floor_at_zero()
    print("executive-oee SQL parity: OK")
