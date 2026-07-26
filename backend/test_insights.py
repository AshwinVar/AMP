"""Mission Control insight-feed read-model tests (ADR-0003 / ADR-0005).

``ai.insights.build_feed`` unifies three sources into one most-recent-first feed
for a single tenant: open AI recommendations, recent *notable* domain events, and
the agents' *proposed* actions. Two things about it are safety-critical and were
previously untested:

  * TENANT ISOLATION. event_log and agent_actions are NOT in
    ``tenancy.SCOPED_MODELS`` (only ai_recommendations is), so the feed's own
    explicit ``tenant_code`` filters are the ONLY thing keeping one tenant's
    downtime, quality fails and agent actions out of another tenant's feed.
  * CRASH SAFETY on a malformed event payload. A payload that is valid JSON but
    not an object ("[1,2]", "42", '"hi"') has no fields to read; before the fix
    it raised AttributeError and 500'd the whole feed for every reader.

Run:  python backend/test_insights.py     (exit 0 = pass)
"""
import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import insights


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _rec(title, when, tenant="DEFAULT", status="Open"):
    return models.AIRecommendation(
        tenant_code=tenant, recommendation_type="predictive_maintenance",
        severity="High", title=title, message="do the thing", status=status,
        related_machine_id=7, created_at=when)


def _event(event_type, when, payload, tenant="DEFAULT"):
    return models.EventLog(
        tenant_code=tenant, event_type=event_type,
        payload=None if payload is None else (payload if isinstance(payload, str) else json.dumps(payload)),
        occurred_at=when)


def _action(summary, when, tenant="DEFAULT", status="Proposed"):
    return models.AgentAction(
        tenant_code=tenant, agent="maintenance", action_type="open_task", summary=summary,
        ref_kind="maintenance_task", ref_id=42, severity="High", status=status,
        related_machine_id=7, created_at=when)


BASE = datetime(2026, 1, 15, 12, 0, 0)


def test_feed_unifies_the_three_sources_and_filters_each():
    db = _fresh_session()
    db.add_all([
        _rec("Reflow oven: high failure risk", BASE + timedelta(minutes=3)),           # -> in
        _rec("Old resolved rec", BASE + timedelta(minutes=9), status="Resolved"),      # not Open -> out
        _event("DowntimeStarted", BASE + timedelta(minutes=2),
                {"machine_id": 7, "reason": "Bearing seized", "duration": "45 min"}),   # notable -> in
        _event("ProductionRecorded", BASE + timedelta(minutes=8), {"machine_id": 7}),   # not notable -> out
        _action("Open maintenance task for M7", BASE + timedelta(minutes=1)),           # Proposed -> in
        _action("Already approved", BASE + timedelta(minutes=7), status="Approved"),    # not Proposed -> out
    ])
    db.commit()

    feed = insights.build_feed(db, "DEFAULT")
    # Exactly the three qualifying rows; the filtered-out ones never appear.
    assert len(feed) == 3, feed
    assert {i["source"] for i in feed} == {"recommendation", "event", "action"}
    titles = {i["title"] for i in feed}
    assert "Old resolved rec" not in titles
    assert "Already approved" not in titles
    assert not any(i["kind"] == "ProductionRecorded" for i in feed)

    # Most-recent-first by occurred_at: rec(t3) > event(t2) > action(t1).
    assert [i["source"] for i in feed] == ["recommendation", "event", "action"]

    rec = next(i for i in feed if i["source"] == "recommendation")
    ev = next(i for i in feed if i["source"] == "event")
    act = next(i for i in feed if i["source"] == "action")

    # Recommendations and actions carry a row id to act on; events don't.
    assert rec["ref_id"] is not None and act["ref_id"] is not None
    assert ev["ref_id"] is None

    # Event severity is mapped from its type, and its title/message come from the
    # payload we serialised.
    assert ev["severity"] == "High"                       # DowntimeStarted -> High
    assert "Bearing seized" in ev["title"]
    assert "45 min" in ev["message"] and "7" in ev["message"]
    assert ev["related_machine_id"] == 7
    print("  feed unifies + filters the three sources: OK")


