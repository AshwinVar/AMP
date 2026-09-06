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
    print("6. NO STARTUP-REACHABLE SEEDER CREATES A USER FROM A LITERAL")
    print("=" * 74)
    # The GMATS defect precisely. main.py's startup block calls several seeders;
    # a literal password in ANY of them ships a credential the same way. AST, not
    # a regex, so a password inside a comment or docstring cannot trip it and a
    # multi-line call cannot hide from it.
    import ast
    STARTUP_SEEDERS = ["main.py", "demo_aeron.py", "gmats_inventory_routes.py",
                       "platform_routes.py", "industrial_adapters.py",
                       "factory_simulator.py", "reset_factory.py", "onboard_tenant.py"]
    literal_seeds = []
    for fname in STARTUP_SEEDERS:
        path = os.path.join(HERE, fname)
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "User"):
                continue
            for kw in node.keywords:
                if kw.arg != "password":
                    continue
                v = kw.value
                # password=hash_password(<literal>)  ->  a shipped credential
                if (isinstance(v, ast.Call) and isinstance(v.args, list) and v.args
                        and isinstance(v.args[0], ast.Constant)
                        and isinstance(v.args[0].value, str)):
                    literal_seeds.append(f"{fname}:{node.lineno}")
                # password=<literal>  ->  worse, and unhashed
                elif isinstance(v, ast.Constant) and isinstance(v.value, str):
                    literal_seeds.append(f"{fname}:{node.lineno} (unhashed!)")
    check(f"none of the {len(STARTUP_SEEDERS)} startup seeders hardcodes a password",
          not literal_seeds, "; ".join(literal_seeds))

    print()
    print("=" * 74)
    print("7. NO MODULE AIMS AT A DEPLOYED HOST BY DEFAULT")
    print("=" * 74)
    # e2e_sim's other half, generalised. A script whose default target is a
    # deployed host runs against production for anyone who forgets a variable —
    # and these scripts mutate state.
    URL_DEFAULT = re.compile(
        r"""^\s*(?:BASE|URL|AMP_URL|HOST|ENDPOINT|API)\w*\s*=\s*(?:os\.environ\.get\(\s*["'][^"']+["']\s*,\s*)?f?["'](https?://[^"']+)["']""",
        re.M)
    remote = []
    for path in source_files():
        text = open(path, encoding="utf-8", errors="replace").read()
        for m in URL_DEFAULT.finditer(text):
            url = m.group(1)
            if url.startswith(("http://localhost", "http://127.0.0.1")):
                continue
            if "{" in url:          # an f-string built from a port/host variable
                continue
            line = text[:m.start()].count("\n") + 1
            remote.append(f"{os.path.relpath(path, HERE)}:{line} -> {url}")
    check("no default target outside localhost", not remote, "; ".join(remote))

    print()
    print("=" * 74)
    print("8. THE PROCESS GUARDS EXIST AND SAY WHAT THEY MUST")
    print("=" * 74)
    # Both of these exist because of a specific incident. A guard file that has
    # been emptied or renamed is a guard that is gone, and nothing else would
    # notice.
    hook = os.path.join(os.path.dirname(HERE), ".githooks", "pre-commit")
    check("the pre-commit hook is committed", os.path.exists(hook), hook)
    if os.path.exists(hook):
        h = open(hook, encoding="utf-8").read()
        check("...it refuses commits on master/main", "master|main)" in h)
        check("...it refuses a detached HEAD", "HEAD)" in h)
        check("...it refuses a staged secret literal",
              "secret-shaped name a literal" in h)
        check("...and it does NOT echo the matching line",
              "NOT printed here" in h)
    guard = os.path.join(HERE, "tree_guard.py")
    check("tree_guard.py is committed", os.path.exists(guard), guard)
    if os.path.exists(guard):
        g = open(guard, encoding="utf-8").read()
        check("...it offers snapshot and verify", "def snapshot" in g and "def verify" in g)
        check("...and it points at worktree isolation as the primary control",
              'isolation: "worktree"' in g)

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
        print()
        print("NOTE: this proves the SOURCE is clean. It says nothing about the")
        print("live database, and on 2026-09-06 those were not the same thing:")
        print("the old password STILL AUTHENTICATED against production, returning")
        print("a token with role=Admin, after a rotation was believed done.")
        print()
        print("Setting GMATS_PASSWORD cannot rotate anything — the seed's")
        print("`if not exists` guard makes it a no-op wherever the user already")
        print("exists, which is every deployed environment. Rotation has to change")
        print("the stored hash on the row. Re-verify against /login, not against")
        print("this file.")
    print("=" * 74)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
