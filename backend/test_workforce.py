"""Operator performance read-model tests (ADR-0007).

Per-operator jobs, good/rejected units and quality rate over the last 7 days,
worst quality (on real volume) first, with plant totals the parts reconcile to.

Run:  python backend/test_workforce.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import workforce


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


_seq = [0]


def _exe(operator, good, rejected, status="Completed", days_ago=0):
    _seq[0] += 1
    return models.OperatorJobExecution(
        execution_no=f"EXE-{_seq[0]}",
        operator_name=operator,
        good_count=good,
        rejected_count=rejected,
        job_status=status,
        started_at=datetime.utcnow() - timedelta(days=days_ago),
    )


def test_rolls_up_per_operator_and_reconciles_to_the_headline():
    db = _fresh_session()
    db.add_all([
        # Anya: 2 jobs, 100 good + 100 good = 200 good, 10 + 10 = 20 rejected
        _exe("Anya", 100, 10, "Completed", 0),
        _exe("Anya", 100, 10, "In Progress", 1),
        # Ben: 1 job, 90 good, 30 rejected  -> the poor-quality operator
        _exe("Ben", 90, 30, "Completed", 0),
        # out-of-window job (8 days ago) must be excluded entirely
        _exe("Anya", 500, 500, "Completed", 8),
    ])
    db.commit()

    s = workforce.build_operator_summary(db, "DEFAULT")

    # Window excludes the 8-days-ago row: 3 jobs, 2 operators.
    assert s["jobs"] == 3
    assert s["operators"] == 2
    assert s["completed"] == 2 and s["active"] == 1

    by = {o["operator"]: o for o in s["by_operator"]}
    # Anya: good 200, rejected 20, units 220, rate 200/220 = 90.9 -> 91
    assert by["Anya"]["good"] == 200 and by["Anya"]["rejected"] == 20
    assert by["Anya"]["units"] == 220 and by["Anya"]["quality_rate"] == 91
    assert by["Anya"]["jobs"] == 2 and by["Anya"]["completed"] == 1 and by["Anya"]["active"] == 1
    # Ben: good 90, rejected 30, units 120, rate 90/120 = 75
    assert by["Ben"]["good"] == 90 and by["Ben"]["rejected"] == 30
    assert by["Ben"]["units"] == 120 and by["Ben"]["quality_rate"] == 75

    # Plant totals are TRUE totals and the parts sum to the whole (rule 3).
    # good: 200 (Anya) + 90 (Ben) = 290; rejected: 20 (Anya) + 30 (Ben) = 50.
    assert s["good_units"] == 290 and s["rejected_units"] == 50
    assert s["total_units"] == 340
    assert s["good_units"] == sum(o["good"] for o in s["by_operator"])
    assert s["rejected_units"] == sum(o["rejected"] for o in s["by_operator"])
    assert s["jobs"] == sum(o["jobs"] for o in s["by_operator"])
    # Pooled plant quality: 290/340 = 85.3 -> 85 (independently derived).
    assert s["quality_rate"] == 85

    # Daily series reconciles to the plant totals too.
    assert sum(d["good"] for d in s["daily"]) == s["good_units"]
    assert sum(d["rejected"] for d in s["daily"]) == s["rejected_units"]
    assert len(s["daily"]) == workforce.WINDOW_DAYS

    # Worst quality (on real volume) first: Ben (75%) ahead of Anya (91%).
    assert s["by_operator"][0]["operator"] == "Ben"
    # Ben is below the plant rate, so he's the one to look at first.
    assert s["needs_attention"]["operator"] == "Ben"


def test_operator_with_no_units_has_no_rate_and_is_never_worst():
    """An operator who booked jobs but no units yet has quality_rate None (not a
    misleading 0%), and must never rank as "worst" ahead of a genuinely poor one."""
    db = _fresh_session()
    db.add_all([
        _exe("Cara", 0, 0, "In Progress", 0),   # just started — no units booked
        _exe("Dev", 80, 20, "Completed", 0),    # 80% on real volume — the real worst
        _exe("Eli", 98, 2, "Completed", 0),     # 98% — best
    ])
    db.commit()

    s = workforce.build_operator_summary(db, "DEFAULT")
    by = {o["operator"]: o for o in s["by_operator"]}
    # No units -> None rate, not 0%.
    assert by["Cara"]["units"] == 0 and by["Cara"]["quality_rate"] is None
    assert by["Dev"]["quality_rate"] == 80 and by["Eli"]["quality_rate"] == 98
    # Ranking: the poor-quality operator leads; the no-units operator sinks to the end.
    assert s["by_operator"][0]["operator"] == "Dev"
    assert s["by_operator"][-1]["operator"] == "Cara"
    # needs_attention is the worst rateable one below the plant rate — Dev, not Cara.
    assert s["needs_attention"]["operator"] == "Dev"


def test_below_volume_floor_is_not_ranked_worst():
    """A reject off a handful of parts is arithmetic, not a signal. An operator
    under the volume floor is never flagged as needing attention."""
    db = _fresh_session()
    # Tiny sample: 2 good, 1 rejected = 3 units (< MIN_UNITS) at 67% — looks awful
    # but is too thin to judge.
    db.add(_exe("Finn", 2, 1, "Completed", 0))
    # A solid operator well over the floor at 96%.
    db.add(_exe("Gia", 96, 4, "Completed", 0))
    db.commit()

    s = workforce.build_operator_summary(db, "DEFAULT")
    by = {o["operator"]: o for o in s["by_operator"]}
    assert by["Finn"]["quality_rate"] == 67    # the rate is still reported honestly
    # ...but Finn is under the floor, so Gia (rateable) leads and Finn sinks.
    assert s["by_operator"][0]["operator"] == "Gia"
    assert s["by_operator"][-1]["operator"] == "Finn"
    # Nobody rateable is below the plant rate (Gia IS essentially the plant), so
    # nothing needs attention — the thin-sample operator must not be surfaced.
    assert s["needs_attention"] is None


def test_uniformly_good_crew_flags_nobody():
    db = _fresh_session()
    db.add_all([
        _exe("H", 100, 0, "Completed", 0),   # 100%
        _exe("I", 100, 0, "Completed", 0),   # 100%
    ])
    db.commit()
    s = workforce.build_operator_summary(db, "DEFAULT")
    assert s["quality_rate"] == 100
    assert s["needs_attention"] is None      # worst of a perfect crew isn't a concern


def test_empty_and_none_fields_are_safe():
    # Empty table -> zeros, quality None, no divide-by-zero, no crash.
    s = workforce.build_operator_summary(_fresh_session(), "DEFAULT")
    assert s["jobs"] == 0 and s["operators"] == 0
    assert s["good_units"] == 0 and s["rejected_units"] == 0 and s["total_units"] == 0
    assert s["quality_rate"] is None
    assert s["by_operator"] == [] and s["needs_attention"] is None
    assert len(s["daily"]) == workforce.WINDOW_DAYS
    assert all(d["good"] == 0 and d["rejected"] == 0 for d in s["daily"])

    # NULL good/rejected counts and a blank operator name must not crash and are
    # treated as 0 / the "—" bucket.
    db = _fresh_session()
    row = _exe("  ", None, None, "Completed", 0)
    row.good_count = None
    row.rejected_count = None
    db.add(row)
    db.commit()
    s2 = workforce.build_operator_summary(db, "DEFAULT")
    assert s2["jobs"] == 1 and s2["operators"] == 1
    assert s2["good_units"] == 0 and s2["rejected_units"] == 0
    assert s2["by_operator"][0]["operator"] == "—"
    assert s2["by_operator"][0]["quality_rate"] is None   # no units -> no rate


def test_true_totals_not_top_n_length():
    """operators is the true head-count, not the capped list length; the plant
    totals cover everyone, not just the page shown."""
    db = _fresh_session()
    # 12 operators, each one job of 50 good / 0 rejected -> 100% each.
    for n in range(12):
        db.add(_exe(f"Op{n:02d}", 50, 0, "Completed", 0))
    db.commit()

    s = workforce.build_operator_summary(db, "DEFAULT")
    assert s["operators"] == 12               # true head-count
    assert len(s["by_operator"]) == workforce.TOP_N   # list is capped at 10
    assert s["jobs"] == 12
    assert s["good_units"] == 12 * 50         # totals cover all 12, not just the page


if __name__ == "__main__":
    test_rolls_up_per_operator_and_reconciles_to_the_headline()
    test_operator_with_no_units_has_no_rate_and_is_never_worst()
    test_below_volume_floor_is_not_ranked_worst()
    test_uniformly_good_crew_flags_nobody()
    test_empty_and_none_fields_are_safe()
    test_true_totals_not_top_n_length()
    print("WORKFORCE OK: per-operator rollup reconciles to headline; None rate when no units; "
          "thin-sample never worst; uniformly-good crew flags nobody; true totals not TOP_N; "
          "windowed; empty/None-safe")
