"""Agent Roster read-model tests (ADR-0004 / ADR-0005).

The roster lists every agent with its role, autonomy (from the auto-approve
policy), and live activity counts from the agent_actions log, tenant-scoped.

Run:  python backend/test_roster.py     (exit 0 = pass)
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import roster


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _action(agent, status, tenant="DEFAULT"):
    return models.AgentAction(tenant_code=tenant, agent=agent, action_type="x", summary="x",
                              ref_kind="maintenance_task", ref_id=1, status=status)


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


if __name__ == "__main__":
    test_roster_lists_agents_with_autonomy_and_activity()
    test_agent_detail_composes_single_agent_cockpit()
    print("ROSTER OK: full fleet listed; autonomy from policy (live); activity counts tenant-scoped; "
          "single-agent cockpit (tally, approval rate, outputs, 7-day series) agent+tenant-scoped")
