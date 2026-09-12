"""A status the backend writes must be a status the dropdown can show.

THE DEFECT
----------
A controlled `<select value={row.status}>` whose `<option>` list does not
contain `row.status` renders with NOTHING selected. React does not warn. The
operator sees an empty box where the status should be, and cannot tell a passed
inspection from a failed one.

Six sections hardcoded their own option lists, and those lists had drifted from
what the backend writes. Measured, not guessed — seed a real factory with
`factory_simulator.seed_all` and count the rows whose stored value is absent
from the list its own screen offers:

    QualityInspection.status      12 rows, 12 BLANK   'Rework' x7, 'Failed' x5
    PurchaseOrder.status           9 rows,  4 BLANK   'Partially Received' x2, 'Overdue' x2
    MaintenanceTask.task_type     16 rows,  9 BLANK   'Corrective' x3, 'Predictive' x3, 'Lubrication' x3
    CustomerOrder.status          12 rows,  8 BLANK   'In Production' x4, 'Ready to Dispatch' x2,
                                                      'Partially Dispatched' x2
    ProductionPlan.status         15 rows,  9 BLANK   'In Progress' x9
    ------------------------------------------------------------------------
    TOTAL                        123 rows, 42 BLANK   34% of every seeded row

Quality is the worst case and the clearest one: nothing in AMP has ever written
"Open", "In Review", "Closed" or "Rejected" onto an inspection, and nothing has
ever read them. Every real inspection is Passed / Failed / Rework, so every row
on that screen showed an empty box — and `statusStyle` coloured only the three
fictional values, so all twelve also rendered the same yellow pill. The column
carried no information at all.

NOT ONLY COSMETIC. `ai/agents.py` proposes a maintenance task or an escalation
with status "Proposed", which no list offered. An operator clicking the blank
box to find out what it was would overwrite it — and `approve_action` only
transitions `if item.status == "Proposed"`, so approving OR rejecting that task
afterwards does nothing, permanently. The blank dropdown is a live way to break
the agent approval chain by looking at it.

THE RULE
--------
For every (model, field) in frontend/lib/status-vocab.json, every value the
backend can write must appear in `options` (human-selectable) or `systemOnly`
(agent/seeder-written, displayable but not choosable).

The vocabulary file is read by BOTH sides — TypeScript renders from it, this
test checks the backend against it. That is the point: the defect was one rule
with two implementations, so the fix cannot be a second copy of the list.

WHY A SCAN AND NOT A FIXTURE. A fixture only proves the rows someone thought to
seed. This walks the AST of every non-test backend module and resolves the
string literals that reach a status/priority/task_type/severity column —
through `if/else`, `random.choice([...])`, and list variables — so a value added
anywhere is caught, including on paths the seeder never takes (the agent
lifecycle: Proposed -> Open | Cancelled, Draft -> Approved | Cancelled).

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_status_vocabulary_parity.py
"""
import ast
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
VOCAB_PATH = os.path.join(HERE, os.pardir, "frontend", "lib", "status-vocab.json")
COMPONENTS = os.path.join(HERE, os.pardir, "frontend", "components")

FIELDS = ("status", "priority", "task_type", "severity")

# Files that describe a test, an audit or a load harness rather than the product.
SKIP_PREFIXES = ("test_", "audit_", "e2e_", "loadtest", "dashboard_perf")

# The six screens whose hardcoded lists caused this. Any status word reappearing
# as a literal <option> in one of them is a re-introduction of the defect.
GUARDED_COMPONENTS = (
    "QualitySection.tsx", "PurchasingSection.tsx", "EscalationSection.tsx",
    "MaintenanceSection.tsx", "OrdersDispatchSection.tsx", "ProductionPlanSection.tsx",
)

# Writes the scan CANNOT resolve statically, each run down by hand once so the
# census is not quietly partial. Format: (model, field) -> (source model, source
# field, why). The vocabulary of the target must cover the vocabulary of the
# source, because the value is copied across verbatim.
COPIED_WRITES = {
    ("ProductionPlan", "status"): (
        "WorkOrder", "status",
        "factory_simulator.py:326 writes `status=wo.status if wo.status != 'On Hold' "
        "else 'Planned'`, i.e. a work order's status lands on the plan unchanged",
    ),
}

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def _strings(node, env, depth=0):
    """Every string literal `node` can evaluate to, as far as is visible statically.

    Returns (values, opaque). `opaque` is True when part of the expression could
    not be seen through — reported rather than silently treated as "no values".
    """
    if depth > 6 or node is None:
        return set(), False
    if isinstance(node, ast.Constant):
        return ({node.value}, False) if isinstance(node.value, str) else (set(), False)
    if isinstance(node, ast.IfExp):                       # "A" if cond else "B"
        a, ua = _strings(node.body, env, depth + 1)
        b, ub = _strings(node.orelse, env, depth + 1)
        return a | b, ua or ub
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        out, opaque = set(), False
        for element in node.elts:
            values, unseen = _strings(element, env, depth + 1)
            out |= values
            opaque = opaque or unseen
        return out, opaque
    if isinstance(node, ast.Name):                        # a local/module list var
        return _strings(env.get(node.id), env, depth + 1)
    if isinstance(node, ast.Subscript):                   # statuses[i % len(...)]
        return _strings(node.value, env, depth + 1)
    if isinstance(node, ast.Call):
        func = node.func
        name = (func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else "")
        if name in ("choice", "choices", "sample"):       # random.choice([...])
            out, opaque = set(), False
            for arg in node.args:
                values, unseen = _strings(arg, env, depth + 1)
                out |= values
                opaque = opaque or unseen
            return out, opaque
    return set(), True


