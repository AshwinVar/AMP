"""End-to-end read-model surface test.

Seeds a fresh DB and drives every read-model endpoint *handler* (found on the
app's own routes) for an authenticated tenant, asserting each returns a JSON
object without raising. This catches a broken endpoint or a regression in one
pillar that breaks a composite (scorecard, handover, weekly-report and briefing
each compose several read-models) — across the whole surface, in one test, with
no HTTP client dependency.

Run:  python backend/test_api_smoke.py     (exit 0 = pass)
"""
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
import models
from database import Base

_engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
Base.metadata.create_all(bind=_engine)
_Session = sessionmaker(bind=_engine, autoflush=False)

USER = {"tenant": "DEFAULT", "sub": "tester", "role": "Admin"}

READ_ENDPOINTS = [
    "/oee-summary", "/losses-summary", "/downtime-summary", "/quality-summary",
    "/production-summary", "/inventory-summary", "/flow-summary", "/shift-summary",
    "/delivery-summary", "/cost-summary", "/handover", "/scorecard", "/briefing",
    "/maintenance-summary", "/twin-overlay", "/weekly-report",
    "/reliability-summary",
]


def _handler(path, method="GET"):
    for r in main.app.routes:
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set()):
            return r.endpoint
    raise AssertionError(f"no {method} route for {path}")


def _seed():
    db = _Session()
    db.add(models.Machine(id=1, name="SMT-Reflow-01", status="Breakdown", utilization=0, line="SMT"))
    db.add(models.Machine(id=2, name="IC-Test-01", status="Running", utilization=90, line="IC"))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=90,
                                   rejected_count=10, created_at=datetime.utcnow()))
    db.add(models.CustomerOrder(order_no="BUG-1", customer_name="Bugatti", product_name="P",
                                order_quantity=100, dispatched_quantity=50, status="Pending",
                                due_date=datetime.utcnow().date()))
    db.commit()
    db.close()


