"""No source file may carry a working credential, and no script may point at
production by default.

WHAT WAS THERE
--------------
`main.py` seeded a real client login for the GMATS tenant:

    db.add(models.User(username="gmats", password=hash_password(<literal>),
                       role="Supervisor", tenant_code="GMATS"))
    log.info("[SEED] GMATS client login (gmats / <literal>)")

A Supervisor credential for a live tenant, in a PUBLIC repository, seeded on
every startup, and written to the application log. `e2e_sim.py` repeated it as a
default alongside `AMP_URL` defaulting to the deployed host, so
`python e2e_sim.py` with no environment set drove state-mutating calls
(`set_status(mid, "Breakdown")`) at production, authenticated with a credential
anyone could read on GitHub.

The standard already existed THREE LINES BELOW the offending seed:

    # Seed a GMATS Admin from env (password never hardcoded — set
    # GMATS_ADMIN_PASSWORD in Railway).

So this was not an unknown rule. It was the exception to a rule the same block
states. That is exactly what a guard is for: a convention nobody has written
down as a test survives only as long as everyone remembers it.

WHAT THIS FILE DOES AND DOES NOT PROVE
---------------------------------------
It proves the SOURCE no longer ships a credential. It does NOT undo the
exposure: the old password is in the git history of a public repository and
must be treated as compromised until it is rotated in production. Rotation needs
production access and is the owner's action, not a code change — the seed's
`if not exists` guard means setting GMATS_PASSWORD does nothing to a database
where the user already exists.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_no_hardcoded_credentials.py
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def source_files():
    """Every tracked-ish .py under backend/, excluding caches and this file."""
    for root, dirs, names in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".venv", "node_modules",
                                                "alembic", ".git")]
        for n in names:
            if n.endswith(".py") and n != os.path.basename(__file__):
                yield os.path.join(root, n)


# A credential-shaped default: NAME = os.environ.get("...", "<non-empty literal>")
# where NAME looks like a secret. Deliberately narrow — the aim is the exact
# shape that shipped, not a general secret scanner, because a noisy guard gets
# disabled and a disabled guard protects nothing.
SECRET_NAME = re.compile(r"(PASS|PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|APIKEY)", re.I)
ENV_DEFAULT = re.compile(
    r"""^\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*os\.environ\.get\(\s*["'][^"']+["']\s*,\s*(?P<default>["'][^"']+["'])\s*\)""",
    re.M)


def main():
    print("=" * 74)
    print("1. NO SOURCE FILE DEFAULTS A SECRET TO A LITERAL")
    print("=" * 74)
    offenders = []
    for path in source_files():
        text = open(path, encoding="utf-8", errors="replace").read()
        for m in ENV_DEFAULT.finditer(text):
            if SECRET_NAME.search(m.group("name")):
                line = text[:m.start()].count("\n") + 1
                offenders.append(f"{os.path.relpath(path, HERE)}:{line} "
                                 f"{m.group('name')}={m.group('default')}")
    check("no secret-shaped env default anywhere in backend/", not offenders,
          "; ".join(offenders))

    print()
    print("=" * 74)
    print("2. THE CREDENTIAL THAT SHIPPED IS GONE FROM SOURCE")
    print("=" * 74)
    # Named explicitly. It is in the git history of a public repo either way, so
    # this asserts only that it is no longer being RE-PUBLISHED on every commit.
    LEAKED = "gmats" + "@2026"          # split so this file is not itself a hit
    hits = []
    for path in source_files():
        text = open(path, encoding="utf-8", errors="replace").read()
        if LEAKED in text:
            line = text[:text.index(LEAKED)].count("\n") + 1
            hits.append(f"{os.path.relpath(path, HERE)}:{line}")
    check("the leaked password appears in no .py file", not hits, "; ".join(hits))

    print()
    print("=" * 74)
    print("3. NO SEED WRITES A PASSWORD INTO THE LOG")
    print("=" * 74)
    # The old seed logged the username and password together on one line. Logs
    # get shipped to third-party aggregators and pasted into issues; a password
    # in one is a password in all of them.
    logged = []
    for path in source_files():
        for i, line in enumerate(open(path, encoding="utf-8", errors="replace"), 1):
            if re.search(r"log\.\w+\(.*(password|passwd)\s*[:=/]", line, re.I):
                logged.append(f"{os.path.relpath(path, HERE)}:{i}")
    check("no log line interpolates a password", not logged, "; ".join(logged))

    print()
    print("=" * 74)
    print("4. e2e_sim DOES NOT AIM AT PRODUCTION BY DEFAULT")
    print("=" * 74)
    sim = open(os.path.join(HERE, "e2e_sim.py"), encoding="utf-8").read()
    m = re.search(r'BASE\s*=\s*os\.environ\.get\(\s*["\']AMP_URL["\']\s*,\s*["\']([^"\']+)["\']', sim)
    check("AMP_URL has a default at all (so a bare run is defined)", m is not None)
    if m:
        default = m.group(1)
        check(f"...and it is local, not a deployed host ({default})",
              default.startswith(("http://localhost", "http://127.0.0.1")), default)
    # BEHAVIOURAL, not textual. The first version of this check was
    # `"AMP_ALLOW_REMOTE" in sim`, and a mutation replacing the whole guard with
    # `if False:` SURVIVED it — the string still appeared in the comment and the
    # error message. A guard asserted by substring is a guard asserted by
    # spelling.
    #
    # The target is 192.0.2.1 (TEST-NET-1, reserved by RFC 5737 and guaranteed
    # not to route). If the guard fires we never open a socket; if it has been
    # broken, the connection attempt goes to a black hole rather than to
    # anyone's production host, and the timeout below reports it as a failure.
    import subprocess
    env = dict(os.environ, AMP_URL="https://192.0.2.1", AMP_PASS="not-a-real-password")
    env.pop("AMP_ALLOW_REMOTE", None)
    try:
        r = subprocess.run(["python", os.path.join(HERE, "e2e_sim.py")], cwd=HERE,
                           capture_output=True, text=True, timeout=25, env=env)
        refused = r.returncode != 0 and "refusing" in (r.stdout + r.stderr).lower()
        detail = (r.stdout + r.stderr).strip().splitlines()[-1:] or ["(no output)"]
    except subprocess.TimeoutExpired:
        refused, detail = False, ["timed out — it tried to connect, so the guard did not fire"]
    check("pointed at a remote host with no opt-in, e2e_sim REFUSES and exits",
          refused, str(detail[-1])[:110])

    env["AMP_ALLOW_REMOTE"] = "1"
    try:
        r2 = subprocess.run(["python", os.path.join(HERE, "e2e_sim.py")], cwd=HERE,
                            capture_output=True, text=True, timeout=25, env=env)
        opted_past = "refusing" not in (r2.stdout + r2.stderr).lower()
    except subprocess.TimeoutExpired:
        opted_past = True      # it got past the guard and tried to connect
    check("...and AMP_ALLOW_REMOTE is what lets a deliberate run through",
          opted_past, "the opt-in did not work, so the guard cannot be bypassed on purpose")
    check("AMP_PASS has no default, so it cannot authenticate with a committed secret",
          re.search(r'PASS\s*=\s*os\.environ\.get\(\s*["\']AMP_PASS["\']\s*\)', sim) is not None)

    print()
    print("=" * 74)
    print("5. THE SEED IS ENV-DRIVEN, LIKE THE ADMIN SEED BESIDE IT")
    print("=" * 74)
    mn = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    check("the Supervisor seed reads GMATS_PASSWORD", "GMATS_PASSWORD" in mn)
    check("...and creates nothing when it is unset",
          re.search(r"if\s+gmats_pw\s+and\s+not\s+db\.query", mn) is not None)
    check("the Admin seed still reads GMATS_ADMIN_PASSWORD (unchanged)",
          "GMATS_ADMIN_PASSWORD" in mn)

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
        print()
        print("NOTE: this proves the SOURCE is clean. The old password remains in")
        print("the git history of a PUBLIC repository and must be rotated in")
        print("production before it can be considered dead. That is an owner")
        print("action — the seed's `if not exists` guard means setting")
        print("GMATS_PASSWORD does nothing where the user already exists.")
    print("=" * 74)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
