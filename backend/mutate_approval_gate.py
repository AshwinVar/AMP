"""Mutation harness for the approval gate (ADR-0015).

A gate is the one kind of code whose tests can pass for the wrong reason. Every
assertion in test_approval_gate.py has the form "this was refused" — and a
refusal is equally produced by the guard under test, by a different guard, or by
a broken fixture. Removing each guard one at a time is the only way to tell
which one is actually holding.

SOME MUTATIONS NEED POSTGRESQL. Two approvers deciding the same proposal at
the same instant are kept apart by ``SELECT ... FOR UPDATE`` on the item row,
and SQLite has no row locks: the race that proves the lock
(test_agent_item_lock.py section 11) only runs on PostgreSQL. So a mutation that
removes just the lock cannot be caught on SQLite; it is listed in PG_ONLY and
reported as ``pg-only`` there, never as caught. ``--postgresql`` runs the
PG_ONLY mutations and the other row-lock mutations; against PostgreSQL every one
must be caught (verify_pg_approvals.py runs it there, on a local scratch
database).

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_approval_gate.py
     DATABASE_URL=<local scratch postgresql> python backend/mutate_approval_gate.py --postgresql
"""
import io
import os
import subprocess
import sys

SUITES = ["test_approval_gate.py", "test_agents.py", "test_agent_decide.py",
          "test_agent_item_lock.py", "test_agent_item_lock_guard.py", "test_insights.py"]

