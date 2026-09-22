# ADR-0039: The Copilot proposes an action — and a model is on none of the write path

**Status:** accepted · **Date:** 2026-09-22 · **Extends** [ADR-0022](0022-copilot-typed-tools-evidence-and-grounding.md) (typed, authorized tools), [ADR-0005](0005-agent-oversight.md) (the approval gate), [ADR-0029](0029-closed-loop-outcomes.md) (the outcome baseline), [ADR-0035](0035-copilot-follow-ups.md) (follow-up threads)

---

## Context

The Copilot could answer every question an owner asked about the plant and
could do nothing about any of it. Measured on master at `9e3f122`, all 26
registered tools were reads — every name a `get_`, `find_` or `explain_`. The
maintenance-task write existed only as a REST route
(`factory_ops_routes.create_maintenance_task`) and the agent-proposal machinery
only in `ai/agents.py`; neither was reachable from a conversation.

The failure was worse than an absence. "Create a maintenance task" matched no
pillar, fell through `PILLAR_TOOL` to the briefing tool, and came back as a
plant summary. So AMP did not do the thing, and did not say it had not done
the thing. A person who asked for a job to be raised got a paragraph about OEE
and no indication that nothing had happened.

Closing that means opening the one door ADR-0022's chain keeps shut:

    USER -> AUTHENTICATION -> RBAC -> TENANT / OEM / CONSENT -> AMP TOOL -> DATA -> MODEL

The obvious fix — register a tool that writes — puts a language model at the
point where a row comes into existence. A model names tools; anything that can
shape a model's output can then cause a write, and text inside the plant's own
data is exactly such a thing. The rule this repository has held from ADR-0022
is that **the model never decides whether the user is authorized; AMP decides.**
A write tool would quietly weaken it to "the model decides what happens, and
AMP decides whether it was allowed".

## Decision

### 1. A tool may DRAFT an action. No tool writes.

`ai/tools/actions.py` registers `draft_maintenance_task(machine)` — a read like
any other. It resolves the machine inside the bound tenant, reads its
condition, and returns a `ToolResult` carrying an `action`: the task AMP would
propose, with the evidence that justifies it. The draft is inert text in an
answer. Nothing exists in the database when it renders.

`ev.PROPOSABLE_KINDS` is a closed set (`maintenance_task` today) because each
kind must name an `ai.agents` path that can actually carry it out. A draft AMP
cannot execute is a promise it cannot keep.

### 2. Three human-authenticated steps, and a model at none of them

    tool (a read)        -> a draft, in the answer
    POST /agent-actions/propose  -> MaintenanceTask "Proposed" + AgentAction "Proposed"
    POST /agent-actions/{id}/approve -> task "Open", outcome baseline frozen

The write route takes **only** `kind` and `machine_id`. Everything else is
re-derived server-side by `actions.draft_for_machine` from the machine at that
instant — the priority, the task type, the wording. The client's copy of the
draft is never read back, so a Medium draft cannot be returned as a Critical
one, and what gets proposed is what AMP would draft now rather than what it
drafted when the answer was shown.

The tenant is the authenticated request's. The machine is looked up inside it,
so an id from another workspace is simply not found. The route's roles
(Admin, Supervisor) are the tool's roles, so AMP never offers somebody a button
that would refuse them.

### 3. A Copilot proposal can never auto-approve

`agents.should_auto_approve` refuses `agent="copilot"` outright. An action a
person asked for through a conversation is the one kind that must always be
decided by a human, because the request arrived as *text* — the one input to
AMP that anybody who can type into the plant's own data can influence.
`set_agent_policy` already drops any key that is not a roster agent, so no
tenant can trust `copilot` through the UI; this closes the `AUTO_APPROVE_AGENTS`
environment variable too, which is not validated against the roster.

### 4. A request is not a question

