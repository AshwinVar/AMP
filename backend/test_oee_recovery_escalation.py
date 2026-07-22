"""Tests for the OEE-recovery escalation generator (factory_ops_routes).

POST /escalations/generate-oee-recovery turns the "fix this first" recovery
insight into a tracked escalation: one per biggest-lever gap, idempotent while
unresolved, a no-op at world-class. The recovery summary is stubbed so no
production data is needed.

Run:  python backend/test_oee_recovery_escalation.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
import ai.recovery as rec
import ai.oee as oeemod
from factory_ops_routes import generate_oee_recovery_escalation

USER = {"tenant": "DEFAULT", "role": "Admin"}


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _summary(**over):
    base = {
        "has_data": True, "at_world_class": False, "biggest_lever": "performance",
        "lever_label": "Performance", "lever_action": "Close the speed loss.",
        "lever_recoverable_units_per_year": 159609,
        "lever_recoverable_value_per_year": 444416,
        "components": [
            {"key": "availability", "current": 90, "target": 90},
            {"key": "performance", "current": 91, "target": 95},
            {"key": "quality", "current": 99, "target": 99},
        ],
    }
    base.update(over)
    return base


def _stub(summary):
    orig = rec.build_recovery_summary
    rec.build_recovery_summary = lambda db, tenant: summary
    return orig


def _stub_oee(machines):
    orig = oeemod.build_oee_summary
    oeemod.build_oee_summary = lambda db, tenant: {"machines": machines}
    return orig


def test_creates_one_escalation_with_the_lever_and_prize():
    db = _fresh_session()
    orig = _stub(_summary())
    try:
        out = generate_oee_recovery_escalation(db=db, current_user=USER)
    finally:
        rec.build_recovery_summary = orig
    assert out["created"] == 1 and out["escalation_id"]
    e = db.query(models.Escalation).one()
    assert e.title == "OEE recovery: close the Performance gap"
    assert e.source == "OEE Recovery" and e.severity == "High" and e.status == "Open"
    assert "£444,416/yr" in e.notes and "91% -> 95%" in e.notes
    assert "Close the speed loss" in e.notes
    print("PASS raises one escalation naming the lever + £ prize + action")


def test_is_idempotent_while_unresolved():
    db = _fresh_session()
    orig = _stub(_summary())
    try:
        first = generate_oee_recovery_escalation(db=db, current_user=USER)
        second = generate_oee_recovery_escalation(db=db, current_user=USER)
    finally:
        rec.build_recovery_summary = orig
    assert second["created"] == 0 and second["escalation_id"] == first["escalation_id"]
    assert db.query(models.Escalation).count() == 1   # no duplicate
    print("PASS idempotent — an open recovery escalation is surfaced, not duplicated")


def test_units_only_when_no_rate():
    db = _fresh_session()
    orig = _stub(_summary(lever_recoverable_value_per_year=None))
    try:
        generate_oee_recovery_escalation(db=db, current_user=USER)
    finally:
        rec.build_recovery_summary = orig
    e = db.query(models.Escalation).one()
    assert "159,609 good units/yr" in e.notes and "£" not in e.notes
    print("PASS no configured rate -> the prize is stated in good units, no made-up £")


def test_targets_worst_machine_on_the_lever():
    db = _fresh_session()
    o_rec = _stub(_summary())  # performance is the lever
    o_oee = _stub_oee([
        {"machine_id": 11, "name": "SMT-Reflow-01", "performance": 70, "has_data": True},
        {"machine_id": 12, "name": "IC-Test-01", "performance": 88, "has_data": True},
    ])
    try:
        generate_oee_recovery_escalation(db=db, current_user=USER)
    finally:
        rec.build_recovery_summary = o_rec
        oeemod.build_oee_summary = o_oee
    e = db.query(models.Escalation).one()
    assert e.machine_id == 11                          # worst performer on the lever
    assert "Start on SMT-Reflow-01 (70% performance)" in e.notes
    print("PASS escalation points at the worst machine on the biggest lever")


def test_no_op_at_world_class():
    db = _fresh_session()
    orig = _stub(_summary(at_world_class=True, biggest_lever=None))
    try:
        out = generate_oee_recovery_escalation(db=db, current_user=USER)
    finally:
        rec.build_recovery_summary = orig
    assert out == {"created": 0, "escalation_id": None}
    assert db.query(models.Escalation).count() == 0
    print("PASS at world-class -> nothing to recover, no escalation raised")


if __name__ == "__main__":
    test_creates_one_escalation_with_the_lever_and_prize()
    test_is_idempotent_while_unresolved()
    test_units_only_when_no_rate()
    test_targets_worst_machine_on_the_lever()
    test_no_op_at_world_class()
    print("ALL OEE-RECOVERY ESCALATION TESTS PASSED")
