"""Action tools: what AMP would PROPOSE, drafted from what it measured (ADR-0039).

WHY A DRAFT AND NOT A WRITE
---------------------------
The Copilot's conversation used to dead-end. "Create a maintenance task" matched
no pillar, fell through to the briefing tool, and answered with a plant summary —
so the one thing an owner most wants to do at the end of a conversation was the
one thing AMP silently did not do, without ever saying it could not.

It would have been easy to close that by registering a tool that writes. That is
exactly what ADR-0022's chain forbids:

    USER -> AUTHENTICATION -> RBAC -> TENANT / OEM / CONSENT -> AMP TOOL -> DATA -> MODEL

A language model names tools. If a named tool writes, then a model — or anything
that can shape a model's output, including text inside the plant's own data —
decides that a row is created. So nothing here writes. A tool in this module
reads the plant and returns a DRAFT: the action AMP would propose, with the
evidence that justifies it. The draft is inert. It becomes real only when a
person raises it through `POST /agent-actions/propose`, which resolves the
machine again from the authenticated request rather than trusting the draft it
is handed, and it takes effect only when a person approves it through
`approvals.authorise` — the same gate the five agents have always passed.

    tool (here)          reads    -> a draft, in the answer
    POST /propose        writes   -> MaintenanceTask "Proposed" + AgentAction "Proposed"
    POST /{id}/approve   executes -> task "Open", outcome baseline frozen (ADR-0029)

Three separate human-authenticated steps, and the model is present at none of
them. What the model can influence is which machine's name it puts in the
argument, and `_resolve_machine` only ever answers with a machine of the bound
tenant — so the worst a wrong name achieves is a NOT FOUND refusal.

WHAT A DRAFT MAY CLAIM
----------------------
The same rule as every other AMP surface: the draft states what was recorded,
and says what it could not read. A machine with nothing in the risk window is
scored by the current-state rules alone — status and utilisation — because the
six history rules read nothing (ADR-0027). The score is still shown, with that
caveat on it and INSUFFICIENT HISTORY as the state, exactly as /machine-health
shows it. It is NOT suppressed: a machine in breakdown right now is the most
urgent job in the plant, and hiding its score because its thirty-day file is
empty would rank it below every machine that has simply been logged.
"""
import models
from ai import evidence as ev
from ai.tools.factory import _fact, _resolve_machine
from ai.tools.registry import Param, tool

M, R = ev.MEASURED, ev.RULE

# The task the Copilot drafts is tagged so it is never confused with the
# Maintenance agent's own `Predictive (auto)` tasks: those came from an event,
# these came from a person asking. ai.agents._open_auto_task_exists checks per
# task_type, so the two cannot suppress each other's proposals either.
COPILOT_TASK_TYPE = "Predictive (Copilot)"

# The rule that turns a risk score into a priority, stated wherever it is read.
# predictive_engine.classify_risk's own bands, named here because a draft has to
# say which threshold earned the word it used.
PRIORITY_RULE = "Critical at risk 75+, High at 50+, Medium below 50"


def _priority(score) -> str:
    if score is None:
        return "Medium"
    if score >= 75:
        return "Critical"
    if score >= 50:
        return "High"
    return "Medium"


def _open_task_count(db, tenant, machine_id) -> int:
    """Open maintenance tasks already on this machine, of any kind. A draft that
    ignored them would invite a second task for a job already booked."""
    from ai.maintenance import OPEN_STATUSES   # lazy: maintenance pulls in the pillars
    return (
        db.query(models.MaintenanceTask)
        .filter(models.MaintenanceTask.tenant_code == tenant,
                models.MaintenanceTask.machine_id == machine_id,
                models.MaintenanceTask.status.in_(OPEN_STATUSES))
        .count()
    )


@tool("draft_maintenance_task",
      "Draft a maintenance task for one machine, for a person to raise and approve. "
      "Reads the machine's condition and writes nothing. Use when the user asks to "
      "create, raise, book or schedule maintenance, a job or an inspection on a machine. "
      "Needs the machine's name.",
      mirrors="/agent-actions/propose", view="inbox", domain="maintenance",
      roles=("Admin", "Supervisor"),
      params={"machine": Param("str", "The machine's name, e.g. CNC-01", required=True)})
def draft_maintenance_task(db, tenant, machine):
    """The maintenance task AMP would propose for this machine, and why.

    Writes nothing. The returned `action` is raised by POST /agent-actions/propose
    and executed by the approval gate; see the module docstring.

    The role list is the one on that route, not a wider one: a draft is only
    shown to somebody who could actually raise it, so AMP never offers an
    Operator a button that would refuse them.
    """
    row, why = _resolve_machine(db, tenant, machine)
    if row is None:
        return ev.refusal("draft_maintenance_task", ev.NOT_FOUND, why)
    return draft_for_machine(db, tenant, row)


