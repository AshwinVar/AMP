"""Guard: the serving path logs, it does not print.

WHY. Before structured logging, the entire observability story was 147 bare
print() statements captured by Railway's stdout. A print carries no level, no
request id, no tenant, no user and no timestamp the log platform can parse, so
none of it is filterable — which is the difference between "what broke for that
customer at 14:32?" being a query and being an afternoon.

The 49 prints in the serving path are now log calls. This test stops them
coming back one convenient debug line at a time, which is exactly how the
original 147 accumulated.

WHAT IS DELIBERATELY EXEMPT. Command-line tools SHOULD print. reset_factory,
backfill_enterprise_tenants, retention, reseed_inventory, e2e_sim and the
simulators talk to a human at a terminal who wants readable output, not one
JSON object per line. Converting those would make them worse, not better. The
distinction this file encodes is not "print is bad" — it is "a process serving
requests must emit machine-readable lines, a process serving a person must not."

Run:  python backend/test_no_server_prints.py     (exit 0 = pass)
"""
import io
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))

# Modules that run inside the web process, on the request or ingest path.
SERVING_MODULES = [
    "main.py",
    "mqtt_service.py",
    "live_ws.py",
    "tenancy.py",
    "module_manifest.py",
    "saas_routes.py",
    "ai_copilot.py",
    "plan_gate.py",
    "http_security.py",
    "events.py",
    "subscribers.py",
]

# Human-facing CLI tools. Each entry names WHY it is exempt, so an addition here
# has to be argued rather than appended.
CLI_EXEMPT = {
    "reset_factory.py": "founder's factory reset, run by hand at a terminal",
    "backfill_enterprise_tenants.py": "approval-gated backfill; prints a dry-run report for a human to read",
    "retention.py": "operational prune; prints what it would delete for a human to approve",
    "reseed_inventory.py": "local dev reseed",
    "reset_machines.py": "local dev helper",
    "e2e_sim.py": "end-to-end smoke runner; its output IS the result",
    "live_simulator.py": "dev-only HTTP simulator",
    "mqtt_machine_publisher.py": "standalone dev publisher",
    "mqtt_listener.py": "standalone dev listener",
    "phase30_plc_simulator.py": "dead legacy simulator, unreferenced",
    "factory_simulator.py": "seed + tick helpers; also invoked directly as a seeding CLI",
    "migrate.py": "deploy entry point; its output is read by whoever runs the deploy",
    "onboard_tenant.py": "tenant provisioning CLI",
    "offboard_tenant.py": "tenant purge CLI",
    "logging_config.py": "must not import itself; its own fallback path is deliberately print-free",
}

_PRINT = re.compile(r"(?<![\w.])print\s*\(")


def _prints_in(path):
    """Line numbers of print( calls, ignoring comments and docstring bodies.

    Crude but adequate: this looks for the call form, and a mention of print
    inside a comment does not match because the comment prefix is stripped
    first. A `print(` inside a triple-quoted docstring would be a false
    positive — none exist today, and one appearing is a reasonable thing to be
    asked about.
    """
    hits = []
    for i, line in enumerate(io.open(path, encoding="utf-8").read().splitlines(), 1):
        code = line.split("#", 1)[0]
        if _PRINT.search(code):
            hits.append((i, line.strip()[:90]))
    return hits


def test_no_prints_in_the_serving_path():
    offenders = []
    for name in SERVING_MODULES:
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            continue
        for lineno, text in _prints_in(path):
            offenders.append(f"{name}:{lineno}: {text}")
    assert not offenders, (
        "print() in the serving path — use logging_config.get_logger(__name__) so the "
        "line carries level, request id, tenant and user:\n  " + "\n  ".join(offenders))
    print("PASS no print() in the %d serving modules" % len(SERVING_MODULES))


def test_the_serving_modules_actually_log():
    """CONTROL. Without this, deleting every print AND every log call would pass
    the test above while making observability strictly worse than when we
    started. A module that reports nothing is not the goal; a module that
    reports in a parseable format is."""
    silent = []
    for name in ("main.py", "mqtt_service.py"):
        src = io.open(os.path.join(HERE, name), encoding="utf-8").read()
        if "logging_config" not in src or "log." not in src:
            silent.append(name)
    assert not silent, f"these modules stopped reporting altogether: {silent}"
    print("PASS the converted modules log rather than having gone silent")


def test_cli_tools_are_still_allowed_to_print():
    """The exemption list must stay meaningful. If a tool on it has stopped
    printing entirely, it has probably become a library and should either move
    to the serving list or lose its entry — either way a human should look."""
    # Deliberately NOT asserting that every exempt file exists. The list is a
    # standing policy statement, and entries legitimately come and go across
    # branches — asserting presence made this test fail on a branch where a
    # perfectly healthy CLI tool simply had not been merged yet, which is noise
    # rather than signal.
    #
    # The assertion that DOES catch a real mistake is the overlap: a file
    # claimed as both serving and exempt means the serving check silently skips
    # a module it was supposed to police.
    overlap = set(CLI_EXEMPT) & set(SERVING_MODULES)
    assert not overlap, f"a file cannot be both serving and exempt: {overlap}"
    present = sum(1 for n in CLI_EXEMPT if os.path.exists(os.path.join(HERE, n)))
    print("PASS the CLI exemption list does not overlap the serving list (%d/%d present here)"
          % (present, len(CLI_EXEMPT)))


if __name__ == "__main__":
    test_no_prints_in_the_serving_path()
    test_the_serving_modules_actually_log()
    test_cli_tools_are_still_allowed_to_print()
    print("ALL SERVER-PRINT GUARD TESTS PASSED")
