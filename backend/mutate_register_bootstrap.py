"""Mutation harness for the first-account registration rule (POST /register).

Each mutation is a small, plausible edit that brings the ignored sign-up role
back, or opens registration past the first account: the schema accepting extra
keys again, the handler writing a role other than Admin, the "any user exists"
guard removed, the tenant defaulting elsewhere. For each the harness applies
the edit, runs the suite that is supposed to notice, and restores the file byte
for byte. A mutation that leaves the suite green SURVIVED: that guard is
untested.

RUN IT ALONE. Like every mutate_* harness it EDITS THE WORKING TREE and restores
it afterwards; anything reading those files meanwhile sees broken code.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_register_bootstrap.py
Exit 0 only if every mutation applied exactly once and was caught.
"""
import io
import os
import subprocess
import sys

SUITES = ["test_register_first_account_only.py"]
SUITE_TIMEOUT = 300

SCHEMAS = "schemas.py"
CORE = "core_routes.py"

# (label, file, old, new)
MUTATIONS = [
    ("the schema accepts (and drops) a role again", SCHEMAS,
     '    model_config = ConfigDict(extra="forbid")\n    username: str\n    password: str\n',
     '    model_config = ConfigDict(extra="ignore")\n    username: str\n    password: str\n'),
    ("the handler takes the Admin's UserCreate schema again", CORE,
     "def register_user(user: schemas.RegisterRequest, db: Session = Depends(_get_db)):",
     "def register_user(user: schemas.UserCreate, db: Session = Depends(_get_db)):"),
    ("the first account is not the Admin", CORE,
     '            role="Admin",            # the first account is always the Admin',
     '            role="Operator",         # the first account is always the Admin'),
    ("registration stays open after the first account", CORE,
     "    if db.query(models.User).count() > 0:\n        raise HTTPException(status_code=403,",
     "    if False:\n        raise HTTPException(status_code=403,"),
    ("the first account lands in a tenant that is not DEFAULT", CORE,
     '            tenant_code="DEFAULT",\n        )\n        db.add(new_user)',
     '            tenant_code="FIRST",\n        )\n        db.add(new_user)'),
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
    print(f"{'mutation':<62} {'verdict':<10} caught by")
    print("-" * 104)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<62} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
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
        print(f"{label:<62} {verdict:<10} {note}", flush=True)
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
