"""A migration test must name the revision it downgrades to, never "-1".

WHY. `alembic downgrade -1` means "one step back from HEAD", not "undo the
migration this file is about". A test written when its migration was head is
correct exactly until the next migration lands — after which it quietly undoes
somebody else's migration and then asserts its own table is gone. The failure
names the wrong file: a verification of 0013 goes red because 0015 was added,
and the person reading it has no reason to suspect a test they did not touch.

This happened twice in one afternoon, to a pair of twins:

  * test_migration_0013_gateway_credentials.py  (SQLite, every CI run)
  * verify_pg_gateway_credentials.py            (PostgreSQL, the migration gate)

Both said `downgrade -1`, both broke when migration 0015 was added, and the
second was only found because CI runs PostgreSQL and a laptop does not. The fix
in both is the same: read the migration's own `down_revision` and name it.

WHAT IS ALLOWED. An absolute revision — a literal like "0012_consent_scope", or
anything read from a migration file at runtime. What is refused is a RELATIVE
target: "-1", "-2", "head-1" and so on.

Run: python backend/test_migration_downgrade_targets_are_absolute.py
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

failures = []

# A downgrade target that is relative to wherever head happens to be.
#
# Anything may sit between the word and the target, because the two real bugs
# spelled it two different ways:
#     alembic(env, "downgrade", "-1")      a positional argument after a comma
#     command.downgrade(cfg, "-1")         a config object in between
# A character class tight enough to match the first missed the second, which is
# the one that had been failing CI the longest.
RELATIVE = re.compile(r"""downgrade[^#\n]*["'](-\d+|head[-+]\d+)["']""", re.I)

# This file quotes the bad forms as fixtures, so it cannot scan itself.
SELF = os.path.basename(__file__)

# Files that drive alembic. Both naming conventions, because the bug lived in
# one of each and a guard that watched only `test_*` would have missed the one
# that actually reached CI.
def _candidates():
    for name in sorted(os.listdir(HERE)):
        if not name.endswith(".py"):
            continue
        if name == SELF:
            continue
        if name.startswith(("test_migration", "verify_pg", "test_boot_migrations",
                            "test_migrate", "test_schema_guard")):
            yield name


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def offenders():
    found = []
    for name in _candidates():
        src = io.open(os.path.join(HERE, name), encoding="utf-8").read()
        for i, line in enumerate(src.splitlines(), start=1):
            code = line.split("#", 1)[0]
            if RELATIVE.search(code):
                found.append(f"{name}:{i}: {line.strip()}")
    return found


def test_no_migration_check_downgrades_relative_to_head():
    hits = offenders()
    assert not hits, (
        "these downgrade relative to HEAD, so the next migration to land will "
        "make them undo it and then fail about their own:\n  "
        + "\n  ".join(hits)
        + "\n\nName the revision instead, read from the migration's own "
          "down_revision (see test_migration_0013_gateway_credentials.py).")

    scanned = list(_candidates())
    # Non-vacuity: a rename or a moved directory must not turn this into a check
    # of nothing.
    assert len(scanned) >= 10, (
        f"only {len(scanned)} migration-driving files found — this guard is "
        f"reading the wrong place, or the naming convention changed")
    check(f"none of the {len(scanned)} migration checks downgrade relative to head", True)


def test_the_pattern_catches_what_it_is_for():
    """The exact lines that shipped, and the fixed forms, both ways."""
    bad = [
        'rc, out = alembic(env, "downgrade", "-1")',
        'command.downgrade(cfg, "-1")',
        "command.downgrade(cfg, '-2')",
        'alembic(env, "downgrade", "head-1")',
    ]
    good = [
        'rc, out = alembic(env, "downgrade", prev)',
        'command.downgrade(cfg, "%(prev)s")',
        'command.downgrade(cfg, "0012_consent_scope")',
        'rc, out = alembic(env, "upgrade", "head")',
        '# this used to say downgrade "-1", which was the bug',
    ]
    for line in bad:
        check(f"caught: {line}", bool(RELATIVE.search(line.split('#', 1)[0])))
    for line in good:
        check(f"allowed: {line}", not RELATIVE.search(line.split("#", 1)[0]))
    assert not failures, "\n  " + "\n  ".join(failures)


if __name__ == "__main__":
    print("=" * 74)
    print("A DOWNGRADE TARGET IS A REVISION, NOT A DISTANCE FROM HEAD")
    print("=" * 74)
    test_no_migration_check_downgrades_relative_to_head()
    test_the_pattern_catches_what_it_is_for()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED - every downgrade names its revision")
