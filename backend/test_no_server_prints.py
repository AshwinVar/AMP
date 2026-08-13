"""Guard: the serving path logs, it does not print.

WHY. Before structured logging, the entire observability story was 147 bare
print() statements captured by Railway's stdout. A print carries no level, no
request id, no tenant, no user and no timestamp the log platform can parse, so
none of it is filterable — which is the difference between "what broke for that
customer at 14:32?" being a query and being an afternoon.

HOW THE FIRST VERSION OF THIS GUARD FAILED, because it is the reason for the
shape of this one. It policed a HAND-WRITTEN list of 11 filenames. The web
process actually imports 72 modules. So the guard passed — accurately, as
written — while four unstructured lines went to stdout on every single boot and
the simulation tick printed on a background asyncio loop inside the web process
every 45 seconds per tenant. Nothing was lying; the list was just a guess that
nobody re-checked, and a guess is exactly what a guard should never depend on.

Worse, two of the offending modules were on the CLI_EXEMPT list. factory_simulator
IS a seeding CLI — and it is ALSO the module whose tick_* helpers run on the
simulation loop in the web process. A whole-file exemption written for the first
role silently covered the second.

SO THIS VERSION CHANGES TWO THINGS.

1. The serving set is COMPUTED, not listed. It is the transitive import closure
   of main.py, so a module that starts being imported by the web process is
   policed from that moment without anyone remembering to add it.

2. Exemptions are per-FUNCTION, not per-file. A dual-use module can have a
   print-free tick path and a chatty CLI path, which is the actual truth about
   factory_simulator and was unrepresentable before.

WHAT IS STILL DELIBERATELY EXEMPT. Command-line tools SHOULD print. A human at a
terminal running a reseed or a retention prune wants readable output, not one
JSON object per line. The distinction this file encodes is not "print is bad" —
it is "a process serving requests must emit machine-readable lines, a process
serving a person must not."

Run:  python backend/test_no_server_prints.py     (exit 0 = pass)
"""
import ast
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# The web process entrypoint. Procfile, railway.toml and the Dockerfile CMD all
# run `uvicorn main:app`, so main.py's import closure IS the set of modules
# loaded in a request-serving process.
ENTRYPOINT = "main.py"

# Functions whose print()s are reached only by a human running a command, even
# though the module around them is imported by the web process. Each entry names
# WHY, so adding one has to be argued rather than appended.
#
# A function listed here is NOT a blanket pass for its module — every other
# function in that module is still policed. That is the whole point.
# Every entry below was checked against what main.py ACTUALLY calls, not against
# what the function is named. main.py imports exactly eight tick_* helpers
# (main.py:321-329, called at :343-356) plus _production_records and
# _machine_events (:439-441). All ten now log. Everything listed here is
# reachable only from seed_all() or an `if __name__` block.
CLI_ONLY_FUNCTIONS = {
    "factory_simulator.py": {
        "seed_all": "the seeding CLI's entry point; its banner and completion line ARE the result",
        # The 21 per-table seeders, called only from seed_all(). Each prints one
        # "[SEED] <table>" progress line so the person running a several-minute
        # seed can see it advancing. That is a progress indicator for a human,
        # which is the one thing JSON logging is worse at.
        "_machines": "seed_all progress line",
        "_suppliers": "seed_all progress line",
        "_inventory": "seed_all progress line",
        "_inventory_transactions": "seed_all progress line",
        "_documents": "seed_all progress line",
        "_work_orders": "seed_all progress line",
        "_production_plans": "seed_all progress line",
        "_schedules": "seed_all progress line",
        "_customer_orders": "seed_all progress line",
        "_purchase_orders": "seed_all progress line",
        "_shifts": "seed_all progress line",
        "_quality": "seed_all progress line",
        "_maintenance": "seed_all progress line",
        "_operator_jobs": "seed_all progress line",
        "_costs": "seed_all progress line",
        "_escalations": "seed_all progress line",
        "_iot": "seed_all progress line",
        "_ai_recs": "seed_all progress line",
        "_notifications": "seed_all progress line",
        "_downtime_logs": "seed_all progress line",
        "_tenant": "seed_all progress line",
        # NOT on main.py's tick list. Verified: `grep -c tick_customer_order main.py` is 0,
        # and main.py:321-329 imports eight tick_* helpers, neither of these among them.
        # If either is ever added to that loop, it must be converted first.
        "tick_escalation": "not imported by main.py's simulation loop; reached only from seed/CLI paths",
        "tick_customer_order": "not imported by main.py's simulation loop; reached only from seed/CLI paths",
        "run_simulation": "CLI runner; prints which seeder failed, for the person watching",
        "_module_or_class_level": "the `if __name__` block: this file doubles as the seeding CLI",
    },
    "reset_factory.py": {
        # Note what is NOT exempt here: rebuild_factory(), the function main.py
        # actually calls on the RESEED_FACTORY path, contains no print at all —
        # every print in this file is in the `if __name__` block. main.py logs
        # the reseed outcome itself at main.py:428/:433.
        "_module_or_class_level": "the `if __name__` block: founder's factory reset CLI",
    },
    "demo_aeron.py": {
        # Same shape as reset_factory above, and checked the same way. The web
        # process reaches this module only on the RESEED_DEMO_OEM path, which
        # calls seed() — and seed(), wipe(), publish() and the two guards
        # contain no print between them. main.py logs the outcome itself.
        # These three are the terminal-facing half of a dual-use file: a person
        # running --reset before a sales meeting wants to read what was built,
        # not parse JSON.
        "status": "CLI: prints the demo's current state for a person about to present",
        "main": "CLI entry point; prints the refusal reason",
        "_run": "CLI dispatch; prints what was rebuilt or published",
    },
}

