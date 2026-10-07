"""Interlocks: tooling conditions that fall due on COUNT, and what AMP does about them.

WHAT AN INTERLOCK IS HERE. A plant's tooling carries obligations that arrive on a
counter rather than a calendar: a mould is serviced every 10,000 shots, a die is
measured for quality every 10,000 parts, a punch is scrapped at its rated life.
Nobody on the floor is counting to ten thousand. The count already exists in
AMP — this turns it into the obligation it implies, at the moment it is reached.

AMP FLAGS. IT DOES NOT BLOCK.
=============================
A real interlock stops a machine, and that is a PLC function with safety rating,
wired by people who certify it. AMP reads; it has no control path and nothing in
it is safety-rated, which the product says in writing. So an interlock here
raises the condition through the SAME approval queue every other agent uses: a
Proposed maintenance task a human sees and decides. Calling this an interlock and
leaving a customer believing the press will stop would be the most dangerous
sentence in the product.

WHY A REGISTRY RATHER THAN AN `if` LADDER. Every plant's obligations differ — a
moulder watches shots, a press shop watches strokes to regrind, a foundry watches
pours to reline. Each is the same shape: read a count off a tool, compare it to a
declared limit, and name the work that falls due. So a check is a small function
with a declared task type, and adding one is adding a function rather than
editing a decision tree that every other check also runs through.

WHAT IS DELIBERATELY NOT HERE. No check invents a limit. A tool whose interval is
unset is not "due by default" and not "overdue at some sensible number" — it is
not watched, and `check` returns nothing for it. Guessing 10,000 because that is
a common figure would put AMP's arithmetic behind a number the customer never
agreed to, and the first time it was wrong it would be wrong in writing.
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional

import models

log = logging.getLogger(__name__)

#: Tools in these states accrue nothing and raise nothing. A tool in the tool
#: room is not running, and a scrapped one is not coming back.
LIVE_STATUSES = ("Active",)


@dataclass(frozen=True)
class Due:
    """One interlock that has come due on one tool.

    `reason` is written for the person who will read it on a work card, with the
    numbers in it: "12,418 of 10,000 shots since last service" tells a toolmaker
    what to expect before they walk to the press.
    """

    key: str
    tool_id: int
    tool_no: str
    task_type: str
    priority: str
    summary: str
    reason: str
    machine_id: Optional[int] = None


#: key -> check. A check receives a tool and returns a Due or None.
_CHECKS: "dict[str, Callable[[models.ToolAsset], Optional[Due]]]" = {}


def check_type(key: str):
    """Register an interlock check. The key is what a config and a log line name."""

    def wrap(fn):
        if key in _CHECKS:
            raise ValueError(f"interlock check {key!r} is already registered")
        _CHECKS[key] = fn
        return fn

    return wrap


def registered() -> List[str]:
    return sorted(_CHECKS)


# ── the checks ──────────────────────────────────────────────────────


@check_type("tool_service_due")
def _service_due(tool) -> Optional[Due]:
    """Routine service, every `service_interval_cycles` shots.

    The ordinary case, and the one a moulder means by "check the die every
    10,000". Raised at the interval and not before; `>=` rather than `==`
    because a batch of 500 parts can step straight over the boundary, and an
    equality test would miss it and never fire again.
    """
    interval = tool.service_interval_cycles
    if not interval or interval <= 0:
        return None
    done = tool.cycles_since_service
    if done < interval:
        return None
    return Due(
        key="tool_service_due",
        tool_id=tool.id,
        tool_no=tool.tool_no,
        task_type="Tool service",
        priority="High",
        summary=f"{tool.name} ({tool.tool_no}) is due for service",
        reason=(f"{done:,} cycles since last service against an interval of "
                f"{interval:,}. Fitted to machine {tool.machine_id or 'none'}."),
        machine_id=tool.machine_id,
    )


@check_type("tool_life_exhausted")
def _life_exhausted(tool) -> Optional[Due]:
    """The tool has reached the life it was rated for.

    Separate from service because the action is different in kind: service is
    work on a tool that continues, and life is the decision to retire it. A
    plant that saw only "service due" on a tool at the end of its life would
    keep servicing something that should have been replaced.
    """
    limit = tool.life_limit_cycles
    if not limit or limit <= 0:
        return None
    total = tool.cycles_total
    if total < limit:
        return None
    return Due(
        key="tool_life_exhausted",
        tool_id=tool.id,
        tool_no=tool.tool_no,
        task_type="Tool life review",
        priority="Critical",
        summary=f"{tool.name} ({tool.tool_no}) has reached its rated life",
        reason=(f"{total:,} cycles against a rated life of {limit:,}. "
                f"Review for refurbishment or replacement before further running."),
        machine_id=tool.machine_id,
    )


@check_type("tool_service_overdue")
def _badly_overdue(tool) -> Optional[Due]:
    """Twice the interval and still running.

    Distinct from `tool_service_due` on purpose. The first raises a task; if that
    task sits unapproved while the press keeps running, nothing escalates and the
    condition is indistinguishable from one raised a minute ago. This fires at
    double the interval so a tool that has been ignored for a whole interval says
    so, with a severity a supervisor sorts to the top.
    """
    interval = tool.service_interval_cycles
    if not interval or interval <= 0:
        return None
    done = tool.cycles_since_service
    if done < interval * 2:
        return None
    return Due(
        key="tool_service_overdue",
        tool_id=tool.id,
        tool_no=tool.tool_no,
        task_type="Tool service overdue",
        priority="Critical",
        summary=f"{tool.name} ({tool.tool_no}) is more than an interval overdue",
        reason=(f"{done:,} cycles since last service — more than twice the "
                f"{interval:,} interval. The earlier service task was not actioned."),
        machine_id=tool.machine_id,
    )


# ── the engine ──────────────────────────────────────────────────────


def check(tool) -> List[Due]:
    """Every interlock due on this tool, in registration order.

    A tool that is not live returns nothing whatever its counters say: a mould
    sitting in the tool room is not accruing obligation, and raising service on
    it would put work in front of a toolmaker for a tool nobody is running.
    """
    if tool is None or tool.status not in LIVE_STATUSES:
        return []
    out = []
    for key in registered():
        try:
            due = _CHECKS[key](tool)
        except Exception:                                   # noqa: BLE001
            # One malformed check must not suppress the others — a tool with a
            # bad life limit should still raise its service.
            log.exception("interlock check %s failed on tool %s", key, tool.tool_no)
            continue
        if due is not None:
            out.append(due)
    return out


def fitted_tools(db, machine_id):
    """The live tools fitted to this machine. Empty when the machine has none."""
    if not machine_id:
        return []
    return (db.query(models.ToolAsset)
              .filter(models.ToolAsset.machine_id == machine_id,
                      models.ToolAsset.status.in_(LIVE_STATUSES))
              .all())


def advance(db, machine_id, parts: int) -> List[models.ToolAsset]:
    """Credit `parts` to every live tool fitted to this machine. Returns them.

    NOT CLAMPED TO ONE TOOL. A machine can carry more than one fixture, and the
    parts it made passed through all of them.

    A NEGATIVE OR ABSENT COUNT ADVANCES NOTHING. Production arithmetic upstream
    can produce a zero for a window with no output, and a negative only from a
    counter that went backwards — which the normalizer already refuses. Letting
    either through would walk a tool's service history backwards.
    """
    try:
        made = int(parts)
    except (TypeError, ValueError):
        return []
    if made <= 0:
        return []
    tools = fitted_tools(db, machine_id)
    for tool in tools:
        tool.parts_total = int(tool.parts_total or 0) + made
    if tools:
        db.flush()
    return tools


def record_service(db, tool, at=None):
    """Clear the service interlock: the work was done, the count starts again.

    `parts_at_last_service` is set to the CURRENT total rather than zeroed, so
    lifetime cycles — which drive the life limit — keep counting. Zeroing the
    total here would make a tool immortal: every service would reset its age and
    the life check could never fire.
    """
    tool.parts_at_last_service = int(tool.parts_total or 0)
    tool.last_service_at = at or datetime.utcnow()
    db.flush()
    return tool
