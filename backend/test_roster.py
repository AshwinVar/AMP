"""Agent Roster read-model tests (ADR-0004 / ADR-0005).

The roster lists every agent with its role, autonomy (from the auto-approve
policy), and live activity counts from the agent_actions log, tenant-scoped.

Run:  python backend/test_roster.py     (exit 0 = pass)
"""
import os
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import roster


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _action(agent, status, tenant="DEFAULT", created_at=None, ref_kind="maintenance_task"):
    return models.AgentAction(tenant_code=tenant, agent=agent, action_type="x", summary="x",
                              ref_kind=ref_kind, ref_id=1, status=status, created_at=created_at)


def test_roster_lists_agents_with_autonomy_and_activity():
    os.environ.pop("AUTO_APPROVE_AGENTS", None)   # default: only reorder is autonomous
    db = _fresh_session()
    db.add_all([
        _action("maintenance", "Proposed"),
        _action("maintenance", "Approved"),
        _action("reorder", "Approved"),
        _action("quality", "Rejected"),
        _action("reorder", "Approved", tenant="GMATS"),   # other tenant, must not count
    ])
    db.commit()

    r = roster.build_roster(db, "DEFAULT")
    assert [a["key"] for a in r] == ["maintenance", "quality", "reorder", "escalation", "yield"]  # full fixed roster
    by_key = {a["key"]: a for a in r}

    # autonomy reflects the policy: only reorder auto-approves by default
    assert by_key["reorder"]["auto_approves"] is True
    assert by_key["maintenance"]["auto_approves"] is False
    assert by_key["escalation"]["auto_approves"] is False

    # activity counts are tenant-scoped
    assert by_key["maintenance"]["total_actions"] == 2 and by_key["maintenance"]["pending"] == 1
    assert by_key["reorder"]["total_actions"] == 1 and by_key["reorder"]["approved"] == 1  # GMATS excluded
    assert by_key["quality"]["rejected"] == 1
    assert by_key["escalation"]["total_actions"] == 0 and by_key["escalation"]["last_action_at"] is None

    # every agent carries role metadata for the UI
    assert all(a["name"] and a["watches"] and a["acts"] for a in r)

    # policy is honoured live: trust quality too and it flips
    os.environ["AUTO_APPROVE_AGENTS"] = "reorder,quality"
    r2 = roster.build_roster(db, "DEFAULT")
    assert {a["key"] for a in r2 if a["auto_approves"]} == {"reorder", "quality"}
    os.environ.pop("AUTO_APPROVE_AGENTS", None)   # cleanup


def test_agent_detail_composes_single_agent_cockpit():
    os.environ.pop("AUTO_APPROVE_AGENTS", None)   # default: maintenance needs approval
    db = _fresh_session()
    db.add_all([
        _action("maintenance", "Approved"),
        _action("maintenance", "Rejected"),
        _action("maintenance", "Proposed"),
        _action("reorder", "Approved"),                     # other agent, excluded
        _action("maintenance", "Approved", tenant="GMATS"), # other tenant, excluded
    ])
    db.commit()

    d = roster.build_agent_detail(db, "DEFAULT", "maintenance")
    assert d["key"] == "maintenance" and d["name"] and d["watches"] and d["acts"]
    assert d["auto_approves"] is False
    assert d["total_actions"] == 3 and d["pending"] == 1          # agent + tenant scoped
    assert d["approved"] == 1 and d["rejected"] == 1
    assert d["approval_rate"] == 50                               # 1 of 2 decided
    assert d["outputs"] == {"maintenance_task": 3}                # produced kinds
    assert len(d["daily"]) == 7 and d["daily"][-1]["count"] == 3  # all logged today
    assert len(d["recent"]) == 3 and d["recent"][0]["id"] > d["recent"][-1]["id"]  # newest first

    # an agent that has never acted still gets its card, with an empty tally
    idle = roster.build_agent_detail(db, "DEFAULT", "escalation")
    assert idle["total_actions"] == 0 and idle["approval_rate"] is None and idle["recent"] == []

    assert roster.build_agent_detail(db, "DEFAULT", "nope") is None  # unknown -> caller 404s


def test_roster_counts_full_history_and_latest_via_sql():
    """build_roster tallies the whole log per agent (an OLD action still counts)
    and reports the latest action as a MAX timestamp — computed in SQL, without
    hydrating the ever-growing agent_actions history on every poll (rule 4)."""
    os.environ.pop("AUTO_APPROVE_AGENTS", None)
    db = _fresh_session()
    now = datetime.utcnow()
    db.add_all([
        _action("maintenance", "Approved", created_at=now - timedelta(days=200)),  # ancient
        _action("maintenance", "Proposed", created_at=now),                        # the latest
        _action("maintenance", "Approved", created_at=now - timedelta(days=1)),    # newest id
        _action("reorder", "Approved", created_at=now, tenant="GMATS"),            # other tenant
    ])
    db.commit()

    by_key = {a["key"]: a for a in roster.build_roster(db, "DEFAULT")}
    m = by_key["maintenance"]
    assert m["total_actions"] == 3 and m["approved"] == 2 and m["pending"] == 1
    # last_action_at is the MAX(created_at) (the Proposed row), NOT the last-inserted
    # (highest-id) row, and NOT the ancient one.
    assert m["last_action_at"] == now.isoformat()
    assert by_key["reorder"]["total_actions"] == 0   # GMATS row excluded


