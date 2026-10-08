"""Mutate the interlock guards; every one must turn the suite red.

Originals are copied aside BEFORE the first mutation and restored after each run
and in `finally`. A harness that leaves a mutant on disk is worse than none.
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Keyed by the REPO-RELATIVE PATH each mutation names, which is the layout
# every other harness uses and the one test_mutation_anchors_apply.py can
# read. This keyed on a one-letter code with the LABEL first, so that guard
# found zero anchors here and said so -- 1,256 anchors checked across 52
# harnesses, and this one contributed none of them.
TARGETS = {"ai/interlocks.py": os.path.join(HERE, "ai", "interlocks.py"),
           "ai/agents.py": os.path.join(HERE, "ai", "agents.py"),
           "models.py": os.path.join(HERE, "models.py")}
TEST = os.path.join(HERE, "test_interlocks.py")

MUTATIONS = [
    ("the interval is tested for equality, so a batch stepping over is missed",
     "ai/interlocks.py",
     "    if done < interval:\n        return None", "    if done != interval:\n        return None"),
    ("a tool that is not live still accrues and raises",
     "ai/interlocks.py",
     "    if tool is None or tool.status not in LIVE_STATUSES:", "    if tool is None:"),
    ("a removed tool is advanced by the machine's output",
     "ai/interlocks.py",
     "                      models.ToolAsset.status.in_(LIVE_STATUSES))",
     "                      models.ToolAsset.id.isnot(None))"),
    ("a negative or absent quantity is allowed through",
     "ai/interlocks.py",
     "    if made <= 0:\n        return []", "    if made is None:\n        return []"),
    ("servicing zeroes the lifetime total, making the tool immortal",
     "ai/interlocks.py",
     "    tool.parts_at_last_service = int(tool.parts_total or 0)",
     "    tool.parts_total = 0\n    tool.parts_at_last_service = 0"),
    # Anchored through the line BELOW the guard, because the same three lines
    # open both due_for_service and the escalation check. Matching twice, the
    # mutation silently landed on whichever came first in the file — so
    # reordering the two functions would have moved it without a word.
    ("an unset interval is treated as due",
     "ai/interlocks.py",
     "    if not interval or interval <= 0:\n        return None\n"
     "    done = tool.cycles_since_service\n    if done < interval:\n",
     "    interval = interval or 1\n"
     "    done = tool.cycles_since_service\n    if done < interval:\n"),
    ("the duplicate guard goes, so every later batch re-raises the task",
     "ai/agents.py",
     "            if _open_auto_task_exists(db, machine_id, due.task_type):\n                continue",
     "            pass"),
    ("cycles are derived per batch, losing the remainder every time",
     "models.py",
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
        for name, key, old, new in MUTATIONS:
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
