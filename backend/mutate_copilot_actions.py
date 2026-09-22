"""Mutation harness for the Copilot's action path (ADR-0039).

This is the one place a conversation can cause a row to exist, so each mutation
below is a small, plausible edit that would quietly let something happen that
nobody approved: the draft offered to a role that cannot raise it, a machine
resolved outside the caller's tenant, the scope taken from the payload, the
client's own priority written instead of AMP's, the duplicate guard dropped,
the never-auto-approve rule dropped, a refused draft raised anyway, and the
router quietly answering a REQUEST with a read again.

For each one the harness applies the edit, runs the suites that are supposed to
notice, and restores the file byte for byte. A mutation that leaves every suite
green SURVIVED: that guard is untested.

RUN IT ALONE. Like every mutate_* harness it EDITS THE WORKING TREE and restores
it afterwards; anything reading those files meanwhile sees broken code.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_copilot_actions.py
Exit 0 only if every mutation applied exactly once and was caught.
"""
import io
import os
import subprocess
import sys

SUITES = ["test_copilot_actions.py", "test_copilot_tools_no_wider_than_routes.py",
          "test_copilot_follow_ups.py", "test_copilot_orchestrator.py"]
SUITE_TIMEOUT = 600   # a mutation that hangs a suite is a survivor, not a wait

ACT = "ai/tools/actions.py"
AGENTS = "ai/agents.py"
ROUTES = "agent_routes.py"
ORCH = "ai/orchestrator.py"
EVID = "ai/evidence.py"

# (label, file, old, new)
MUTATIONS = [
    # ── who may be OFFERED the draft ────────────────────────────────────────
    ("tool: the draft is offered to every role, including one that cannot raise it", ACT,
     '      roles=("Admin", "Supervisor"),\n',
     '      roles=(),\n'),
    ("tool: the machine is resolved without the caller's tenant", ACT,
     "    row, why = _resolve_machine(db, tenant, machine)\n    if row is None:\n"
     "        return ev.refusal(\"draft_maintenance_task\", ev.NOT_FOUND, why)",
     "    row, why = _resolve_machine(db, tenant, machine)\n    if False:\n"
     "        return ev.refusal(\"draft_maintenance_task\", ev.NOT_FOUND, why)"),

    # ── what the draft may CLAIM ────────────────────────────────────────────
    ("tool: a machine with nothing recorded is given the top priority", ACT,
     '    if score is None:\n        return "Medium"',
     '    if score is None:\n        return "Critical"'),
    ("tool: a score with no history behind it is shown as if the history backed it", ACT,
     '              detail=("hand-weighted thresholds, not machine learning" if measured else',
     '              detail=("hand-weighted thresholds, not machine learning" if True else'),
    ("tool: an empty risk window is reported as a clean one", ACT,
     "        state = ev.INSUFFICIENT_HISTORY",
     "        state = ev.OK"),
    ("tool: the answer no longer says nothing was created", ACT,
     '               f"Nothing has been created: raising it puts it in the approval queue, and it "\n'
     '               f"only takes effect once somebody approves it.")',
     '               f"Done.")'),

    # ── the never-auto-approve rule ─────────────────────────────────────────
    ("agents: a Copilot proposal may auto-approve, so a conversation executes it", AGENTS,
     '    if getattr(action, "agent", None) == COPILOT_AGENT:\n        return False',
     '    if False:\n        return False'),
    ("agents: the copilot's own tasks are filed under a roster agent instead", AGENTS,
     'COPILOT_AGENT = "copilot"',
     'COPILOT_AGENT = "reorder"'),
    ("agents: the proposal takes the client's wording rather than AMP's draft", AGENTS,
     '        machine_id=machine.id, task_type=draft["task_type"], priority=draft["priority"],',
     '        machine_id=machine.id, task_type=draft.get("task_type"), priority="Critical",'),

    # ── the write route ─────────────────────────────────────────────────────
    ("route: any kind at all may be proposed", ROUTES,
     '    if kind not in ev.PROPOSABLE_KINDS:',
     '    if False:'),
    ("route: the machine is looked up across every tenant", ROUTES,
     "    machine = db.query(models.Machine).filter(\n"
     "        models.Machine.id == machine_id, models.Machine.tenant_code == tenant).first()",
     "    machine = db.query(models.Machine).filter(\n"
     "        models.Machine.id == machine_id).first()"),
    ("route: the scope comes from the payload instead of the request", ROUTES,
     "    tenant = request_tenant(current_user)\n    kind = payload.get(\"kind\") if isinstance(payload, dict) else None",
     "    tenant = payload.get(\"tenant\") or request_tenant(current_user)\n"
     "    kind = payload.get(\"kind\") if isinstance(payload, dict) else None"),
    ("route: a machine id of any shape is accepted", ROUTES,
     "    if not isinstance(machine_id, int) or isinstance(machine_id, bool):",
     "    if machine_id is None:"),
    ("route: the same proposal can be raised again and again", ROUTES,
     "    if ai_agents._open_auto_task_exists(db, machine.id, ai_actions.COPILOT_TASK_TYPE):",
     "    if False:"),
    ("route: the client's copy of the draft is written instead of AMP's", ROUTES,
     "    action = ai_agents.propose_requested_task(\n"
     "        db, tenant, machine, draft.action, requested_by=str(current_user.get(\"sub\") or \"\"))",
     "    action = ai_agents.propose_requested_task(\n"
     "        db, tenant, machine, {**draft.action, **payload}, "
     "requested_by=str(current_user.get(\"sub\") or \"\"))"),
    ("route: a refused draft is raised anyway", ROUTES,
     "    if draft.refused or not draft.action:\n        raise HTTPException(status_code=400, detail=draft.summary)",
     "    if False:\n        raise HTTPException(status_code=400, detail=draft.summary)"),
    ("route: Operators may raise a proposal too", ROUTES,
     'def propose_agent_action(payload: dict, db: Session = Depends(_get_db),\n'
     '                         current_user: dict = Depends(require_roles(["Admin", "Supervisor"]))):',
     'def propose_agent_action(payload: dict, db: Session = Depends(_get_db),\n'
     '                         current_user: dict = Depends(require_roles(["Admin", "Supervisor", "Operator"]))):'),

    # ── the router: a request is not a question ─────────────────────────────
    ("orchestrator: a request to act is answered with a read again", ORCH,
     "    if _asks_for_an_action(question):\n        if r.kind == \"machine\":",
     "    if False:\n        if r.kind == \"machine\":"),
    ("orchestrator: a request with no machine gets the help text back", ORCH,
     "    if plan.matched == _ACTION_NEEDS_MACHINE:\n        return _ACTION_NEEDS_MACHINE_TEXT, \"machines\"",
     "    if False:\n        return _ACTION_NEEDS_MACHINE_TEXT, \"machines\""),
    ("orchestrator: every question inherits the conversation's machine, not just a request", ORCH,
     "    if (thread and (wants_action or _refers_to_a_machine(question))\n"
     "            and assistant._machine_named(db, question) is None):",
     "    if thread and assistant._machine_named(db, question) is None:"),

    # ── the type that carries a draft ───────────────────────────────────────
    ("evidence: a tool may draft any kind of action it likes", EVID,
     "            if self.action.get(\"kind\") not in PROPOSABLE_KINDS:",
     "            if False:"),
    ("evidence: a REFUSAL may carry a draft, turning 'you may not' into a button", EVID,
     "            if self.refused:\n"
     "                raise ValueError(f\"{self.tool}: a refusal ({self.state}) cannot draft an action\")",
     "            if False:\n"
     "                raise ValueError(f\"{self.tool}: a refusal ({self.state}) cannot draft an action\")"),
    ("evidence: a drafted action may name its machine by anything at all", EVID,
     "            if not isinstance(self.action.get(\"machine_id\"), int) or "
     "isinstance(self.action.get(\"machine_id\"), bool):",
     "            if False:"),
]


