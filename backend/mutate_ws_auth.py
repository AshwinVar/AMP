"""Mutation harness for live WebSocket authentication (ADR-0016).

Every assertion in test_live_ws_auth.py section 1 has the form "this connection
was refused", and a refusal is equally produced by the check under test, by a
different check, or by a fixture that never built a valid token in the first
place. Removing each check one at a time is the only way to learn which one is
holding.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_ws_auth.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_live_ws_auth.py", "test_live_ws.py", "test_core_routes.py"]

MUTATIONS = [
    # --- is there a credential at all --------------------------------------
    ("a missing token is allowed through", "ws_auth.py",
     '    if not token:\n        raise WsDenied(NEEDS_AUTH, "A token is required")',
     '    if not token:\n        pass'),
    ("an invalid signature is allowed through", "ws_auth.py",
     '    except JWTError:\n        raise WsDenied(NEEDS_AUTH, "Invalid token")',
     '    except JWTError:\n        return {}'),
    ("expiry is not enforced", "ws_auth.py",
     "        return jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])",
     "        return jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM],\n"
     "                          options={'verify_exp': False})"),
    ("an expired token is reported as REFUSED, so the client stops retrying",
     "ws_auth.py",
     '        raise WsDenied(NEEDS_AUTH, "Token expired")',
     '        raise WsDenied(REFUSED, "Token expired")'),

    # --- who the credential names -------------------------------------------
    ("a token naming no user is allowed through", "ws_auth.py",
     '    if not username:\n        raise WsDenied(REFUSED, "Token names no user")',
     '    if not username:\n        username = "anonymous"'),
    ("a token with no tenant claim is allowed through", "ws_auth.py",
     '    if not claimed:\n        raise WsDenied(REFUSED, "Token names no workspace")',
     '    if not claimed:\n        pass'),
    ("the user is never looked up (the token is believed)", "ws_auth.py",
     "    user = db.query(models.User).filter(models.User.username == username).first()",
     "    user = models.User(username=username, tenant_code=claimed, is_active=True)"),
    ("a DELETED user is allowed through", "ws_auth.py",
     '    if user is None:\n        raise WsDenied(REFUSED, "This account no longer exists")',
     '    if user is None:\n        pass'),
    ("a DISABLED user is allowed through", "ws_auth.py",
     '    if not user.is_active:\n        raise WsDenied(REFUSED, "This account has been disabled")',
     '    if not user.is_active:\n        pass'),

    # --- which tenant the connection is bound to -----------------------------
    ("the claimed tenant is not checked against the account", "ws_auth.py",
     "    if tenant != claimed:",
     "    if False:"),
    ("the tenant is taken from the TOKEN instead of the account", "ws_auth.py",
     '    tenant = user.tenant_code or ""',
     '    tenant = claimed'),
    # --- the endpoint actually calls the gate --------------------------------
    ("the endpoint stops authenticating", "main.py",
     '        tenant = ws_auth.resolve(db, websocket.query_params.get("token"))',
     '        tenant = tenancy.tenant_from_token(websocket.query_params.get("token"))'),
    ("a refusal accepts the socket first", "main.py",
     "        await websocket.close(code=denied.code, reason=denied.reason)\n"
     '        log.info("WebSocket refused (%s): %s", denied.code, denied.reason)\n'
     "        return",
     "        await websocket.accept()\n"
     "        await websocket.close(code=denied.code, reason=denied.reason)\n"
     "        return"),
    ("a client frame is ignored rather than refused", "main.py",
     "                await websocket.close(code=ws_auth.NO_CLIENT_FRAMES,\n"
     '                                      reason="This feed accepts no client messages")\n'
     "                break",
     "                continue"),

    # --- the manager's own guards --------------------------------------------
    ("the manager binds a connection with no tenant", "live_ws.py",
     '        if not tenant:\n            log.info("WebSocket refused: no tenant bound")\n'
     "            return False",
     "        if False:\n            return False"),
    ("a payload with no tenant is broadcast to everyone matching None",
     "live_ws.py",
     '        if not target:\n',
     '        if False:\n'),
    ("the broadcast filter is dropped entirely", "live_ws.py",
     "            if tenant != target:\n                continue  # only same-tenant connections receive this payload",
     "            if False:\n                continue  # only same-tenant connections receive this payload"),
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


# A mutation here is one a DIFFERENT guard already covers. Each needs a reason
# that survives reading, not an excuse.
EXPECTED_SURVIVORS = {
    "a payload with no tenant is broadcast to everyone matching None":
        "shadowed by the connect guard: no connection is bound to None any "
        "more, so nothing matches a tenant-less payload either way. The two "
        "guards defend different invariants (who may register vs what may be "
        "sent), and only a caller appending to active_connections directly -- "
        "which nothing does -- could tell them apart.",
}


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
                s.replace("test_", "").replace(".py", "")[:20] for s in failing)
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