def test_agent_detail_bounds_unbounded_history():
    """The cockpit reads one agent's slice of an append-only log on a polled
    endpoint, so it must not hydrate the whole history: lifetime tallies come from
    SQL GROUP BY counts (an OLD action still counts toward them), the 7-day series
    is bounded to the window (the old action is NOT in it, so it sums to less than
    the lifetime total), last_action_at is the MAX timestamp (not the newest row
    id), and the evidence list is a LIMIT (rule 4)."""
    os.environ.pop("AUTO_APPROVE_AGENTS", None)
    db = _fresh_session()
    now = datetime.utcnow()
    old = now - timedelta(days=100)      # outside the 7-day window
    mid = now - timedelta(days=3)        # inside the window

    rows = [_action("maintenance", "Approved", created_at=now) for _ in range(18)]
    rows.append(_action("maintenance", "Proposed", created_at=mid, ref_kind="inspection"))
    # inserted LAST -> highest id but the OLDEST timestamp: proves MAX(created_at),
    # not order-by-id, drives last_action_at.
    rows.append(_action("maintenance", "Rejected", created_at=old))
    rows.append(_action("reorder", "Approved", created_at=now))                     # other agent
    rows.append(_action("maintenance", "Approved", created_at=now, tenant="GMATS")) # other tenant
    db.add_all(rows)
    db.commit()

    d = roster.build_agent_detail(db, "DEFAULT", "maintenance")

    # Lifetime tally over the WHOLE log (old row included), agent + tenant scoped.
    assert d["total_actions"] == 20
    assert d["approved"] == 18 and d["rejected"] == 1 and d["pending"] == 1
    assert d["approval_rate"] == 95                       # round(18 / 19 decided * 100)
    assert d["outputs"] == {"maintenance_task": 19, "inspection": 1}

    # last_action_at is the MAX timestamp (today), NOT the last-inserted (old) row.
    assert d["last_action_at"] == now.isoformat()

    # 7-day series is windowed: today = 18, three-days-ago = 1; the 100-day-old row
    # is excluded, so the series sums to 19 — one short of the lifetime 20.
    assert len(d["daily"]) == 7
    assert d["daily"][-1]["count"] == 18                  # today
    assert sum(x["count"] for x in d["daily"]) == 19      # the ancient action isn't in the window

    # Evidence is capped (LIMIT 15), newest-id first.
    assert len(d["recent"]) == 15
    assert d["recent"][0]["id"] > d["recent"][-1]["id"]


def test_agent_policy_reflects_saved_and_default_source():
    from ai import agents
    os.environ.pop("AUTO_APPROVE_AGENTS", None)   # default trust: only reorder
    db = _fresh_session()

    # no saved policy -> source is the platform default
    p = roster.build_agent_policy(db, "DEFAULT")
    assert p["source"] == "default"
    assert [a["key"] for a in p["agents"]] == ["maintenance", "quality", "reorder", "escalation", "yield"]
    by_key = {a["key"]: a for a in p["agents"]}
    assert by_key["reorder"]["auto_approves"] is True
    assert by_key["maintenance"]["auto_approves"] is False
    assert all(a["name"] and a["watches"] and a["acts"] for a in p["agents"])   # role metadata for the UI

    # saving a policy flips the source to 'tenant' and reflects the exact choice
    agents.set_agent_policy(db, "DEFAULT", ["maintenance"])
    p2 = roster.build_agent_policy(db, "DEFAULT")
    assert p2["source"] == "tenant"
    by_key2 = {a["key"]: a for a in p2["agents"]}
    assert by_key2["maintenance"]["auto_approves"] is True
    assert by_key2["reorder"]["auto_approves"] is False        # no longer in the trusted set


if __name__ == "__main__":
    test_roster_lists_agents_with_autonomy_and_activity()
    test_agent_detail_composes_single_agent_cockpit()
    test_roster_counts_full_history_and_latest_via_sql()
    test_agent_detail_bounds_unbounded_history()
    test_agent_policy_reflects_saved_and_default_source()
    print("ROSTER OK: full fleet listed; autonomy from policy (live); activity counts tenant-scoped; "
          "single-agent cockpit (tally, approval rate, outputs, 7-day series) agent+tenant-scoped")
