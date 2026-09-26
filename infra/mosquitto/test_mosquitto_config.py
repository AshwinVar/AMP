"""The ACL is the only thing standing between one gateway and every tenant.

backend/mqtt_identity.py is built on a single load-bearing sentence: the topic
is "a claim the BROKER has already checked". Every guarantee downstream of it —
that FACTORY_B's telemetry cannot land in FACTORY_A's rows, that a compromised
gateway can lie about a machine but not about a customer — rests on the ACL
that entrypoint.sh renders. Nothing else in AMP checks it, because by the time
a message reaches AMP the check is supposed to have happened.

So this suite exists to answer one question: can any value a human might put
into a Railway environment variable produce an ACL that grants more than the
one topic it was meant to?

What is pinned:

  * a gateway's write grant is EXACTLY `{prefix}/{tenant}/{site}/machines` —
    one tenant, one site, one topic
  * '+' and '#' in a tenant or site STOP THE CONTAINER. They are the whole
    attack: `topic write flowmes/+/PLANT1/machines` is every customer at once,
    and it is one character away from correct
  * the ACL has no topic line outside a user block — that is how mosquitto
    spells "everyone, including anonymous", and its absence is what makes the
    file default-deny
  * AMP's own subscriber can READ every tenant and WRITE nothing
  * a missing password stops the container rather than rendering a user who
    cannot be authenticated
  * the identifier charset has not drifted from backend/mqtt_identity.py

What it cannot prove: that mosquitto enforces the file the way the file reads.
There is no broker here and no Docker in this environment. The ACL grammar is
taken from mosquitto's documentation, and the claim "this file denies X" is
verified against a real broker in the commissioning steps in README.md, not
here. That distinction is the point of this paragraph.

Run: python infra/mosquitto/test_mosquitto_config.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
ENTRYPOINT = os.path.join(HERE, "entrypoint.sh")

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def find_sh():
    """A POSIX shell. Present on CI; on Windows it ships with Git."""
    found = shutil.which("sh") or shutil.which("bash")
    if found:
        return found
    for guess in (r"C:\Program Files\Git\usr\bin\sh.exe",
                  r"C:\Program Files\Git\bin\sh.exe"):
        if os.path.exists(guess):
            return guess
    return None


SH = find_sh()
if SH is None:
    print("NO POSIX SHELL FOUND — cannot exercise entrypoint.sh.")
    print("This suite tests a shell script; skipping it would be reporting a")
    print("pass nobody earned, so it fails instead. Install Git for Windows or")
    print("run it on CI.")
    sys.exit(1)


# A stand-in for mosquitto_passwd, which is not installed outside the image.
# It records WHICH USER was written and with which flags, and deliberately
# never records the password -- a test fixture that writes credentials to a
# temp file is a test fixture that leaks them into CI logs.
FAKE_PASSWD = """#!/bin/sh
last=""
out=""
for a in "$@"; do
    out="$out $last"
    last="$a"
