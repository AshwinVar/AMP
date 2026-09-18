"""Structural guard: the approval lock has one home and every writer calls it.

test_agent_item_lock.py proves the six handlers refuse a held item TODAY. This
file makes sure the next handler does too. A new PATCH /purchase-orders/{id}/x,
or a route module that loads a MaintenanceTask and writes to it, passes every
behavioural test ever written -- because nobody wrote one for it -- and quietly
reopens the bypass. So the guard reads the code:

  1. ROUTE INVENTORY. Walk main.app.routes. Every PATCH / PUT / DELETE under
     /maintenance/tasks/, /escalations/ or /purchase-orders/ must call
     approvals.refuse_if_awaiting_decision before it writes. At least the six
     known handlers must be found, by name, or the walk has gone blind. Every
     PATCH / PUT there, and the three create handlers, must also call
     approvals.refuse_manual_pending_status: only an agent puts an item into
     its pending status, or an orphaned proposal is re-armed by hand.
  2. WRITER SCAN. An AST pass over the route modules (*_routes.py and main.py,
     NOT scripts): any function that loads a MaintenanceTask, PurchaseOrder or
     Escalation row and then writes to it (attribute assignment, setattr,
     db.delete) must call the lock on that variable BEFORE its first write --
     order matters, because the predicate reads item.status. A bulk
     query(...).update()/.delete() on those models can never be guarded and is
     always an offender. The models come from approvals.PENDING, so a fourth
     proposable kind extends the guard by itself.
  3. REGISTRY COMPLETENESS. The ref_kind literals ai/agents.py passes to
     _propose( equal approvals.PENDING's keys; each pending status is what
     apply_decision compares against; each is systemOnly in status-vocab.json.
  4. SELF-PROBES. The detector flags synthetic bypasses and passes synthetic
     safe code; removing or moving the real helper call in memory turns the
     guard red.

KNOWN BLIND SPOTS (stated, not hidden): a model class held in a variable
(``wipe(model)``), raw SQL text, and writes routed through a helper function
the route calls. The route inventory still covers the HTTP surface of the
three URL families whatever the helper structure. Scripts (reset_factory,
reseed_inventory) are deliberately not scanned; they have no HTTP path.

Run: DATABASE_URL="sqlite:///./ci.db" python test_agent_item_lock_guard.py
"""
import ast
import glob
import inspect
import json
import os
import sys
import textwrap

from fastapi.routing import APIRoute

import approvals

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = "refuse_if_awaiting_decision"
STATUS_HELPER = "refuse_manual_pending_status"
APPROVABLE = {model.__name__ for model, _ in approvals.PENDING.values()}
ROUTE_FAMILIES = {"maintenance_task": "/maintenance/tasks/",
                  "escalation": "/escalations/",
                  "purchase_order": "/purchase-orders/"}
KNOWN_HANDLERS = {"update_maintenance_task", "delete_maintenance_task",
                  "update_escalation", "delete_escalation",
                  "update_purchase_order", "delete_purchase_order"}
KNOWN_CREATORS = {"create_maintenance_task", "create_escalation", "create_purchase_order"}

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


# ── the detector ────────────────────────────────────────────────────
def _row_models(node):
    """Approvable model names loaded as ROWS inside `node`: query(models.X) or
    .get(models.X, ...). Column/aggregate queries (models.X.id) do not count."""
    found = set()
    for n in ast.walk(node):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in ("query", "get") and n.args):
            arg = n.args[0]
            if (isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name)
                    and arg.value.id == "models" and arg.attr in APPROVABLE):
                found.add(arg.attr)
    return found


def _is_helper_call(n, name=HELPER):
    if not isinstance(n, ast.Call):
        return False
    f = n.func
    return (isinstance(f, ast.Name) and f.id == name) or (
        isinstance(f, ast.Attribute) and f.attr == name)


def _calls(src, name):
    return any(_is_helper_call(n, name) for n in ast.walk(ast.parse(src)))