`_asks_for_an_action` matches command shapes ("raise a maintenance task on
CNC-01"), deliberately narrow so that the many questions which merely contain
the word *maintenance* keep the answers they have. A request that names no
machine, and cannot take one from the conversation, is answered by saying so —
not by the help text, which lists what AMP can tell you and never mentions that
the task was not created.

A request with no machine in it takes the conversation's machine, where
ADR-0035 requires a question to carry a pronoun first. "Create a maintenance
task." names nothing at all, so the only machine it can mean is the one being
discussed; inferring wrongly is cheap and visible, because AMP says which
machine it drafted for, creates nothing, and a person still has to raise it.

### 5. A refusal cannot carry a draft

`ev.ToolResult` refuses to be constructed with both a refusal state and an
action. A result that says "you may not see this" and offers a button is the
one shape that could turn a refusal into an action, so it cannot be built —
an invariant where the type is, rather than a filter downstream that no test
could ever make fire.

## Consequences

**Positive.** A conversation now ends somewhere. The owner's four-turn journey
— *how is my factory doing* → *why are we behind* → *what happened to M4* →
*create a maintenance task* — completes, and the last turn produces a real
queue entry rather than a paragraph about OEE. The request travels the path the
five agents already use, so it inherits the approval gate, the audit record,
the held-item lock and the frozen outcome baseline without any of them being
rebuilt. `ADR-0022`'s rule is intact: no tool writes, and authorization is still
decided before a handler runs.

**Negative.** Two human steps where one might feel natural: a person raises the
draft, and a person approves it. That is deliberate — proposing and approving
are different decisions, and the second is where the gate lives — but somebody
who raises their own proposal and then approves it will notice the second
click. The routing is keyword-shaped, so a command phrased in a way
`_ACTION_PHRASES` does not cover still reaches a read; that failure is now
loud (the answer says nothing was created) rather than silent. And a person who
asks for something AMP cannot propose is told what AMP *can* do rather than
having the request quietly reinterpreted.

## Alternatives considered

**Register a write tool.** Rejected. It puts a language model at the point a
row is created, and the model's input includes text from the plant's own data.

**Let the copilot create the task directly and skip the queue.** Rejected. The
value of the AgentAction path is that the item is *held* until decided; writing
straight to Open would create the one thing ADR-0005 exists to prevent, an
action nobody approved.

**Have the client send the whole draft back.** Rejected, and pinned by a test:
the priority is the part a caller would most want to edit, so the route
re-derives it from the machine and ignores what it was handed.

## Honest limits

Nothing here creates a purchase order, an escalation, a work order or a
message. `PROPOSABLE_KINDS` has one entry. The Copilot still cannot act
unattended, cannot approve its own proposal, and cannot raise a second
proposal on a machine whose first is still undecided.

## Evidence

`test_copilot_actions.py` — fourteen sections: the draft writes nothing, a
follow-up inherits the conversation's machine, a request with no machine says
so, questions that mention maintenance keep their answers, an Operator is
refused, a forged thread naming another factory's machine yields nothing and
echoes nothing, the route writes exactly one Proposed task and one Proposed
action, a duplicate is refused, a payload cannot choose the workspace, a
refused draft is never raised, the client's priority is discarded, approval
runs the existing gate and freezes the outcome baseline, and the type refuses a
draft AMP could not carry out.

`mutate_copilot_actions.py` — 23 mutations across the tool, the agents module,
the write route, the router and the evidence type; all 23 caught, with no
`EXPECTED_SURVIVORS` entry. Its first run left seven survivors, and every one
was a real gap rather than noise: the unmeasured-machine branch was untested (no
fixture machine lacked history), a tenant smuggled in the payload was untested,
the route's check on a refused draft was unreachable, the type's own invariants
were untested, and `COPILOT_AGENT` turned out to be defined twice in
`ai/agents.py`. One survivor was genuinely redundant code — a `not r.refused`
filter in the orchestrator that the new type invariant makes unreachable — and
was deleted rather than kept with an excuse.

The most valuable find was the second run's. A mutation removing the gate on
`priority` failed nothing, which sent me back to `predictive_engine`: three of
its eleven rules read CURRENT STATE, not history — in breakdown now (35 points),
in maintenance now (15), utilisation under 40% (20). The first version of this
tool had forced every machine with an empty thirty-day file to Medium and
published its risk as UNKNOWN, which would have ranked a machine that is broken
*right now* below every machine that had merely been logged, and would have
answered a question differently from the machine page beside it. The rule above
— the empty history changes what the score MEANS, not whether it is shown — is
that fix.

`frontend/components/CopilotProposal.test.tsx` — seven cases, mostly about what
the card says and when: nothing exists before the button is pressed, only
`kind` and `machine_id` are sent, and after raising it says *waiting for
approval — nothing has been carried out yet*. A card that reads like a
confirmation is how somebody comes to believe a job was booked that was not.
