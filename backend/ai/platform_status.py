"""Platform status — the AI platform's self-report (ADR-0003).

"AI as a platform, not scattered scripts": this reports what the platform has
wired up — every read-model service registered under ``ai.*``, the agent
workforce, and whether the LLM copilot is connected — plus a live count of the
tenant's logged agent actions. A read-model over the platform itself; it adds no
storage.
"""
import models

name = "platform_status"


def build_platform_status(db, tenant: str) -> dict:
    """The platform surface: registered read-models, the agent roster, LLM-copilot
    connectivity, and the tenant's logged agent-action count."""
    import ai  # lazy: this module is part of the ai package
    from ai.roster import AGENTS

    read_models = sorted({
        mod.name for mod in vars(ai).values()
        if getattr(mod, "__package__", "") == "ai" and isinstance(getattr(mod, "name", None), str)
    })
    actions = (db.query(models.AgentAction)
               .filter(models.AgentAction.tenant_code == tenant).count())

    llm = False
    try:
        llm = ai.copilot.is_enabled()
    except Exception:
        llm = False

    return {
        "read_models": read_models,
        "read_model_count": len(read_models),
        "agents": [a["key"] for a in AGENTS],
        "agent_count": len(AGENTS),
        "copilot": {"rule_based": True, "llm_enabled": llm},
        "agent_actions_logged": actions,
    }