# Whole files that the web process never imports. These are not in main.py's
# closure, so they are outside this guard's computed scope anyway — the list is
# kept only so that a reader can see they were considered rather than missed.
KNOWN_CLI_TOOLS = {
    "backfill_enterprise_tenants.py": "approval-gated backfill; prints a dry-run report",
    "retention.py": "operational prune; prints what it would delete for a human to approve",
    "reseed_inventory.py": "local dev reseed",
    "reset_machines.py": "local dev helper",
    "e2e_sim.py": "end-to-end smoke runner; its output IS the result",
    "live_simulator.py": "dev-only HTTP simulator",
    "mqtt_machine_publisher.py": "standalone dev publisher",
    "mqtt_listener.py": "standalone dev listener",
    "phase30_plc_simulator.py": "dead legacy simulator, unreferenced",
    "migrate.py": "deploy entry point; its output is read by whoever runs the deploy",
    "onboard_tenant.py": "tenant provisioning CLI",
    "offboard_tenant.py": "tenant purge CLI",
}


def _local_modules():
    """Every importable module name in the backend tree, flat and one level deep."""
    names = set()
    for f in os.listdir(HERE):
        if f.endswith(".py"):
            names.add(f[:-3])
    for d in os.listdir(HERE):
        sub = os.path.join(HERE, d)
        if not os.path.isdir(sub) or d.startswith((".", "_")) or d in ("venv", "alembic"):
            continue
        for f in os.listdir(sub):
            if f.endswith(".py"):
                names.add(d + "." + f[:-3])
    return names


def _module_path(mod):
    return os.path.join(HERE, mod.replace(".", os.sep) + ".py")


def serving_modules():
    """The transitive import closure of main.py, as relative paths.

    FUNCTION-LOCAL IMPORTS COUNT, and that is not an oversight — it is the
    correction to a first attempt at this walk that counted only module-level
    imports. The reasoning for excluding them was "a function-local import only
    loads when someone calls that function, which is the operator-triggered
    reseed case". That is true of main.py:426 (reset_factory, behind
    RESEED_FACTORY) and false of main.py:322-329, where `_simulation_loop`
    imports the tick_* helpers function-locally and is then started
    unconditionally by asyncio.create_task at main.py:405.

    So the module-level rule dropped factory_simulator — the single worst
    offender, printing on a background loop every 45s per tenant — and would
    have shipped the same class of hole this rewrite exists to close.

    The safe direction is over-inclusion: pull in everything reachable, then
    argue individual functions out through CLI_ONLY_FUNCTIONS where a human
    reads the output. Over-inclusion costs an explicit, reviewed exemption.
    Under-inclusion costs nothing and is invisible, which is worse.
    """
    local = _local_modules()
    seen, stack = set(), ["main"]
    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = _module_path(mod)
        if not os.path.exists(path):
            continue
        try:
            tree = ast.parse(io.open(path, encoding="utf-8").read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):                 # every import, at any depth
            targets = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                targets = [node.module] + [node.module + "." + a.name for a in node.names]
            for t in targets:
                if t in local:
                    stack.append(t)
    return sorted(os.path.relpath(_module_path(m), HERE).replace(os.sep, "/")
                  for m in seen if os.path.exists(_module_path(m)))


def _print_sites(path):
    """(lineno, enclosing_function, source_line) for every print() call.

    AST rather than a regex: the previous version matched text, so a `print(`
    inside a docstring would have been a false positive and — more to the point
    — it could not tell you which FUNCTION the call was in, which is the fact
    the per-function exemptions are built on.
    """
    try:
        tree = ast.parse(io.open(path, encoding="utf-8").read())
    except SyntaxError:
        return []
    lines = io.open(path, encoding="utf-8").read().splitlines()

    # Recursive descent with an explicit stack, NOT ast.walk. ast.walk is
    # breadth-first, so an outer function is visited before an inner one and a
    # setdefault-based owner map records the OUTERMOST def — the opposite of
    # what an exemption needs. With a stack, the innermost enclosing def is on
    # top when the call is reached, which is the function a reader would name.
    sites = []

    def visit(node, stack):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack = stack + [node.name]
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "print"):
            name = stack[-1] if stack else "_module_or_class_level"
            text = lines[node.lineno - 1].strip()[:90] if node.lineno <= len(lines) else ""
            sites.append((node.lineno, name, text))
        for child in ast.iter_child_nodes(node):
            visit(child, stack)

    visit(tree, [])
    return sorted(sites)


