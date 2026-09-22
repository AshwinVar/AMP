"""Factory Pulse — the owner's one-glance command header (ADR-0003 / ADR-0006).

Composes read-models the platform already produces into a single snapshot that
answers two questions at once: *how is my factory* and *what needs me?* Fleet
health comes from the machine twins, the agent workload from the impact rollup.
A read-model over read-models — it adds no storage and is tenant-scoped through
its components (ADR-0002).
"""
from ai import twin, impact

name = "pulse"


def _headline(avg_health, measured, machines, needs_attention, awaiting) -> str:
    # The average is over the machines whose score read something. A fleet
    # where nothing has been recorded has no health figure, and says so —
    # it used to read "Fleet health 100" (every unmeasured 100 averaged in) or,
    # with no machines at all, "Fleet health 0".
    if avg_health is None:
        parts = ["Fleet health not measured"
                 + (" (nothing recorded yet)" if machines else " (no machines yet)")]
    elif measured < machines:
        parts = [f"Fleet health {avg_health} ({measured} of {machines} measured)"]
    else:
        parts = [f"Fleet health {avg_health}"]
    if needs_attention:
        parts.append(f"{needs_attention} machine{'s' if needs_attention != 1 else ''} need attention")
    if awaiting:
        parts.append(f"{awaiting} approval{'s' if awaiting != 1 else ''} awaiting you")
    if len(parts) == 1:
        parts.append("all clear")
    return " · ".join(parts)


def build_pulse(db, tenant: str, now=None) -> dict:
    """The command header for one tenant: fleet health from the twins, agent
    workload from the impact rollup, and the single machine that most needs a
    look (the twins are already worst-health-first).

    `now`: the instant the agent window ends at. A composing read-model passes
    its own clock rather than letting each component read the wall clock
    separately (#696) — and it is what lets a test pin the week the Autonomy
    tile reports, instead of hoping the fixture and the query agree."""
    twins = twin.build_twins(db, tenant)
    imp = impact.build_impact(db, tenant, now=now)

    # The fleet figure is twin.fleet_health's, not a second copy of the
    # arithmetic: the Command Centre shows the same number, and two definitions
    # are how the owner's home screen and this header start disagreeing about
    # one plant. The measured-only rule and the None-not-zero rule live there.
    fleet = twin.fleet_health(twins)

    return {
        "fleet": fleet,
        "agents": {
            "agents_active": len(imp["agents_active"]),
            "actions_7d": imp["last_7_days"]["total"],
            # THE WINDOWED RATE, because this tile's own caption says "/7d".
            # It used to be the lifetime rate under that caption: a fleet that
            # auto-approved everything for a year and nothing this week read
            # "Autonomy 100% · 12 actions / 7d". None, never 0, when the week
            # decided nothing — "0% ran autonomously" is a real reading.
            "auto_rate": imp["last_7_days"]["auto_rate"],
            "auto_measured": imp["last_7_days"]["measured"],
            "auto_decided": imp["last_7_days"]["decided"],
            "auto_window": imp["last_7_days"]["window"],
            "awaiting_you": imp["pending_backlog"],
        },
        "headline": _headline(fleet["avg_health"], fleet["measured"], fleet["machines"],
                              fleet["needs_attention"], imp["pending_backlog"]),
    }
