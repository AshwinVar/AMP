"""Mutation harness for the OEM lifecycle WRITES and their events (ADR-0017).

The read side has mutate_oem_auth and mutate_oem_sharing. A write adds failure
modes a read cannot have, and this harness is aimed squarely at them:

    a manufacturer editing a row that is not theirs
    a capability gate that is decorative
    an event filed in the WRONG TENANT -- the quiet one, because nothing looks
      broken: the write succeeds, the screen updates, and a manufacturer's
      private record is simply sitting in somebody else's history

That last class is why `oem_events.publish` refuses a tenant-less event rather
than letting events.EventBus fall back to its "DEFAULT" stamp. Removing that
refusal is the first mutation below.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_oem_lifecycle.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_oem_lifecycle_writes.py", "test_oem_routes.py",
          "test_oem_authorization.py", "test_oem_service.py"]

MUTATIONS = [
    # --- the event lands in the wrong tenant --------------------------------
    ("a tenant-less event is published anyway (lands under DEFAULT)",
     "oem_events.py",
     '    if not getattr(event, "tenant_code", None):\n        return False',
     "    if False:\n        return False"),
    ("the event is filed under the MANUFACTURER instead of the customer",
     "oem_routes.py",
     "    oem_events.publish(event_bus, db, oem_events.MachineCommissioned(\n"
     '        tenant_code=inst.factory_tenant_code or "",',
     "    oem_events.publish(event_bus, db, oem_events.MachineCommissioned(\n"
     '        tenant_code=principal["oem"],'),
    ("the notification is raised in the wrong tenant", "oem_subscribers.py",
     "        tenant_code=tenant_code,", '        tenant_code="DEFAULT",'),

    # --- ownership ----------------------------------------------------------
    ("commissioning stops checking who owns the machine", "oem_routes.py",
     '    inst = oem_sharing.get_installation(db, principal["oem"], installation_id)\n'
     "    if inst is None:\n"
     '        raise HTTPException(status_code=404, detail="Machine not found")\n\n'
     "    model = _models_by_id",
     "    inst = db.query(models.MachineInstallation).filter(\n"
     "        models.MachineInstallation.id == installation_id).first()\n"
     "    if inst is None:\n"
     '        raise HTTPException(status_code=404, detail="Machine not found")\n\n'
     "    model = _models_by_id"),
    ("a competitor's machine is a 403 (an existence oracle)", "oem_routes.py",
     '    inst = oem_sharing.get_installation(db, principal["oem"], installation_id)\n'
     "    if inst is None:\n"
     '        raise HTTPException(status_code=404, detail="Machine not found")\n\n'
     "    hours = payload.service_hours",
     '    inst = oem_sharing.get_installation(db, principal["oem"], installation_id)\n'
     "    if inst is None:\n"
     '        raise HTTPException(status_code=403, detail="Not your machine")\n\n'
     "    hours = payload.service_hours"),

    # --- capabilities -------------------------------------------------------
    ("commissioning needs no capability at all", "oem_routes.py",
     '                           oem_auth.require_oem("commission"))):',
     '                           oem_auth.require_oem("read_fleet"))):'),
    ("recording a service needs only read access", "oem_routes.py",
     '                       oem_auth.require_oem("manage_service"))):',
     '                       oem_auth.require_oem("read_fleet"))):'),

    # --- the lifecycle stops being a state machine --------------------------
    ("an impossible transition is accepted over HTTP", "oem_routes.py",
     "    try:\n"
     "        oem_service.transition(inst, payload.target)\n"
     "    except oem_service.LifecycleError as e:",
     "    try:\n"
     "        inst.status = payload.target\n"
     "    except oem_service.LifecycleError as e:"),

    # --- the service clock ---------------------------------------------------
    ("a negative service reading is stored instead of refused", "oem_routes.py",
     "    if hours is not None and hours < 0:\n"
     "        raise HTTPException(status_code=400,\n"
     '                            detail="service_hours cannot be negative")',
     "    if False:\n"
     "        raise HTTPException(status_code=400,\n"
     '                            detail="service_hours cannot be negative")'),
    ("recording a service does not actually move the clock", "oem_routes.py",
     "    inst.last_service_hours = hours",
     "    inst.last_service_hours = inst.last_service_hours"),

    # --- honesty about commissioning ----------------------------------------
    ("the event claims every commissioning check passed", "oem_routes.py",
     '        checks_passed=bool(report["ready"])))',
     "        checks_passed=True))"),
    ("an incomplete commissioning is reported to the customer as routine",
     "oem_subscribers.py",
     '        "Warning" if incomplete else "Info",',
     '        "Info",'),
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


# A mutation another guard already covers. Each needs a reason that survives
# reading -- "it seemed fine" is how a real gap gets filed as expected.
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
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label][:70] + "..."
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