def draft_for_machine(db, tenant, row) -> ev.ToolResult:
    """The same draft, for a machine row the caller has already resolved.

    The route that RAISES a draft re-derives it through here from the machine id
    in the authenticated request, rather than taking the priority, the task type
    or the wording back from the client. So a caller cannot raise a Critical task
    by editing a Medium draft, and what gets proposed is what AMP would draft for
    that machine at that moment — not what it drafted when the answer was shown.
    """
    from ai import prediction   # lazy: prediction pulls in the engine

    risk = prediction.risk_for_machine(db, row.id) or {}
    score = risk.get("risk_score")
    measured = bool(risk.get("has_recorded_input"))
    reasons = list(risk.get("reasons") or [])
    open_tasks = _open_task_count(db, tenant, row.id)
    # The score decides the priority whether or not the HISTORY rules had
    # anything to read. Three of the engine's rules are current state, not
    # history — in breakdown now (35 points), in maintenance now (15), utilisation
    # under 40% (20) — so a machine nobody has logged a row against can still be
    # visibly broken this minute. Forcing that machine to Medium, as this first
    # did, would take a plant's most urgent job and rank it lowest because its
    # thirty-day file was empty. What the empty history changes is what the score
    # MEANS, and that is said in the fact's detail and in the state below, which
    # is how ADR-0027 already handles it on /machine-health.
    priority = _priority(score)

    facts = [
        _fact("machine.name", "Machine", row.name, M, source="machines", window="now"),
        _fact("machine.status", "Status", row.status or "unknown", M, source="machines", window="now"),
        _fact("action.priority", "Proposed priority", priority, R, window="now",
              detail=PRIORITY_RULE),
        _fact("machine.open_maintenance", "Open maintenance tasks", open_tasks, M, "tasks",
              "maintenance_tasks", "now"),
        # The score is always stated, and always says what it could read. The
        # honesty rule (ADR-0027) is about MEANING, not about hiding the figure:
        # /machine-health publishes the same score with the same caveat, and a
        # draft that showed UNKNOWN where the machine page shows a number would
        # be a second answer to one question.
        _fact("machine.risk", "Risk score", score if score is not None else 0, R, "/100",
              "rule-based risk points (predictive_engine)", "last 30 days",
              detail=("hand-weighted thresholds, not machine learning" if measured else
                      "hand-weighted thresholds, not machine learning; no downtime, production or "
                      "breakdown was recorded for this machine in the risk window, so only the "
                      "current-state rules (status, utilisation) could score it")),
    ]
    if measured:
        facts.append(_fact("machine.downtime_minutes", "Downtime in the risk window",
                           risk.get("downtime_minutes") or 0, M, "minutes", "downtime_logs",
                           "last 30 days"))
    for i, reason in enumerate(reasons[:3]):
        facts.append(_fact(f"machine.risk_reason.{i}", "Risk reason", reason, R,
                           source="predictive_engine", window="last 30 days"))

    if measured:
        state = ev.OK
        basis = (f"{row.name} is at risk {score} out of 100"
                 + (f" ({reasons[0]})" if reasons else "") + ".")
    else:
        # Not a refusal to draft: a person may always want a job raised. But the
        # thirty-day file is empty, so the reader is told the score comes only
        # from what the machine says about itself right now.
        state = ev.INSUFFICIENT_HISTORY
        basis = (f"{row.name} is at risk {score} out of 100 from its current state alone"
                 + (f" ({reasons[0]})" if reasons else "")
                 + "; nothing was recorded for it in the risk window.")

    already = (f" There {'is' if open_tasks == 1 else 'are'} already {open_tasks} open "
               f"maintenance task{'' if open_tasks == 1 else 's'} on this machine."
               if open_tasks else "")
    summary = (f"Ready to propose a {priority} maintenance task for {row.name}. {basis}{already} "
               f"Nothing has been created: raising it puts it in the approval queue, and it "
               f"only takes effect once somebody approves it.")

    return ev.ToolResult(
        tool="draft_maintenance_task", state=state, summary=summary, facts=facts, view="inbox",
        notes=["AMP has not created anything. This is a draft for you to raise and approve."],
        action={
            "kind": "maintenance_task",
            "machine_id": row.id,
            "machine": row.name,
            "task_type": COPILOT_TASK_TYPE,
            "priority": priority,
            "summary": f"Open a {priority} maintenance task for {row.name}",
            "reason": basis,
            "label": f"Propose a {priority} maintenance task for {row.name}",
        },
    )