def test_tenant_isolation_across_all_three_sources():
    """The whole point of the explicit tenant filters: event_log and
    agent_actions are not auto-scoped, so a leak here would surface another
    tenant's events/actions in this tenant's Mission Control."""
    db = _fresh_session()
    db.add_all([
        _rec("DEFAULT rec", BASE + timedelta(minutes=1), tenant="DEFAULT"),
        _event("DowntimeStarted", BASE + timedelta(minutes=2), {"reason": "default stop"}, tenant="DEFAULT"),
        _action("DEFAULT action", BASE + timedelta(minutes=3), tenant="DEFAULT"),
        _rec("GMATS rec", BASE + timedelta(minutes=4), tenant="GMATS"),
        _event("DowntimeStarted", BASE + timedelta(minutes=5), {"reason": "gmats stop"}, tenant="GMATS"),
        _action("GMATS action", BASE + timedelta(minutes=6), tenant="GMATS"),
    ])
    db.commit()

    default = insights.build_feed(db, "DEFAULT")
    assert len(default) == 3
    blob = json.dumps(default)
    assert "GMATS" not in blob and "gmats stop" not in blob

    gmats = insights.build_feed(db, "GMATS")
    assert len(gmats) == 3
    assert all("DEFAULT" not in i["title"] for i in gmats)
    assert not any("default stop" in i["message"] for i in gmats)

    # A tenant with nothing gets an empty feed, not a crash.
    assert insights.build_feed(db, "NOBODY") == []
    print("  tenant isolation across recs/events/actions: OK")


def test_malformed_and_missing_payloads_do_not_crash_the_feed():
    """A non-dict JSON payload (list/number/string), broken JSON, or NULL must
    degrade to a bare event — never take the whole feed down. This pins the fix."""
    db = _fresh_session()
    db.add_all([
        _event("DowntimeStarted", BASE + timedelta(minutes=5), "[1, 2, 3]"),   # valid JSON, not an object
        _event("QualityInspectionFailed", BASE + timedelta(minutes=4), "42"),  # valid JSON number
        _event("InventoryLow", BASE + timedelta(minutes=3), '"a bare string"'),# valid JSON string
        _event("DowntimeStarted", BASE + timedelta(minutes=2), "{not valid json"),  # broken JSON
        _event("InventoryLow", BASE + timedelta(minutes=1), None),             # NULL payload
        # a well-formed one still renders normally alongside the bad ones
        _event("DowntimeStarted", BASE + timedelta(minutes=6),
               {"machine_id": 3, "reason": "Coolant low", "duration": "20 min"}),
    ])
    db.commit()

    feed = insights.build_feed(db, "DEFAULT")           # must not raise
    assert len(feed) == 6
    # every event still has a title and no machine leaked from the malformed rows
    assert all(i["title"] for i in feed)
    bad = next(i for i in feed if "Coolant low" not in (i["title"] or ""))
    assert bad["related_machine_id"] is None
    good = next(i for i in feed if "Coolant low" in i["title"])
    assert good["related_machine_id"] == 3
    print("  malformed / non-dict / NULL payloads survive: OK")


def test_limit_returns_the_globally_most_recent_across_sources():
    """Each source is capped to `limit` then merged and re-sorted, so the result
    is the true global top-N by time — even when the newest items are spread
    across all three sources."""
    db = _fresh_session()
    # 9 rows at strictly increasing minutes 0..8, interleaved across sources.
    db.add_all([
        _rec("R0", BASE + timedelta(minutes=0)),
        _action("A0", BASE + timedelta(minutes=1)),
        _event("DowntimeStarted", BASE + timedelta(minutes=2), {"reason": "E0"}),
        _rec("R1", BASE + timedelta(minutes=3)),
        _action("A1", BASE + timedelta(minutes=4)),
        _event("DowntimeStarted", BASE + timedelta(minutes=5), {"reason": "E1"}),
        _rec("R2", BASE + timedelta(minutes=6)),
        _action("A2", BASE + timedelta(minutes=7)),
        _event("DowntimeStarted", BASE + timedelta(minutes=8), {"reason": "E2"}),
    ])
    db.commit()

    feed = insights.build_feed(db, "DEFAULT", limit=4)
    assert len(feed) == 4
    # Independently: the four latest are minutes 8,7,6,5 -> E2 (event), A2 (action),
    # R2 (recommendation), E1 (event), in that order.
    assert [i["source"] for i in feed] == ["event", "action", "recommendation", "event"]
    assert "E2" in feed[0]["title"]
    assert feed[1]["title"] == "A2"
    assert feed[2]["title"] == "R2"
    assert "E1" in feed[3]["title"]
    print("  limit returns the global most-recent across sources: OK")


def test_empty_database_is_empty_safe():
    db = _fresh_session()
    assert insights.build_feed(db, "DEFAULT") == []
    print("  empty database -> empty feed: OK")


if __name__ == "__main__":
    test_feed_unifies_the_three_sources_and_filters_each()
    test_tenant_isolation_across_all_three_sources()
    test_malformed_and_missing_payloads_do_not_crash_the_feed()
    test_limit_returns_the_globally_most_recent_across_sources()
    test_empty_database_is_empty_safe()
    print("INSIGHTS OK: three-source feed; per-source filtering; tenant isolation; "
          "malformed/non-dict/NULL payloads survive; global most-recent limit; empty-safe")