done
echo "$out" >> "$MOSQUITTO_PASSWD_LOG"
"""


def render(env, expect_ok=True):
    """Run entrypoint.sh --render-only in a throwaway config dir.

    Returns (returncode, acl_text, passwd_log, stderr).
    """
    workdir = tempfile.mkdtemp(prefix="amp-mosq-")
    fake = os.path.join(workdir, "fake_mosquitto_passwd")
    with open(fake, "w", newline="\n") as fh:
        fh.write(FAKE_PASSWD)
    os.chmod(fake, 0o755)
    log = os.path.join(workdir, "passwd.log")

    full = dict(os.environ)
    # Strip anything the developer's own shell might already be exporting, so a
    # local run and a CI run see the same environment.
    for key in list(full):
        if key.startswith("GATEWAY_") or key.startswith("AMP_BACKEND_") \
                or key.startswith("MQTT_"):
            del full[key]
    full.update({
        "AMP_MQTT_CONFIG_DIR": workdir,
        "MOSQUITTO_PASSWD_BIN": fake,
        "MOSQUITTO_PASSWD_LOG": log,
    })
    full.update({k: str(v) for k, v in env.items()})

    proc = subprocess.run([SH, ENTRYPOINT, "--render-only"],
                          env=full, capture_output=True, text=True, timeout=60)
    acl = ""
    acl_path = os.path.join(workdir, "acl")
    if os.path.exists(acl_path):
        with open(acl_path) as fh:
            acl = fh.read()
    passwd_log = ""
    if os.path.exists(log):
        with open(log) as fh:
            passwd_log = fh.read()
    if expect_ok and proc.returncode != 0:
        print("    (render failed unexpectedly)", proc.stderr.strip()[:300])
    return proc.returncode, acl, passwd_log, proc.stderr


def topic_lines(acl):
    """Every `topic ...` line, paired with the user block it sits in."""
    user = None
    out = []
    for raw in acl.splitlines():
        line = raw.strip()
        if line.startswith("#") or not line:
            continue
        if line.startswith("user "):
            user = line.split(None, 1)[1]
        elif line.startswith("topic "):
            out.append((user, line))
    return out


ONE_GATEWAY = {
    "AMP_BACKEND_PASSWORD": "backend-secret",
    "GATEWAY_1_USER": "gw-acme-plant1",
    "GATEWAY_1_PASSWORD": "gateway-secret",
    "GATEWAY_1_TENANT": "ACME",
    "GATEWAY_1_SITE": "PLANT1",
}


# ── 1. the grant is exactly one topic ──────────────────────────────────
section("1. A gateway may publish to its own topic and nowhere else")

rc, acl, log, err = render(ONE_GATEWAY)
check("rendering succeeds for a well-formed gateway", rc == 0, err.strip()[:200])

pairs = topic_lines(acl)
gw = [(u, t) for u, t in pairs if u == "gw-acme-plant1"]
check("the gateway gets exactly one topic grant", len(gw) == 1, str(gw))
check("...and it is write on its own tenant and site",
      gw == [("gw-acme-plant1", "topic write flowmes/ACME/PLANT1/machines")], str(gw))
check("the gateway is granted no read at all",
      not any("read" in t for _, t in gw), str(gw))

# The wildcard is what a hand-written ACL reaches for when the author is tired.
check("no gateway grant contains an MQTT wildcard",
      not any(("+" in t or "#" in t) for u, t in pairs if u != "amp-backend"),
      str(pairs))


# ── 2. AMP's subscriber reads everything and writes nothing ────────────
section("2. AMP's own credential is read-only")

backend = [(u, t) for u, t in pairs if u == "amp-backend"]
check("the backend reads the multi-tenant wildcard",
      ("amp-backend", "topic read flowmes/+/+/machines") in backend, str(backend))
check("the backend is granted no write anywhere",
      not any("write" in t for _, t in backend), str(backend))
check("the legacy untenanted topic is NOT granted when no legacy tenant is set",
      not any(t.endswith("flowmes/machines") for _, t in backend), str(backend))

rc2, acl2, _, _ = render(dict(ONE_GATEWAY, MQTT_LEGACY_TENANT="GMATS"))
legacy = [(u, t) for u, t in topic_lines(acl2) if u == "amp-backend"]
check("...and IS granted, read-only, once one is",
      ("amp-backend", "topic read flowmes/machines") in legacy, str(legacy))


# ── 3. default deny ────────────────────────────────────────────────────
section("3. The file denies anything it does not name")

orphan = [(u, t) for u, t in pairs if u is None]
check("no topic line sits outside a user block",
      orphan == [], f"these apply to EVERYONE including anonymous: {orphan}")
check("no pattern/anonymous grants are emitted",
      "pattern " not in acl and "topic readwrite" not in acl, acl[:300])


# ── 4. the attack: a wildcard in a tenant or site ──────────────────────
section("4. A wildcard in a tenant or site stops the container")

ATTACKS = [
    ("GATEWAY_1_TENANT", "+", "one character from correct, and it is every tenant"),
    ("GATEWAY_1_TENANT", "#", "the multi-level wildcard"),
    ("GATEWAY_1_TENANT", "ACME/../OTHER", "a separator smuggled into a segment"),
    ("GATEWAY_1_TENANT", "", "empty collapses the segment"),
    ("GATEWAY_1_TENANT", "ACME PLANT", "a space splits the topic line"),
    ("GATEWAY_1_SITE", "+", "the site segment is a segment too"),
    ("GATEWAY_1_SITE", "#", "..."),
    ("GATEWAY_1_SITE", "A/B", "..."),
]
for var, value, why in ATTACKS:
    rc_a, acl_a, _, err_a = render(dict(ONE_GATEWAY, **{var: value}), expect_ok=False)
    refused = rc_a != 0 and "REFUSING TO START" in err_a
    check(f"{var}={value!r} is refused  ({why})", refused,
          f"rc={rc_a} acl={acl_a!r}")
    # The half-written file does not matter on its own -- the container exits,
    # so mosquitto never loads it. What matters is that the refused value never
    # became a GRANT. (Checking `value not in acl` instead would be checking
    # nothing: '#' opens every comment line and '+' is in the backend's own
    # legitimate wildcard.)
    granted = [t for _, t in topic_lines(acl_a) if t.startswith("topic write")]
    check(f"...and no write grant was emitted for {value!r}",
          granted == [], str(granted))

# The username reaches the passwd file rather than a topic line, but a newline
# or a colon in it would corrupt that file's format.
for bad in ("gw:one", "gw\nuser", "-leading-dash"):
    rc_u, _, _, err_u = render(dict(ONE_GATEWAY, GATEWAY_1_USER=bad), expect_ok=False)
    check(f"a username of {bad!r} is refused",
          rc_u != 0 and "REFUSING TO START" in err_u, f"rc={rc_u}")

# AMP's OWN username is written into a `user` line by the same code path, and a
# mutation run caught that nothing here was testing it: deleting its validation
# left every check passing. It is the more dangerous of the two, because the
# line it can inject sits above the gateway blocks.
# An EMPTY value is deliberately not in this list: `${AMP_BACKEND_USER:-amp-backend}`
# reads empty as unset and falls back to the default, which is the behaviour we want.
for bad in ("amp\ntopic readwrite flowmes/+/+/machines", "amp:x", "-amp"):
    rc_s, acl_s2, _, err_s = render(dict(ONE_GATEWAY, AMP_BACKEND_USER=bad),
                                    expect_ok=False)
    check(f"AMP_BACKEND_USER of {bad!r} is refused",
          rc_s != 0 and "REFUSING TO START" in err_s, f"rc={rc_s}")
    check(f"...and injected no grant via {bad!r}",
          "readwrite" not in acl_s2, acl_s2[:200])


# ── 5. a missing password is a refusal, not a silent user ──────────────
section("5. Nothing starts without the credentials it claims to enforce")

rc_b, _, _, err_b = render({"GATEWAY_1_USER": "gw-x", "GATEWAY_1_PASSWORD": "p",
                            "GATEWAY_1_TENANT": "ACME"}, expect_ok=False)
check("no AMP_BACKEND_PASSWORD refuses to start",
      rc_b != 0 and "AMP_BACKEND_PASSWORD" in err_b, f"rc={rc_b} {err_b[:160]}")

rc_g, _, _, err_g = render({"AMP_BACKEND_PASSWORD": "x", "GATEWAY_1_USER": "gw-x",
                            "GATEWAY_1_TENANT": "ACME"}, expect_ok=False)
check("a gateway with a user but no password refuses to start",
      rc_g != 0 and "GATEWAY_1_PASSWORD" in err_g, f"rc={rc_g} {err_g[:160]}")

rc_t, _, _, err_t = render({"AMP_BACKEND_PASSWORD": "x", "GATEWAY_1_USER": "gw-x",
                            "GATEWAY_1_PASSWORD": "p"}, expect_ok=False)
check("a gateway with no tenant refuses to start",
      rc_t != 0 and "GATEWAY_1_TENANT" in err_t, f"rc={rc_t} {err_t[:160]}")


# ── 6. the no-site spelling, and several gateways ──────────────────────
section("6. The '-' site token and multiple gateways")

rc_s, acl_s, log_s, _ = render({"AMP_BACKEND_PASSWORD": "x",
                                "GATEWAY_1_USER": "gw-solo", "GATEWAY_1_PASSWORD": "p",
                                "GATEWAY_1_TENANT": "SOLO"})
check("a gateway with no site publishes under the '-' token",
      ("gw-solo", "topic write flowmes/SOLO/-/machines") in topic_lines(acl_s), acl_s)

many = {"AMP_BACKEND_PASSWORD": "x"}
for n, (t, s) in enumerate([("ACME", "PLANT1"), ("ACME", "PLANT2"), ("BETA", "-")], start=1):
    many[f"GATEWAY_{n}_USER"] = f"gw{n}"
    many[f"GATEWAY_{n}_PASSWORD"] = f"p{n}"
    many[f"GATEWAY_{n}_TENANT"] = t
    many[f"GATEWAY_{n}_SITE"] = s
rc_m, acl_m, log_m, _ = render(many)
grants = sorted(t for u, t in topic_lines(acl_m) if u and u.startswith("gw"))
check("three gateways get three disjoint grants",
      grants == ["topic write flowmes/ACME/PLANT1/machines",
                 "topic write flowmes/ACME/PLANT2/machines",
                 "topic write flowmes/BETA/-/machines"], str(grants))
check("two sites of one tenant cannot write to each other's topic",
      "flowmes/ACME/+/machines" not in acl_m, acl_m[:200])

# A slot is only read while the numbering is contiguous. Stated as a test
# because the failure mode is a gateway that authenticates and then silently
# cannot publish -- exactly the report a commissioning engineer misreads as a
# PLC problem.
gap = {"AMP_BACKEND_PASSWORD": "x",
       "GATEWAY_1_USER": "gw1", "GATEWAY_1_PASSWORD": "p", "GATEWAY_1_TENANT": "A",
       "GATEWAY_3_USER": "gw3", "GATEWAY_3_PASSWORD": "p", "GATEWAY_3_TENANT": "C"}
rc_gap, acl_gap, _, _ = render(gap)
check("a gap in the numbering stops the scan (GATEWAY_3 is NOT rendered)",
      "gw3" not in acl_gap and "gw1" in acl_gap,
      "if this ever changes, README.md's numbering rule must change with it")


# ── 7. every rendered user is also a passwd entry ──────────────────────
section("7. Identity and authority name the same people")

acl_users = {u for u, _ in topic_lines(acl_m) if u}
passwd_users = set()
for line in log_m.splitlines():
    parts = line.split()
    if parts:
        passwd_users.add(parts[-1])
check("every user in the ACL has a password file entry",
      acl_users == passwd_users, f"acl={sorted(acl_users)} passwd={sorted(passwd_users)}")
check("the password file is created once and appended to after that",
      log_m.count("-c") == 1, log_m)
check("no password reached the fixture's log",
      "p1" not in log_m and "p2" not in log_m, log_m)


# ── 8. the charset has not drifted from AMP's ──────────────────────────
section("8. The broker and the backend agree on what an identifier is")

with open(os.path.join(REPO, "backend", "mqtt_identity.py")) as fh:
    backend_src = fh.read()
with open(ENTRYPOINT) as fh:
    script_src = fh.read()

# Not by comparing two copies of a regex -- that only proves someone typed the
# same characters twice. Each implementation is asked about each value, and the
# answers must agree. The first version of this suite compared regex STRINGS,
# passed, and missed that the shell side was using `grep`, which matches line by
# line and so accepted "gw\ntopic write flowmes/+/+/machines".
sys.path.insert(0, os.path.join(REPO, "backend"))
import mqtt_identity  # noqa: E402

CORPUS = [
    "ACME", "A", "a1", "0", "9z", "A-B_C.D", "_under", ".dot", "-lead",
    "", "-", "--", "+", "#", "/", "A/B", "A B", "A\tB", "gw:one",
    "gw\nuser", "\nACME", "ACME\n", "ACME\n\n", "ACME;rm -rf /", "A*", "A?",
    "A$B", "A`B", "A'B", 'A"B', "A\\B", "é", "ACME ", " ACME",
    "x" * 63, "x" * 64, "x" * 65, "x" * 200,
]


def shell_accepts(value):
    # Through the environment, not argv -- see the note beside
    # --check-identifier in entrypoint.sh.
    env = dict(os.environ, AMP_CHECK_VALUE=value)
    return subprocess.run([SH, ENTRYPOINT, "--check-identifier"],
                          env=env, capture_output=True, timeout=60).returncode == 0


def backend_accepts(value):
    return bool(mqtt_identity._IDENTIFIER.match(value))


looser = []
for value in CORPUS:
    if shell_accepts(value) and not backend_accepts(value):
        looser.append(value)
check("the broker never accepts an identifier the backend would reject",
      looser == [],
      f"the ACL would grant topics AMP itself considers invalid: {looser!r}")

# The other direction is allowed to differ, but only in the safe direction and
# only where it is understood. Python's `$` matches before a FINAL newline, so
# `_IDENTIFIER.match("ACME\n")` succeeds in the backend; `case` has no such
# quirk, so the broker refuses it. Stricter at the boundary that enforces is
# the right way round, and it is pinned here so it stays deliberate.
stricter = [v for v in CORPUS if backend_accepts(v) and not shell_accepts(v)]
check("the only place the broker is stricter is the trailing-newline quirk",
      stricter == ["ACME\n"], f"unexpected divergence: {stricter!r}")
check("...and a bare tenant still resolves identically on both sides",
      all(shell_accepts(v) == backend_accepts(v)
          for v in ("ACME", "PLANT1", "GMATS", "-", "+", "#", "A/B")),
      "the everyday values must not be a special case")

# The prefix default is the other thing that must agree, and it fails silently:
# the broker grants topics nobody is subscribed to, so the gateway reports a
# healthy publish and AMP sees nothing.
with open(os.path.join(REPO, "backend", "mqtt_service.py")) as fh:
    service_src = fh.read()
backend_prefix = re.search(r'DEFAULT_TOPIC_PREFIX = "([^"]+)"', service_src)
script_prefix = re.search(r'PREFIX="\$\{MQTT_TOPIC_PREFIX:-([^}]+)\}"', script_src)
check("the default topic prefix is findable in both",
      backend_prefix is not None and script_prefix is not None)
if backend_prefix and script_prefix:
    check("...and they match",
          backend_prefix.group(1) == script_prefix.group(1),
          f"backend={backend_prefix.group(1)} script={script_prefix.group(1)}")


print()
print("=" * 74)
if failures:
    print(f"FAILED ({len(failures)})")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