def scan_function(fn):
    """[(problem, line)] for one function: unguarded writes to approvable rows.
    Also returns whether the function writes to an approvable row at all."""
    bindings = []   # (line, name, is_approvable)
    writes = []     # (line, name)
    guards = []     # (line, name)
    problems = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    bindings.append((n.lineno, t.id, bool(_row_models(n.value))))
                elif isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                    writes.append((n.lineno, t.value.id))
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
            t = n.target
            if isinstance(t, ast.Name) and isinstance(n, ast.AnnAssign) and n.value is not None:
                bindings.append((n.lineno, t.id, bool(_row_models(n.value))))
            elif isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                writes.append((n.lineno, t.value.id))
        elif isinstance(n, ast.For) and isinstance(n.target, ast.Name):
            bindings.append((n.lineno, n.target.id, bool(_row_models(n.iter))))
        elif isinstance(n, ast.Call):
            if _is_helper_call(n):
                for arg in n.args:
                    if isinstance(arg, ast.Name):
                        guards.append((n.lineno, arg.id))
            elif isinstance(n.func, ast.Name) and n.func.id == "setattr" and n.args \
                    and isinstance(n.args[0], ast.Name):
                writes.append((n.lineno, n.args[0].id))
            elif isinstance(n.func, ast.Attribute) and n.func.attr in ("delete", "update"):
                if n.args and isinstance(n.args[0], ast.Name) and n.func.attr == "delete":
                    writes.append((n.lineno, n.args[0].id))
                elif _row_models(n.func.value):
                    problems.append((f"bulk .{n.func.attr}() on {sorted(_row_models(n.func.value))}",
                                     n.lineno))
    bindings.sort()
    is_writer = bool(problems)
    # Attribute each write to the NEAREST preceding binding of its name.
    first_write = {}
    for line, name in sorted(writes):
        prior = [b for b in bindings if b[1] == name and b[0] < line]
        if not prior or not prior[-1][2]:
            continue
        key = (prior[-1][0], name)
        first_write.setdefault(key, line)
    for (bind_line, name), write_line in first_write.items():
        is_writer = True
        if not any(g_name == name and bind_line < g_line < write_line for g_line, g_name in guards):
            problems.append((f"writes {name!r} (bound line {bind_line}) without calling "
                             f"{HELPER}({name}) first", write_line))
    return is_writer, problems


