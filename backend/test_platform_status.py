"""Platform status tests (ADR-0003).

The AI platform's self-report: registered read-models, agent roster, copilot
connectivity. (The public /health check is owned by platform_routes and covered
by test_health.py.) Run:
    python backend/test_platform_status.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import platform_status


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_platform_status_reports_the_surface():
    db = _fresh_session()
    db.add(models.AgentAction(tenant_code="DEFAULT", agent="escalation", action_type="raise_escalation",
                              summary="x", ref_kind="escalation", ref_id=1, status="Proposed"))
    db.commit()

    s = platform_status.build_platform_status(db, "DEFAULT")
    # the read-model surface is discovered and includes the pillars we built
    assert s["read_model_count"] >= 20
    for rm in ("oee", "cost", "delivery", "scorecard", "maintenance", "assistant", "briefing"):
        assert rm in s["read_models"], f"{rm} not reported"
    # the five agents are listed
    assert set(s["agents"]) == {"maintenance", "quality", "reorder", "escalation", "yield"}
    assert s["agent_count"] == 5
    # copilot is rule-based on, LLM off without a key; the tenant's action counted
    assert s["copilot"]["rule_based"] is True and s["copilot"]["llm_enabled"] is False
    assert s["agent_actions_logged"] == 1


if __name__ == "__main__":
    test_platform_status_reports_the_surface()
    print("PLATFORM STATUS OK: self-report lists read-models + 5 agents + copilot state + action count")
