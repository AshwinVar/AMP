"""Mutation harness for the approval gate (ADR-0015).

A gate is the one kind of code whose tests can pass for the wrong reason. Every
assertion in test_approval_gate.py has the form "this was refused" — and a
refusal is equally produced by the guard under test, by a different guard, or by
a broken fixture. Removing each guard one at a time is the only way to tell
which one is actually holding.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_approval_gate.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_approval_gate.py", "test_agents.py", "test_agent_decide.py"]

MUTATIONS = [
    # --- who is asking -----------------------------------------------------
    ("the actor is never looked up (the token is believed)", "approvals.py",
     "    user = db.query(models.User).filter(models.User.username == username).first()",
     "    user = models.User(username=username, role=(actor or {}).get('role'),\n"
     "                       tenant_code=action.tenant_code, is_active=True)"),
    ("a deleted user is accepted", "approvals.py",
     '    if user is None:\n        raise ApprovalDenied(401, "This account no longer exists")',
     '    if user is None:\n        pass'),
    ("a disabled account may still approve", "approvals.py",
     '    if not user.is_active:',
     '    if False:'),
    # `if not user.is_active` vs `if user.is_active is False` is NOT a
    # behavioural difference today, because the column cannot hold NULL. That
    # makes the column definition the guard, so the column definition is what
    # gets mutated: make it nullable and an unknown is_active becomes reachable.
    ("users.is_active becomes nullable (an unknown active state)", "models.py",
     "    is_active = Column(Boolean, nullable=False, default=True, server_default=sa_true())",
     "    is_active = Column(Boolean, default=True)"),
    ("the role is read from the TOKEN, not the database", "approvals.py",
     "    if user.role not in APPROVER_ROLES:",
     "    if (actor or {}).get('role') not in APPROVER_ROLES:"),
    ("the role check is dropped (any role approves)", "approvals.py",
     "    if user.role not in APPROVER_ROLES:",
     "    if False:"),
    ("Operator is added to the approving roles", "approvals.py",
     'APPROVER_ROLES = ("Admin", "Supervisor")',
     'APPROVER_ROLES = ("Admin", "Supervisor", "Operator")'),
    ("the actor's own tenant is not compared to the action's", "approvals.py",
     '    if (user.tenant_code or "") != action.tenant_code:',
     '    if False:'),

    # --- which action, and in what state ------------------------------------
    ("the tenant check is dropped", "approvals.py",
     "    if not tenant or action.tenant_code != tenant:",
     "    if False:"),
    ("the tenant check runs AFTER state and freshness (leaks the state)",
     "approvals.py",
     "        _check_tenant(action, (actor or {}).get(\"tenant\"))\n"
     "    _check_state(action)\n"
     "    _check_freshness(action, now)",
     "        pass\n"
     "    _check_state(action)\n"
     "    _check_freshness(action, now)\n"
     "    if require_actor:\n"
     "        _check_tenant(action, (actor or {}).get(\"tenant\"))"),
    ("a cross-tenant probe is told the action exists (403 not 404)",
     "approvals.py",
     '        # 404 rather than 403: a cross-tenant probe must not learn that the id\n'
     '        # exists. Matches the route\'s existing behaviour.\n'
     '        raise ApprovalDenied(404, "Agent action not found")',
     '        raise ApprovalDenied(403, "Not your agent action")'),
    ("an already-decided action may be decided again", "approvals.py",
     "    if status not in DECIDABLE:",
     "    if False:"),
    ("Approved and Rejected become decidable states", "approvals.py",
     'DECIDABLE = ("Proposed",)',
     'DECIDABLE = ("Proposed", "Approved", "Rejected")'),
    ("a non-dict actor crashes instead of refusing", "approvals.py",
     "        if actor is not None and not isinstance(actor, dict):\n"
     '            raise ApprovalDenied(401, "Not authenticated")',
     "        if False:\n"
     '            raise ApprovalDenied(401, "Not authenticated")'),
    ("a misspelled decision is let through (and silently rejects)",
     "approvals.py",
     '    if decision not in ("approve", "reject"):\n'
     '        raise ApprovalDenied(400, f"Unknown decision {decision!r}")',
     '    if decision not in ("approve", "reject"):\n'
     '        pass'),

    # --- how old is too old -------------------------------------------------
    ("the freshness check is dropped", "approvals.py",
     "    if is_expired(action, now):",
     "    if False:"),
    ("a missing expires_at means 'never expires'", "approvals.py",
     "    return now >= action.created_at + timedelta(days=DEFAULT_TTL_DAYS)",
     "    return False"),
    ("an unmeasurable age fails OPEN instead of closed", "approvals.py",
     "        # No creation time either: treat as expired rather than eternally\n"
     "        # actionable. Fail closed on an unmeasurable age.\n"
     "        return True",
     "        return False"),
    ("expiry at exactly the deadline is still actionable", "approvals.py",
     "        return now >= action.expires_at",
     "        return now > action.expires_at"),
    ("the default TTL becomes ten years", "approvals.py",
     "DEFAULT_TTL_DAYS = 7", "DEFAULT_TTL_DAYS = 3650"),

    # --- housekeeping -------------------------------------------------------
    ("housekeeping sweeps fresh proposals too", "approvals.py",
     "        models.AgentAction.status == \"Proposed\").all()\n"
     "        if is_expired(a, now)]",
     "        models.AgentAction.status == \"Proposed\").all()]"),
    ("housekeeping sweeps every tenant's queue", "approvals.py",
     "        models.AgentAction.tenant_code == tenant,\n",
     ""),
    ("housekeeping rewrites decisions a human already made", "approvals.py",
     '        models.AgentAction.status == "Proposed").all()',
     '        models.AgentAction.id > 0).all()'),

    # --- the gate is reachable from every caller ----------------------------
    ("apply_decision stops calling the gate", "ai/agents.py",
     "    approvals.authorise(db, action, actor, decision, require_actor=require_actor)",
     "    pass"),
    ("apply_decision defaults to not requiring an actor", "ai/agents.py",
     "def apply_decision(db, action, decision, decided_by=None, actor=None,\n"
     "                   require_actor=True) -> None:",
     "def apply_decision(db, action, decision, decided_by=None, actor=None,\n"
     "                   require_actor=False) -> None:"),
    ("the route stops passing the actor through", "agent_routes.py",
     "        actor={**current_user, \"tenant\": tenant})",
     "        actor=None, require_actor=False)"),
]


def run_suites():
    failed = []
    for suite in SUITES:
        proc = subprocess.run([sys.executable, suite], capture_output=True,
                              text=True, errors="replace",
                              cwd=os.path.dirname(os.path.abspath(__file__)))
        if proc.returncode != 0:
            failed.append(suite)
    return failed


# A mutation listed here is one whose behaviour a DIFFERENT guard already
# covers. Each needs a reason that survives reading, not an excuse.
EXPECTED_SURVIVORS = {}


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    originals = {}
    for _, path, _, _ in MUTATIONS:
        if path not in originals:
            originals[path] = io.open(os.path.join(here, path),
                                      encoding="utf-8").read()

    baseline = run_suites()
    if baseline:
        print(f"ABORT: suites already failing before any mutation: {baseline}")
        return 2
    print(f"baseline: all {len(SUITES)} suites green\n")
    print(f"{'mutation':<62} {'verdict':<10} caught by")
    print("-" * 104)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<62} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
            survived.append(f"{label} (pattern did not apply)")
            continue
        io.open(os.path.join(here, path), "w", encoding="utf-8",
                newline="\n").write(source.replace(old, new, 1))
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8",
                    newline="\n").write(source)
        if failing:
            verdict, note = "caught", ", ".join(
                s.replace("test_", "").replace(".py", "")[:20] for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label]
        else:
            verdict, note = "SURVIVED", "-- nothing --"
        print(f"{label:<62} {verdict:<10} {note}")
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