# The --postgresql mutations touch approvals._check_item / locate_pending_item
# and the Approvals list's paging, all exercised by test_agent_item_lock.py.
# Running fewer suites can only turn a catch into a survivor, never the reverse.
POSTGRESQL_SUITES = ["test_agent_item_lock.py"]

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
     "        withdrawn = approvals.withdraw(db, action_id, tenant, by=approver,\n"
     "                                       reason=refusal.detail)",
     "        withdrawn = 1"),
    ("the orphan sweep withdraws live proposals too", "approvals.py",
     "        if item is None:\n            withdrawn += withdraw(",
     "        if True:\n            withdrawn += withdraw("),
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
     "                         if p.tenant_code == row.tenant_code\n",
     "                         if True\n"),
    ("the lock ignores the licence clause", "approvals.py",
     "        if licensed[row.tenant_code]:",
     "        if True:"),
    ("a missing TenantConfig fails OPEN (nothing held)", "approvals.py",
     "    if config is None:\n        return True",
     "    if config is None:\n        return False"),
    ("the lock is removed from update_purchase_order", "orders_routes.py",
     "    approvals.refuse_if_awaiting_decision(db, po, current_user)\n"
     "    # ...and only the agent puts one back into \"Draft\".\n",
     "    # ...and only the agent puts one back into \"Draft\".\n"),
    ("the lock is removed from delete_maintenance_task", "factory_ops_routes.py",
     "    approvals.refuse_if_awaiting_decision(db, task, current_user)\n\n    db.delete(task)",
     "\n    db.delete(task)"),

    # --- a plan change cannot re-arm a proposal a human changed (round 2) ----
    ("an edit on a plan without Approvals leaves the proposal live (re-armable)",
     "approvals.py",
     "        if rewritten is not None:",
     "        if False:"),
    ("the edit's withdrawal commits on its own, outside the edit's transaction",
     "approvals.py",
     "                             \"no longer be the agent's.\"))\n        return\n",
     "                             \"no longer be the agent's.\"))\n        db.commit()\n        return\n"),
    ("a held item's write withdraws its proposal instead of refusing (409 lost)",
     "approvals.py",
     "    proposal = held.get(item.id)\n    if proposal is None:\n"
     "        rewritten = unlicensed.get(item.id)",
     "    proposal = None\n    if proposal is None:\n"
     "        rewritten = held.get(item.id) or unlicensed.get(item.id)"),
    ("the edit's withdrawal is recorded against nobody", "approvals.py",
     "            withdraw(db, rewritten.id, rewritten.tenant_code, by=actor_name(actor),",
     "            withdraw(db, rewritten.id, rewritten.tenant_code, by=None,"),

    # --- every withdrawal records who caused it (round 2) ---------------------
    ("the route's withdrawal is recorded against nobody", "agent_routes.py",
     "        withdrawn = approvals.withdraw(db, action_id, tenant, by=approver,",
     "        withdrawn = approvals.withdraw(db, action_id, tenant, by=None,"),
    ("a withdrawal writes no audit row", "approvals.py",
     "    if changed:\n        import platform_routes",
     "    if False:\n        import platform_routes"),
    ("a compare-and-set that lost still writes an audit row", "approvals.py",
     "    if changed:\n        import platform_routes",
     "    if True:\n        import platform_routes"),
    ("a withdrawal's audit row is left without its tenant", "approvals.py",
     "                                  reason, tenant_code=tenant_code)",
     "                                  reason)"),

    # --- a proposal names only a row written no later than itself (round 2) --
    ("the lock lets a stale proposal hold a newer row with its item's id", "approvals.py",
     "                         and _written_before(row, p)), None)",
     "                         ), None)"),
    ("the gate decides a newer row that reuses the item's id", "approvals.py",
     "    if not _written_before(item, action):",
     "    if False:"),
    ("an unknown creation time counts as written before the proposal", "approvals.py",
     "    return (item.created_at is not None and proposal.created_at is not None\n"
     "            and item.created_at <= proposal.created_at)",
     "    return (item.created_at is None or proposal.created_at is None\n"
     "            or item.created_at <= proposal.created_at)"),
    ("a row written at the proposal's own instant is not the proposal's", "approvals.py",
     "            and item.created_at <= proposal.created_at)",
     "            and item.created_at < proposal.created_at)"),

    # --- Mission Control says whether a proposal can still be approved --------
    ("a Mission Control action never reports expired", "ai/insights.py",
     "        expired=approvals.is_expired(a, now),",
     "        expired=False,"),
    ("the purchase-order list loses its awaiting_approval flag", "orders_routes.py",
     "    return approvals.annotate_awaiting_decision(db, models.PurchaseOrder, rows)",
     "    return rows"),
    ("the reorder agent stops stamping its tenant on the PO", "ai/agents.py",
     "        tenant_code=event.tenant_code,   # explicit: see _propose_task\n",
     ""),

    # --- nobody moves an item into its pending status by hand -----------------
    ("the pending-status rule is dropped", "approvals.py",
     "    if new_status == pending and current_status != pending:",
     "    if False:"),
    ("the pending-status rule refuses a row re-sending its own status", "approvals.py",
     "    if new_status == pending and current_status != pending:",
     "    if new_status == pending:"),
    ("a PATCH may move a task back into Proposed", "factory_ops_routes.py",
     "    approvals.refuse_manual_pending_status(models.MaintenanceTask, payload.status, task.status)\n",
     ""),
    ("a PATCH may move a PO back into Draft", "orders_routes.py",
     "    approvals.refuse_manual_pending_status(models.PurchaseOrder, payload.status, po.status)\n",
     ""),
    ("a PATCH may move an escalation back into Proposed", "factory_ops_routes.py",
     "    approvals.refuse_manual_pending_status(models.Escalation, payload.status, escalation.status)\n",
     ""),
    ("a POST may create a task straight into Proposed", "factory_ops_routes.py",
     "    approvals.refuse_manual_pending_status(models.MaintenanceTask, task.status)\n",
     ""),
    ("a POST may create a PO straight into Draft", "orders_routes.py",
     "    approvals.refuse_manual_pending_status(models.PurchaseOrder, po.status)\n",
     ""),
    ("a POST may create an escalation straight into Proposed", "factory_ops_routes.py",
     "    approvals.refuse_manual_pending_status(models.Escalation, escalation.status)\n",
     ""),

    # --- every proposal is reachable from Approvals ---------------------------
    ("the Approvals list ignores its offset (no way past page one)", "agent_routes.py",
     "            .offset(offset).limit(limit).all())",
     "            .limit(limit).all())"),
    ("the Approvals list page is uncapped", "agent_routes.py",
     "    limit = max(1, min(limit, AGENT_ACTIONS_PAGE))",
     "    limit = max(1, limit)"),
    ("a negative offset reaches the database", "agent_routes.py",
     "    offset = max(0, offset)",
     "    offset = offset"),
    ("rows sharing a timestamp have no stable order to page by", "agent_routes.py",
     "    rows = (q.order_by(models.AgentAction.created_at.desc(), models.AgentAction.id.desc())",
     "    rows = (q.order_by(models.AgentAction.created_at.desc())"),
    ("an agent action never reports expired", "agent_routes.py",
     '        "expired": a.status in approvals.DECIDABLE and approvals.is_expired(a, now),',
     '        "expired": False,'),
    ("a decided action reports expired", "agent_routes.py",
     '        "expired": a.status in approvals.DECIDABLE and approvals.is_expired(a, now),',
     '        "expired": approvals.is_expired(a, now),'),
    ("approving an expired proposal sends the approver back to the agent", "approvals.py",
     '            "This proposal has expired and can no longer be approved: the "\n'
     '            "conditions it was based on may have changed. It can only be "\n'
     '            "rejected, which releases the item it holds.")',
     '            "This proposal has expired and can no longer be actioned. The "\n'
     '            "conditions it was based on may have changed; ask the agent to "\n'
     '            "re-evaluate.")'),

    # --- the item row lock: two decisions at once -----------------------------
    ("the item check reads without the lock (lock=True -> lock=False)", "approvals.py",
     "    item, reason = locate_pending_item(db, action, lock=True)",
     "    item, reason = locate_pending_item(db, action, lock=False)"),
    ("the item row is not locked (FOR UPDATE dropped, re-read kept)", "approvals.py",
     "        query = query.populate_existing().with_for_update()",
     "        query = query.populate_existing()"),
    ("the lock trusts the copy a session already holds (no re-read)", "approvals.py",
     "        query = query.populate_existing().with_for_update()",
     "        query = query.with_for_update()"),
]

