"""Factory Pulse — the owner's one-glance command header (ADR-0003 / ADR-0006).

Composes read-models the platform already produces into a single snapshot that
answers two questions at once: *how is my factory* and *what needs me?* Fleet
health comes from the machine twins, the agent workload from the impact rollup.
A read-model over read-models — it adds no storage and is tenant-scoped through
its components (ADR-0002).
"""
from ai import twin, impact

name = "pulse"


def _headline(avg_health, needs_attention, awaiting) -> str:
    parts = [f"Fleet health {avg_health}"]
    if needs_attention:
        parts.append(f"{needs_attention} machine{'s' if needs_attention != 1 else ''} need attention")
    if awaiting:
        parts.append(f"{awaiting} approval{'s' if awaiting != 1 else ''} awaiting you")
    if len(parts) == 1:
        parts.append("all clear")
    return " · ".join(parts)


def build_pulse(db, tenant: str) -> dict:
    """The command header for one tenant: fleet health from the twins, agent
    workload from the impact rollup, and the single machine that most needs a
    look (the twins are already worst-health-first)."""
    twins = twin.build_twins(db, tenant)
    imp = impact.build_impact(db, tenant)

    machines = len(twins)
    avg_health = round(sum(t["health_score"] for t in twins) / machines) if machines else 0
    needs_attention = sum(1 for t in twins if t["health_band"] in ("At risk", "Critical"))
    worst = twins[0] if twins else None

    return {
        "fleet": {
            "machines": machines,
            "avg_health": avg_health,
            "needs_attention": needs_attention,
            "worst": None if not worst else {
                "machine_id": worst["machine_id"],
                "name": worst["name"],
                "health_score": worst["health_score"],
                "health_band": worst["health_band"],
            },
        },
        "agents": {
            "agents_active": len(imp["agents_active"]),
            "actions_7d": imp["last_7_days"]["total"],
            "auto_rate": imp["auto_rate"],
            "awaiting_you": imp["pending_backlog"],
        },
        "headline": _headline(avg_health, needs_attention, imp["pending_backlog"]),
    }
