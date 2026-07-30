"""Quality route registration test (ADR-0009).

Quality inspections (list / create / update / delete) + the defect escalation
generator live in quality_routes.register(app), peeled out of main.py. Guards
registration + sole ownership, and that the QualityInspectionFailed event
publish survived the move.

Also covers the null-count hardening: passed/failed/rework/scrap are
Column(Integer, default=0) WITHOUT nullable=False, and inspected_quantity is
Column(Integer, nullable=False) WITHOUT a default — but a nullable=False constraint
is not retro-applied to a raw-SQL / migration / legacy row, so any of the five can be
a genuine NULL. A None on the column then either crashes the `passed + failed`
invariant check / the `min(...)` non-negative guard (TypeError -> 500) or crashes
response serialization (QualityInspectionResponse types all five non-optional int).

Run:  python backend/test_quality_routes.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from fastapi import HTTPException

import main
import models
import quality_routes as QR
import schemas
import tenancy as T
from database import Base

EXPECTED = {
    "/quality/inspections",
    "/quality/inspections/{inspection_id}",
    "/quality/generate-defect-escalations",
}


def test_quality_paths_owned_by_module():
    owners = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(owners)
    assert not missing, f"quality paths not registered: {missing}"
    wrong = {p: mods for p, mods in owners.items() if mods != {"quality_routes"}}
    assert not wrong, f"quality paths not owned solely by quality_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} quality paths owned by quality_routes")


def test_failed_inspection_still_publishes_event():
    import inspect
    import quality_routes
    src = inspect.getsource(quality_routes)
    assert "QualityInspectionFailed(" in src, "QualityInspectionFailed publish lost in extraction"
    assert "event_bus.publish" in src, "event_bus.publish lost in extraction"
    print("PASS a failed inspection still publishes QualityInspectionFailed")


TENANT = "T1"


def _iso_session():
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _make_inspection(db, no, inspected, passed, failed, rework=0, scrap=0, status="Open"):
    row = models.QualityInspection(
        tenant_code=TENANT, inspection_no=no, inspector="QC-1",
        inspected_quantity=inspected, passed_quantity=passed, failed_quantity=failed,
        rework_quantity=rework, scrap_quantity=scrap, status=status,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _null_out(db, row_id, columns):
    # Force a real NULL — passed/failed/rework/scrap are Column(Integer, default=0),
    # so passing None through the ORM would apply the default. Raw SQL plants the NULL
    # a raw-SQL / migration / cleared write would leave, exactly like the sibling
    # null-safe route tests (inventory / recommendations / costing).
    sets = ", ".join(f"{c} = NULL" for c in columns)
    db.execute(text(f"UPDATE quality_inspections SET {sets} WHERE id = :i"), {"i": row_id})
    db.commit()
    db.expire_all()


def _update(db, row_id, **patch):
    tok = T.set_current_tenant(TENANT)
    try:
        payload = schemas.QualityInspectionUpdate(**patch)
        return QR.update_quality_inspection(
            row_id, payload, db=db, current_user={"tenant": TENANT},
        )
    finally:
        T.reset_current_tenant(tok)


def _create(db, **kw):
    tok = T.set_current_tenant(TENANT)
    try:
        payload = schemas.QualityInspectionCreate(inspector="QC-1", **kw)
        return QR.create_quality_inspection(
            payload, db=db, current_user={"tenant": TENANT},
        )
    finally:
        T.reset_current_tenant(tok)


def test_patch_explicit_null_count_heals_to_zero_not_500():
    # A client PATCHing an explicit `{"passed_quantity": null}` (exclude_unset keeps
    # an explicit null) must not crash. NULL is the column's own default of 0, so the
    # stored/returned value heals to a concrete int 0 — which also proves the
    # non-optional int response contract would serialize cleanly (no None leaks out).
    db = _iso_session()
    row = _make_inspection(db, "QI-NULL", inspected=100, passed=95, failed=5)
    result = _update(db, row.id, passed_quantity=None)
    assert result.passed_quantity == 0, result.passed_quantity
    assert isinstance(result.passed_quantity, int), type(result.passed_quantity)
    db.refresh(row)
    assert row.passed_quantity == 0, row.passed_quantity
    print("PASS explicit null passed_quantity heals to 0, not a 500")


def test_unrelated_patch_over_legacy_null_row_is_safe():
    # A pre-existing NULL failed_quantity (raw-SQL / migration) touched by an UNRELATED
    # patch ({"status": "Rework"}, which never sets failed) reaches the invariant check
    # with failed still None. The old `passed + None` raised TypeError -> 500; now it
    # coalesces to 0, applies the status, and heals the NULL. Independent numbers:
    # passed stays 90, failed heals 0, 90 + 0 = 90 <= 100.
    db = _iso_session()
    row = _make_inspection(db, "QI-LEGACY", inspected=100, passed=90, failed=10)
    _null_out(db, row.id, ["failed_quantity", "rework_quantity", "scrap_quantity"])
    result = _update(db, row.id, status="Rework")
    assert result.status == "Rework", result.status
    assert result.failed_quantity == 0, result.failed_quantity
    assert result.rework_quantity == 0 and result.scrap_quantity == 0
    assert result.passed_quantity == 90, result.passed_quantity
    print("PASS unrelated patch over a legacy NULL row is safe (90 + 0 = 90 <= 100)")


def test_list_serialises_legacy_null_row_without_500():
    # The bug this fix closes: GET /quality/inspections returns raw ORM rows, and
    # FastAPI validates each through QualityInspectionResponse (passed/failed/rework/
    # scrap_quantity are non-optional int). A legacy NULL row (raw-SQL / migration)
    # never went through the PATCH in-handler heal, so the raw-ORM list serializer
    # was the one reader left un-healed — a single such row raised ValidationError
    # and 500-ed the WHOLE list, hiding every good inspection. model_validate is
    # exactly what the response layer does; assert the NULL counts heal to 0 while
    # the good row (and inspected_quantity, which is nullable=False) are untouched.
    db = _iso_session()
    tok = T.set_current_tenant(TENANT)
    try:
        good = _make_inspection(db, "QI-GOOD", inspected=100, passed=90, failed=10,
                                rework=3, scrap=2)
        bad = _make_inspection(db, "QI-LEGACY-NULL", inspected=50, passed=0, failed=0)
        _null_out(db, bad.id, ["passed_quantity", "failed_quantity",
                               "rework_quantity", "scrap_quantity"])
        rows = QR.get_quality_inspections(db=db, current_user={"tenant": TENANT})
        serialised = [schemas.QualityInspectionResponse.model_validate(r) for r in rows]
    finally:
        T.reset_current_tenant(tok)
    by_no = {s.inspection_no: s for s in serialised}
    nulled = by_no["QI-LEGACY-NULL"]
    assert nulled.passed_quantity == 0 and nulled.failed_quantity == 0, (
        nulled.passed_quantity, nulled.failed_quantity)
    assert nulled.rework_quantity == 0 and nulled.scrap_quantity == 0
    assert nulled.inspected_quantity == 50, nulled.inspected_quantity  # nullable=False, verbatim
    assert isinstance(nulled.passed_quantity, int)
    # the good row's real values are untouched — the heal only rewrites None
    ok = by_no["QI-GOOD"]
    assert (ok.passed_quantity, ok.failed_quantity, ok.rework_quantity, ok.scrap_quantity) == (90, 10, 3, 2)
    print("PASS GET list serialises a legacy NULL row as 0 (not a 500 that hides every inspection)")


def test_response_heals_null_inspected_quantity_not_500():
    # inspected_quantity is Column(Integer, nullable=False) WITHOUT a default. A
    # raw-SQL / migration / legacy row can still hold NULL (the constraint is not
    # retro-applied to pre-existing rows — the same reason analytics_engine coalesces
    # ProductionRecord/ShiftData's own nullable=False count columns), and
    # QualityInspectionResponse types inspected_quantity as a non-optional int — so a
    # single such row raised ValidationError during serialisation and 500-ed the WHOLE
    # GET /quality/inspections list, hiding every good inspection. #423 healed the four
    # default-0 counts here but skipped this column on a mistaken "nullable=False needs
    # no heal". model_validate is exactly what the FastAPI response layer applies to
    # each ORM row returned by get_quality_inspections; a transient ORM object with
    # inspected_quantity=None is the exact attribute state a loaded legacy-NULL row
    # presents (SQLite's enforced NOT NULL means a real on-disk NULL can only predate
    # the constraint, so it can't be planted via raw SQL the way the nullable sibling
    # columns can). Assert the NULL heals to a concrete int 0 while a good row's real
    # inspected_quantity passes through verbatim.
    good = models.QualityInspection(
        id=1, tenant_code=TENANT, inspection_no="QI-GOOD-INS", inspector="QC-1",
        inspected_quantity=120, passed_quantity=100, failed_quantity=20,
        rework_quantity=0, scrap_quantity=0, status="Open",
    )
    bad = models.QualityInspection(
        id=2, tenant_code=TENANT, inspection_no="QI-NULL-INS", inspector="QC-1",
        inspected_quantity=None, passed_quantity=0, failed_quantity=0,
        rework_quantity=0, scrap_quantity=0, status="Open",
    )
    good_s = schemas.QualityInspectionResponse.model_validate(good)
    bad_s = schemas.QualityInspectionResponse.model_validate(bad)
    assert bad_s.inspected_quantity == 0, bad_s.inspected_quantity
    assert isinstance(bad_s.inspected_quantity, int), type(bad_s.inspected_quantity)
    assert good_s.inspected_quantity == 120, good_s.inspected_quantity  # untouched
    print("PASS response serialisation heals a NULL inspected_quantity to 0 (not a 500 that hides every inspection)")


def test_patch_over_legacy_null_inspected_quantity_is_safe():
    # An unrelated patch ({"status": "Rework"}, which never sets inspected_quantity)
    # over a row whose inspected_quantity is a legacy NULL reaches the guards with it
    # still None: the OLD code did min(None, 0, 0, 0, 0) and `0 + 0 > None`, both
    # TypeError -> unhandled 500. Now inspected_quantity heals to 0, the status
    # applies, and the row is healed on write. Independent numbers: passed 0, failed 0,
    # inspected heals 0, so 0 + 0 = 0 <= 0 (invariant OK) and min(...) = 0 (>= 0 OK).
    #
    # A real on-disk NULL can't be planted under SQLite's enforced NOT NULL, so
    # reproduce the exact attribute state a loaded legacy-NULL row has — the attribute
    # is None when the handler reads it — by clearing it in memory. autoflush is turned
    # off so the handler's own SELECT returns this identity-mapped object (None intact,
    # populate_existing is False so a query never overwrites a modified attribute)
    # rather than trying to flush the transient NULL first; the heal then assigns 0,
    # which is what actually commits.
    db = _iso_session()
    row = _make_inspection(db, "QI-NULL-INS-PATCH", inspected=40, passed=0, failed=0)
    db.autoflush = False
    row.inspected_quantity = None
    result = _update(db, row.id, status="Rework")
    assert result.status == "Rework", result.status
    assert result.inspected_quantity == 0, result.inspected_quantity
    db.refresh(row)
    assert row.inspected_quantity == 0, row.inspected_quantity  # healed on write
    print("PASS unrelated patch over a legacy NULL inspected_quantity is safe (heals to 0, not a 500)")


def test_valid_update_applies_and_preserves_invariant():
    # The preserved behaviour: a valid patch applies, and the invariant still rejects
    # passed + failed > inspected with a clean 400 (never a 500). Numbers derived
    # independently: 60 + 30 = 90 <= 100 (ok); then 80 + 30 = 110 > 100 (reject).
    db = _iso_session()
    row = _make_inspection(db, "QI-OK", inspected=100, passed=0, failed=0)
    result = _update(db, row.id, passed_quantity=60, failed_quantity=30)
    assert result.passed_quantity == 60 and result.failed_quantity == 30
    try:
        _update(db, row.id, passed_quantity=80, failed_quantity=30)
        assert False, "passed + failed > inspected must raise, not succeed"
    except HTTPException as exc:
        assert exc.status_code == 400, exc.status_code
    print("PASS valid update applies; the invariant still rejects overflow with a 400")


def test_create_rejects_negative_count_the_invariant_would_pass():
    # The key case the upper-bound invariant CANNOT catch: passed=5, failed=-5,
    # inspected=10 -> passed+failed = 0 <= 10, so the "cannot exceed inspected"
    # check passes and the OLD code stored a row with failed_quantity = -5. That
    # negative then drives the fail rate (failed/inspected = -50%) below zero and
    # the quality rate above 100% in every rollup that sums it. The non-negative
    # guard rejects it with a clean 400, and nothing is persisted. Numbers derived
    # independently: 5 + (-5) = 0 <= 10 (invariant OK) yet min(...) = -5 < 0 (reject).
    db = _iso_session()
    try:
        _create(db, inspection_no="QI-NEG", inspected_quantity=10,
                passed_quantity=5, failed_quantity=-5)
        assert False, "a negative failed_quantity must raise, not store a corrupt row"
    except HTTPException as exc:
        assert exc.status_code == 400, exc.status_code
    db.rollback()
    stored = (
        db.query(models.QualityInspection)
        .filter(models.QualityInspection.inspection_no == "QI-NEG")
        .first()
    )
    assert stored is None, "rejected inspection must not be persisted"
    print("PASS create rejects a negative count the upper-bound invariant would pass (400)")


def test_create_accepts_valid_counts():
    # Regression: a clean, non-negative inspection still creates. 90 + 8 = 98 <= 100,
    # all counts >= 0 -> stored with the values as given.
    db = _iso_session()
    row = _create(db, inspection_no="QI-POS", inspected_quantity=100,
                  passed_quantity=90, failed_quantity=8, rework_quantity=1, scrap_quantity=1)
    assert row.passed_quantity == 90 and row.failed_quantity == 8, (row.passed_quantity, row.failed_quantity)
    assert row.rework_quantity == 1 and row.scrap_quantity == 1
    print("PASS create accepts a valid non-negative inspection")


def test_update_rejects_negative_count():
    # Parity on the patch path: PATCHing failed_quantity=-5 onto a valid row is
    # physically impossible and must 400 rather than persist. The stored row is
    # unchanged (still failed=10) after the rejection.
    db = _iso_session()
    row = _make_inspection(db, "QI-UNEG", inspected=100, passed=90, failed=10)
    try:
        _update(db, row.id, failed_quantity=-5)
        assert False, "a negative failed_quantity patch must raise, not persist"
    except HTTPException as exc:
        assert exc.status_code == 400, exc.status_code
    db.rollback()
    db.refresh(row)
    assert row.failed_quantity == 10, row.failed_quantity
    print("PASS update rejects a negative count with a 400 (stored row unchanged)")


if __name__ == "__main__":
    test_quality_paths_owned_by_module()
    test_failed_inspection_still_publishes_event()
    test_patch_explicit_null_count_heals_to_zero_not_500()
    test_unrelated_patch_over_legacy_null_row_is_safe()
    test_list_serialises_legacy_null_row_without_500()
    test_response_heals_null_inspected_quantity_not_500()
    test_patch_over_legacy_null_inspected_quantity_is_safe()
    test_valid_update_applies_and_preserves_invariant()
    test_create_rejects_negative_count_the_invariant_would_pass()
    test_create_accepts_valid_counts()
    test_update_rejects_negative_count()
    print("ALL QUALITY ROUTE TESTS PASSED")
