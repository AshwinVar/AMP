"""Structural guards for the AMP-native AI integration (ADR-0020).

Guards that read the source, because the failure they prevent is a line of code
someone adds later and no behavioural test happens to exercise. Each one asserts
it FOUND its targets: a structural check that matches nothing reports all-clear.

  1. CONSENT AT THE CALL SITE  every call to telemetry_anomaly.service.score_machine
                               in native_ai_routes.py passes gate=DbConsentGate(...).
  2. ONE-WAY DEPENDENCY        no amp_ai module imports ai_copilot, native_ai_routes or
                               read_model_routes: the models never reach the web layer.
  3. UNSCOPED MODEL, GUARDED   AiLearningConsent is documented in
                               test_unscoped_model_reads.MANUALLY_SCOPED, and every
                               read of it - in amp_ai/ and in the routes, which that
                               suite's sweep does not scan - filters tenant_code.
  4. EVERY ROUTE AUTHENTICATES the native AI routes exist, each depends on
                               get_current_user, and the writes require a role.
  5. THROTTLES AND PINS        the scoring endpoints and the cards are rate-limited; the registry
                               reads each capability's PINNED hash, not its own copy.

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_integration_structural.py
"""
import ast
import inspect
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def parse(rel):
    path = os.path.join(HERE, *rel.split("/"))
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    return src, ast.parse(src)


