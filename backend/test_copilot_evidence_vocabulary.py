"""The evidence vocabulary is ONE list, written in two languages (ADR-0022).

backend/ai/evidence.py defines what a Copilot figure can be (provenance), what
a result can be (data states and refusals) and what a cause can be (root-cause
labels). frontend/lib/evidence.ts renders them. If the backend adds a state the
frontend does not know, the UI shows no notice for it; if the frontend spells a
provenance differently, its chip silently falls back to "unknown".

This reads the four lists out of the TypeScript source and requires them to equal
the Python ones, in order.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_copilot_evidence_vocabulary.py
"""
import os
import re
import sys

from ai import evidence as ev

TS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend", "lib", "evidence.ts")

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def ts_list(source, name):
    m = re.search(r"export const " + name + r" = \[(.*?)\] as const;", source, re.S)
    return re.findall(r'"([^"]+)"', m.group(1)) if m else None


def main():
    source = open(TS, encoding="utf-8").read()
    for name, py in (("PROVENANCE", ev.PROVENANCE), ("DATA_STATES", ev.DATA_STATES),
                     ("REFUSALS", ev.REFUSALS), ("CAUSE_LABELS", ev.CAUSE_LABELS),
                     ("LIKELIHOOD", ev.LIKELIHOOD),
                     # ADR-0039: what a Copilot answer may offer to propose. The
                     # screen decides what button to draw from this set, so a kind
                     # added on one side only would render a button for something
                     # AMP cannot carry out, or hide one it can.
                     ("PROPOSABLE_KINDS", ev.PROPOSABLE_KINDS)):
        ts = ts_list(source, name)
        check(f"frontend {name} is present", ts is not None)
        check(f"frontend {name} == backend {name}, in order", ts == list(py), f"ts={ts} py={list(py)}")
    meanings = re.findall(r'^\s+"?([A-Z][A-Z -]+?)"?: "', source[source.find("PROVENANCE_MEANING"):], re.M)
    check("every provenance has a meaning in the UI", set(ev.PROVENANCE) <= set(meanings), str(meanings))
    if failures:
        print(f"\n{len(failures)} FAILED")
        sys.exit(1)
    print("\nALL CHECKS PASSED")

# ── Collected by pytest as well as run as a script ────────────────────
#
# CI's per-file runner (`python test_copilot_evidence_vocabulary.py`) is the contract
# and is unchanged. The separate coverage job collects every suite into ONE
# pytest process to compute the branch-coverage floor, and pytest only collects
# module-level `test_*` functions -- this file had none, so every line it
# exercises counted as untested. conftest.py describes the intended shape.
def test_everything():
    """The whole suite as one case, failing with whatever it recorded."""
    code = None
    try:
        code = main()
    except SystemExit as exc:          # several suites exit from inside main()
        code = exc.code
    assert not failures, "\n  " + "\n  ".join(str(f) for f in failures)
    assert code in (0, None), f"the suite exited with {code}"


if __name__ == "__main__":
    main()
