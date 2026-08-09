"""Mutation harness for service intelligence and telemetry profiles (ADR-0017).

The security spine has its own harnesses (mutate_oem_auth, mutate_oem_sharing).
This one attacks a different property, and the more slippery one: HONESTY.

Nothing here leaks a customer's data. Every mutation below makes AMP say
something confident that nobody can justify — a default service interval, a
warranty it was never told about, a clamped temperature, a projection drawn from
two points. That is the failure mode this module was written against, because a
figure like that puts an engineer in a van, and it is the same class of
fabrication the platform removed from OEE in ADR-0014.

A test suite that passes with these mutations in place is a suite that would let
the fabrication back in.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_oem_service.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_oem_service.py", "test_oem_routes.py", "test_connected_equipment.py"]

MUTATIONS = [
    # --- the bug this module was rewritten to fix ---------------------------
    ("service position goes back to `hours % interval`", "oem_service.py",
     '    last = getattr(installation, "last_service_hours", None)\n'
     "    since = hours - (last or 0.0)",
     '    last = getattr(installation, "last_service_hours", None)\n'
     "    since = hours % interval"),
    ("a counter reading below the last service is reported as fresh",
     "oem_service.py",
     "    if since < 0:", "    if False:"),

    # --- inventing what nobody supplied -------------------------------------
    ("a model with no service interval is given a default one", "oem_service.py",
     "    if not interval:\n"
     '        return {"state": "not_configured", "hours_remaining": None,\n'
     "                \"reason\": f\"model {getattr(model, 'model_code', '?')} defines no \"\n"
     '                          f"service interval"}',
     "    if not interval:\n"
     "        interval = 2000"),
    ("a machine that never reported hours is treated as brand new",
     "oem_service.py",
     "    if hours is None:\n"
     '        return {"state": "unknown", "hours_remaining": None,\n'
     '                "reason": "this machine has never reported operating hours"}',
     "    if hours is None:\n"
     "        hours = 0.0"),
    ("an unrecorded warranty is reported as EXPIRED", "oem_service.py",
     "    if end is None:\n"
     '        return {"state": "unknown", "days_remaining": None,\n'
     '                "reason": "no warranty end date recorded for this installation"}',
     "    if end is None:\n"
     '        return {"state": "expired", "days_remaining": -1,\n'
     '                "reason": "no warranty end date recorded for this installation"}'),
    ("an unrecorded warranty is reported as ACTIVE", "oem_service.py",
     "    if end is None:\n"
     '        return {"state": "unknown", "days_remaining": None,\n'
     '                "reason": "no warranty end date recorded for this installation"}',
     "    if end is None:\n"
     '        return {"state": "active", "days_remaining": 365,\n'
     '                "reason": "no warranty end date recorded for this installation"}'),

    # --- confidence that was chosen rather than derived ----------------------
    ("the projection's confidence is a chosen number", "oem_service.py",
     "    confidence = min(0.85, 0.35 + 0.05 * len(samples) + 0.01 * span_days)",
     "    confidence = 0.95"),
    ("an arithmetic recommendation is dressed up with a confidence figure",
     "oem_service.py",
     '            "confidence": None,          # arithmetic, not a prediction',
     '            "confidence": 0.92,'),
    ("a trend is drawn from samples inside a single day", "oem_service.py",
     "    if span_days < 1:\n        return None",
     "    if False:\n        return None"),

    # --- claiming to know what it cannot know --------------------------------
    ("a silent machine is declared faulty rather than unexplained",
     "oem_service.py",
     '                "reason": "This machine has not reported recently. It is either "\n'
     '                          "switched off, disconnected, or faulty — this cannot "\n'
     '                          "be told apart from here.",',
     '                "reason": "This machine has failed and needs attention.",'),

    # --- the lifecycle stops being a gate ------------------------------------
    ("commissioning can be skipped entirely", "oem_service.py",
     "    if not can_transition(current, target):", "    if False:"),
    ("commissioning reports ready even when a check failed", "oem_service.py",
     '    return {"ready": all(c["passed"] for c in out), "checks": out}',
     '    return {"ready": True, "checks": out}'),

    # --- the queue stops leading with the worst ------------------------------
    ("the fleet queue is returned unsorted", "oem_service.py",
     '    out.sort(key=lambda r: (rank.get(r["severity"], 9), r["machine"]))',
     "    out.reverse()"),

    # --- telemetry: a broken profile stops being an error --------------------
    ("an unparseable profile silently becomes 'reports nothing'",
     "oem_telemetry.py",
     '            raise ProfileError(f"telemetry profile is not valid JSON: {e}")',
     "            return []"),
    ("a signal defined twice is accepted", "oem_telemetry.py",
     '            raise ProfileError(f"signal {name!r} is defined twice")',
     "            pass"),
    ("an unknown datatype is accepted", "oem_telemetry.py",
     '        if sig["datatype"] not in VALID_TYPES:\n'
     "            raise ProfileError(\n"
     "                f\"signal {name!r} has unknown datatype {sig['datatype']!r}\")",
     "        if False:\n"
     "            raise ProfileError(\n"
     "                f\"signal {name!r} has unknown datatype {sig['datatype']!r}\")"),

    # --- telemetry: the reading stops being the reading ----------------------
    ("an out-of-range reading is CLAMPED to the configured maximum",
     "oem_telemetry.py",
     "            if hi is not None and number > float(hi):\n"
     "                in_range = False",
     "            if hi is not None and number > float(hi):\n"
     "                number = float(hi)"),
    ("a tag the profile does not describe is dropped silently",
     "oem_telemetry.py",
     "        if sig is None:\n            unknown.append(source)\n            continue",
     "        if sig is None:\n            continue"),
    ("machine state is inferred from any boolean", "oem_telemetry.py",
     '    return [s["name"] for s in profile if s["state_signal"]]',
     '    return [s["name"] for s in profile\n'
     '            if s["state_signal"] or s["datatype"] == "bool"]'),
]


def run_suites():
    failed = []
    here = os.path.dirname(os.path.abspath(__file__))
    for suite in SUITES:
        proc = subprocess.run([sys.executable, suite], capture_output=True,
                              text=True, errors="replace", cwd=here)
        if proc.returncode != 0:
            failed.append(suite)
    return failed


# A mutation another guard already covers. Each needs a reason that survives
# reading — "it seemed fine" is how a real gap gets filed as expected.
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