# Mutations SQLite cannot judge. On SQLite a survivor here is reported as
# `pg-only`, not SURVIVED; on PostgreSQL it must be caught like any other.
PG_ONLY = {
    "the item row is not locked (FOR UPDATE dropped, re-read kept)":
        "SQLite has no row locks; the PostgreSQL race (verify_pg_approvals.py) catches it",
    "a negative offset reaches the database":
        "SQLite reads a negative OFFSET as 0; PostgreSQL refuses it (verify_pg_approvals.py)",
}
# What --postgresql runs: every PG_ONLY mutation plus the rest of the row lock.
POSTGRESQL_RUN = set(PG_ONLY) | {
    "the item check reads without the lock (lock=True -> lock=False)",
    "the lock trusts the copy a session already holds (no re-read)",
}


def on_postgresql():
    return os.environ.get("DATABASE_URL", "").startswith("postgresql")


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


def main(argv=None):
    global SUITES
    argv = sys.argv[1:] if argv is None else argv
    here = os.path.dirname(os.path.abspath(__file__))
    mutations = MUTATIONS
    if "--postgresql" in argv:
        if not on_postgresql():
            print("ABORT: --postgresql needs DATABASE_URL on a local scratch PostgreSQL")
            return 2
        mutations = [m for m in MUTATIONS if m[0] in POSTGRESQL_RUN]
        SUITES = POSTGRESQL_SUITES
        if len(mutations) != len(POSTGRESQL_RUN):
            print(f"ABORT: POSTGRESQL_RUN names {len(POSTGRESQL_RUN)} mutations, "
                  f"{len(mutations)} exist")
            return 2
    print(f"engine: {'PostgreSQL' if on_postgresql() else 'SQLite'}; "
          f"{len(mutations)} mutations; suites: {', '.join(SUITES)}")
    # Read and write byte-faithfully (newline=""). The files are CRLF in a
    # Windows checkout; reading with universal newlines and writing "\n" used to
    # leave every mutated file converted to LF after a run, even though the
    # "restored" check (which compared normalised text) said yes.
    originals = {}
    for _, path, _, _ in mutations:
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
    pg_only = 0
    for label, path, old, new in mutations:
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
        elif label in PG_ONLY and not on_postgresql():
            verdict, note = "pg-only", PG_ONLY[label]
            pg_only += 1
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
    if pg_only:
        print(f"{len(mutations) - pg_only} of {len(mutations)} mutations caught; {pg_only} "
              "pg-only (run --postgresql against PostgreSQL: verify_pg_approvals.py)")
        return 0
    print(f"all {len(mutations)} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
