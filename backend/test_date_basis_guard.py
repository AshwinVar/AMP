"""Guard: test fixtures must date rows the way the code under test reads them.

Every request-time date comparison in this codebase is against
``datetime.utcnow().date()`` — 26 modules do it, none do otherwise. A fixture
built from ``date.today()`` is the LOCAL date, and the two disagree for any
timezone ahead of UTC in the hours after local midnight.

That is not theoretical. ``test_orders_routes`` seeded a purchase order "1 day
ago" with ``date.today()`` and asserted 3 overdue; at 00:20 local (23:20 UTC the
previous day) that row landed exactly on UTC today, stopped being overdue, and
the assertion got 2.

The reason it survived so long is the nastiest part: **CI cannot catch it.**
GitHub runners run in UTC, so the two bases agree there and the suite is green.
It only fails on a developer's machine, only for part of the day, and then heals
itself — which reads as flakiness rather than a bug.

So this guard is static, not behavioural: it fails on the *call*, wherever the
clock happens to be when it runs.

Run:  python backend/test_date_basis_guard.py     (exit 0 = pass)
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Files allowed to call date.today(), with the reason. Anything here that no
# longer needs the exemption fails the build, so the list cannot rot into a
# blanket excuse (same rule as the SCOPED_MODELS allowlist in
# test_unscoped_model_reads.py).
ALLOWED = {
    # factory_simulator captures a module-level `today = date.today()` at import
    # and is a seeding/simulation script, not request-handling code. A test that
    # calibrates against it has to speak the same clock.
}


def _calls_date_today(tree) -> list:
    """Line numbers of `date.today()` / `datetime.date.today()` calls.

    AST rather than grep on purpose: four test files mention ``date.today()`` in
    a comment explaining why they avoid it, and a textual guard would flag those
    for saying the right thing.
    """
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "today":
            continue
        target = func.value
        if isinstance(target, ast.Name) and target.id == "date":
            hits.append(node.lineno)
        elif (
            isinstance(target, ast.Attribute)
            and target.attr == "date"
            and isinstance(target.value, ast.Name)
            and target.value.id == "datetime"
        ):
            hits.append(node.lineno)
    return hits


def test_no_test_seeds_rows_with_the_local_date():
    offenders = {}
    checked = 0
    for name in sorted(os.listdir(HERE)):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        if name == os.path.basename(__file__):
            continue
        checked += 1
        with open(os.path.join(HERE, name), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=name)
        hits = _calls_date_today(tree)
        if hits and name not in ALLOWED:
            offenders[name] = hits

    assert checked > 100, f"only scanned {checked} test files — the sweep is not finding them"
    assert not offenders, (
        "these tests seed rows with the LOCAL date while the code under test "
        "compares against datetime.utcnow().date(); they will fail for part of "
        f"every day on any machine ahead of UTC, and CI (UTC) will not see it: {offenders}"
    )
    print(f"PASS no test seeds rows with the local date ({checked} test files scanned)")


def test_the_allowlist_has_not_rotted():
    for name in sorted(ALLOWED):
        path = os.path.join(HERE, name)
        assert os.path.exists(path), f"{name} is allowlisted but no longer exists"
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=name)
        assert _calls_date_today(tree), (
            f"{name} is allowlisted for date.today() but no longer calls it — "
            "drop the entry so the exemption cannot spread"
        )
    print(f"PASS date.today() allowlist is honest ({len(ALLOWED)} entries)")


def test_the_guard_can_actually_see_a_violation():
    """Prove the detector works, so an empty result means 'clean', not 'blind'."""
    tree = ast.parse("from datetime import date\nx = date.today()\n")
    assert _calls_date_today(tree) == [2], _calls_date_today(tree)

    tree = ast.parse("import datetime\nx = datetime.date.today()\n")
    assert _calls_date_today(tree) == [2], _calls_date_today(tree)

    # A comment mentioning it is not a call — this is why the guard parses.
    tree = ast.parse("# date.today() is local and would drift the window\nx = 1\n")
    assert _calls_date_today(tree) == []

    # utcnow().date() is the correct basis and must never be flagged.
    tree = ast.parse("from datetime import datetime\nx = datetime.utcnow().date()\n")
    assert _calls_date_today(tree) == []
    print("PASS the detector sees real calls, ignores comments, and allows utcnow().date()")


if __name__ == "__main__":
    test_the_guard_can_actually_see_a_violation()
    test_the_allowlist_has_not_rotted()
    test_no_test_seeds_rows_with_the_local_date()
    print("date-basis guard: OK")
    sys.exit(0)
