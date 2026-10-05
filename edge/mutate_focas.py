"""Mutate the FOCAS adapter's guards; every one must turn the suite red.

SAFE BY CONSTRUCTION: the original is copied aside BEFORE the first mutation and
restored from that copy after each run and in `finally`. A harness that leaves a
mutation on disk is worse than no harness.
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "ampedge", "adapters", "focas.py")
BACKUP = TARGET + ".orig"
TEST = os.path.join(HERE, "test_focas_end_to_end.py")

MUTATIONS = [
    ("an undefined macro reads as 0 instead of refusing",
     '''            return base.no_data(
                tag, f"{how} is not defined on this control (the control flagged the value "
                     f"invalid, which is not the same as zero)")''',
     '''            return base.Reading(tag=tag, value=0, quality=base.GOOD, source_time=False)'''),

    ("the gateway claims the control stamped the reading",
     'return base.Reading(tag=tag, value=value, quality=base.GOOD, source_time=False)\n\n    async def _call',
     'return base.Reading(tag=tag, value=value, quality=base.GOOD, source_time=True)\n\n    async def _call'),

    ("an unknown status field is accepted instead of refused",
     '''        if field not in STATUS_FIELDS:
            raise base.AdapterError(
                f"{raw!r} names no FOCAS status field. Available: "
                f"{', '.join(STATUS_FIELDS)}.")''',
     '''        pass'''),

    ("macro numbers below 1 are accepted",
     '''    if number < 1:
        raise base.AdapterError(f"macro numbers start at 1, not {number}")''',
     '''    pass'''),

    ("the status record is re-read for every status tag",
     '''        if any(self._is_status(s.get("address")) for s in specs):''',
     '''        if False:'''),
]


def run_suite():
    env = dict(os.environ, DATABASE_URL="sqlite:///./ci_edge.db")
    p = subprocess.run([sys.executable, TEST], capture_output=True, text=True,
                       timeout=180, env=env, cwd=os.path.dirname(HERE))
    return p.returncode


def main():
    shutil.copyfile(TARGET, BACKUP)
    original = open(BACKUP, encoding="utf-8").read()
    survived = []
    try:
        if run_suite() != 0:
            print("BASELINE IS ALREADY RED - fix that before trusting any mutant")
            return 1
        print("baseline green\n")
        for name, old, new in MUTATIONS:
            if old not in original:
                print(f"  SKIP   {name}  (anchor not found - the harness has drifted)")
                survived.append(name + " [ANCHOR MISSING]")
                continue
            open(TARGET, "w", encoding="utf-8").write(original.replace(old, new, 1))
            try:
                rc = run_suite()
            finally:
                open(TARGET, "w", encoding="utf-8").write(original)
            if rc == 0:
                print(f"  SURVIVED  {name}")
                survived.append(name)
            else:
                print(f"  caught    {name}")
    finally:
        open(TARGET, "w", encoding="utf-8").write(original)
        os.remove(BACKUP)
    print()
    if survived:
        print(f"{len(survived)}/{len(MUTATIONS)} SURVIVED - investigate WHY each one did:")
        for s in survived:
            print("  -", s)
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