def test_no_prints_in_the_serving_path():
    """Every print() in a module the web process imports, except in a function
    explicitly argued to be CLI-only."""
    modules = serving_modules()
    offenders = []
    for rel in modules:
        path = os.path.join(HERE, rel)
        exempt = CLI_ONLY_FUNCTIONS.get(rel, {})
        for lineno, func, text in _print_sites(path):
            if func in exempt:
                continue
            offenders.append("%s:%d in %s(): %s" % (rel, lineno, func, text))
    assert not offenders, (
        "print() reachable from the web process — use logging_config.get_logger(__name__) "
        "so the line carries level, request id, tenant and user. If the call really is "
        "only reached by a human running a command, add that FUNCTION to "
        "CLI_ONLY_FUNCTIONS with a reason:\n  " + "\n  ".join(offenders))
    print("PASS no unexempted print() across the %d modules the web process imports"
          % len(modules))


def test_the_computed_serving_set_is_not_trivially_small():
    """CONTROL for the computation itself.

    If the import walk silently broke — a parse error swallowed, a name-matching
    bug — it would return a nearly empty set and the test above would pass by
    checking nothing. That is precisely how the previous version of this guard
    gave a green tick while four lines a boot went unstructured, so the failure
    mode gets its own assertion rather than trust.
    """
    modules = serving_modules()
    assert len(modules) >= 50, (
        "the import walk found only %d serving modules — it is probably broken, "
        "and a broken walk makes the print check vacuous: %s" % (len(modules), modules))
    # Spot-check across the layers: entrypoint, a route module, a domain module,
    # an infra module. Each is imported at module level by main.py or its chain.
    for expected in ("main.py", "auth.py", "tenancy.py", "factory_simulator.py",
                     "industrial_adapters.py", "gmats_inventory_routes.py",
                     "mqtt_service.py", "live_ws.py", "logging_config.py"):
        assert expected in modules, "%s should be in the computed serving set" % expected
    print("PASS the import walk finds %d serving modules including every spot-check"
          % len(modules))


def test_the_walk_would_catch_a_newly_imported_module():
    """CONTROL. The whole claim of this rewrite is that the scope maintains
    itself. Prove the closure really is transitive rather than one level deep —
    a one-level walk would still miss most of the tree and would have missed
    auth.py, which main.py reaches only via tenancy."""
    modules = serving_modules()
    direct = set()
    tree = ast.parse(io.open(os.path.join(HERE, ENTRYPOINT), encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Import):
            direct.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            direct.add(node.module)
    # auth.py is the proof: main.py does not import it directly.
    assert "auth" not in direct, (
        "main.py now imports auth directly, so it is no longer a witness for "
        "transitivity — pick another module that is only reached indirectly")
    assert "auth.py" in modules, (
        "the walk is not transitive: auth.py is reached only via tenancy, and it is "
        "the module every authenticated request depends on")
    print("PASS the walk is transitive (auth.py is reached only indirectly, and is found)")


def test_the_serving_modules_actually_log():
    """CONTROL. Without this, deleting every print AND every log call would pass
    the checks above while making observability strictly worse than when we
    started. A module that reports nothing is not the goal; a module that
    reports in a parseable format is."""
    silent = []
    for name in ("main.py", "mqtt_service.py", "factory_simulator.py",
                 "industrial_adapters.py", "gmats_inventory_routes.py"):
        src = io.open(os.path.join(HERE, name), encoding="utf-8").read()
        if "get_logger" not in src or "log." not in src:
            silent.append(name)
    assert not silent, f"these modules stopped reporting altogether: {silent}"
    print("PASS the converted modules log rather than having gone silent")


def test_every_exemption_still_names_something_real():
    """An exemption for a function that no longer exists is stale permission.

    It is not merely untidy: the next person reads the list as a description of
    the code and it is describing something that is gone.
    """
    stale = []
    for rel, funcs in CLI_ONLY_FUNCTIONS.items():
        path = os.path.join(HERE, rel)
        if not os.path.exists(path):
            stale.append("%s (file missing)" % rel)
            continue
        present = {f for _, f, _ in _print_sites(path)}
        for func in funcs:
            if func not in present:
                stale.append("%s:%s() no longer contains a print()" % (rel, func))
    assert not stale, (
        "stale entries in CLI_ONLY_FUNCTIONS — remove them so the list keeps "
        "describing the code:\n  " + "\n  ".join(stale))
    print("PASS every CLI exemption still names a function that really prints")


if __name__ == "__main__":
    test_no_prints_in_the_serving_path()
    test_the_computed_serving_set_is_not_trivially_small()
    test_the_walk_would_catch_a_newly_imported_module()
    test_the_serving_modules_actually_log()
    test_every_exemption_still_names_something_real()
    print("ALL SERVING-PRINT GUARD TESTS PASSED")
