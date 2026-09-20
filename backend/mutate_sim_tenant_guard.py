"""Mutation harness for the simulator's tenant guard (factory_simulator._bound_tenant).

The guard is one function and one line per tick, and every line is load-
bearing on its own: a tick that lost its call would read every tenant's rows
and file what it wrote under DEFAULT, exactly as measured on 2026-09-20 (41
rows in 12 unbound rounds), while every OTHER tick still refused and every
suite that binds a tenant still passed. So each tick's line is deleted in
turn, then the guard itself is bent the three ways a well-meant edit would
bend it: inverted, softened into a DEFAULT fallback, and reworded so the
refusal no longer says what it is. Last, the CLI's own binding is removed.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_sim_tenant_guard.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_sim_ticks_need_a_tenant.py", "test_telemetry_coverage.py",
          "audit_three_factory_simulation.py"]

S = "factory_simulator.py"

MUTATIONS = [
    # --- one tick at a time loses its guard --------------------------------
    ("tick_production runs unbound", S,
     "    _bound_tenant()\n    if random.random() > 0.25:",
     "    if random.random() > 0.25:"),
    ("tick_machine_status runs unbound", S,
     "    _bound_tenant()\n    machines = db.query(models.Machine).all()\n    if not machines:\n"
     "        return\n    machine = random.choice(machines)\n    old = machine.status",
     "    machines = db.query(models.Machine).all()\n    if not machines:\n"
     "        return\n    machine = random.choice(machines)\n    old = machine.status"),
    ("tick_status_heartbeat falls back to DEFAULT instead of refusing", S,
     "    tenant = _bound_tenant()",
     "    tenant = tenancy.current_tenant() or \"DEFAULT\""),
    ("tick_work_order_progress runs unbound", S,
     "    _bound_tenant()\n    wo = db.query(models.WorkOrder).filter(",
     "    wo = db.query(models.WorkOrder).filter("),
    ("tick_shift_entry runs unbound", S,
     "    _bound_tenant()\n    now   = datetime.now()",
     "    now   = datetime.now()"),
    ("tick_quality runs unbound", S,
     "    _bound_tenant()\n    wos      = db.query(models.WorkOrder)",
     "    wos      = db.query(models.WorkOrder)"),
    ("tick_operator runs unbound", S,
     "    _bound_tenant()\n    job = db.query(models.OperatorJobExecution)",
     "    job = db.query(models.OperatorJobExecution)"),
    ("tick_iot runs unbound", S,
     "    _bound_tenant()\n    machines = db.query(models.Machine).all()\n    if not machines:\n"
     "        return\n    machine  = random.choice(machines)",
     "    machines = db.query(models.Machine).all()\n    if not machines:\n"
     "        return\n    machine  = random.choice(machines)"),
    ("tick_inventory runs unbound", S,
     "    _bound_tenant()\n    count = db.query(models.InventoryTransaction)",
     "    count = db.query(models.InventoryTransaction)"),
    ("tick_escalation runs unbound", S,
     "    _bound_tenant()\n    machines = db.query(models.Machine).filter(\n        models.Machine.status == \"Breakdown\"",
     "    machines = db.query(models.Machine).filter(\n        models.Machine.status == \"Breakdown\""),
    ("tick_customer_order runs unbound", S,
     "    _bound_tenant()\n    order = db.query(models.CustomerOrder)",
     "    order = db.query(models.CustomerOrder)"),
    ("run_simulation (the CLI's chooser) no longer refuses before choosing", S,
     "    _bound_tenant()\n    actions = [",
     "    actions = ["),

    # --- the guard itself --------------------------------------------------
    ("the guard is inverted: bound refuses, unbound proceeds", S,
     "    if not tenant:\n        raise ValueError(\"simulator ticks run for one bound tenant;",
     "    if tenant:\n        raise ValueError(\"simulator ticks run for one bound tenant;"),
    ("the guard softens into a DEFAULT fallback", S,
     "        raise ValueError(\"simulator ticks run for one bound tenant; with none bound a tick \"\n"
     "                         \"would read every tenant's rows and file what it wrote under DEFAULT\")",
     "        return \"DEFAULT\""),
    ("the refusal no longer says what it is", S,
     "\"simulator ticks run for one bound tenant; with none bound a tick \"",
     "\"refused: \""),

    # --- the CLI ------------------------------------------------------------
    ("the CLI stops binding the demo workspace", S,
     "    scope = tenancy.set_current_tenant(tenancy.DEFAULT_TENANT)\n    try:\n        tick = 0",
     "    scope = tenancy.set_current_tenant(tenancy.current_tenant())\n    try:\n        tick = 0"),
]


def run_suites():
    failed = []
    here = os.path.dirname(os.path.abspath(__file__))
    for suite in SUITES:
        if not os.path.exists(os.path.join(here, suite)):
            continue
        proc = subprocess.run([sys.executable, suite], capture_output=True, text=True,
                              errors="replace", cwd=here)
        if proc.returncode != 0:
            failed.append(suite)
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
    print(f"{'mutation':<66} {'verdict':<10} caught by")
    print("-" * 108)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<66} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
            survived.append(f"{label} (pattern did not apply)")
            continue
        io.open(os.path.join(here, path), "w", encoding="utf-8",
                newline="\n").write(source.replace(old, new, 1))
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source)
        if failing:
            verdict, note = "caught", ", ".join(
                s.replace("test_", "").replace(".py", "")[:26] for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label]
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
            print(f"  - {s}")
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
