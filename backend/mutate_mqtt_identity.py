"""Mutation harness: break each tenant-identity guard, prove a test dies.

A guard nobody would notice the removal of is not a guard, it is a comment. Each
entry below deletes or weakens exactly one control and asserts the suite goes
red. A SURVIVED line means the assertion protecting it is missing or vacuous.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_mqtt_identity.py
"""
import io
import os
import subprocess
import sys

# Three environments, because the guards are not all reachable in one process.
# test_mqtt_ingest_without_orm_hooks.py runs WITHOUT tenancy.install_scoping():
# with the hooks installed they silently substitute for ingest's own tenant
# filter and stamps, and four mutations that reintroduce the original bug on the
# standalone mqtt_listener.py path SURVIVED until that file existed.
SUITES = ["test_mqtt_tenant_identity.py",
          "test_mqtt_ingest_without_orm_hooks.py",
          "test_mqtt_resilience.py"]

MUTATIONS = [
    ("machine resolved without the tenant filter", "mqtt_service.py",
     "        models.Machine.tenant_code == route.tenant,\n", ""),
    ("machine resolved without the site filter", "mqtt_service.py",
     "        models.Machine.site == route.site,\n", ""),
    ("new machine created with no tenant (falls back to the column default)",
     "mqtt_service.py", "        tenant_code=route.tenant,\n        site=route.site,\n",
     "        site=route.site,\n"),
    ("ProductionRecord written without a tenant", "mqtt_service.py",
     "                machine_id=machine.id,\n                tenant_code=machine.tenant_code,\n"
     "                planned_minutes=planned_minutes,",
     "                machine_id=machine.id,\n                planned_minutes=planned_minutes,"),
    ("DowntimeLog written without a tenant", "mqtt_service.py",
     '                machine_id=machine.id,\n                tenant_code=machine.tenant_code,\n'
     '                reason="Breakdown",',
     '                machine_id=machine.id,\n                reason="Breakdown",'),
    ("MachineEvent written without a tenant", "mqtt_service.py",
     "                machine_id=machine.id,\n                tenant_code=machine.tenant_code,\n"
     "                machine_name=machine.name,",
     "                machine_id=machine.id,\n                machine_name=machine.name,"),
    ("unprovisioned tenants accepted", "mqtt_service.py",
     "        if not tenant_is_provisioned(db, route.tenant):",
     "        if False:"),
    ("payload allowed to override the topic's tenant", "mqtt_service.py",
     "            mqtt_identity.check_payload_agrees(route, payload)\n", ""),
    # This one needs TWO edits to be a real mutation, and finding that out was
    # the point of running it. Deleting the `if not legacy_tenant: raise` alone
    # survives because `_identifier("")` on the next line rejects the empty
    # string anyway — a second guard, so the message is still refused. Supplying
    # a `or "DEFAULT"` fallback alone survives too, because that line is
    # UNREACHABLE while the raise above it still fires. Only removing the refusal
    # AND naming a fallback owner reproduces the original defect, where an
    # untenanted packet silently became DEFAULT's.
    ("legacy topic guesses DEFAULT instead of refusing", "mqtt_identity.py",
     ["        if not legacy_tenant:\n",
      '_identifier(legacy_tenant, "legacy tenant")'],
     ["        if False:\n",
      '_identifier(legacy_tenant or "DEFAULT", "legacy tenant")']),
    ("identifier charset opened up (wildcards and traversal allowed)",
     "mqtt_identity.py", r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$', r'^.*$'),
    ("machine name no longer validated", "mqtt_identity.py",
     '    return _identifier(payload.get("machine"), "machine")',
     '    return payload.get("machine")'),
    # The race-recovery path exists only because the constraint above created
    # the race. Removing the recovery must be caught, or the loser silently
    # drops a machine's first packet.
    ("lost create race drops the message instead of recovering", "mqtt_service.py",
     "        db.rollback()\n        machine = find()\n        if machine is None:\n            raise",
     "        db.rollback()\n        raise"),
    ("race recovery invents a machine instead of re-selecting", "mqtt_service.py",
     "        machine = find()\n        if machine is None:\n            raise",
     "        machine = None\n        if machine is None:\n            raise"),
    ("database identity constraint dropped", "models.py",
     '        UniqueConstraint("tenant_code", "site", "name", name="uq_machine_identity"),\n',
     ""),
    ("site column made nullable (NULL != NULL defeats the constraint)", "models.py",
     'site = Column(String, nullable=False, default="", server_default="")',
     'site = Column(String, nullable=True)'),
]


def run_suites():
    """Which suites fail? Returns the list of failing suite names."""
    failed = []
    for suite in SUITES:
        proc = subprocess.run([sys.executable, suite], capture_output=True,
                              text=True, errors="replace", cwd=os.path.dirname(
                                  os.path.abspath(__file__)))
        if proc.returncode != 0:
            failed.append(suite)
    return failed


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    originals = {}
    for _, path, _, _ in MUTATIONS:
        if path not in originals:
            originals[path] = io.open(os.path.join(here, path),
                                      encoding="utf-8").read()

    baseline = run_suites()
    if baseline:
        print(f"ABORT: suites are already failing before any mutation: {baseline}")
        return 2
    print("baseline: both suites green\n")
    print(f"{'mutation':<62} {'verdict':<10} caught by")
    print("-" * 110)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        # A mutation may need several coordinated edits — see the legacy-topic
        # entry, where one edit alone is caught by a different guard and the
        # other alone is unreachable.
        olds = old if isinstance(old, list) else [old]
        news = new if isinstance(new, list) else [new]
        misses = [o for o in olds if source.count(o) != 1]
        if misses:
            # A pattern that does not apply reports SKIP, never "caught": a
            # mutation that never happened proves nothing about the guard.
            print(f"{label:<62} {'SKIP':<10} {len(misses)} pattern(s) did not "
                  f"match exactly once in {path}")
            survived.append(f"{label} (pattern did not apply)")
            continue
        mutated = source
        for o, n in zip(olds, news):
            mutated = mutated.replace(o, n, 1)
        io.open(os.path.join(here, path), "w", encoding="utf-8",
                newline="\n").write(mutated)
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8",
                    newline="\n").write(source)
        verdict = "caught" if failing else "SURVIVED"
        print(f"{label:<62} {verdict:<10} "
              f"{', '.join(s.replace('test_mqtt_', '').replace('.py', '')[:18] for s in failing) or '-- nothing --'}")
        if not failing:
            survived.append(label)

    # This harness edits real source files and restores them in a finally. Say so
    # out loud rather than trusting it: a harness that silently left a mutation
    # behind would be the worst possible bug to have in a repository.
    dirty = [p for p, original in originals.items()
             if io.open(os.path.join(here, p), encoding="utf-8").read() != original]
    print()
    print(f"source files restored: {'yes' if not dirty else 'NO — DIRTY: ' + str(dirty)}")
    if dirty:
        return 3

    if survived:
        print(f"{len(survived)} MUTATION(S) SURVIVED — investigate each:")
        for s in survived:
            print("   *", s)
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