def scan_source(source, filename="<src>"):
    """{function_name: (is_writer, problems)} for every function in `source`."""
    out = {}
    for n in ast.walk(ast.parse(source, filename)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = scan_function(n)
    return out


def route_modules():
    return sorted(glob.glob(os.path.join(HERE, "*_routes.py"))) + [os.path.join(HERE, "main.py")]


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# =====================================================================
def test_route_inventory():
    print("=" * 74)
    print("1. EVERY ITEM-ADDRESSED WRITE ROUTE CALLS THE LOCK")
    print("=" * 74)
    import main
    check("every proposable kind has a URL family to inventory",
          set(ROUTE_FAMILIES) == set(approvals.PENDING), str(set(approvals.PENDING)))
    found = {}
    creators = set()
    roots = {path.rstrip("/") for path in ROUTE_FAMILIES.values()}
    for route in main.app.routes:
        if not isinstance(route, APIRoute):
            continue
        if "POST" in route.methods and route.path in roots:
            fn = route.endpoint
            creators.add(fn.__name__)
            check(f"POST {route.path} ({fn.__name__}) calls {STATUS_HELPER}",
                  _calls(textwrap.dedent(inspect.getsource(fn)), STATUS_HELPER), "no call")
            continue
        if not route.methods & {"PATCH", "PUT", "DELETE"}:
            continue
        if not route.path.startswith(tuple(ROUTE_FAMILIES.values())):
            continue
        fn = route.endpoint
        src = textwrap.dedent(inspect.getsource(fn))
        is_writer, problems = scan_source(src)[fn.__name__]
        calls_helper = any(_is_helper_call(n) for n in ast.walk(ast.parse(src)))
        found[fn.__name__] = (sorted(route.methods), route.path)
        check(f"{sorted(route.methods)} {route.path} ({fn.__name__}) calls {HELPER} before writing",
              calls_helper and not problems, str(problems) or "no call at all")
        if route.methods & {"PATCH", "PUT"}:
            check(f"{sorted(route.methods)} {route.path} ({fn.__name__}) calls {STATUS_HELPER}",
                  _calls(src, STATUS_HELPER), "no call")
    check(f"the inventory is not blind: found {len(found)} routes (>= 6)", len(found) >= 6,
          str(found))
    check("...including all six known handlers by name", KNOWN_HANDLERS <= set(found),
          str(KNOWN_HANDLERS - set(found)))
    check("...and all three create handlers by name", KNOWN_CREATORS <= creators,
          str(KNOWN_CREATORS - creators))


def test_writer_scan():
    print()
    print("=" * 74)
    print("2. NO ROUTE MODULE WRITES A PROPOSABLE ITEM WITHOUT THE LOCK")
    print("=" * 74)
    writers, offenders = set(), []
    for path in route_modules():
        for name, (is_writer, problems) in scan_source(_read(path), path).items():
            if is_writer:
                writers.add(name)
            for problem, line in problems:
                offenders.append(f"{os.path.basename(path)}:{line} {name}: {problem}")
    check("the scan is not vacuous: it detects all six known writers",
          KNOWN_HANDLERS <= writers, str(KNOWN_HANDLERS - writers))
    check("no route function writes a proposable item without the lock first",
          not offenders, "; ".join(offenders))
    check("no script is scanned (reseed_inventory / reset_factory are not route modules)",
          not any(os.path.basename(p) in ("reseed_inventory.py", "reset_factory.py")
                  for p in route_modules()))


def test_registry_is_complete():
    print()
    print("=" * 74)
    print("3. THE PENDING TABLE MATCHES WHAT AGENTS PROPOSE AND WHAT THE GATE MOVES")
    print("=" * 74)
    agents_src = _read(os.path.join(HERE, "ai", "agents.py"))
    tree = ast.parse(agents_src)
    proposed_kinds = set()
    decided = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_propose":
            for kw in n.keywords:
                if kw.arg == "ref_kind" and isinstance(kw.value, ast.Constant):
                    proposed_kinds.add(kw.value.value)
            # _propose(db, tenant, agent, action_type, summary, ref_kind, ref_id, ...)
            if len(n.args) >= 6 and isinstance(n.args[5], ast.Constant):
                proposed_kinds.add(n.args[5].value)
        if isinstance(n, ast.FunctionDef) and n.name == "apply_decision":
            # `if action.ref_kind == "<kind>": ... if item and item.status == "<pending>"`
            for branch in ast.walk(n):
                if (isinstance(branch, ast.If) and isinstance(branch.test, ast.Compare)
                        and isinstance(branch.test.left, ast.Attribute)
                        and branch.test.left.attr == "ref_kind"):
                    kind = branch.test.comparators[0].value
                    for inner in ast.walk(ast.Module(body=branch.body, type_ignores=[])):
                        if (isinstance(inner, ast.Compare) and isinstance(inner.left, ast.Attribute)
                                and inner.left.attr == "status"
                                and isinstance(inner.comparators[0], ast.Constant)):
                            decided.setdefault(kind, inner.comparators[0].value)
    check("agents propose exactly the kinds in approvals.PENDING",
          proposed_kinds == set(approvals.PENDING), f"{proposed_kinds} vs {set(approvals.PENDING)}")
    check("apply_decision moves exactly those kinds, out of the same pending statuses",
          decided == {k: s for k, (_, s) in approvals.PENDING.items()}, str(decided))
    vocab = json.load(open(os.path.join(HERE, "..", "frontend", "lib", "status-vocab.json"),
                           encoding="utf-8"))
    for kind, (model, pending) in approvals.PENDING.items():
        system_only = vocab.get(model.__name__, {}).get("status", {}).get("systemOnly", [])
        check(f"{model.__name__}.status '{pending}' is systemOnly in status-vocab.json",
              pending in system_only, str(system_only))


def test_the_guard_can_fail():
    print()
    print("=" * 74)
    print("4. SELF-PROBES: THE DETECTOR GOES RED ON A BYPASS")
    print("=" * 74)

    def verdict(src):
        return scan_source(textwrap.dedent(src))["f"]

    bad = {
        "unguarded attribute write": """
            def f(db, i):
                po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == i).first()
                po.status = "Open"
        """,
        "unguarded setattr": """
            def f(db, i, body):
                t = db.query(models.MaintenanceTask).filter(models.MaintenanceTask.id == i).first()
                for k, v in body.items():
                    setattr(t, k, v)
        """,
        "unguarded delete": """
            def f(db, i):
                e = db.get(models.Escalation, i)
                db.delete(e)
        """,
        "helper called AFTER the write": """
            def f(db, i):
                po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == i).first()
                po.status = "Open"
                approvals.refuse_if_awaiting_decision(db, po, user)
        """,
        "helper called on a different variable": """
            def f(db, i, other):
                po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == i).first()
                approvals.refuse_if_awaiting_decision(db, other, user)
                po.notes = "x"
        """,
        "bulk update": """
            def f(db):
                db.query(models.PurchaseOrder).filter(models.PurchaseOrder.status == "Draft").update({"status": "Open"})
        """,
        "a loop writing every row": """
            def f(db):
                for e in db.query(models.Escalation).all():
                    e.owner = "x"
        """,
    }
    for label, src in bad.items():
        is_writer, problems = verdict(src)
        check(f"flags: {label}", is_writer and bool(problems), str(problems))

    good = {
        "helper called before the write": """
            def f(db, i):
                po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == i).first()
                approvals.refuse_if_awaiting_decision(db, po, user)
                po.status = "Open"
        """,
        "a constructor, not a loaded row": """
            def f(db):
                po = models.PurchaseOrder(po_no="x")
                po.status = "Draft"
        """,
        "an aggregate query": """
            def f(db):
                n = db.query(models.MaintenanceTask.machine_id, func.count()).first()
                n.x = 1
        """,
        "a name rebound to another model": """
            def f(db, i):
                x = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == i).first()
                x = db.query(models.Supplier).first()
                x.status = "Active"
        """,
    }
    for label, src in good.items():
        is_writer, problems = verdict(src)
        check(f"passes: {label}", not problems, str(problems))

    # The real source, mutated in memory.
    path = os.path.join(HERE, "factory_ops_routes.py")
    real = _read(path)
    call = "    approvals.refuse_if_awaiting_decision(db, task, current_user)\n"
    check("setup: the real update_maintenance_task call is present", real.count(call) == 2,
          str(real.count(call)))
    removed = real.replace(call, "", 1)
    check("removing the real call from update_maintenance_task turns the scan red",
          bool(scan_source(removed)["update_maintenance_task"][1]))
    loop = "    for key, value in payload.model_dump(exclude_unset=True).items():\n        setattr(task, key, value)\n"
    check("setup: the setattr loop is present", loop in removed)
    moved = removed.replace(loop, loop + call, 1)
    check("moving the call below the setattr loop turns the scan red",
          bool(scan_source(moved)["update_maintenance_task"][1]))

    # ...and the route inventory's own check, on the mutated endpoint source.
    import factory_ops_routes
    ep_src = textwrap.dedent(inspect.getsource(factory_ops_routes.update_maintenance_task))
    mutated = ep_src.replace("approvals.refuse_if_awaiting_decision(db, task, current_user)", "pass", 1)
    check("setup: the endpoint source carries the call", mutated != ep_src)
    check("the route-inventory check goes red on the mutated endpoint",
          not any(_is_helper_call(n) for n in ast.walk(ast.parse(mutated))))

    # ...and the pending-status check, on a PATCH and on a create.
    import orders_routes
    for fn, call in ((orders_routes.update_purchase_order,
                      "approvals.refuse_manual_pending_status(models.PurchaseOrder, payload.status, po.status)"),
                     (factory_ops_routes.create_escalation,
                      "approvals.refuse_manual_pending_status(models.Escalation, escalation.status)")):
        ep_src = textwrap.dedent(inspect.getsource(fn))
        check(f"setup: {fn.__name__} carries the pending-status call",
              ep_src.count(call) == 1 and _calls(ep_src, STATUS_HELPER))
        check(f"removing it from {fn.__name__} turns the inventory check red",
              not _calls(ep_src.replace(call, "pass", 1), STATUS_HELPER))


if __name__ == "__main__":
    test_route_inventory()
    test_writer_scan()
    test_registry_is_complete()
    test_the_guard_can_fail()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        sys.exit(1)
    print("THE LOCK HAS ONE HOME, AND EVERY WRITER CALLS IT")
