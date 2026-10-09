"""Mutate the dry-contact counter; every one must turn the suite red.

EVERY MUTATION BELOW INFLATES OR FABRICATES PRODUCTION, which is the only
direction this component can fail in that nobody catches. A count that is too
low gets queried by the person whose bonus depends on it. A count that is too
high gets believed.

    bounce counted            one cycle becomes three, at random
    the first sample counted  a phantom part on every gateway restart
    a dead port read as zero  a stopped adapter looks like a stopped machine
    the shared port closed    three machines silently stop counting
    a release counted         every shot counted twice

Originals are copied aside BEFORE the first mutation and restored after each run
and in `finally`. A harness that leaves a mutant on disk is worse than none.

Run: python edge/mutate_contact_counting.py
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Keyed by the REPO-RELATIVE PATH each mutation names, the layout every other
# harness uses and the one test_mutation_anchors_apply.py reads.
TARGETS = {
    "edge/ampedge/adapters/contact.py":
        os.path.join(HERE, "ampedge", "adapters", "contact.py"),
    "edge/ampedge/config.py": os.path.join(HERE, "ampedge", "config.py"),
}
TEST = os.path.join(HERE, "test_contact_counting.py")

C = "edge/ampedge/adapters/contact.py"
G = "edge/ampedge/config.py"

MUTATIONS = [
    # ── Bounce ───────────────────────────────────────────────────────
    ("contact bounce is counted, so one cycle becomes several", C,
     "            if now - self._changed_at[name] < self.debounce_s:\n"
     "                continue                 # contact bounce, not a second cycle",
     "            if False:\n"
     "                continue                 # contact bounce, not a second cycle"),
    ("the debounce window is measured from the wrong instant", C,
     "            self._changed_at[name] = now\n"
     "            if now_high:",
     "            if now_high:"),
    ("a debounce long enough to swallow cycles is accepted", C,
     "        if self.debounce_ms > MAX_DEBOUNCE_MS:",
     "        if False:"),

    # ── The first sample ─────────────────────────────────────────────
    ("a line already high at startup counts as a cycle", C,
     "            if was is None:",
     "            if False:"),

    # ── Which edge ───────────────────────────────────────────────────
    ("the release is counted as well as the close, doubling every shift", C,
     "            if now_high:\n                self.counts[name] += 1",
     "            if True:\n                self.counts[name] += 1"),
    ("only the release counts, so the count lags by a cycle", C,
     "            if now_high:\n                self.counts[name] += 1",
     "            if not now_high:\n                self.counts[name] += 1"),

    # ── Absence ──────────────────────────────────────────────────────
    ("a port that stopped answering keeps reporting its last count", C,
     "            if w.error:\n"
     "                # The sampler is failing. The count we hold is STALE, not",
     "            if False:\n"
     "                # The sampler is failing. The count we hold is STALE, not"),
    ("a port never sampled reports a count of zero", C,
     "            if w.last_sample_at is None:",
     "            if False:"),
    ("a sampler that cannot read the lines says it succeeded", C,
     "            self.error = f\"{self.port} stopped answering: {exc}\"\n"
     "            return False",
     "            self.error = f\"{self.port} stopped answering: {exc}\"\n"
     "            return True"),
    ("an unknown line is read as a line that never fires", C,
     "            if key is None:",
     "            if False:"),

    # ── One port, four machines ──────────────────────────────────────
    ("the first machine to disconnect closes the port for all four", C,
     "            if w._refs <= 0:",
     "            if True:"),
    ("every machine gets its own watcher, so three of four never sample", C,
     "    w = _WATCHERS.get(port)\n    if w is None:",
     "    w = None\n    if w is None:"),
    ("the four lines share one counter", C,
     '        self.counts = {name: 0 for name in ("cts", "dsr", "cd", "ri")}',
     '        self.counts = dict.fromkeys(("cts", "dsr", "cd", "ri"), 0)\n'
     '        self.counts = _SHARED'),

    # ── The config refuses what cannot work ──────────────────────────
    ("a contact with no wire is accepted and silently counts nothing", G,
     '        if not str(connection.get("serial_port") or "").strip():',
     "        if False:"),
    ("a contact given a network address is accepted", G,
     '        for key in ("host", "url"):',
     "        for key in ():"),
    ("'contact' is dropped from the supported protocols", G,
     'PROTOCOLS = ("opcua", "modbus", "focas", "contact")',
     'PROTOCOLS = ("opcua", "modbus", "focas")'),
]


def run():
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, TEST], capture_output=True, text=True,
                          timeout=300, env=env, cwd=os.path.dirname(HERE)).returncode


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