def call_name(node):
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def gate_calls(tree):
    """[(lineno, passes DbConsentGate(...) as gate=)] for every score_machine call."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and call_name(node) == "score_machine":
            ok = any(kw.arg == "gate" and isinstance(kw.value, ast.Call) and call_name(kw.value) == "DbConsentGate"
                     for kw in node.keywords)
            out.append((node.lineno, ok))
    return out


def section_consent_at_call_site():
    print("=" * 74)
    print("1. EVERY ANOMALY SCORE IS ASKED WITH THE DATABASE CONSENT GATE")
    print("=" * 74)
    _, tree = parse("native_ai_routes.py")
    calls = gate_calls(tree)
    check("native_ai_routes.py calls score_machine at least once (the guard found its target)",
          len(calls) >= 1, str(calls))
    check("...and every call passes gate=DbConsentGate(...)", calls and all(ok for _, ok in calls), str(calls))
    bad = ast.parse("def h(db, t, m):\n    return service.score_machine(db, t, m, gate=AlwaysYes())\n")
    worse = ast.parse("def h(db, t, m):\n    return service.score_machine(db, t, m, gate=gate)\n")
    check("self-test: a different gate is flagged", gate_calls(bad) == [(2, False)], str(gate_calls(bad)))
    check("self-test: a gate from a variable is flagged", gate_calls(worse) == [(2, False)], str(gate_calls(worse)))


def section_one_way():
    print()
    print("=" * 74)
    print("2. THE MODELS NEVER IMPORT THE WEB LAYER")
    print("=" * 74)
    root = os.path.join(HERE, "amp_ai")
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        files += [os.path.join(dirpath, f) for f in filenames if f.endswith(".py")]
    forbidden = {"ai_copilot", "native_ai_routes", "read_model_routes", "main", "fastapi"}
    hits = []
    for path in files:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for n in names:
                if n.split(".")[0] in forbidden:
                    hits.append(f"{os.path.relpath(path, HERE)}: {n}")
    check("scanned the whole amp_ai package (at least 35 modules)", len(files) >= 35, str(len(files)))
    check("no amp_ai module imports ai_copilot, the routes, main or fastapi", not hits, str(hits))


def section_unscoped_model():
    print()
    print("=" * 74)
    print("3. THE CONSENT TABLE IS OUTSIDE THE HOOK, AND EVERY READ OF IT IS GUARDED")
    print("=" * 74)
    import models
    import tenancy
    import test_unscoped_model_reads as U
    check("AiLearningConsent carries tenant_code", hasattr(models.AiLearningConsent, "tenant_code"))
    check("...is NOT auto-scoped", models.AiLearningConsent not in tenancy.SCOPED_MODELS)
    check("...and is documented in MANUALLY_SCOPED with a reason",
          bool(U.MANUALLY_SCOPED.get("AiLearningConsent")), str(U.MANUALLY_SCOPED.get("AiLearningConsent")))
    watched = set(U.MANUALLY_SCOPED)
    targets, unguarded = 0, []
    for rel in ("amp_ai/consent.py", "native_ai_routes.py", "amp_ai/registry.py"):
        src, tree = parse(rel)
        for node in ast.walk(tree):
            targets += sum(1 for m in U._query_targets(node) if m == "AiLearningConsent")
        unguarded += [f"{rel}:{ln} {fn}() reads {ms}" for fn, ms, ln in
                      U._unguarded_reads(os.path.join(HERE, *rel.split("/")), watched)]
    check("the sweep found the consent reads (at least 2 query sites)", targets >= 2, str(targets))
    check("...and every one is inside a function that filters tenant_code", not unguarded, str(unguarded))


def section_routes():
    print()
    print("=" * 74)
    print("4. THE ROUTES EXIST AND AUTHENTICATE")
    print("=" * 74)
    import auth
    import main
    wanted = {("GET", "/ai/native/failure-risk"), ("GET", "/ai/native/anomaly/machines/{machine_id}"),
              ("GET", "/ai/models"), ("GET", "/ai/models/{name}"), ("GET", "/ai-consent"),
              ("PUT", "/ai-consent/{capability}")}
    found = {}
    for r in main.app.routes:
        for m in getattr(r, "methods", ()) or ():
            if (m, getattr(r, "path", "")) in wanted:
                found[(m, r.path)] = r
    check("all six native AI routes are registered", set(found) == wanted, str(sorted(wanted - set(found))))

    def calls_of(dependant, acc):
        for d in dependant.dependencies:
            acc.append(d.call)
            calls_of(d, acc)
        return acc

    roles = {}
    for key, r in found.items():
        calls = calls_of(r.dependant, [])
        check(f"{key[0]} {key[1]} depends on get_current_user", auth.get_current_user in calls)
        checkers = [c for c in calls if "role_checker" in getattr(c, "__qualname__", "")]
        allowed = None
        for c in checkers:
            cells = inspect.getclosurevars(c).nonlocals
            allowed = sorted(cells.get("allowed_roles") or [])
        roles[key] = allowed
    expect = {("GET", "/ai/native/failure-risk"): ["Admin", "Supervisor"],
              ("GET", "/ai/native/anomaly/machines/{machine_id}"): ["Admin", "Supervisor"],
              ("GET", "/ai-consent"): ["Admin", "Supervisor"],
              ("PUT", "/ai-consent/{capability}"): ["Admin"],
              ("GET", "/ai/models"): None, ("GET", "/ai/models/{name}"): None}
    for key, want in expect.items():
        check(f"{key[0]} {key[1]} roles == {want}", roles.get(key) == want, str(roles.get(key)))


def section_throttle_and_pins():
    print()
    print("=" * 74)
    print("5. THROTTLES AND PINNED HASHES")
    print("=" * 74)
    import http_security
    from amp_ai import registry
    from amp_ai.copilot_intent import classifier
    from amp_ai.failure_risk import predict
    from amp_ai.telemetry_anomaly import service
    for prefix in ("/ai/native/failure-risk", "/ai/native/anomaly", "/ai/models"):
        check(f"RATE_LIMITS has {prefix}", prefix in http_security.RATE_LIMITS)
    check("the registry is exactly the three capabilities",
          sorted(registry.MODELS) == ["copilot_intent", "failure_risk", "telemetry_anomaly"], str(sorted(registry.MODELS)))
    pins = {"failure_risk": predict.ARTIFACT_SHA256, "telemetry_anomaly": service.EVAL_SHA256,
            "copilot_intent": classifier.ARTIFACT_SHA256}
    for name, pin in pins.items():
        check(f"registry pin for {name} is the capability's own pinned constant",
              registry.pinned_sha256(name) == pin, str(registry.pinned_sha256(name)))
    src, _ = parse("amp_ai/registry.py")
    import re
    literal = re.findall(r"[0-9a-f]{64}", src)
    check("amp_ai/registry.py holds no hash literal of its own (one pin per model)", not literal, str(literal))
    from ai import assistant
    sig = inspect.signature(assistant.answer)
    check("assistant.answer has proposer=None", "proposer" in sig.parameters
          and sig.parameters["proposer"].default is None, str(sig))


def main_():
    section_consent_at_call_site()
    section_one_way()
    section_unscoped_model()
    section_routes()
    section_throttle_and_pins()
    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def test_amp_ai_integration_structural():
    assert main_() == 0, failures


if __name__ == "__main__":
    sys.exit(main_())
