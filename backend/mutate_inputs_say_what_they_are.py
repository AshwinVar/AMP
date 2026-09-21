"""Mutation harness for the three inputs that now say what they are.

Each mutation quietly brings an accepted-but-inert input back: the storage
link accepting any scheme (a stored `javascript:` link on a shared screen), the
validator detached from create or from update, the mapping row claiming to be
applied, the documented cost grouping silently gaining a reference. For each
the harness applies the edit, runs the suite that is supposed to notice, and
restores the file byte for byte. A mutation that leaves the suite green
SURVIVED: that guard is untested.

RUN IT ALONE. Like every mutate_* harness it EDITS THE WORKING TREE and restores
it afterwards; anything reading those files meanwhile sees broken code.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_inputs_say_what_they_are.py
Exit 0 only if every mutation applied exactly once and was caught.
"""
import io
import os
import subprocess
import sys

SUITES = ["test_inputs_say_what_they_are.py"]
SUITE_TIMEOUT = 300

SCHEMAS = "schemas.py"

# (label, file, old, new)
MUTATIONS = [
    ("the storage link accepts any scheme (a javascript: link is stored)", SCHEMAS,
     '    if not text.lower().startswith(("http://", "https://")):\n        raise ValueError(STORAGE_LINK_NOT_A_URL)\n    return text',
     "    return text"),
    ("a blank storage link is stored as text", SCHEMAS,
     "    if not text:\n        return None\n    if not text.lower().startswith",
     "    if False:\n        return None\n    if not text.lower().startswith"),
    ("the validator is detached from the create schema", SCHEMAS,
     '    notes: Optional[str] = None\n    _link_is_a_url = field_validator("storage_link", mode="before")(_http_link_or_none)\n\n\nclass ComplianceDocumentUpdate',
     "    notes: Optional[str] = None\n\n\nclass ComplianceDocumentUpdate"),
    ("the validator is detached from the update schema", SCHEMAS,
     '    notes: Optional[str] = None\n    _link_is_a_url = field_validator("storage_link", mode="before")(_http_link_or_none)\n\n\nclass ComplianceDocumentResponse',
     "    notes: Optional[str] = None\n\n\nclass ComplianceDocumentResponse"),
    ("a mapping row claims to be applied", SCHEMAS,
     "    applied: bool = False\n    created_at: Optional[datetime] = None",
     "    applied: bool = True\n    created_at: Optional[datetime] = None"),
]


def run_suites():
    failed = []
    here = os.path.dirname(os.path.abspath(__file__))
    for suite in SUITES:
        try:
            proc = subprocess.run([sys.executable, suite], capture_output=True, text=True, errors="replace",
                                  cwd=here, timeout=SUITE_TIMEOUT)
            if proc.returncode != 0:
                failed.append(suite)
        except subprocess.TimeoutExpired:
            failed.append(suite + " (timeout)")
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
    print(f"{'mutation':<66} {'verdict':<10} caught by")
    print("-" * 108)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<66} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
            survived.append(f"{label} (pattern did not apply)")
            continue
        io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source.replace(old, new, 1))
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source)
        if failing:
            verdict, note = "caught", ", ".join(s.replace("test_", "").replace(".py", "")[:28] for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label][:70] + "..."
        else:
            verdict, note = "SURVIVED", "-- nothing --"
        print(f"{label:<66} {verdict:<10} {note}", flush=True)
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
