"""Mutation harness for the device-topic refusal (a per-device MQTT topic is not routing).

Each mutation is a small, plausible edit that quietly brings the accepted-but-
inert input back: the validator accepting any value, the validator detached
from the schema, a blank topic stored as text, the drawer's read model
publishing the stored topic again, and PATCH learning to set one. For each the
harness applies the edit, runs the suite that is supposed to notice, and
restores the file byte for byte. A mutation that leaves the suite green
SURVIVED: that guard is untested.

RUN IT ALONE. Like every mutate_* harness it EDITS THE WORKING TREE and restores
it afterwards; anything reading those files meanwhile sees broken code.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_device_topic.py
Exit 0 only if every mutation applied exactly once and was caught.
"""
import io
import os
import subprocess
import sys

SUITES = ["test_device_topic_is_not_routing.py"]
SUITE_TIMEOUT = 300

SCHEMAS = "schemas.py"
CONN = "ai/connectivity.py"

# (label, file, old, new)
MUTATIONS = [
    ("the validator accepts any topic (stored, read by nothing)", SCHEMAS,
     "    raise ValueError(DEVICE_TOPIC_NOT_ROUTING)",
     "    return value"),
    ("the validator is detached from the create schema", SCHEMAS,
     '    _no_topic = field_validator("topic", mode="before")(_refuse_device_topic)',
     "    _no_topic = None"),
    ("a blank topic is stored as text instead of NULL", SCHEMAS,
     "    if isinstance(value, str) and not value.strip():\n        return None\n    raise ValueError(DEVICE_TOPIC_NOT_ROUTING)",
     "    if isinstance(value, str) and not value.strip():\n        return value\n    raise ValueError(DEVICE_TOPIC_NOT_ROUTING)"),
    ("the refusal loses its reason", SCHEMAS,
     "    raise ValueError(DEVICE_TOPIC_NOT_ROUTING)",
     '    raise ValueError("topic is not allowed")'),
    ("the drawer's read model publishes the stored topic again", CONN,
     '            "ip_address": d.ip_address,\n            # No "topic": nothing routes by a device\'s topic (ADR-0011), and\n'
     "            # printing one beside the device read as if it configured ingest.\n"
     '            "status": d.status,',
     '            "ip_address": d.ip_address,\n            "topic": d.topic,\n            "status": d.status,'),
    ("PATCH learns to set a topic", SCHEMAS,
     "class IndustrialDeviceUpdate(BaseModel):\n    status: Optional[str] = None",
     "class IndustrialDeviceUpdate(BaseModel):\n    topic: Optional[str] = None\n    status: Optional[str] = None"),
]


def run_suites():
    failed = []
    here = os.path.dirname(os.path.abspath(__file__))
    for suite in SUITES:
        try:
            proc = subprocess.run([sys.executable, suite], capture_output=True, text=True, errors="replace",
                                  cwd=here, timeout=SUITE_TIMEOUT)
            if proc.returncode != 0:
                failed.append(suite)
        except subprocess.TimeoutExpired:
            failed.append(suite + " (timeout)")
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
        io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source.replace(old, new, 1))
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source)
        if failing:
            verdict, note = "caught", ", ".join(s.replace("test_", "").replace(".py", "")[:28] for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label][:70] + "..."
        else:
            verdict, note = "SURVIVED", "-- nothing --"
        print(f"{label:<66} {verdict:<10} {note}", flush=True)
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
