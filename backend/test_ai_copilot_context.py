"""Copilot factory-context grounding tests.

The natural-language copilot grounds the model in a compact snapshot built by
`_build_factory_context`. The OEE line in that snapshot MUST be the same pooled
plant OEE (ratio of sums) the dashboards show — a mean of per-record OEE (mean
of ratios) over-weights tiny runs and would feed the model a number that
disagrees with every OEE card. This pins that line to the pooled definition and
covers the empty and zero-denominator edges.

Run:  python backend/test_ai_copilot_context.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
import ai_copilot
from analytics_engine import pooled_oee


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _oee_line(context: str):
    for line in context.splitlines():
        if "OEE" in line:
            return line
    return None


def test_context_oee_is_pooled_not_mean_of_ratios():
    # One big run at 20% OEE and one tiny perfect run at 100%. Mean of the two
    # per-record OEEs is 60%; the volume-weighted pooled OEE is far lower. The
    # copilot must ground the model in the pooled figure the dashboards report.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="Big", status="Running", utilization=80))
    db.add(models.Machine(id=2, name="Tiny", status="Running", utilization=80))
    # Big: A=500/1000=.5, P=(30*500)/(500*60)=.5, Q=400/500=.8 -> OEE 20%
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=1000, runtime_minutes=500,
                                   ideal_cycle_time_seconds=30, total_count=500, good_count=400, rejected_count=100))
    # Tiny: A=P=Q=1 -> OEE 100%
    db.add(models.ProductionRecord(machine_id=2, planned_minutes=10, runtime_minutes=10,
                                   ideal_cycle_time_seconds=60, total_count=10, good_count=10, rejected_count=0))
    db.commit()

    recs = db.query(models.ProductionRecord).all()
    # Independently derived pooled OEE: planned 1010, runtime 510, total 510,
    # good 410, ideal-seconds 30*500+60*10=15600.
    #   A=510/1010=.50495  P=15600/(510*60)=.50980  Q=410/510=.80392
    #   OEE = .50495*.50980*.80392 = .2069 -> 21%
    assert pooled_oee(recs)["oee"] == 21, pooled_oee(recs)["oee"]
    mean_of_ratios = round((20 + 100) / 2)          # = 60, the old wrong figure

    context = ai_copilot._build_factory_context(db, "DEFAULT")
    line = _oee_line(context)
    assert line is not None, context
    assert "21%" in line, line                       # the pooled, dashboard-consistent number
    assert f"{mean_of_ratios}%" not in line, line     # emphatically NOT the mean of ratios
    print(f"PASS copilot context OEE is pooled (21%), not the mean of ratios ({mean_of_ratios}%)")


def test_context_no_production_has_no_oee_line():
    # No production records -> no OEE line at all (no fabricated 0%/NaN, no crash).
    db = _fresh_session()
    db.add(models.Machine(id=1, name="Idle", status="Running", utilization=80))
    db.commit()
    context = ai_copilot._build_factory_context(db, "DEFAULT")
    assert _oee_line(context) is None, context
    print("PASS copilot context omits OEE when there is no production")


def test_context_zero_denominator_record_does_not_crash():
    # A record with zero planned/runtime/total (all divisors zero) must pool to a
    # real 0% OEE, not raise. Pooling guards every denominator.
    db = _fresh_session()
    db.add(models.Machine(id=1, name="Cold", status="Running", utilization=0))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=0, runtime_minutes=0,
                                   ideal_cycle_time_seconds=0, total_count=0, good_count=0, rejected_count=0))
    db.commit()
    context = ai_copilot._build_factory_context(db, "DEFAULT")
    line = _oee_line(context)
    assert line is not None and "0%" in line, line
    print("PASS copilot context handles a zero-denominator record (pooled 0%)")


if __name__ == "__main__":
    test_context_oee_is_pooled_not_mean_of_ratios()
    test_context_no_production_has_no_oee_line()
    test_context_zero_denominator_record_does_not_crash()
    print("ALL COPILOT CONTEXT TESTS PASSED")