def test_every_read_model_endpoint_answers():
    _seed()

    # every read-model endpoint handler returns a JSON object without raising
    for ep in READ_ENDPOINTS:
        result = _handler(ep)(db=_Session(), current_user=USER)
        assert isinstance(result, dict), f"{ep} did not return an object"

    # composites carry the seeded data through the full chain
    assert _handler("/scorecard")(db=_Session(), current_user=USER)["kpis"]
    assert _handler("/oee-summary")(db=_Session(), current_user=USER)["plant"]["has_data"] is True
    assert "# Weekly Plant Report" in _handler("/weekly-report")(db=_Session(), current_user=USER)["markdown"]
    assert any(a["key"] == "machines_down" for a in _handler("/briefing")(db=_Session(), current_user=USER)["alerts"])

    # the rule-first copilot answers a question from the same data
    ask = _handler("/copilot/ask", "POST")
    ans = ask(payload={"question": "Which machines are down?"}, db=_Session(), current_user=USER)
    assert "SMT-Reflow-01" in ans["answer"] and ans["view"] == "machines"

    # the copilot digest, entity search, and platform self-report also answer
    digest = _handler("/copilot/digest")(db=_Session(), current_user=USER)
    assert digest["digest"]
    hits = _handler("/search")(q="BUG-1", db=_Session(), current_user=USER)["results"]
    assert any(h["type"] == "customer order" and h["view"] == "orders" for h in hits)
    plat = _handler("/platform/status")(db=_Session(), current_user=USER)
    assert plat["read_model_count"] >= 25 and plat["agent_count"] == 5

    # ── New read-model endpoints exercised through their actual route handlers ──
    # The original READ_ENDPOINTS list predates the later ADR-0007 pillars, so a
    # broken route wiring or a projection that silently drops/renames a field on
    # any of these would have gone uncaught. Each handler below is the real
    # endpoint pulled off app.routes; asserting its documented keys closes that gap.
    NEW_SUMMARY_KEYS = {
        "/reliability-summary":   ["days", "machines_tracked", "total_failures",
                                   "mttr_minutes", "mtbf_hours", "availability",
                                   "by_machine", "bottleneck", "top_modes"],
        "/connectivity-summary":  ["machines_tracked", "reporting", "fresh", "stale",
                                   "dark", "connectivity_score", "devices",
                                   "signal_quality", "by_machine"],
        "/schedule-summary":      ["days", "total", "met", "on_track", "behind",
                                   "missed", "attainment_rate", "by_shift",
                                   "by_machine", "daily", "today"],
        "/supply-summary":        ["total", "received", "on_track", "at_risk", "late",
                                   "receipt_rate", "by_supplier", "chase"],
        "/coverage-summary":      ["window_days", "total_items", "out_of_stock",
                                   "running_out", "critical", "watch", "items"],
        "/quality-trend":         ["days", "current", "prior", "delta_pts",
                                   "direction", "defect_movers", "series"],
        "/maintenance-execution": ["days", "completed", "on_time", "late",
                                   "compliance_rate", "backlog", "by_machine", "chase"],
        "/recovery-summary":      ["has_data", "oee", "world_class", "gap_points",
                                   "components", "biggest_lever"],
        "/compliance-summary":    ["total", "overdue", "due_soon", "pending_approval",
                                   "by_status", "documents"],
    }
    for ep, expected in NEW_SUMMARY_KEYS.items():
        result = _handler(ep)(db=_Session(), current_user=USER)
        assert isinstance(result, dict), f"{ep} did not return an object"
        missing = [k for k in expected if k not in result]
        assert not missing, f"{ep} missing keys {missing}"

    # Drill-down endpoints (arg-taking) — same route layer, exercised with a
    # representative key; each must echo its argument and carry its shape.
    dr = _handler("/downtime-reason")(reason="Changeover", db=_Session(), current_user=USER)
    assert dr["reason"] == "Changeover" and {"total_events", "total_minutes", "by_machine"} <= dr.keys()

    qd = _handler("/quality-defect")(category="Solder Bridge", db=_Session(), current_user=USER)
    assert qd["category"] == "Solder Bridge" and {"failed", "rework", "scrap", "by_machine"} <= qd.keys()

    ip = _handler("/inventory-part")(item_code="NOPE-1", db=_Session(), current_user=USER)
    assert ip["item_code"] == "NOPE-1" and ip["found"] is False and "days_of_cover" in ip

    ss = _handler("/schedule-shift")(shift="A", db=_Session(), current_user=USER)
    assert ss["shift"] == "A" and ss["found"] is False and {"attainment_rate", "rank", "by_machine"} <= ss.keys()

    sp = _handler("/supply-supplier")(supplier="Acme", db=_Session(), current_user=USER)
    assert sp["supplier"] == "Acme" and {"receipt_rate", "total", "recent"} <= sp.keys()

    wt = _handler("/work-order-trace")(work_order_no="WO-NONE", db=_Session(), current_user=USER)
    assert wt["work_order_no"] == "WO-NONE" and wt["found"] is False and {"timeline", "gaps", "materials"} <= wt.keys()

    # Drill-downs that hit the *found* path on the seeded machine / customer, so a
    # real row flows through the tenant-scoped read (not just an empty shape). These
    # pin seed-derived values, so a serializer that garbled the payload would fail.
    rm = _handler("/reliability-machine")(machine_id=1, db=_Session(), current_user=USER)
    assert rm["found"] is True and rm["name"] == "SMT-Reflow-01" and rm["status"] == "Breakdown"
    assert rm["machines_tracked"] == 2 and {"mtbf_hours", "availability", "top_modes"} <= rm.keys()

    cm = _handler("/connectivity-machine")(machine_id=1, db=_Session(), current_user=USER)
    assert cm["found"] is True and cm["name"] == "SMT-Reflow-01" and {"state", "by_signal", "blind_spots"} <= cm.keys()

    rel = _handler("/reliability-summary")(db=_Session(), current_user=USER)
    con = _handler("/connectivity-summary")(db=_Session(), current_user=USER)
    assert rel["machines_tracked"] == 2 and rel["total_failures"] == 0
    assert con["machines_tracked"] == 2

    dc = _handler("/delivery-customer")(customer="Bugatti", db=_Session(), current_user=USER)
    assert dc["customer"] == "Bugatti" and dc["total"] == 1
    assert dc["ordered_units"] == 100 and dc["dispatched_units"] == 50 and dc["fulfillment_rate"] == 50


if __name__ == "__main__":
    test_every_read_model_endpoint_answers()
    print(f"API SURFACE OK: all {len(READ_ENDPOINTS)} read-model endpoints return an object; composites "
          "(scorecard/weekly-report/briefing) carry seeded data; copilot ask + digest, entity search "
          "and platform status answer")
