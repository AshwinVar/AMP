"""Behavioural tests for the defect-escalation generator (quality_routes).

/quality/generate-defect-escalations turns quality inspections into Escalation
rows. Before this, its test coverage was registration-only, and it scanned the
whole (growing) quality_inspections table in Python. The scan is now bounded in
SQL — only inspections that can raise an escalation come back — so these tests
pin the threshold (fail rate >= 10% OR scrap > 0), the exact-10% boundary, the
Critical-vs-High severity split, open-escalation dedup, and the edges that used
to be Python guards: zero inspected quantity and a NULL scrap column (which would
previously crash on `None <= 0`).

Run:  python backend/test_defect_escalations.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from quality_routes import generate_defect_escalations

USER = {"role": "Admin", "tenant_code": "DEFAULT"}


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _insp(no, inspected, failed=0, scrap=0):
    return models.QualityInspection(
        inspection_no=no,
        inspector="QA",
        inspected_quantity=inspected,
        passed_quantity=max(inspected - failed, 0),
        failed_quantity=failed,
        scrap_quantity=scrap,
        rework_quantity=0,
        status="Open",
    )


def _run(db):
    return generate_defect_escalations(db=db, current_user=USER)["created"]


def _escalations(db):
    return db.query(models.Escalation).all()


def test_threshold_and_severity():
    """Six inspections, hand-derived. Escalation fires when fail rate >= 10%
    (failed/inspected) OR scrap > 0; severity is Critical at fail rate >= 20%,
    else High. Only the qualifying rows produce an escalation."""
    db = _fresh_session()
    for insp in [
        _insp("A", 100, failed=25),          # 25% -> escalate, Critical
        _insp("B", 100, failed=12),          # 12% -> escalate, High
        _insp("C", 100, failed=10),          # exactly 10% -> escalate, High
        _insp("D", 100, failed=5, scrap=0),  #  5%, no scrap -> NO escalation
        _insp("E", 100, failed=5, scrap=3),  #  5% but scrap>0 -> escalate, High
        _insp("F", 0, failed=0),             # inspected 0 -> skipped, no crash
    ]:
        db.add(insp)
    db.commit()

    created = _run(db)
    assert created == 4, f"expected 4 escalations, got {created}"

    by_title = {e.title: e for e in _escalations(db)}
    assert set(by_title) == {
        "Quality issue: A",
        "Quality issue: B",
        "Quality issue: C",
        "Quality issue: E",
    }, f"unexpected escalations: {sorted(by_title)}"
    assert by_title["Quality issue: A"].severity == "Critical"
    assert by_title["Quality issue: B"].severity == "High"
    assert by_title["Quality issue: C"].severity == "High"   # exact-10% boundary keeps
    assert by_title["Quality issue: E"].severity == "High"   # scrap-driven, low fail rate
    print("PASS threshold + exact-10% boundary + severity split")


def test_below_threshold_rows_are_not_selected():
    """A clean, high-volume run (fail rate well under 10%, no scrap) raises
    nothing — and the SQL bound means those rows are never even fetched."""
    db = _fresh_session()
    db.add(_insp("CLEAN", 1000, failed=1, scrap=0))   # 0.1% -> no escalation
    db.add(_insp("CLEAN2", 500, failed=49, scrap=0))  # 9.8% -> no escalation
    db.commit()
    assert _run(db) == 0
    assert _escalations(db) == []
    print("PASS below-threshold clean runs raise nothing")


def test_null_scrap_does_not_crash():
    """A NULL scrap column used to crash the old `scrap_quantity <= 0` guard.
    A high-fail inspection with NULL scrap must still escalate cleanly."""
    db = _fresh_session()
    row = _insp("NULLSCRAP", 50, failed=20)  # 40% -> Critical
    row.scrap_quantity = None
    db.add(row)
    # A low-fail row with NULL scrap must NOT escalate (and must not crash).
    low = _insp("NULLSCRAP_LOW", 50, failed=2)  # 4%
    low.scrap_quantity = None
    db.add(low)
    db.commit()

    assert _run(db) == 1
    esc = _escalations(db)
    assert len(esc) == 1
    assert esc[0].title == "Quality issue: NULLSCRAP"
    assert esc[0].severity == "Critical"
    print("PASS NULL scrap column escalates without crashing")


def test_null_failed_quantity_with_scrap_does_not_crash():
    """A NULL failed_quantity used to crash the divide. The SQL filter selects a
    row on scrap_quantity > 0 regardless of failed_quantity, so an inspection with
    recorded scrap but a NULL (raw SQL / cleared) failed_quantity reaches the
    Python fail-rate divide — `None / inspected_quantity` raised TypeError and
    500'd the generator. A NULL count is the column default of 0, so fail rate is a
    clean 0% and the row still escalates on its scrap alone (High, not Critical)."""
    from sqlalchemy import text

    db = _fresh_session()
    # inspected 100, scrap 5, failed cleared to a real SQL NULL below.
    scrapped = _insp("NULLFAIL_SCRAP", 100, failed=0, scrap=5)
    db.add(scrapped)
    # A NULL failed_quantity with NO scrap must NOT be selected at all (the SQL
    # predicate is NULL/false on both branches) — so it neither escalates nor crashes.
    clean = _insp("NULLFAIL_CLEAN", 100, failed=0, scrap=0)
    db.add(clean)
    db.commit()
    db.execute(text(
        "UPDATE quality_inspections SET failed_quantity = NULL "
        "WHERE inspection_no IN ('NULLFAIL_SCRAP', 'NULLFAIL_CLEAN')"
    ))
    db.commit()
    db.expire_all()

    assert _run(db) == 1
    esc = _escalations(db)
    assert len(esc) == 1
    assert esc[0].title == "Quality issue: NULLFAIL_SCRAP"
    # fail rate = 0(NULL->0)/100*100 = 0.0% -> below 20% -> High, driven by scrap.
    assert esc[0].severity == "High", esc[0].severity
    assert "Fail rate 0.0%" in esc[0].notes, esc[0].notes
    assert "scrap 5" in esc[0].notes, esc[0].notes
    print("PASS NULL failed_quantity with scrap -> 0% fail rate, escalates without crashing")


def test_dedup_open_but_regenerate_after_resolved():
    """An already-OPEN escalation for the same inspection is not duplicated; a
    RESOLVED one does not block a fresh escalation."""
    db = _fresh_session()
    db.add(_insp("DUP", 100, failed=30))
    db.add(_insp("GONE", 100, failed=30))
    db.add(models.Escalation(title="Quality issue: DUP", severity="High",
                             status="Open", source="Quality",
                             owner="Quality Lead", department="Quality"))
    db.add(models.Escalation(title="Quality issue: GONE", severity="High",
                             status="Resolved", source="Quality",
                             owner="Quality Lead", department="Quality"))
    db.commit()

    created = _run(db)
    # DUP already open -> skipped; GONE resolved -> a new one is created.
    assert created == 1, f"expected 1 new escalation, got {created}"
    open_dup = [e for e in _escalations(db) if e.title == "Quality issue: DUP"]
    gone = [e for e in _escalations(db) if e.title == "Quality issue: GONE"]
    assert len(open_dup) == 1, "existing open escalation should not be duplicated"
    assert len(gone) == 2, "resolved escalation should not block a new one"
    print("PASS dedup on open, regenerate after resolved")


def test_empty_table():
    """No inspections -> nothing raised, no error."""
    db = _fresh_session()
    assert _run(db) == 0
    assert _escalations(db) == []
    print("PASS empty inspections table")


if __name__ == "__main__":
    test_threshold_and_severity()
    test_below_threshold_rows_are_not_selected()
    test_null_scrap_does_not_crash()
    test_null_failed_quantity_with_scrap_does_not_crash()
    test_dedup_open_but_regenerate_after_resolved()
    test_empty_table()
    print("ALL DEFECT-ESCALATION TESTS PASSED")
