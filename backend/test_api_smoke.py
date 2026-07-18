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


if __name__ == "__main__":
    test_every_read_model_endpoint_answers()
    print(f"API SURFACE OK: all {len(READ_ENDPOINTS)} read-model endpoints return an object; composites "
          "(scorecard/weekly-report/briefing) carry seeded data; copilot ask + digest, entity search "
          "and platform status answer")
