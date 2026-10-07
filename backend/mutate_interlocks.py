"""Mutate the interlock guards; every one must turn the suite red.

Originals are copied aside BEFORE the first mutation and restored after each run
and in `finally`. A harness that leaves a mutant on disk is worse than none.
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGETS = {"i": os.path.join(HERE, "ai", "interlocks.py"),
           "a": os.path.join(HERE, "ai", "agents.py"),
           "m": os.path.join(HERE, "models.py")}
TEST = os.path.join(HERE, "test_interlocks.py")

MUTATIONS = [
    ("i", "the interval is tested for equality, so a batch stepping over is missed",
     "    if done < interval:\n        return None", "    if done != interval:\n        return None"),
    ("i", "a tool that is not live still accrues and raises",
     "    if tool is None or tool.status not in LIVE_STATUSES:", "    if tool is None:"),
    ("i", "a removed tool is advanced by the machine's output",
     "                      models.ToolAsset.status.in_(LIVE_STATUSES))",
     "                      models.ToolAsset.id.isnot(None))"),
    ("i", "a negative or absent quantity is allowed through",
     "    if made <= 0:\n        return []", "    if made is None:\n        return []"),
    ("i", "servicing zeroes the lifetime total, making the tool immortal",
     "    tool.parts_at_last_service = int(tool.parts_total or 0)",
     "    tool.parts_total = 0\n    tool.parts_at_last_service = 0"),
    ("i", "an unset interval is treated as due",
     "    if not interval or interval <= 0:\n        return None\n    done = tool.cycles_since_service",
     "    interval = interval or 1\n    done = tool.cycles_since_service"),
    ("a", "the duplicate guard goes, so every later batch re-raises the task",
     "            if _open_auto_task_exists(db, machine_id, due.task_type):\n                continue",
     "            pass"),
    ("m", "cycles are derived per batch, losing the remainder every time",
     "        made = int(self.parts_total or 0) - int(self.parts_at_last_service or 0)\n"
     "        return max(0, made) // max(1, int(self.cavities or 1))",
     "        made = int(self.parts_total or 0) - int(self.parts_at_last_service or 0)\n"
     "        return max(0, made)"),
]


def run():
    env = dict(os.environ, DATABASE_URL="sqlite:///./ci_interlocks.db")
    return subprocess.run([sys.executable, TEST], capture_output=True, text=True,
                          timeout=240, env=env, cwd=os.path.dirname(HERE)).returncode


def main():
    originals = {}
    for k, path in TARGETS.items():
        shutil.copyfile(path, path + ".orig")
        originals[k] = open(path + ".orig", encoding="utf-8").read()
    survived = []
    try:
        if run() != 0:
            print("BASELINE IS ALREADY RED - fix that before trusting any mutant")
            return 1
        print("baseline green\n")
        for key, name, old, new in MUTATIONS:
            src = originals[key]
            if old not in src:
                print(f"  SKIP      {name}  (anchor missing - harness drifted)")
                survived.append(name + " [ANCHOR MISSING]")
                continue
            open(TARGETS[key], "w", encoding="utf-8").write(src.replace(old, new, 1))
            try:
                rc = run()
            finally:
                open(TARGETS[key], "w", encoding="utf-8").write(src)
            print(("  SURVIVED  " if rc == 0 else "  caught    ") + name)
            if rc == 0:
                survived.append(name)
    finally:
        for k, path in TARGETS.items():
            open(path, "w", encoding="utf-8").write(originals[k])
            os.remove(path + ".orig")
    print()
    if survived:
        print(f"{len(survived)}/{len(MUTATIONS)} SURVIVED - investigate WHY each did:")
        for s in survived:
            print("  -", s)
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
