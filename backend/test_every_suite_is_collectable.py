"""Every backend suite must expose at least one test pytest can collect.

WHY THIS EXISTS. CI runs each suite twice, for two different questions. The
per-file runner (`python test_X.py`) answers "do the tests pass" and is the
contract. The coverage job collects the same files into ONE pytest process and
answers "how much of the application is exercised" — and pytest only collects
module-level `test_*` functions.

Thirty-seven suites had none. They passed in CI every time, so nothing looked
wrong, while the code they exercise counted as untested against a floor that is
supposed to describe how well the application is tested. A suite that cannot be
collected is a suite the floor is blind to, and the floor is the number people
argue about when they decide whether to write another test.

conftest.py already describes the intended shape — "module-level `test_*`
functions plus an `if __name__ == \"__main__\":` block that calls each one" —
but describing a convention is not enforcing one, and the gap grew to
thirty-seven files unnoticed.

WHAT THIS DOES NOT CHECK. That the collected test is a GOOD one. A suite whose
`test_everything()` runs the whole file is collectable and that is all this
asserts. The point is narrower than it looks: no suite may be invisible.

Run: python backend/test_every_suite_is_collectable.py
"""
import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

failures = []

# Deliberately EMPTY, and the assertion below keeps it that way unless somebody
# writes down a reason. A suite that genuinely cannot be collected — one that
# drops and recreates the schema, say — belongs in a CI step of its own, the way
# the AERON demo does (see .github/workflows/ci.yml), not in an allowlist here
# where it is invisible again.
EXEMPT: dict[str, str] = {}


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


class _Reported:
    """Makes `check()` fail the pytest case as well as the script.

    `check()` only RECORDS; the script runner reads `failures` at the end. Under
    pytest a test function that merely called check() would pass no matter what
    it found — which is exactly the kind of invisible guard this file exists to
    stop. Entering snapshots the count, leaving asserts nothing was added.
    """

    def __enter__(self):
        self._at = len(failures)
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            new = failures[self._at:]
            assert not new, "\n  " + "\n  ".join(new)
        return False


def collectable_tests(path):
    """Module-level functions pytest would collect from this file."""
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    return [n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("test")]


def test_every_suite_exposes_a_collectable_test():
    suites = sorted(f for f in os.listdir(HERE)
                    if f.startswith("test_") and f.endswith(".py"))
    # Non-vacuity: a moved directory or a changed naming convention must not
    # turn this into a check of nothing.
    assert len(suites) > 300, (
        f"only {len(suites)} suites found in {HERE} — this guard is reading the "
        f"wrong place, or the naming convention changed")

    invisible = []
    for name in suites:
        if name in EXEMPT:
            continue
        if not collectable_tests(os.path.join(HERE, name)):
            invisible.append(name)

    with _Reported():
        check(f"all {len(suites)} suites expose a test pytest can collect",
              not invisible,
              "these are invisible to the coverage job:\n    " + "\n    ".join(invisible))
        check("the exemption list is empty, or every entry states why",
              all(EXEMPT.values()), str([k for k, v in EXEMPT.items() if not v]))


def test_the_guard_can_actually_fail():
    """A guard nobody has seen fail is a guard nobody should trust.

    Parses a file that has an `if __name__` ladder and no `test_*` function —
    the exact shape the thirty-seven had — and asserts it is reported as
    invisible.
    """
    import tempfile
    standalone = (
        "def main():\n"
        "    print('ok')\n"
        "\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    main()\n")
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "test_pretend.py")
        io.open(p, "w", encoding="utf-8").write(standalone)
        p2 = os.path.join(tmp, "test_pretend_ok.py")
        io.open(p2, "w", encoding="utf-8").write(
            standalone + "\n\ndef test_everything():\n    main()\n")
        with _Reported():
            check("a suite with only main() is seen as uncollectable",
                  collectable_tests(p) == [], str(collectable_tests(p)))
            check("...and one with a test_ function is seen as collectable",
                  collectable_tests(p2) == ["test_everything"], str(collectable_tests(p2)))


if __name__ == "__main__":
    print("=" * 74)
    print("NO SUITE IS INVISIBLE TO THE COVERAGE JOB")
    print("=" * 74)
    test_every_suite_exposes_a_collectable_test()
    test_the_guard_can_actually_fail()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED - every suite is collectable")
