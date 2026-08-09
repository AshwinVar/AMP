"""Mutation harness for OEM authorization (ADR-0017).

Every assertion in test_oem_authorization is "this was refused" or "this saw
nothing", and both are equally produced by the guard under test, by a different
guard, or by an empty fixture. Removing each guard one at a time is the only way
to learn which one is holding.

The most important row is the LAST one: replacing the sentinel with None. That
single change is the difference between an OEM seeing nothing and an OEM seeing
every customer's factory, and it must be caught loudly.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_oem_auth.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_oem_authorization.py", "test_oem_foundation.py",
          "test_tenancy.py", "test_tenant_isolation_http.py"]

MUTATIONS = [
    # --- the sentinel: the keystone ----------------------------------------
    ("THE KEYSTONE: an OEM binds None instead of the sentinel", "oem_auth.py",
     '    return f"{SENTINEL_PREFIX}{oem_code}"',
     "    return None"),
    ("the sentinel collides with a real tenant code", "oem_auth.py",
     '    return f"{SENTINEL_PREFIX}{oem_code}"',
     "    return oem_code"),
    ("every OEM shares one sentinel", "oem_auth.py",
     '    return f"{SENTINEL_PREFIX}{oem_code}"',
     '    return SENTINEL_PREFIX + "ALL"'),
    ("the middleware stops binding the sentinel at all", "tenancy.py",
     "    oem_code = oem_auth.oem_of_claims(claims)\n"
     "    if oem_code:\n"
     "        return oem_auth.sentinel_tenant(oem_code)",
     "    oem_code = None\n"
     "    if oem_code:\n"
     "        return oem_auth.sentinel_tenant(oem_code)"),
    ("the OEM branch runs AFTER the founder preview (X-Tenant wins)",
     "tenancy.py",
     "    oem_code = oem_auth.oem_of_claims(claims)\n"
     "    if oem_code:\n"
     "        return oem_auth.sentinel_tenant(oem_code)\n"
     '    if claim_tenant == DEFAULT_TENANT and header_tenant and claim_role == "Admin":\n'
     "        return header_tenant",
     '    if claim_tenant == DEFAULT_TENANT and header_tenant and claim_role == "Admin":\n'
     "        return header_tenant\n"
     "    oem_code = oem_auth.oem_of_claims(claims)\n"
     "    if oem_code:\n"
     "        return oem_auth.sentinel_tenant(oem_code)"),

    # --- who the token is ---------------------------------------------------
    ("a factory token is accepted as an OEM session", "oem_auth.py",
     '    if claims.get("principal") != "oem":\n        return None',
     '    if False:\n        return None'),
    ("the oem claim is taken without a type check", "oem_auth.py",
     "    return code if isinstance(code, str) and code else None",
     "    return code"),
    ("a token naming no user is allowed through", "oem_auth.py",
     '    if not isinstance(username, str) or not username:\n'
     '        raise OemDenied(401, "Token names no user")',
     '    if False:\n        raise OemDenied(401, "Token names no user")'),
    ("the OEM user is never looked up (the token is believed)", "oem_auth.py",
     "    user = (db.query(models.OemUser)\n"
     "              .filter(models.OemUser.username == username).first())",
     "    user = models.OemUser(username=username, oem_code=oem_code,\n"
     '                          role=claims.get("role"), is_active=True)'),
    ("a DELETED OEM user is accepted", "oem_auth.py",
     '    if user is None:\n        raise OemDenied(401, "This account no longer exists")',
     '    if user is None:\n        pass'),
    ("a DISABLED OEM user is accepted", "oem_auth.py",
     '    if not user.is_active:\n'
     '        raise OemDenied(403, "This account has been disabled")',
     '    if False:\n        raise OemDenied(403, "This account has been disabled")'),
    ("the user's own OEM is not compared to the claim", "oem_auth.py",
     '    if (user.oem_code or "") != oem_code:',
     "    if False:"),
    ("the role is read from the TOKEN, not the database", "oem_auth.py",
     "    if user.role not in OEM_ROLES:",
     '    if claims.get("role") not in OEM_ROLES:'),
    ("a factory role satisfies the OEM role check", "oem_auth.py",
     'OEM_ROLES = (OEM_ADMIN, OEM_SERVICE_MANAGER, OEM_SERVICE_ENGINEER, OEM_VIEWER)',
     'OEM_ROLES = (OEM_ADMIN, OEM_SERVICE_MANAGER, OEM_SERVICE_ENGINEER,\n'
     '             OEM_VIEWER, "Admin", "Supervisor", "Operator")'),

    # --- the organisation ---------------------------------------------------
    ("a suspended organisation still authenticates", "oem_auth.py",
     '    if not org.is_active:\n'
     '        raise OemDenied(403, "This organisation has been suspended")',
     '    if False:\n'
     '        raise OemDenied(403, "This organisation has been suspended")'),
    ("an unknown organisation still authenticates", "oem_auth.py",
     '    if org is None:\n'
     '        raise OemDenied(403, "This organisation no longer exists")',
     '    if org is None:\n        pass'),

    # --- capabilities -------------------------------------------------------
    ("a viewer gains every capability", "oem_auth.py",
     '    OEM_VIEWER: {"read_fleet"},',
     '    OEM_VIEWER: {"read_fleet", "manage_users", "manage_branding",\n'
     '                 "manage_service", "commission", "manage_models",\n'
     '                 "manage_installations"},'),
]


def run_suites():
    failed = []
    for suite in SUITES:
        proc = subprocess.run([sys.executable, suite], capture_output=True,
                              text=True, errors="replace",
                              cwd=os.path.dirname(os.path.abspath(__file__)))
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
            io.open(os.path.join(here, path), "w", encoding="utf-8",
                    newline="\n").write(source)
        if failing:
            verdict, note = "caught", ", ".join(
                s.replace("test_", "").replace(".py", "")[:22] for s in failing)
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
