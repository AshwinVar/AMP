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

SUITES = ["test_approval_gate.py", "test_agents.py", "test_agent_decide.py",
          "test_agent_item_lock.py", "test_agent_item_lock_guard.py"]

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
    # Anchored on the comment above User.is_active: the bare column line also
    # appears on two OEM models, and a pattern that hits 3x SKIPs.
    ("users.is_active becomes nullable (an unknown active state)", "models.py",
     "    # rejecting an agent action, which moves money and material.\n"
     "    is_active = Column(Boolean, nullable=False, default=True, server_default=sa_true())",
     "    # rejecting an agent action, which moves money and material.\n"
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
     "    _check_state(action)\n",
     "        pass\n"
     "    _check_state(action)\n"
     "    _check_freshness(action, now)\n"
     "    if require_actor:\n"
     "        _check_tenant(action, (actor or {}).get(\"tenant\"))\n"),
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
     "            actor={**current_user, \"tenant\": tenant})",
     "            actor=None, require_actor=False)"),

    # --- no decision without its item (ADR-0015 addendum) -------------------
    ("the item check is removed (a decision on a moved item is recorded)",
     "approvals.py",
     "    _check_item(db, action)\n    return user",
     "    return user"),
    ("the item check runs BEFORE the actor (a non-approver withdraws)",
     "approvals.py",
     "    user = _check_actor(db, action, actor) if require_actor else None\n"
     "    _check_item(db, action)\n",
     "    _check_item(db, action)\n"
     "    user = _check_actor(db, action, actor) if require_actor else None\n"),
    ("the item may be in any status (pending test dropped)", "approvals.py",
     "    if item.status != pending:",
     "    if False:"),
    ("the item may belong to another tenant", "approvals.py",
     "    query = db.query(model).filter(model.id == action.ref_id,\n"
     "                                   model.tenant_code == action.tenant_code)",
     "    query = db.query(model).filter(model.id == action.ref_id)"),
    ("reject is refused when expired (reject exemption lost)", "approvals.py",
     '    if decision == "approve":\n        _check_freshness(action, now)',
     '    if True:\n        _check_freshness(action, now)'),
    ("freshness gates reject instead of approve", "approvals.py",
     '    if decision == "approve":\n        _check_freshness(action, now)',
     '    if decision == "reject":\n        _check_freshness(action, now)'),
    ("withdraw loses its compare-and-set (overwrites a decision)", "approvals.py",
     "        models.AgentAction.status.in_(DECIDABLE),\n    ).update(",
     "    ).update("),
    ("the route records nothing instead of withdrawing", "agent_routes.py",
     "        withdrawn = approvals.withdraw(db, action_id, tenant)",
     "        withdrawn = 1"),
    ("the orphan sweep withdraws live proposals too", "approvals.py",
     "        if locate_pending_item(db, proposal)[0] is None:",
     "        if True:"),
    ("the orphan sweep ignores its tenant scope", "approvals.py",
     "    if tenant is not None:\n        query = query.filter(",
     "    if False:\n        query = query.filter("),

    # --- the lock -------------------------------------------------------------
    ("the lock ignores item status (holds moved rows)", "approvals.py",
     "    candidates = [r for r in rows if r.status == pending and r.id is not None]",
     "    candidates = [r for r in rows if r.id is not None]"),
    ("the lock counts decided actions (holds look-alikes)", "approvals.py",
     "        models.AgentAction.status.in_(DECIDABLE),\n    ).order_by(",
     "    ).order_by("),
    ("the lock ignores the action's kind", "approvals.py",
     "        models.AgentAction.ref_kind == kind,\n",
     ""),
    ("the lock ignores the action's tenant", "approvals.py",
     "                         if p.tenant_code == row.tenant_code), None)",
     "                         if True), None)"),
    ("the lock ignores the licence clause", "approvals.py",
     "        if licensed[row.tenant_code]:",
     "        if True:"),
    ("a missing TenantConfig fails OPEN (nothing held)", "approvals.py",
     "    if config is None:\n        return True",
     "    if config is None:\n        return False"),
    ("the lock is removed from update_purchase_order", "orders_routes.py",
     "    approvals.refuse_if_awaiting_decision(db, po)\n\n    # received_quantity",
     "\n    # received_quantity"),
    ("the lock is removed from delete_maintenance_task", "factory_ops_routes.py",
     "    approvals.refuse_if_awaiting_decision(db, task)\n\n    db.delete(task)",
     "\n    db.delete(task)"),
    ("the purchase-order list loses its awaiting_approval flag", "orders_routes.py",
     "    return approvals.annotate_awaiting_decision(db, models.PurchaseOrder, rows)",
     "    return rows"),
    ("the reorder agent stops stamping its tenant on the PO", "ai/agents.py",
     "        tenant_code=event.tenant_code,   # explicit: see _propose_task\n",
     ""),
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
    # Read and write byte-faithfully (newline=""). The files are CRLF in a
    # Windows checkout; reading with universal newlines and writing "\n" used to
    # leave every mutated file converted to LF after a run, even though the
    # "restored" check (which compared normalised text) said yes.
    originals = {}
    for _, path, _, _ in MUTATIONS:
        if path not in originals:
            originals[path] = io.open(os.path.join(here, path),
                                      encoding="utf-8", newline="").read()

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
        if "\r\n" in source:
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
        if source.count(old) != 1:
            print(f"{label:<62} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
            survived.append(f"{label} (pattern did not apply)")
            continue
        io.open(os.path.join(here, path), "w", encoding="utf-8",
                newline="").write(source.replace(old, new, 1))
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8",
                    newline="").write(source)
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
             if io.open(os.path.join(here, p), encoding="utf-8", newline="").read() != original]
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
