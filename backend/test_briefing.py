"""Morning-briefing read-model tests (ADR-0007).

The briefing composes the pillar read-models (OEE, losses, downtime, quality,
flow, inventory) plus a machine-status glance into one prioritized digest:
headline OEE + trend, ranked alerts (most urgent first), and a few wins.

Run:  python backend/test_briefing.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import briefing


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_briefing_ranks_alerts_and_surfaces_wins():
    db = _fresh_session()
    now = datetime.utcnow()
    # A down machine (high-urgency) and a healthy one.
    db.add(models.Machine(id=1, name="SMT-Reflow-01", status="Breakdown", utilization=0, line="SMT"))
    db.add(models.Machine(id=2, name="IC-Test-01", status="Running", utilization=95, line="IC"))
    # Production so OEE has data (low performance -> a real OEE loss).
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=90,
                                   rejected_count=10, created_at=now))
    # Out-of-stock part (high-urgency supply alert).
    db.add(models.InventoryItem(item_code="CLB-PCB", item_name="Cluster PCB", category="PCB",
                                current_stock=0, reorder_level=50, unit="pcs", supplier="Acme"))
    # A downtime event and a failing inspection.
    db.add(models.DowntimeLog(machine_id=1, reason="Breakdown", duration="40 min", created_at=now))
    db.add(models.QualityInspection(inspection_no="QC-1", machine_id=1, inspector="qa",
                                    inspected_quantity=100, passed_quantity=90, failed_quantity=10,
                                    defect_category="solder"))
    db.commit()

    b = briefing.build_briefing(db, "DEFAULT")
    assert b["has_data"] is True
    assert b["oee"] > 0 and b["oee_trend"] in {"up", "down", "flat"}
    keys = [a["key"] for a in b["alerts"]]
    # every pillar signal is represented
    assert {"machines_down", "out_of_stock", "oee_loss", "quality", "downtime"} <= set(keys)
    # ranked: the two high-severity alerts lead the feed
    assert {b["alerts"][0]["key"], b["alerts"][1]["key"]} == {"machines_down", "out_of_stock"}
    assert all(a["severity"] in {"high", "medium", "low"} for a in b["alerts"])
    # the down-machine alert names the machine
    md = next(a for a in b["alerts"] if a["key"] == "machines_down")
    assert "SMT-Reflow-01" in md["detail"]
    # headline mentions OEE and the attention count
    assert "OEE" in b["headline"] and "attention" in b["headline"]


def test_briefing_empty_is_safe():
    b = briefing.build_briefing(_fresh_session(), "DEFAULT")
    assert b["has_data"] is False and b["alerts"] == [] and b["wins"] == []
    assert b["oee"] == 0 and b["oee_trend"] == "flat"


def test_clean_plant_has_no_alerts_but_reports_wins():
    db = _fresh_session()
    now = datetime.utcnow()
    db.add(models.Machine(id=1, name="IC-Test-01", status="Running", utilization=98, line="IC"))
    # High-OEE run over several days so the plant reads world-class and trend has data.
    for i in range(6):
        db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=478,
                                       ideal_cycle_time_seconds=60, total_count=478, good_count=478,
                                       rejected_count=0, created_at=now - timedelta(days=i)))
    db.add(models.QualityInspection(inspection_no="QC-1", machine_id=1, inspector="qa",
                                    inspected_quantity=100, passed_quantity=100, failed_quantity=0))
    db.commit()

    b = briefing.build_briefing(db, "DEFAULT")
    assert b["has_data"] is True
    # nothing broken, nothing short on stock, quality clean -> no alerts
    assert b["alerts"] == []
    # but the wins surface the world-class OEE and clean yield
    win_titles = " ".join(w["title"] for w in b["wins"])
    assert "world-class" in win_titles and "First-pass yield" in win_titles


if __name__ == "__main__":
    test_briefing_ranks_alerts_and_surfaces_wins()
    test_briefing_empty_is_safe()
    test_clean_plant_has_no_alerts_but_reports_wins()
    print("BRIEFING OK: composes pillar read-models into a ranked alert feed (high-first) + wins; "
          "headline OEE + trend; empty-safe; clean plant -> no alerts, only wins")
