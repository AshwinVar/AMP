"""AI recommendation mapper + persistence tests (ADR-0003 / ADR-0002).

``ai/recommendations.py`` is the thin mapper that turns domain events and risk
rows into stored, per-tenant ``AIRecommendation`` suggestions. It had no test of
its own, yet it owns two things that are easy to get quietly wrong:

  * the event -> ``Recommendation`` mapping — the quality fail-rate maths, the
    severity thresholds, and the out-of-stock escalation;
  * ``persist``'s open-only dedupe, which leans entirely on the tenant-scoping
    layer (ADR-0002) to keep one tenant's open suggestion from suppressing an
    identical suggestion for another tenant.

Every expected number here is derived by hand from the inputs (not read back
from the code), and the edges the correctness rules call out are pinned: a
zero inspected quantity (no divide-by-zero), a missing defect category, an
out-of-stock item, and cross-tenant isolation of the dedupe.

Run:  python backend/test_recommendations.py     (exit 0 = pass)
Also collectable by pytest.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import tenancy
from database import Base
from ai import recommendations
from events import InventoryLow, QualityInspectionFailed


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


# ---------------------------------------------------------------------------
# Mapper functions — pure, no DB. Expected values derived by hand.
# ---------------------------------------------------------------------------

def test_from_low_stock_maps_and_escalates_when_out():
    # Below the line but with stock left -> Medium.
    ev = InventoryLow(tenant_code="GMATS", item_id=7, item_code="BRK-100",
                      item_name="Brake Pad", current_stock=3, reorder_level=10)
    rec = recommendations.from_low_stock(ev)
    assert rec.recommendation_type == "reorder_stock"
    assert rec.title == "Reorder Brake Pad (BRK-100)"     # subject-identifying title (dedupe key)
    assert rec.severity == "Medium"
    assert rec.confidence == 90
    assert "3" in rec.message and "10" in rec.message      # current stock and reorder level named

    # Already out (0 or below) -> High.
    assert recommendations.from_low_stock(
        InventoryLow(tenant_code="GMATS", item_id=7, item_code="BRK-100",
                     item_name="Brake Pad", current_stock=0, reorder_level=10)).severity == "High"
    # Over-issued into a negative balance still reads as out -> High, not a crash.
    assert recommendations.from_low_stock(
        InventoryLow(tenant_code="GMATS", item_id=7, item_code="BRK-100",
                     item_name="Brake Pad", current_stock=-2, reorder_level=10)).severity == "High"


def test_from_quality_defect_rate_and_severity_thresholds():
    # 3 of 20 -> 15.0% -> Critical (>= 10).
    rec = recommendations.from_quality_defect(
        QualityInspectionFailed(tenant_code="GMATS", inspection_no="QC-001",
                                failed_quantity=3, inspected_quantity=20,
                                machine_id=5, defect_category="solder bridge"))
    assert rec.recommendation_type == "quality_defect"
    assert rec.title == "Quality: 3 failed on QC-001"
    assert rec.related_machine_id == 5
    assert rec.severity == "Critical"                      # 15.0% >= 10
    assert "3 of 20" in rec.message and "15.0%" in rec.message and "solder bridge" in rec.message

    def rate_severity(failed, inspected):
        r = recommendations.from_quality_defect(
            QualityInspectionFailed(tenant_code="GMATS", inspection_no="QC-X",
                                    failed_quantity=failed, inspected_quantity=inspected))
        return r.severity

    # Boundary walk (thresholds: >=10 Critical, >=5 High, else Medium):
    assert rate_severity(2, 20) == "Critical"              # 10.0% -> exactly the Critical floor
    assert rate_severity(5, 100) == "High"                 #  5.0% -> exactly the High floor
    assert rate_severity(4, 100) == "Medium"               #  4.0% -> below High
    assert rate_severity(9, 100) == "High"                 #  9.0% -> High, just under Critical


def test_from_quality_defect_zero_inspected_and_missing_defect():
    # Zero inspected must not divide by zero: rate 0.0 -> Medium, and the missing
    # defect category falls back to a readable phrase (not "None").
    rec = recommendations.from_quality_defect(
        QualityInspectionFailed(tenant_code="GMATS", inspection_no="QC-Z",
                                failed_quantity=1, inspected_quantity=0,
                                defect_category=None))
    assert rec.severity == "Medium"                        # 0.0% -> below every escalation
    assert "0.0%" in rec.message
    assert "unspecified defects" in rec.message
    assert rec.related_machine_id is None


def test_from_risk_composes_title_and_reasons():
    risk = {
        "machine_id": 5, "machine_name": "CNC-01",
        "risk_level": "Critical", "risk_score": 88,
        "recommendation": "Schedule inspection",
        "reasons": ["120 downtime minutes", "5 downtime events"],
    }
    rec = recommendations.from_risk(risk)
    assert rec.recommendation_type == "predictive_maintenance"
    assert rec.title == "CNC-01: critical failure risk"    # risk level lower-cased into the title
    assert rec.severity == "Critical"
    assert rec.confidence == 88
    assert rec.related_machine_id == 5
    assert rec.message == "Schedule inspection (120 downtime minutes, 5 downtime events)"


# ---------------------------------------------------------------------------
# persist() — open-only dedupe, enforced per-tenant by the scoping layer.
# ---------------------------------------------------------------------------

def _low_stock_rec():
    return recommendations.from_low_stock(
        InventoryLow(tenant_code="ignored", item_id=1, item_code="BRK-100",
                     item_name="Brake Pad", current_stock=0, reorder_level=10))


def test_persist_dedupes_open_and_maps_every_field():
    tenancy.install_scoping()
    db = _fresh_session()
    tok = tenancy.set_current_tenant("GMATS")
    try:
        rec = recommendations.from_risk({
            "machine_id": 9, "machine_name": "CNC-09", "risk_level": "High",
            "risk_score": 71, "recommendation": "Inspect spindle", "reasons": ["hot bearing"],
        })
        assert recommendations.persist(db, rec) is True    # first write happens
        db.commit()
        # An identical *open* suggestion is not stored twice.
        assert recommendations.persist(db, rec) is False
        db.commit()

        rows = db.query(models.AIRecommendation).all()
        assert len(rows) == 1
        r = rows[0]
        # Every field carried across, and the tenant was stamped by the scoping layer.
        assert r.recommendation_type == "predictive_maintenance"
        assert r.title == "CNC-09: high failure risk"
        assert r.severity == "High"
        assert r.message == "Inspect spindle (hot bearing)"
        assert r.confidence == 71
        assert r.related_machine_id == 9
        assert r.status == "Open"
        assert r.tenant_code == "GMATS"
    finally:
        tenancy.reset_current_tenant(tok)


def test_persist_dedupe_does_not_collide_across_tenants():
    # The dedupe key is (type, title, Open) — WITHOUT an explicit tenant filter,
    # so an identical suggestion for two tenants must still produce two rows. If
    # the scoping layer ever stops filtering the dedupe SELECT, one tenant's open
    # suggestion would silently suppress the other's — this pins that it doesn't.
    tenancy.install_scoping()
    db = _fresh_session()

    tok = tenancy.set_current_tenant("GMATS")
    try:
        assert recommendations.persist(db, _low_stock_rec()) is True
        db.commit()
    finally:
        tenancy.reset_current_tenant(tok)

    tok = tenancy.set_current_tenant("DEFAULT")
    try:
        # Same (type, title) as GMATS' row, but a different tenant -> a fresh write.
        assert recommendations.persist(db, _low_stock_rec()) is True
        db.commit()
        assert db.query(models.AIRecommendation).count() == 1        # DEFAULT sees only its own
        assert db.query(models.AIRecommendation).one().tenant_code == "DEFAULT"
    finally:
        tenancy.reset_current_tenant(tok)

    tok = tenancy.set_current_tenant("GMATS")
    try:
        assert db.query(models.AIRecommendation).count() == 1        # GMATS still sees only its own
        assert db.query(models.AIRecommendation).one().tenant_code == "GMATS"
    finally:
        tenancy.reset_current_tenant(tok)


def test_persist_reopens_after_the_previous_one_is_closed():
    # Dedupe only looks at *open* rows: once a suggestion is resolved/dismissed,
    # the same finding is allowed to be raised again.
    tenancy.install_scoping()
    db = _fresh_session()
    tok = tenancy.set_current_tenant("GMATS")
    try:
        assert recommendations.persist(db, _low_stock_rec()) is True
        db.commit()
        # Close the first suggestion out.
        row = db.query(models.AIRecommendation).one()
        row.status = "Resolved"
        db.commit()
        # The same finding can now be stored again as a new open suggestion.
        assert recommendations.persist(db, _low_stock_rec()) is True
        db.commit()
        statuses = sorted(r.status for r in db.query(models.AIRecommendation).all())
        assert statuses == ["Open", "Resolved"]
    finally:
        tenancy.reset_current_tenant(tok)


if __name__ == "__main__":
    test_from_low_stock_maps_and_escalates_when_out()
    test_from_quality_defect_rate_and_severity_thresholds()
    test_from_quality_defect_zero_inspected_and_missing_defect()
    test_from_risk_composes_title_and_reasons()
    test_persist_dedupes_open_and_maps_every_field()
    test_persist_dedupe_does_not_collide_across_tenants()
    test_persist_reopens_after_the_previous_one_is_closed()
    print("RECOMMENDATIONS OK: mapper maths + severity thresholds + zero/None edges; "
          "open-only dedupe; per-tenant isolation (ADR-0002); reopen-after-close")