def _local_env(scope, base):
    """Name -> value-expression for this scope only.

    Deliberately does NOT descend into nested functions: walking the whole module
    let one function's `statuses` list leak into another's, which attributed
    WorkOrder's vocabulary to CustomerOrder and made the census look clean.
    """
    env = dict(base)
    stack = list(scope.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue                                      # a sibling scope's names
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            env.setdefault(node.targets[0].id, node.value)
        stack.extend(ast.iter_child_nodes(node))
    return env


def _model_of_query(node, models):
    """'MaintenanceTask' for `db.query(models.MaintenanceTask).filter(...).first()`."""
    for child in ast.walk(node):
        if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                and child.func.attr == "query"):
            for arg in child.args:
                if isinstance(arg, ast.Attribute) and arg.attr in models:
                    return arg.attr
    return None


def scan(models):
    """(model, field) -> {value: "file:line"} for every literal the backend writes."""
    found = {(m, f): {} for m in models for f in FIELDS}
    opaque = []
    for dirpath, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "alembic", "node_modules")]
        for filename in sorted(files):
            if not filename.endswith(".py") or filename.startswith(SKIP_PREFIXES):
                continue
            path = os.path.join(dirpath, filename)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except (SyntaxError, UnicodeDecodeError):
                continue
            rel = os.path.relpath(path, HERE).replace("\\", "/")
            module_env = _local_env(tree, {})
            scopes = [tree] + [n for n in ast.walk(tree)
                               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for scope in scopes:
                env = _local_env(scope, module_env)
                # var -> model, by nearest preceding binding: `item` is rebound
                # per branch in ai/agents.approve_action, once per ref_kind.
                binds = [(n.lineno, n.targets[0].id, _model_of_query(n.value, models))
                         for n in ast.walk(scope)
                         if isinstance(n, ast.Assign) and len(n.targets) == 1
                         and isinstance(n.targets[0], ast.Name)
                         and _model_of_query(n.value, models)]
                for node in ast.walk(scope):
                    # models.Machine(..., status="Running", ...)
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr in models):
                        for kw in node.keywords:
                            if kw.arg in FIELDS:
                                values, unseen = _strings(kw.value, env)
                                for value in values:
                                    found[(node.func.attr, kw.arg)].setdefault(
                                        value, f"{rel}:{node.lineno}")
                                if unseen:
                                    opaque.append((node.func.attr, kw.arg, f"{rel}:{node.lineno}"))
                    # row.status = "Completed"
                    if (isinstance(node, ast.Assign) and len(node.targets) == 1
                            and isinstance(node.targets[0], ast.Attribute)
                            and node.targets[0].attr in FIELDS
                            and isinstance(node.targets[0].value, ast.Name)):
                        var = node.targets[0].value.id
                        candidates = [b for b in binds if b[1] == var and b[0] <= node.lineno]
                        if not candidates:
                            continue
                        model = max(candidates)[2]
                        values, unseen = _strings(node.value, env)
                        for value in values:
                            found[(model, node.targets[0].attr)].setdefault(
                                value, f"{rel}:{node.lineno}")
                        if unseen:
                            opaque.append((model, node.targets[0].attr, f"{rel}:{node.lineno}"))
    return found, opaque


