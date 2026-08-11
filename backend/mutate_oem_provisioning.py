"""Mutation harness for OEM onboarding and the manufacturer's front door.

A login route and a user-creation route are where multi-tenant systems classically
leak, and in ways that look like working software:

    a session that authenticates but is the WRONG KIND
    a user-creation route that honours an oem_code from the BODY
    a founder gate that a customer Admin also satisfies
    a lockout guard that cannot fire

That last one is not hypothetical here. The first version of the lockout logic
had a "you cannot demote yourself" rule AND an organisation-level rule, and the
second could never execute — any caller with `manage_users` who is not the target
is themselves a remaining administrator. It was deleted rather than kept as
decoration, and the mutation below now proves the surviving rule is load-bearing.

RUN IT ALONE. Like every mutate_* harness, this one EDITS THE WORKING TREE and
restores it afterwards, so anything else reading those files meanwhile sees a
deliberately broken codebase. Running it beside a full test sweep produced a
confusing "failure" in an unrelated suite that was simply the sweep catching
`if existing is not None:` while it was mutated to `if False:`.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_oem_provisioning.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_oem_provisioning.py", "test_oem_authorization.py",
          "test_oem_routes.py", "test_saas_routes.py"]

MUTATIONS = [
    # --- the front door hands out the wrong kind of session -----------------
    ("the OEM login omits principal=oem (an inert, factory-shaped token)",
     "oem_routes.py",
     '    token = create_access_token({"sub": user.username, "role": user.role,\n'
     '                                 "principal": "oem", "oem": user.oem_code})',
     '    token = create_access_token({"sub": user.username, "role": user.role,\n'
     '                                 "oem": user.oem_code})'),
    ("the OEM login issues a token for a DIFFERENT organisation", "oem_routes.py",
     '                                 "principal": "oem", "oem": user.oem_code})',
     '                                 "principal": "oem", "oem": "OEM_RIVAL"})'),
    ("a wrong password is distinguishable from an unknown user", "oem_routes.py",
     "    if user is None or not verify_password(payload.password, user.password):\n"
     '        raise HTTPException(status_code=401, detail="Invalid username or password")',
     "    if user is None:\n"
     '        raise HTTPException(status_code=401, detail="No such user")\n'
     "    if not verify_password(payload.password, user.password):\n"
     '        raise HTTPException(status_code=401, detail="Wrong password")'),
    ("a disabled account can still sign in", "oem_routes.py",
     "    if not user.is_active:\n"
     '        raise HTTPException(status_code=403, detail="This account has been disabled")',
     "    if False:\n"
     '        raise HTTPException(status_code=403, detail="This account has been disabled")'),
    ("a SUSPENDED organisation can still sign in", "oem_routes.py",
     "    if not org.is_active:\n"
     '        raise HTTPException(status_code=403, detail="This organisation has been suspended")',
     "    if False:\n"
     '        raise HTTPException(status_code=403, detail="This organisation has been suspended")'),

    # --- user creation crosses the organisation boundary --------------------
    # TWO edits, because one cannot express this bug. Pydantic drops unknown
    # fields, so `payload.oem_code` does not exist unless the MODEL declares it —
    # the single-edit version of this mutation SURVIVED for that reason, which is
    # a mutation that cannot fail rather than a guard that works.
    ("a new user is placed by an oem_code from the REQUEST BODY", "oem_routes.py",
     [("class OemUserCreate(BaseModel):\n    username: str\n    role: str",
       "class OemUserCreate(BaseModel):\n    username: str\n    role: str\n"
       "    oem_code: str | None = None"),
      ('    user = models.OemUser(oem_code=principal["oem"], username=username,',
       '    user = models.OemUser(oem_code=payload.oem_code or principal["oem"], '
       "username=username,")],
     None),
    ("the roster is not filtered by manufacturer", "oem_routes.py",
     "    rows = (db.query(models.OemUser)\n"
     '              .filter(models.OemUser.oem_code == principal["oem"])\n'
     "              .order_by(models.OemUser.id.asc()).all())",
     "    rows = (db.query(models.OemUser)\n"
     "              .order_by(models.OemUser.id.asc()).all())"),
    ("a competitor's user can be edited", "oem_routes.py",
     "    user = (db.query(models.OemUser)\n"
     '              .filter(models.OemUser.oem_code == principal["oem"],\n'
     "                      models.OemUser.id == user_id).first())",
     "    user = (db.query(models.OemUser)\n"
     "              .filter(models.OemUser.id == user_id).first())"),
    ("any role string is accepted, including a FACTORY role", "oem_routes.py",
     "    if payload.role not in oem_auth.OEM_ROLES:\n"
     "        raise HTTPException(\n"
     "            status_code=400,\n"
     '            detail=f"Unknown role. Choose one of: {\', \'.join(oem_auth.OEM_ROLES)}")\n'
     "\n"
     "    username = (payload.username or \"\").strip()",
     "    if False:\n"
     "        raise HTTPException(\n"
     "            status_code=400,\n"
     '            detail=f"Unknown role. Choose one of: {\', \'.join(oem_auth.OEM_ROLES)}")\n'
     "\n"
     "    username = (payload.username or \"\").strip()"),

    # --- the organisation locks itself out ----------------------------------
    ("the last administrator can be demoted", "oem_routes.py",
     "        if remaining == 0:", "        if False:"),

    # --- the username namespace ---------------------------------------------
    ("a manufacturer account may take a FACTORY login's name", "oem_auth.py",
     "    if db.query(models.User).filter(models.User.username == name).first():",
     "    if False:"),

    # --- the code that becomes the sentinel ---------------------------------
    ("any manufacturer code is accepted, including one with a colon",
     "oem_auth.py",
     "    if not _OEM_CODE.match(text):", "    if False:"),

    # --- the founder gate ----------------------------------------------------
    ("a customer Admin satisfies the founder gate", "oem_admin_routes.py",
     '    if (current_user or {}).get("tenant") != DEFAULT_TENANT:',
     "    if False:"),
    ("the founder can mint unlimited logins into a manufacturer",
     "oem_admin_routes.py",
     "    if existing is not None:", "    if False:"),

    # --- password handling ---------------------------------------------------
    ("the temporary password is stored in the clear", "oem_admin_routes.py",
     "                          password=hash_password(temp_password),",
     "                          password=temp_password,"),
    ("changing a password does not check the current one", "oem_routes.py",
     "    if user is None or not verify_password(payload.current_password, user.password):\n"
     '        raise HTTPException(status_code=401, detail="Current password is incorrect")',
     "    if user is None:\n"
     '        raise HTTPException(status_code=401, detail="Current password is incorrect")'),
]


def run_suites():
    here = os.path.dirname(os.path.abspath(__file__))
    failed = []
    for suite in SUITES:
        proc = subprocess.run([sys.executable, suite], capture_output=True,
                              text=True, errors="replace", cwd=here)
        if proc.returncode != 0:
            failed.append(suite)
    return failed


EXPECTED_SURVIVORS = {}


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    originals = {}
    for _, path, _, _ in MUTATIONS:
        if path not in originals:
            originals[path] = io.open(os.path.join(here, path),
                                      encoding="utf-8").read()

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
        # A mutation may be a single (old, new) or a LIST of (old, new) edits.
        # Some faithful mutations need two: adding a field to a request model is
        # meaningless unless a handler also reads it, and Pydantic SILENTLY DROPS
        # unknown fields — so a one-line "read payload.oem_code" mutation is a
        # no-op that SURVIVES for a reason that has nothing to do with the guard.
        # Found exactly that way.
        edits = old if isinstance(old, list) else [(old, new)]
        counts = [source.count(o) for o, _ in edits]
        if any(c != 1 for c in counts):
            print(f"{label:<66} {'SKIP':<10} pattern hits {counts} in {path}")
            survived.append(f"{label} (pattern did not apply)")
            continue
        mutated = source
        for o, n in edits:
            mutated = mutated.replace(o, n, 1)
        io.open(os.path.join(here, path), "w", encoding="utf-8",
                newline="\n").write(mutated)
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8",
                    newline="\n").write(source)
        if failing:
            verdict, note = "caught", ", ".join(
                s.replace("test_", "").replace(".py", "")[:24] for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label][:60] + "..."
        else:
            verdict, note = "SURVIVED", "-- nothing --"
        print(f"{label:<66} {verdict:<10} {note}")
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
