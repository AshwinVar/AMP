"""Native copilot intent model: inference is pure standard library, never touches the database or a tenant, and learns nothing at runtime.

WHY THIS EXISTS
---------------
The authorization chain for AMP-native AI is
    USER -> AUTHENTICATION -> RBAC -> TENANT/OEM/CONSENT -> AMP TOOL -> DATA -> MODEL.
The copilot model sits at the very end: it sees a question string and returns a
proposed intent NAME. The existing tenant-scoped pillar functions then fetch the
data. For that to hold structurally, the code that runs at request time
(classifier.py, labels.py, corpus.py and the package __init__) must:

  * import only the standard library and amp_ai.core (AST scan, transitive);
  * never import models, database, tenancy, ai, sqlalchemy, fastapi or the
    LLM copilot module - so it cannot build a query or pick a tenant;
  * contain no eval/exec/pickle (all five copilot_intent files are scanned);
  * load nothing application-side when actually imported and used in a clean
    interpreter (checked by importing it and listing sys.modules);
  * take no db or tenant argument, and hold no state that a question could
    change: routing the same questions in a different order, or after other
    tenants' questions, gives identical decisions, and neither the artifact nor
    the classifier object changes. A model learned from one tenant's questions
    could otherwise leak into another tenant's answers (constraint 7).

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_intent_isolation.py
"""
import copy
import glob
import inspect
import os
import subprocess
import sys

from amp_ai.core import purity as P

BACKEND = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(BACKEND, "amp_ai", "copilot_intent")
RUNTIME_FILES = [os.path.join(PKG, f) for f in ("__init__.py", "labels.py", "corpus.py", "classifier.py")]
APP_MODULES = {"models", "database", "tenancy", "ai", "sqlalchemy", "fastapi", "main", "ai_copilot",
               "predictive_engine", "oee_contract", "work_order_status", "starlette", "pydantic"}
failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def violation(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except P.PurityViolation as e:
        return e
    return None


def section_structure():
    print("\n[runtime files are standard library + amp_ai.core only]")
    check("all four runtime files exist", all(os.path.isfile(p) for p in RUNTIME_FILES),
          str([p for p in RUNTIME_FILES if not os.path.isfile(p)]))
    v = violation(P.assert_stdlib_only, RUNTIME_FILES, min_files=4, extra_allowed={"amp_ai"})
    check("assert_stdlib_only(classifier, labels, corpus, __init__; extra_allowed={amp_ai})", v is None, str(v))
    v = violation(P.assert_no_forbidden_imports, RUNTIME_FILES, APP_MODULES, min_files=4)
    check("no import of models/database/tenancy/ai/sqlalchemy/fastapi/ai_copilot (transitive)", v is None, str(v))
    everything = sorted(glob.glob(os.path.join(PKG, "*.py")))
    check("the package has exactly the five planned modules",
          {os.path.basename(p) for p in everything} ==
          {"__init__.py", "labels.py", "corpus.py", "classifier.py", "build.py"},
          str([os.path.basename(p) for p in everything]))
    v = violation(P.assert_no_dynamic_code, everything, min_files=5)
    check("no eval/exec/compile/pickle anywhere in copilot_intent (5 files)", v is None, str(v))
    import ast
    with open(os.path.join(PKG, "__init__.py"), encoding="utf-8") as fh:
        init_tree = ast.parse(fh.read())
    check("the package __init__ imports nothing (AST)",
          not [n for n in ast.walk(init_tree) if isinstance(n, (ast.Import, ast.ImportFrom))])

    print("\n[self-test: the scan would catch a database import in classifier.py]")
    import tempfile
    import shutil
    root = tempfile.mkdtemp(prefix="intent_iso_")
    try:
        pkg = os.path.join(root, "amp_ai", "copilot_intent")
        os.makedirs(pkg)
        for rel in ("amp_ai/__init__.py", "amp_ai/copilot_intent/__init__.py"):
            open(os.path.join(root, *rel.split("/")), "w").close()
        with open(os.path.join(root, "models.py"), "w") as fh:
            fh.write("X = 1\n")
        bad = os.path.join(pkg, "classifier.py")
        with open(bad, "w") as fh:
            fh.write("import json\nimport models\n")
        check("a classifier importing models is refused",
              violation(P.assert_no_forbidden_imports, [bad], APP_MODULES, min_files=1) is not None)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def section_clean_interpreter():
    print("\n[a clean interpreter that loads and uses the classifier imports nothing application-side]")
    code = (
        "import sys\n"
        f"sys.path.insert(0, {BACKEND!r})\n"
        "from amp_ai.copilot_intent import classifier\n"
        "clf = classifier.load()\n"
        "d = clf.route('how many machines are down right now')\n"
        f"bad = sorted(n for n in sys.modules if n.split('.')[0] in {sorted(APP_MODULES)!r})\n"
        "net = sorted(n for n in sys.modules if n.split('.')[0] in ('socket', 'ssl', 'urllib', 'http', 'pickle'))\n"
        "print('BAD=' + ','.join(bad)); print('NET=' + ','.join(net)); print('ROUTE=' + str(d.route))\n"
    )
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)   # proves no database module is needed at all
    r = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True)
    fields = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    check("the subprocess ran (without DATABASE_URL)", r.returncode == 0, (r.stderr or "")[-600:])
    check("it reported its module lists (the check is not vacuous)",
          {"BAD", "NET", "ROUTE"} <= set(fields), r.stdout[-300:])
    check("no application or database module was imported", fields.get("BAD") == "", fields.get("BAD", "?"))
    check("no network or pickle module was imported", fields.get("NET") == "", fields.get("NET", "?"))