def main():
    vocab = json.load(open(VOCAB_PATH, encoding="utf-8"))
    models = sorted(vocab)
    known = {(m, f): set(spec["options"]) | set(spec["systemOnly"])
             for m, fields in vocab.items() for f, spec in fields.items()}

    print("=" * 74)
    print("1. EVERY STATUS THE BACKEND WRITES CAN BE SHOWN BY ITS DROPDOWN")
    print("=" * 74)
    found, opaque = scan(models)
    unshowable = []
    for (model, field), values in sorted(found.items()):
        if not values:
            continue
        if (model, field) not in known:
            continue                       # a field with no dropdown — nothing to blank
        for value, where in sorted(values.items()):
            if value not in known[(model, field)]:
                unshowable.append(f"{model}.{field}={value!r} ({where})")
    for row in unshowable:
        print(f"      UNSHOWABLE  {row}")
    check(f"no backend-written value is missing from the vocabulary "
          f"({len(unshowable)} unshowable)", not unshowable, "; ".join(unshowable[:6]))

    print()
    print("=" * 74)
    print("2. THE VALUES COPIED FROM ANOTHER MODEL ARE COVERED TOO")
    print("=" * 74)
    # The scan cannot see through `status=wo.status`. Rather than let that read as
    # "nothing found", each copy site is declared and asserted.
    for (model, field), (src_model, src_field, why) in COPIED_WRITES.items():
        source = known.get((src_model, src_field), set())
        target = known.get((model, field), set())
        # Stated as a positive count rather than "nothing is missing": an absence
        # assertion passes just as happily when the comparison has been broken and
        # there is nothing left to compare. Mutation testing earned this — setting
        # the difference to [] survived the first round.
        covered = source & target
        check(f"{model}.{field} shows all {len(source)} of {src_model}.{src_field}'s "
              f"values ({why.split(',')[0]})",
              len(source) >= 3 and len(covered) == len(source),
              f"covers {len(covered)} of {len(source)}: missing {sorted(source - target)}")

    print()
    print("=" * 74)
    print("3. A HUMAN CANNOT PICK A STATUS ONLY AN AGENT MAY SET")
    print("=" * 74)
    # "Proposed" must be displayable but never selectable: ai/agents.approve_action
    # transitions only `if item.status == "Proposed"`, so a human who sets or clears
    # it by hand makes the pending approval a permanent no-op.
    overlap = [f"{m}.{f}: {sorted(set(s['options']) & set(s['systemOnly']))}"
               for m, fields in vocab.items() for f, s in fields.items()
               if set(s["options"]) & set(s["systemOnly"])]
    check("no value is both selectable and system-only", not overlap, "; ".join(overlap))
    system_only = sorted({v for fields in vocab.values() for s in fields.values()
                          for v in s["systemOnly"]})
    check(f"...and the system-only set is not empty ({', '.join(system_only)})",
          len(system_only) >= 3, str(system_only))

    print()
    print("=" * 74)
    print("4. NO SCREEN HAS GONE BACK TO A HARDCODED LIST")
    print("=" * 74)
    words = {v for fields in vocab.values() for s in fields.values()
             for v in s["options"] + s["systemOnly"]}
    literal_option = re.compile(r"<option>\s*([^<>{}]+?)\s*</option>")
    regressions = []
    unwired = []
    for name in GUARDED_COMPONENTS:
        path = os.path.join(COMPONENTS, name)
        if not os.path.exists(path):
            regressions.append(f"{name}: missing")
            continue
        source = open(path, encoding="utf-8").read()
        for text in literal_option.findall(source):
            if text in words:
                regressions.append(f"{name}: <option>{text}</option>")
        if "statusOptions(" not in source:
            unwired.append(name)
    for row in regressions:
        print(f"      HARDCODED  {row}")
    check(f"the six guarded screens render options from the shared vocabulary "
          f"({len(regressions)} hardcoded)", not regressions, "; ".join(regressions[:6]))
    # Without these two the section is vacuous in the worst way: break the regex,
    # or delete a screen's selects entirely, and "0 hardcoded" reads as success.
    # A detector that has stopped detecting must fail, not report all-clear.
    check(f"...and all {len(GUARDED_COMPONENTS)} of them call statusOptions()",
          not unwired, "no statusOptions() in: " + ", ".join(unwired))
    probe = "  <option>In Review</option>\n  <option value=\"\">Machine</option>\n"
    check("...and the hardcoded-option detector still detects one",
          literal_option.findall(probe) == ["In Review"],
          f"probe matched {literal_option.findall(probe)!r}")

    print()
    print("=" * 74)
    print("5. THE SCAN IS NOT VACUOUS")
    print("=" * 74)
    # A scan that resolves nothing agrees with every vocabulary. Pin what it finds,
    # so a broken resolver fails here instead of passing section 1 in silence.
    pairs = [k for k, v in found.items() if v]
    total = sum(len(v) for v in found.values())
    check(f"the scan resolves {total} literals across {len(pairs)} (model, field) pairs",
          total >= 40 and len(pairs) >= 9, f"{total} literals, {len(pairs)} pairs")
    # The agent lifecycle is the path a fixture would miss; prove it is reached.
    for model, field, value in (("MaintenanceTask", "status", "Proposed"),
                                ("Escalation", "status", "Proposed"),
                                ("PurchaseOrder", "status", "Draft"),
                                ("MaintenanceTask", "status", "Cancelled")):
        check(f"...including {model}.{field}={value!r} from the agent lifecycle",
              value in found.get((model, field), {}),
              f"not found; scan resolved {sorted(found.get((model, field), {}))}")
    print(f"      (statically unresolvable writes, reported not hidden: {len(opaque)})")
    for model, field, where in sorted(set(opaque))[:8]:
        print(f"        opaque  {model}.{field}  {where}")

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for failure in failures:
            print(f"  - {failure}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


def test_status_vocabulary_parity():
    """The pytest entry point — a suite exposing only main() contributes nothing
    to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
