"""Mutation harness for follow-up questions (ADR-0035).

A thread is text the caller sends about their own conversation. The rule it
lives under is that it can NAME things and never AUTHORIZE them, and every
line that keeps that rule is one a well-meant edit could remove without any
passing case noticing on its own:

  * the referent looked up UNSCOPED, so another company's machine name in a
    forged thread resolves to that company's machine;
  * the caps removed, so a thread of any length is walked;
  * the referent words widened to "and", so every "and ..." question becomes a
    question about the last machine;
  * the cleaning dropped, so an "answer", an "evidence" or a "tenant" a turn
    carries reaches the planner;
  * the referent read from calls only, or the pronoun test skipped;
  * a route that drops the thread on the floor;
  * the window check that stops counting the conversation.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_copilot_follow_ups.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_copilot_follow_ups.py", "test_copilot_orchestrator.py", "test_copilot_eval.py"]

O = os.path.join("ai", "orchestrator.py")
L = os.path.join("ai", "llm.py")
R = "read_model_routes.py"

MUTATIONS = [
    ("the referent is looked up with no tenant bound (another company's name resolves)", O,
     "            if isinstance(name, str) and name:\n"
     "                m = assistant._machine_named(db, name)\n",
     "            if isinstance(name, str) and name:\n"
     "                _tok = __import__(\"tenancy\").set_current_tenant(None)\n"
     "                try:\n"
     "                    m = assistant._machine_named(db, name)\n"
     "                finally:\n"
     "                    __import__(\"tenancy\").reset_current_tenant(_tok)\n"),
    ("the turn cap is removed", O,
     "    return out[-MAX_THREAD_TURNS:]",
     "    return out"),
    ("the call cap is removed", O,
     "        for c in (raw_calls if isinstance(raw_calls, list) else [])[:MAX_THREAD_CALLS]:",
     "        for c in (raw_calls if isinstance(raw_calls, list) else []):"),
    ("non-scalar arguments survive cleaning", O,
     "                     if isinstance(k, str) and isinstance(v, (str, int, float, bool))}",
     "                     if isinstance(k, str)}"),
    ("a turn keeps everything it carried", O,
     "        if question or calls:\n            out.append({\"question\": question, \"calls\": calls})",
     "        if question or calls:\n            out.append(dict(turn, question=question, calls=calls))"),
    ("a call keeps everything it carried", O,
     "            calls.append({\"tool\": c[\"tool\"][:80], \"arguments\": args})",
     "            calls.append(dict(c, tool=c[\"tool\"][:80], arguments=args))"),
    ("the referent words widen to 'and'", O,
     "_REFERENT_WORDS = (\"it\", \"its\", \"it's\",",
     "_REFERENT_WORDS = (\"and\", \"it\", \"its\", \"it's\","),
    ("every follow-up is treated as pointing at a machine", O,
     "    return any(f\" {w} \" in q for w in _REFERENT_WORDS)",
     "    return True"),
    ("a machine named in the follow-up itself no longer wins", O,
     "    if thread and _refers_to_a_machine(question) and assistant._machine_named(db, question) is None:",
     "    if thread and _refers_to_a_machine(question):"),
    ("the referent is read from calls only, never from a prior question", O,
     "        m = assistant._machine_named(db, turn[\"question\"]) if turn[\"question\"] else None\n"
     "        if m is not None:\n            return m\n    return None",
     "    return None"),
    ("the planner is handed the raw thread, not the cleaned one", O,
     "    thread = clean_thread(thread)\n    rules = plan_rules(db, q, proposer, thread)",
     "    thread = thread if isinstance(thread, list) else []\n    rules = plan_rules(db, q, proposer, thread)"),
    ("the model is asked the raw question even when AMP resolved a pronoun", O,
     "        asked = f\"{q} ({rules.resolved['machine']})\" if rules.resolved else q",
     "        asked = q"),
    ("the answer claims the resolved machine whatever the model's plan used", O,
     "    used = any(isinstance(a, dict) and a.get(\"machine\") == name for _n, a in plan.calls)\n"
     "    return dict(rules.resolved) if used else None",
     "    return dict(rules.resolved)"),
    ("/copilot/ask drops the thread", R,
     "                               proposer=ai_copilot.native_proposer(), thread=thread)",
     "                               proposer=ai_copilot.native_proposer(), thread=None)"),
    ("the window check stops counting the conversation", L,
     "            _check_fits(estimate_tokens(json.dumps(functions), *[m[\"content\"] for m in messages]), plan_tokens(),",
     "            _check_fits(estimate_tokens(json.dumps(functions), messages[0][\"content\"], messages[-1][\"content\"]), plan_tokens(),"),
    ("the model is told the prior turns without what AMP ran", L,
     "            if turn.get(\"calls\"):\n                ran = \"; \".join(",
     "            if False:\n                ran = \"; \".join("),
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
    print(f"{'mutation':<74} {'verdict':<10} caught by")
    print("-" * 116)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<74} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
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
        print(f"{label:<74} {verdict:<10} {note}")
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
            print(f"  - {s}")
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