def section_purity_at_runtime():
    print("\n[routing is a pure function of the artifact and the question]")
    from amp_ai.copilot_intent import classifier as C

    sig = inspect.signature(C.IntentClassifier.route)
    check("route(self, question) takes no db / tenant / user argument",
          list(sig.parameters) == ["self", "question"], str(list(sig.parameters)))
    for name, fn in inspect.getmembers(C, inspect.isfunction):
        params = set(inspect.signature(fn).parameters)
        check(f"classifier.{name}() takes no db/tenant/session argument",
              not (params & {"db", "tenant", "session", "tenant_code", "user"}), str(params))

    def state(obj):
        # Model objects have no __eq__; compare their serialised parameters instead.
        return {k: (v.to_dict() if hasattr(v, "to_dict") else copy.deepcopy(v))
                for k, v in vars(obj).items() if k != "artifact"}

    clf = C.load()
    artifact_before = copy.deepcopy(clf.artifact)
    state_before = state(clf)
    tenant_a = ["what is our oee this week", "which machines are down", "stock position pls",
                "tenant A secret order 7781 for Bugatti is late", "how did the night shift do"]
    tenant_b = ["any quality issues", "maintenance backlog", "what did downtime cost us",
                "compare this week with last week", "hello"]
    first = [clf.route(q) for q in tenant_b]
    for q in tenant_a * 20:
        clf.route(q)
    after_a = [clf.route(q) for q in tenant_b]
    reversed_b = list(reversed([clf.route(q) for q in reversed(tenant_b)]))
    check("tenant B's decisions are identical before and after routing tenant A's questions", first == after_a)
    check("decisions do not depend on the order questions arrive", first == reversed_b)
    fresh = C.IntentClassifier(copy.deepcopy(artifact_before))
    check("a freshly constructed classifier gives the same decisions (no hidden learning)",
          [fresh.route(q) for q in tenant_b] == first)
    check("the loaded artifact dict is unchanged after 110 routings", clf.artifact == artifact_before)
    state_after = state(clf)
    check("the classifier's state really was captured (the comparison is not vacuous)", len(state_before) >= 3,
          str(sorted(state_before)))
    check("the classifier object's state is unchanged (no question history, no running statistics)",
          state_after == state_before, str(sorted(set(state_after) ^ set(state_before))))
    check("load() is cached: the same object is returned", C.load() is C.load())


def main():
    print("=" * 74)
    print("AMP-native copilot intent: isolation")
    print("=" * 74)
    section_structure()
    section_clean_interpreter()
    section_purity_at_runtime()
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


def test_amp_ai_intent_isolation():
    """pytest entry point; CI runs this file as a standalone script."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
