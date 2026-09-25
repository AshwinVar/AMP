"""Mutation harness for machine identity and gateway adoption.

MANDATORY, per the pilot sprint's own testing rules: machine identity is on the
list that gets deep tests immediately, alongside tenant isolation and gateway
authentication. It earns it — this is the code that decides which physical
machine a packet is about, and getting it wrong either writes one customer's
telemetry onto another's machine or silently doubles a pilot's machine list on
its first packet.

Each mutation is a plausible edit that breaks one of the four rules:

  * a gateway packet must UPDATE the machine the customer already created
  * it must never register a SECOND machine of the same name
  * ambiguity is REFUSED, never guessed
  * a refusal is RECORDED, and adoption never crosses a workspace

RUN IT ALONE. Like every mutate_* harness it EDITS THE WORKING TREE and restores
it afterwards; anything reading those files meanwhile sees broken code.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_machine_identity.py
Exit 0 only if every mutation applied exactly once and was caught.
"""
import io
import os
import subprocess
import sys

SUITES = ["test_machine_identity_adoption.py", "test_mqtt_tenant_identity.py",
          "test_mqtt_resilience.py", "test_machines_routes.py"]
SUITE_TIMEOUT = 600

MQTT = "mqtt_service.py"
SCHEMAS = "schemas.py"

# (label, file, old, new)
MUTATIONS = [
    # ── adoption happens at all ──────────────────────────────────────────────
    ("the gateway stops adopting, so the first packet doubles the machine list", MQTT,
     "    adopted = adopt_candidate(db, route, name)\n    if adopted is not None:",
     "    adopted = adopt_candidate(db, route, name)\n    if False:"),
    ("adoption finds the machine and does not give it the site, so it is adopted forever", MQTT,
     "        adopted.site = route.site",
     "        adopted.site = adopted.site"),

    # ── adoption stays inside one workspace ──────────────────────────────────
    ("adoption reaches across workspaces, claiming another customer's machine", MQTT,
     "    siteless = db.query(models.Machine).filter(\n"
     "        models.Machine.tenant_code == route.tenant,\n"
     "        models.Machine.name == name,",
     "    siteless = db.query(models.Machine).filter(\n"
     "        models.Machine.name == name,"),
    ("adoption claims a machine that already HAS a site, moving it", MQTT,
     "        (models.Machine.site == \"\") | (models.Machine.site.is_(None)),\n    ).all()",
     "    ).all()"),

    # ── ambiguity is refused, not guessed ────────────────────────────────────
    ("a name that already exists at another site is adopted anyway, a guess", MQTT,
     "    if elsewhere:\n        raise AmbiguousMachineIdentity(",
     "    if False:\n        raise AmbiguousMachineIdentity("),
    ("two siteless rows of one name are resolved by whichever came first", MQTT,
     "    if len(siteless) > 1:\n        raise AmbiguousMachineIdentity(",
     "    if False:\n        raise AmbiguousMachineIdentity("),
    ("the conflict check counts machines at ANY site, including this one", MQTT,
     "        models.Machine.site != \"\",\n        models.Machine.site.isnot(None),\n    ).count()",
     "    ).count()"),

    # ── a refusal is recorded, and recorded once ─────────────────────────────
    ("a refused packet is dropped with no record at all", MQTT,
     "            _record_identity_conflict(db, route, machine_name, str(clash))",
     "            pass"),
    ("the conflict is recorded again for every packet, flooding the feed", MQTT,
     "    if existing is not None:\n        return",
     "    if False:\n        return"),
    ("a conflict already read is treated as still open, so it never re-raises", MQTT,
     "        models.Notification.status != \"Read\",",
     "        models.Notification.status == \"Read\","),

    # ── a refused packet does not become a duplicate ─────────────────────────
    #
    # THE LABEL ON THIS ONE WAS WRONG on its first run, and is corrected here
    # rather than quietly left standing. Removing the `return` does NOT create a
    # duplicate: `machine` is bound only inside the try, so the next line
    # (`machine.status`) raises UnboundLocalError, which on_message's outer
    # `except Exception` swallows. Both paths refuse; the `return` is what makes
    # the refusal clean rather than a swallowed crash. See EXPECTED_SURVIVORS.
    ("a refused packet leaves by raising instead of returning", MQTT,
     "            _record_identity_conflict(db, route, machine_name, str(clash))\n            return",
     "            _record_identity_conflict(db, route, machine_name, str(clash))"),

    # ── the site a human sets is a topic segment ─────────────────────────────
    ("a site with a slash or an MQTT wildcard is accepted from the form", SCHEMAS,
     "        if not isinstance(v, str) or not mqtt_identity._IDENTIFIER.match(v.strip()):",
     "        if False:"),
]


def run_suites():
    failed = []
    here = os.path.dirname(os.path.abspath(__file__))
    for suite in SUITES:
        try:
            proc = subprocess.run([sys.executable, suite], capture_output=True, text=True,
                                  errors="replace", cwd=here, timeout=SUITE_TIMEOUT)
            if proc.returncode != 0:
                failed.append(suite)
        except subprocess.TimeoutExpired:
            failed.append(suite + " (timeout)")
    return failed


# Nothing here. This dict held one entry for about an hour: the mutation below
# survived, and the reason was real (both paths refuse; the difference is only a
# swallowed UnboundLocalError). But "the outcome is the same" was the wrong
# conclusion — the log differs, and the log is what a commissioning engineer
# reads to tell which side is broken. Section 9 of the suite pins that, so the
# mutation is caught rather than excused.
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
    print(f"{'mutation':<74} {'verdict':<10} caught by")
    print("-" * 118)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<74} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
            survived.append(f"{label} (pattern did not apply)")
            continue
        io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source.replace(old, new, 1))
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source)
        if failing:
            verdict, note = "caught", ", ".join(s.replace("test_", "").replace(".py", "")[:24] for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label][:70] + "..."
        else:
            verdict, note = "SURVIVED", "-- nothing --"
        print(f"{label:<74} {verdict:<10} {note}", flush=True)
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
