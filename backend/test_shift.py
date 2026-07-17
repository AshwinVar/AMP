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

    # empty -> zeros, no divide-by-zero
    empty = shift.build_shift_summary(_fresh_session(), "DEFAULT")
    assert empty["entries"] == 0 and empty["attainment"] == 0 and empty["shifts"] == []
    assert empty["best"] is None and empty["worst"] is None


if __name__ == "__main__":
    test_shift_summary_rolls_up_attainment_by_shift()
    print("SHIFT OK: attainment per shift (grouped by base name); best/worst; windowed; empty-safe")
