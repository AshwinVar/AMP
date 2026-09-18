"""Every mutation in every backend/mutate_*.py still applies to the code.

THE DEFECT
----------
A mutation harness breaks one guard at a time and asserts that a suite goes
red. It finds the line to break by its text, an anchor. When a change rewrites
that line, the anchor matches nothing, and the harness reports SKIP or NOT
APPLIED for that entry. It does that only when somebody runs the harness, and
CI runs none of them: each takes minutes.

On 2026-09-18 a run of every harness found 11 mutations in 6 harnesses that had
stopped applying. Ordinary refactors had stranded every one:

  #614   moved the preview test into tenancy.is_preview (3 AI-consent
         guards), the grant union into oem_sharing.widen_grants, and gave
         the OEM viewer read_contracts
  #616   moved the grant refusal into oem_sharing.refused_grants
  #631   put the reserved-namespace test between the OEM and preview branches
  #509   gave MachineInstallation the same `site` line as Machine, so that
         anchor matched twice, and that harness skips a double match
  #522   put the service-hours consent check inside one anchor's block and
         turned another's in-place sort into the function's return

The guards were all still in place and still tested by their suites. What had
stopped, without anyone seeing, was the proof that the suites would notice a
guard going. mutate_doc_numbers.py records the same thing happening before
(#509's second `username` line), found by hand.

THE RULE
--------
Every anchor occurs exactly once in its target, read as the harnesses read it
(text mode), unless it is PINNED below with its expected count and why the
first occurrence is the site meant. A pinned count that changes fails too: a new
identical line ABOVE the intended site would silently retarget the mutation.
Every harness must yield at least one mutation, so a layout this test does not
understand fails rather than checking nothing. It takes seconds, so the PR that
strands an anchor is the PR that fails.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_mutation_anchors_apply.py
"""
import importlib
import io
import os

BACKEND = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BACKEND)

# (harness, label) -> (expected count, why the FIRST occurrence is the intended
# site). Each of these harnesses replaces the first occurrence only.
PINNED = {
    ("mutate_cancelled_po.py", "the summary stops publishing the cancelled count"):
        (2, "build_supply_summary comes first; build_supplier_detail repeats the block"),
    ("mutate_open_escalation.py",
     "document-review generator: a spelling the regex cannot see (notin_)"):
        (3, "generate_document_review_escalations is the first of the file's three generators"),
    ("mutate_open_escalation.py",
     "late-order generator: another unseen spelling (~(== 'Resolved'))"):
        (2, "generate_late_order_escalations precedes generate_overdue_po_escalations"),
}

_SOURCE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".mjs")
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def _is_path(value):
    return isinstance(value, str) and value.endswith(_SOURCE_SUFFIXES) and "\n" not in value


def _edits(entry):
    """(label, [(path, anchor), ...]) for one mutation entry, or None if the
    entry is not one. The harnesses use four layouts:
        (label, path, old, new, ...)
        (label, path, [(old, new), ...], ...)
        (label, [(path, old, new), ...], ...)          the _one() helper
        (label, path, [old, ...], [new, ...])
    """
    if not (isinstance(entry, (tuple, list)) and len(entry) >= 2 and isinstance(entry[0], str)):
        return None
    label, rest = entry[0], entry[1:]
    first = rest[0]
    if isinstance(first, list) and first and all(
            isinstance(e, tuple) and len(e) == 3 and _is_path(e[0]) for e in first):
        return label, [(p, old) for p, old, _new in first]
    if _is_path(first) and len(rest) >= 2:
        second = rest[1]
        if isinstance(second, str):
            return label, [(first, second)]
        if isinstance(second, list) and second and all(
                isinstance(e, tuple) and len(e) == 2 for e in second):
            return label, [(first, old) for old, _new in second]
        if isinstance(second, list) and second and all(isinstance(e, str) for e in second):
            return label, [(first, old) for old in second]
    return None


def _mutations(module):
    found = []
    for value in vars(module).values():
        if isinstance(value, (list, tuple)):
            for entry in value:
                parsed = _edits(entry)
                if parsed:
                    found.append(parsed)
    return found


def _read(path):
    for base in (BACKEND, ROOT):
        full = os.path.join(base, path)
        if os.path.isfile(full):
            return io.open(full, encoding="utf-8").read()
    return None


def main():
    harnesses = sorted(f for f in os.listdir(BACKEND)
                       if f.startswith("mutate_") and f.endswith(".py"))
    check("the harnesses were found", len(harnesses) >= 20, f"{len(harnesses)}")
    seen_pins = set()
    total = 0
    for name in harnesses:
        module = importlib.import_module(name[:-3])
        mutations = _mutations(module)
        check(f"{name}: its mutations were read ({len(mutations)})", len(mutations) > 0,
              "no mutation in a layout this test understands")
        for label, edits in mutations:
            for path, anchor in edits:
                total += 1
                source = _read(path)
                if source is None:
                    check(f"{name}: [{label}] targets a file that exists", False, path)
                    continue
                hits = source.count(anchor) if anchor else 0
                pin = PINNED.get((name, label))
                if pin:
                    seen_pins.add((name, label))
                    if hits != pin[0]:
                        check(f"{name}: [{label}] still matches its pinned {pin[0]} sites in {path}",
                              False, f"{hits} now: re-check which site the first one is ({pin[1]})")
                elif hits != 1:
                    check(f"{name}: [{label}] applies exactly once in {path}", False,
                          f"{hits} matches: the anchor no longer names one line")
    stale_pins = sorted(set(PINNED) - seen_pins)
    check("CONTROL: every PINNED entry still names a mutation", not stale_pins, str(stale_pins))
    print(f"\n  {total} anchors in {len(harnesses)} harnesses checked")

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


def test_mutation_anchors_apply():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
