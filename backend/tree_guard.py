"""Prove that a parallel investigation left the working tree untouched.

WHY THIS EXISTS
---------------
A read-only investigation agent was told, in its prompt, in capitals, not to
edit anything. It edited `backend/oem_sharing.py` anyway and did not restore it:

    -    if SHARE_OPERATING_HOURS in grants:
    +    if True:

That is a CONSENT GATE. With it disabled, a customer's machine operating hours
are published to the equipment's manufacturer whether or not the customer
granted `SHARE_OPERATING_HOURS`. The edit sat in the working tree while
unrelated work was being committed around it. AMP's own tests caught it — both
OEM suites failed — but only because the change happened to be covered. Nothing
in the process would have noticed an edit to something untested, and the
instruction not to edit had already been ignored once.

An instruction is not a control. This is the control.

USE
---
    python tree_guard.py snapshot        # before spawning parallel agents
    ...run the investigation...
    python tree_guard.py verify          # names every tracked file that moved

`verify` exits non-zero if anything changed, so it can gate a commit or a
script. Compare with `git status` only if you already know what you changed;
the point here is the files you did NOT change.

THE STRONGER CONTROL, WHICH THIS DOES NOT REPLACE
--------------------------------------------------
Investigators that only need to read should not be pointed at the development
tree at all. The Workflow/Agent API takes `isolation: "worktree"`, which gives
each agent its own git worktree; an edit there cannot reach the tree you commit
from. Use that FIRST. This harness is the backstop for the case where an agent
does share the tree — and the evidence that it stayed clean.

Snapshots live in the scratch directory, never in the repository: a manifest of
file hashes is not source, and committing one would be one more thing to keep in
sync with reality.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(tempfile.gettempdir(), "amp_tree_guard.json")


def _tracked_files():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO,
                         capture_output=True, text=True).stdout
    return [p for p in out.split("\0") if p]


def _digest(path):
    full = os.path.join(REPO, path)
    try:
        with open(full, "rb") as fh:
            h = hashlib.sha256()
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except FileNotFoundError:
        return None            # deleted, or never checked out


def _scan():
    return {p: _digest(p) for p in _tracked_files()}


def snapshot():
    state = {"head": subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO,
                                    capture_output=True, text=True).stdout.strip(),
             "branch": subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                      cwd=REPO, capture_output=True, text=True).stdout.strip(),
             "files": _scan()}
    with open(MANIFEST, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    print(f"tree_guard: snapshot of {len(state['files'])} tracked files "
          f"on {state['branch']} @ {state['head'][:9]}")
    print(f"  -> {MANIFEST}")
    return 0


def verify():
    if not os.path.exists(MANIFEST):
        print("tree_guard: no snapshot to compare against — run `snapshot` first.")
        return 2
    with open(MANIFEST, encoding="utf-8") as fh:
        state = json.load(fh)
    before, after = state["files"], _scan()

    changed = sorted(p for p in before if p in after and before[p] != after[p])
    deleted = sorted(p for p in before if after.get(p) is None and before[p] is not None)
    added = sorted(p for p in after if p not in before)

    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO,
                            capture_output=True, text=True).stdout.strip()
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO,
                          capture_output=True, text=True).stdout.strip()
    moved = (branch != state["branch"] or head != state["head"])
    if moved:
        # Not a failure by itself, but it changes what "changed" means: files
        # differ because the checkout differs, not because someone edited them.
        print(f"tree_guard: NOTE — the checkout moved since the snapshot "
              f"({state['branch']}@{state['head'][:9]} -> {branch}@{head[:9]}). "
              f"Differences below include that.")

    for label, rows in (("MODIFIED", changed), ("DELETED", deleted), ("ADDED (tracked)", added)):
        if rows:
            print(f"tree_guard: {label} ({len(rows)}):")
            for p in rows:
                print(f"    {p}")

    if not (changed or deleted or added):
        print(f"tree_guard: CLEAN — all {len(before)} tracked files byte-identical "
              f"to the snapshot.")
        return 0
    print("tree_guard: the working tree is NOT as it was. Inspect each file above "
          "with `git diff -- <path>` before committing anything.")
    return 1


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "snapshot":
        return snapshot()
    if cmd == "verify":
        return verify()
    print(__doc__.strip().splitlines()[0])
    print("\nusage: python tree_guard.py {snapshot|verify}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
