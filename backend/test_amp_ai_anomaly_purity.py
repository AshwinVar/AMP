"""Telemetry anomaly: structural guards on the package.

WHAT IS PINNED HERE
-------------------
* The scorer (``series``, ``baseline``), the evaluation generator (``synthetic``)
  and the evaluation harness (``build_eval``) are standard library plus
  ``amp_ai.core`` only - no numpy, no network, no database, no pickle - and the
  package ``__init__`` imports nothing.
* No file in the package runs dynamic code (eval / exec / compile / pickle).
* The scorer never imports the generator or the harness (the model cannot
  know how its evaluation data was made), and the production path
  (``db_telemetry``, ``service``) never imports synthetic data, the harness,
  the rule-based predictors or the LLM copilot.
* The production path only READS: no session write call appears in
  ``db_telemetry`` or ``service``.
* Every ``db.query(...)`` chain in the production path filters ``tenant_code``
  explicitly, and the scan asserts it found the queries it expects (a guard
  that matches nothing reports all-clear).
* ``service.score_machine`` accepts no caller-supplied readings: its
  parameters are exactly (db, tenant, machine_id, *, gate, now), and ``gate``
  has no default.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_anomaly_purity.py
"""
import ast
import inspect
import os
import tempfile

from amp_ai.core import purity as P

BACKEND = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(BACKEND, "amp_ai", "telemetry_anomaly")


def path(name):
    return os.path.join(PKG, name + ".py")


PURE = [path(n) for n in ("__init__", "series", "baseline", "synthetic", "build_eval")]
PRODUCTION = [path(n) for n in ("db_telemetry", "service")]
ALL = PURE + PRODUCTION
WRITE_METHODS = frozenset({"add", "add_all", "commit", "delete", "merge", "flush", "execute", "bulk_save_objects",
                           "bulk_insert_mappings", "bulk_update_mappings", "rollback", "refresh"})

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def passes(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
        return True, ""
    except P.PurityViolation as exc:
        return False, str(exc)


# --------------------------------------------------------------------------- imports
def section_imports():
    print("\n[imports: pure modules are standard library + amp_ai.core; no dynamic code anywhere]")
    ok, detail = passes(P.assert_stdlib_only, PURE, min_files=len(PURE))
    check(f"__init__, series, baseline, synthetic, build_eval are standard library only ({len(PURE)} files)", ok, detail)
    ok, detail = passes(P.assert_no_dynamic_code, ALL, min_files=len(ALL))
    check(f"no eval/exec/compile/pickle in any of the {len(ALL)} package files", ok, detail)
    with open(path("__init__"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    check("the package __init__ imports nothing (so purity checks of its modules stay meaningful)",
          not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(tree)))

    ok, detail = passes(P.assert_no_forbidden_imports, [path("series"), path("baseline")],
                        {"amp_ai.telemetry_anomaly.synthetic", "amp_ai.telemetry_anomaly.build_eval",
                         "amp_ai.telemetry_anomaly.db_telemetry", "amp_ai.telemetry_anomaly.service"}, min_files=2)
    check("the scorer does not import the evaluation generator, the harness or the database path", ok, detail)
    ok, detail = passes(P.assert_no_forbidden_imports, PRODUCTION,
                        {"amp_ai.telemetry_anomaly.synthetic", "amp_ai.telemetry_anomaly.build_eval",
                         "predictive_engine", "ai", "ai_copilot", "factory_simulator", "industrial_adapters"},
                        min_files=2, transitive=False)
    check("the production path never imports synthetic data, the harness, the rule predictors or the copilot",
          ok, detail)

    root = tempfile.mkdtemp(prefix="amp_anomaly_purity_")
    try:
        bad = os.path.join(root, "bad.py")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("import sqlalchemy\n")
        ok, _ = passes(P.assert_stdlib_only, [bad], min_files=1)
        check("self-test: the guard does refuse a database import", not ok)
    finally:
        for name in os.listdir(root):
            os.remove(os.path.join(root, name))
        os.rmdir(root)


# --------------------------------------------------------------------------- read-only, tenant-filtered
def _tree(name):
    with open(path(name), encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=path(name))


def write_calls(tree):
    return sorted({(node.lineno, node.func.attr) for node in ast.walk(tree)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr in WRITE_METHODS})


def query_chains(tree):
    """For every ``db.query(...)`` call: the method names chained on it and whether a .filter() compares tenant_code."""
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    chains = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "query"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "db"):
            continue
        methods, tenant_filtered = [], False
        current = node
        while True:
            attr = parents.get(current)
            call = parents.get(attr)
            if not (isinstance(attr, ast.Attribute) and attr.value is current
                    and isinstance(call, ast.Call) and call.func is attr):
                break
            methods.append(attr.attr)
            if attr.attr == "filter":
                for arg in call.args:
                    if (isinstance(arg, ast.Compare) and isinstance(arg.left, ast.Attribute)
                            and arg.left.attr == "tenant_code" and len(arg.ops) == 1 and isinstance(arg.ops[0], ast.Eq)
                            and isinstance(arg.comparators[0], ast.Name) and arg.comparators[0].id == "tenant"):
                        tenant_filtered = True
            current = call
        chains.append((node.lineno, methods, tenant_filtered))
    return chains


def section_read_only_and_tenant():
    print("\n[production path: read-only, every query tenant-filtered]")
    for name, expected_queries in (("db_telemetry", 4), ("service", 1)):
        tree = _tree(name)
        writes = write_calls(tree)
        check(f"{name}.py makes no session write call", writes == [], str(writes))
        chains = query_chains(tree)
        check(f"{name}.py: the scan found its {expected_queries} db.query chain(s)", len(chains) == expected_queries,
              str(chains))
        unfiltered = [(line, methods) for line, methods, ok in chains if not ok]
        check(f"{name}.py: every db.query chain filters tenant_code == tenant explicitly", chains and not unfiltered,
              str(unfiltered))

    probe = ast.parse("rows = db.query(T.x).filter(T.machine_id == m).all()\n"
                      "ok = db.query(T.x).filter(T.tenant_code == tenant, T.machine_id == m).first()\n"
                      "db.commit()\n")
    found = query_chains(probe)
    check("self-test: the scanner tells a tenant-filtered query from an unfiltered one",
          [f for _, _, f in found] == [False, True], str(found))
    check("self-test: the scanner sees a session write", write_calls(probe) == [(3, "commit")])


# --------------------------------------------------------------------------- the service signature
def section_service_signature():
    print("\n[service: no caller-supplied readings, the gate is mandatory]")
    from amp_ai.telemetry_anomaly import service   # imports models
    sig = inspect.signature(service.score_machine)
    params = list(sig.parameters.values())
    check("score_machine(db, tenant, machine_id, *, gate, now)",
          [p.name for p in params] == ["db", "tenant", "machine_id", "gate", "now"], str(sig))
    check("gate and now are keyword-only",
          all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params if p.name in ("gate", "now")))
    check("gate has no default: every caller must pass a consent gate",
          sig.parameters["gate"].default is inspect.Parameter.empty)
    check("no parameter accepts readings or a baseline from the caller",
          not any("reading" in p.name or "baseline" in p.name or "values" in p.name for p in params))


SECTIONS = [section_imports, section_read_only_and_tenant, section_service_signature]


def main():
    print("=" * 74)
    print("Telemetry anomaly: purity and structural guards")
    print("=" * 74)
    for section in SECTIONS:
        section()
    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


def test_amp_ai_anomaly_purity():
    """pytest entry point; CI runs this file as a standalone script."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
