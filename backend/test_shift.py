"""Shift performance read-model tests (ADR-0007).

Attainment (actual vs target) per shift over the last 7 days, grouped by base
shift name (the date suffix is stripped), with best/worst and out-of-window
entries excluded.

Run:  python backend/test_shift.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import shift


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _shift(name, target, actual, days_ago=0):
    return models.ShiftData(shift_name=name, target_output=target, actual_output=actual,
                            created_at=datetime.utcnow() - timedelta(days=days_ago))


def test_shift_summary_rolls_up_attainment_by_shift():
    db = _fresh_session()
    db.add_all([
        _shift("Shift A – 17 Jul", 400, 380, 0),   # en-dash + date
        _shift("Shift A – 16 Jul", 400, 340, 1),   # same shift, another day
        _shift("Shift B - 17 Jul", 400, 320, 0),   # hyphen separator
        _shift("Shift C", 400, 396, 0),            # no date suffix
        _shift("Shift A – old", 400, 400, 9),      # outside the 7-day window
    ])
    db.commit()

    s = shift.build_shift_summary(db, "DEFAULT")
    assert s["entries"] == 4                        # 9-days-ago excluded
    by = {x["shift"]: x for x in s["shifts"]}
    assert set(by) == {"Shift A", "Shift B", "Shift C"}   # grouped by base name
    # Shift A: (380+340)/(400+400) = 720/800 = 90%
    assert by["Shift A"]["target"] == 800 and by["Shift A"]["actual"] == 720
    assert by["Shift A"]["attainment"] == 90 and by["Shift A"]["entries"] == 2
    assert by["Shift B"]["attainment"] == 80 and by["Shift C"]["attainment"] == 99
    assert s["best"]["shift"] == "Shift C" and s["worst"]["shift"] == "Shift B"
    # overall = (720+320+396)/1600 = 1436/1600 = 89.75 -> 90
    assert s["attainment"] == 90

    # empty -> no attainment to report (None, not a misleading 0%), no divide-by-zero
    empty = shift.build_shift_summary(_fresh_session(), "DEFAULT")
    assert empty["entries"] == 0 and empty["attainment"] is None and empty["shifts"] == []
    assert empty["best"] is None and empty["worst"] is None


def test_unplanned_shift_has_no_attainment_and_is_never_worst():
    """A shift logged with no target (target_output == 0) has no plan to measure
    against. Its attainment must be None — not 0% — and it must never be picked as
    the "worst" shift ahead of a genuinely under-performing planned shift."""
    db = _fresh_session()
    db.add_all([
        _shift("Shift A", 400, 380, 0),   # 95% — the real best
        _shift("Shift B", 400, 300, 0),   # 75% — the real worst
        _shift("Shift C", 0, 120, 0),     # unplanned overtime: no target
    ])
    db.commit()

    s = shift.build_shift_summary(db, "DEFAULT")
    by = {x["shift"]: x for x in s["shifts"]}
    # The unplanned shift is reported, but with no attainment (not 0%).
    assert by["Shift C"]["attainment"] is None
    assert by["Shift A"]["attainment"] == 95 and by["Shift B"]["attainment"] == 75
    # best/worst rank only the shifts with a target — C is neither despite its 0.
    assert s["best"]["shift"] == "Shift A"
    assert s["worst"]["shift"] == "Shift B"   # NOT Shift C
    # Grand totals stay true: 800 planned, 380+300+120 = 800 produced.
    assert s["target"] == 800 and s["actual"] == 800
    # Headline pools the grand totals (800/800 = 100), independently derived.
    assert s["attainment"] == 100


def test_no_shift_has_a_target_reports_no_attainment():
    """When nothing in the window has a target, the headline attainment is None
    (no plan to measure) and there is no best/worst to name — but the entries and
    produced output are still counted honestly."""
    db = _fresh_session()
    db.add_all([
        _shift("Shift A", 0, 200, 0),
        _shift("Shift B", 0, 150, 0),
    ])
    db.commit()

    s = shift.build_shift_summary(db, "DEFAULT")
    assert s["entries"] == 2
    assert s["attainment"] is None
    assert s["target"] == 0 and s["actual"] == 350   # true output still reported
    assert all(x["attainment"] is None for x in s["shifts"])
    assert s["best"] is None and s["worst"] is None


def test_base_shift_name_is_none_and_blank_safe():
    """Grouping must not crash on a missing/blank shift name (the separator scan
    used to run before the None fallback)."""
    assert shift._base_shift(None) == ""
    assert shift._base_shift("   ") == ""
    assert shift._base_shift("Shift A - 17 Jul") == "Shift A"
    assert shift._base_shift("Nights") == "Nights"


if __name__ == "__main__":
    test_shift_summary_rolls_up_attainment_by_shift()
    test_unplanned_shift_has_no_attainment_and_is_never_worst()
    test_no_shift_has_a_target_reports_no_attainment()
    test_base_shift_name_is_none_and_blank_safe()
    print("SHIFT OK: honest attainment (None when no plan); unplanned shift never worst; "
          "grand totals true; name None/blank-safe; windowed; empty-safe")
