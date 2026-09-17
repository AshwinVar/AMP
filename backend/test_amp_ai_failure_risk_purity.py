"""Failure-risk model code: pure where it must be, never near a query where it must not be.

WHY THIS EXISTS
---------------
The founder's rules for AMP-native AI are structural, so they are checked
structurally (an AST scan via ``amp_ai.core.purity``, which follows local
imports transitively) rather than trusted to review:

  1  PURE CORE     history.py, features.py and synthetic.py - the code that
                   defines what a machine-week IS and what the model may see -
                   import only the standard library (plus ``duration``, the
                   repo's one duration parser, itself standard library only)
  2  NO QUERY      predict.py, the module that turns histories into scores,
                   imports no database layer, no session, no tenancy and not
                   the database loader: the model is handed histories that the
                   AMP data layer fetched, it never fetches them
  3  BUILD OFFLINE build.py imports no database session, loader or tenancy
                   (it imports the rule scorer, which pulls the ORM models in,
                   but test_amp_ai_failure_risk_build.py proves no connection
                   is ever opened)
  4  NO DYNAMIC    no pickle/marshal/shelve, no eval/exec/compile/__import__ in
                   any failure-risk module
  5  FOUND THEM    every guard names the exact files it expects and asserts it
                   found them: a guard that matches nothing reports all-clear

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_failure_risk_purity.py
"""
import os

from amp_ai.core import purity as P

BACKEND = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(BACKEND, "amp_ai", "failure_risk")

EXPECTED_MODULES = {"__init__", "history", "features", "synthetic", "baseline_rule", "build",
                    "db_history", "predict"}

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def module(name):
    return os.path.join(PKG, f"{name}.py")


def violation(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except P.PurityViolation as exc:
        return exc
    return None


# --------------------------------------------------------------------------- 5 (first: the targets exist)
def section_found_targets():
    print("\n5. The guards' targets exist")
    present = {os.path.splitext(f)[0] for f in os.listdir(PKG) if f.endswith(".py")}
    check("amp_ai/failure_risk holds exactly the planned modules", present == EXPECTED_MODULES,
          f"missing {sorted(EXPECTED_MODULES - present)}, unexpected {sorted(present - EXPECTED_MODULES)}")


# --------------------------------------------------------------------------- 1
def section_pure_core():
    print("\n1. history / features / synthetic are standard library only")
    paths = [module("history"), module("features"), module("synthetic")]
    err = violation(P.assert_stdlib_only, paths, min_files=3, extra_allowed={"duration"})
    check("standard library (+ duration) only, transitively", err is None, str(err))
    for name in ("history", "features", "synthetic"):
        err = violation(P.assert_no_forbidden_imports, [module(name)],
                        {"predictive_engine", "ai", "models", "database", "sqlalchemy", "oee_contract",
                         "tenancy", "work_order_status"}, min_files=1)
        check(f"{name}.py never reaches the rule scorer, ai or the database layer", err is None, str(err))


# --------------------------------------------------------------------------- 2
def section_predict_never_queries():
    print("\n2. predict.py cannot build or run a query")
    forbidden = {"sqlalchemy", "database", "tenancy", "models", "oee_contract",
                 "amp_ai.failure_risk.db_history", "amp_ai.failure_risk.build", "amp_ai.failure_risk.synthetic"}
    # transitive=False: predict imports baseline_rule, whose predictive_engine import loads the ORM
    # models module (documented in baseline_rule). predict itself must not reach for any of it.
    err = violation(P.assert_no_forbidden_imports, [module("predict")], forbidden, min_files=1, transitive=False)
    check("predict.py imports no DB layer, session, tenancy, loader, builder or generator", err is None, str(err))
    for name in ("features", "history"):
        err = violation(P.assert_no_forbidden_imports, [module(name)], {"amp_ai.failure_risk.db_history"},
                        min_files=1)
        check(f"{name}.py does not import the database loader", err is None, str(err))


# --------------------------------------------------------------------------- 3
def section_build_offline():
    print("\n3. build.py has no route to a database session")
    forbidden = {"sqlalchemy", "database", "tenancy", "amp_ai.failure_risk.db_history", "oee_contract"}
    err = violation(P.assert_no_forbidden_imports, [module("build")], forbidden, min_files=1, transitive=False)
    check("build.py imports no session, engine, tenancy or database loader", err is None, str(err))


# --------------------------------------------------------------------------- 4
def section_no_dynamic_code():
    print("\n4. No dynamic code or code-executing serialisation")
    paths = [module(name) for name in sorted(EXPECTED_MODULES)]
    err = violation(P.assert_no_dynamic_code, paths, min_files=len(EXPECTED_MODULES))
    check(f"none of the {len(EXPECTED_MODULES)} failure-risk modules uses eval/exec/pickle", err is None, str(err))


def main():
    print("=" * 74)
    print("AMP-native AI failure risk: purity")
    print("=" * 74)
    section_found_targets()
    section_pure_core()
    section_predict_never_queries()
    section_build_offline()
    section_no_dynamic_code()
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


def test_amp_ai_failure_risk_purity():
    """pytest entry point; CI runs this file as a standalone script."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