def run_suites():
    failed = []
    here = os.path.dirname(os.path.abspath(__file__))
    for suite in SUITES:
        try:
            proc = subprocess.run([sys.executable, suite], capture_output=True, text=True, errors="replace",
                                  cwd=here, timeout=SUITE_TIMEOUT)
            if proc.returncode != 0:
                failed.append(suite)
        except subprocess.TimeoutExpired:
            failed.append(suite + " (timeout)")
    return failed


# A mutation here is one a DIFFERENT guard already covers. Each needs a reason
# that survives reading.
EXPECTED_SURVIVORS = {}


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    originals = {}
    for _, path, _, _ in MUTATIONS:
        if path not in originals:
            originals[path] = io.open(os.path.join(here, path), encoding="utf-8").read()

    baseline = run_suites()
    if baseline:
        print(f"ABORT: suites already failing before any mutation: {baseline}")
        return 2
    print(f"baseline: all {len(SUITES)} suites green\n")
    print(f"{'mutation':<80} {'verdict':<10} caught by")
    print("-" * 124)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<80} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
            survived.append(f"{label} (pattern did not apply)")
            continue
        io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source.replace(old, new, 1))
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source)
        if failing:
            verdict, note = "caught", ", ".join(s.replace("test_", "").replace(".py", "")[:28] for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label][:70] + "..."
        else:
            verdict, note = "SURVIVED", "-- nothing --"
        print(f"{label:<80} {verdict:<10} {note}", flush=True)
        if verdict == "SURVIVED":
            survived.append(label)

    dirty = [p for p, original in originals.items()
             if io.open(os.path.join(here, p), encoding="utf-8").read() != original]
    print()
    print(f"source files restored: {'yes' if not dirty else 'NO - DIRTY: ' + str(dirty)}")
    if dirty:
        return 3
    if survived:
        print(f"{len(survived)} MUTATION(S) SURVIVED - investigate each:")
        for s in survived:
            print("   *", s)
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
