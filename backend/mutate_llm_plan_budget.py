"""Mutation harness for the planning budget and its diagnosis (ADR-0034).

Nothing here is arithmetic. What is fragile is that two different failures look
identical on the wire -- a model that ran out of room and a model that declined
both return an empty message -- and AMP spent its first real model evaluation
blaming the wrong one.

  * the budget can drop back to a number no reasoning model can meet;
  * the budget can stop being configurable, so the next model has no remedy;
  * nonsense configuration can silently disable the budget entirely;
  * truncation can stop being reported, which is the state the fix removed;
  * truncation can be reported for an empty plan that was NOT truncated, which
    is the same confusion pointing the other way.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_llm_plan_budget.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_llm_plan_budget.py", "test_copilot_orchestrator.py", "test_copilot_local_provider.py"]

L = os.path.join("ai", "llm.py")
C = "ai_copilot.py"
O = os.path.join("ai", "orchestrator.py")

MUTATIONS = [
    # --- the budget ----------------------------------------------------------
    ("the budget drops back to the 300 that measured 0/8", L,
     "PLAN_TOKENS_DEFAULT = 1200", "PLAN_TOKENS_DEFAULT = 300"),
    ("the budget stops being configurable", L,
     '        value = int(os.environ.get("AMP_LLM_PLAN_TOKENS", "") or PLAN_TOKENS_DEFAULT)',
     "        value = PLAN_TOKENS_DEFAULT"),
    ("a configured zero disables the budget instead of falling back", L,
     "    return value if value > 0 else PLAN_TOKENS_DEFAULT", "    return value"),
    ("bad configuration raises instead of falling back", L,
     "    except ValueError:\n        return PLAN_TOKENS_DEFAULT", "    except TypeError:\n        return PLAN_TOKENS_DEFAULT"),
    ("planning stops asking for the configured budget", L,
     "                                     max_tokens=plan_tokens())",
     "                                     max_tokens=300)"),

    # --- the diagnosis --------------------------------------------------------
    ("a truncated plan is silently treated as a refusal again", L,
     '        if not calls and out.get("finish_reason") == "length" and self._on_error:',
     "        if False:"),
    ("every empty plan is blamed on the budget, truncated or not", L,
     '        if not calls and out.get("finish_reason") == "length" and self._on_error:',
     "        if not calls and self._on_error:"),
    ("a plan that DID name a tool is reported as truncated anyway", L,
     '        if not calls and out.get("finish_reason") == "length" and self._on_error:',
     '        if out.get("finish_reason") == "length" and self._on_error:'),
    ("the report stops naming the knob that fixes it", L,
     '                f"(raise AMP_LLM_PLAN_TOKENS if this model reasons before answering)"))',
     '                f"(see the documentation)"))'),

    # --- carrying the reason out at all ---------------------------------------
    ("the provider stops reporting why the model stopped", C,
     '                "finish_reason": finish if isinstance(finish, str) else None,',
     '                "finish_reason": None,'),
    ("a non-string finish reason is passed through unchecked", C,
     '                "finish_reason": finish if isinstance(finish, str) else None,',
     '                "finish_reason": finish,'),

    # --- token counts (§11) ---------------------------------------------------
    ("the provider stops carrying token counts", C,
     '        self.last_usage = {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")\n'
     '                           if isinstance(usage.get(k), int)}',
     '        self.last_usage = {}'),
    ("a count that is not an integer is logged anyway", C,
     '                           if isinstance(usage.get(k), int)}',
     '                           if usage.get(k) is not None}'),
    # The counts are logged under names with no "token" in them because the
    # redactor blanks any key that has one. Logging the runtime's own names
    # looks identical in a unit test and is [REDACTED] in production.
    ("the counts are logged under the runtime's own names, which the redactor blanks", O,
     '        "usage": _usage_for_log(getattr(llm, "last_usage", None) if llm is not None else None)}})',
     '        "usage": (getattr(llm, "last_usage", None) if llm is not None else None)}})'),
]


def run_suites():
    failed = []
    here = os.path.dirname(os.path.abspath(__file__))
    for suite in SUITES:
        if not os.path.exists(os.path.join(here, suite)):
            continue
        proc = subprocess.run([sys.executable, suite], capture_output=True, text=True,
                              errors="replace", cwd=here)
        if proc.returncode != 0:
            failed.append(suite)
    return failed


EXPECTED_SURVIVORS = {}


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    originals = {}
    for _, path, _, _ in MUTATIONS:
        if path not in originals:
            originals[path] = io.open(os.path.join(here, path), encoding="utf-8").read()

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
        if source.count(old) != 1:
            print(f"{label:<62} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
            survived.append(f"{label} (pattern did not apply)")
            continue
        io.open(os.path.join(here, path), "w", encoding="utf-8",
                newline="\n").write(source.replace(old, new, 1))
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source)
        if failing:
            verdict, note = "caught", ", ".join(
                s.replace("test_", "").replace(".py", "")[:26] for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label]
        else:
            verdict, note = "SURVIVED", "-- nothing --"
        print(f"{label:<62} {verdict:<10} {note}")
        if verdict == "SURVIVED":
            survived.append(label)

    dirty = [p for p, original in originals.items()
             if io.open(os.path.join(here, p), encoding="utf-8").read() != original]
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
