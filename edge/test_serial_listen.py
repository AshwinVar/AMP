"""The serial listener must never write, and must never silently do nothing.

TWO THINGS ARE ASSERTED, and they fail in opposite directions.

NEVER WRITES. This is pointed at a DB9 inside the cabinet of a running
injection moulding machine, on a port that may be shared with a robot or the
HMI's own link to the PLC. A single stray byte on a line somebody else is
talking on can corrupt their frame. "It only reads" is the entire basis on
which it is safe to plug into a stranger's plant, so it is read out of the
source rather than trusted to review -- and read from the AST, because a grep
for "write" matches the comment promising it never writes, which is exactly the
line that would be there.

NEVER SILENTLY DOES NOTHING. `--sweep` with no `--port` used to print the port
list and exit 0. On the floor, with one adapter in one machine, that is the
whole command being a no-op that looks like a completed run -- and a sweep
producing no output is indistinguishable from a port that stayed silent, which
is the one distinction this tool exists to make. A tool whose failure mode is
"looks like it worked" is worse on a shop floor than one that crashes.

Run: python edge/test_serial_listen.py
"""
import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "serial_listen.py")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

failures = []

# Every pyserial member that puts a byte on the wire, or asserts a line that a
# device can see. Named in full so a new one cannot slip in under a pattern
# this file happens not to match. `write_timeout` is a constructor keyword and
# not a transmission, so it is excluded by exact-name matching rather than by a
# substring test.
WRITES = {
    "write", "writelines", "flushOutput", "reset_output_buffer",
    "send_break", "sendBreak", "setBreak", "break_condition",
    "setRTS", "rts", "setDTR", "dtr",
}


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def names_mentioned(tree):
    """Every attribute, name and getattr-string the module mentions.

    Attributes in any position, not only call position: a tool that did
    `send = con.write` and later called `send(...)` would hide a write from a
    call-only walker. String constants handed to getattr() count too, because
    getattr(con, "write") is a write no attribute node would reveal.
    """
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
    return found


def test_it_never_writes():
    tree = ast.parse(io.open(SOURCE, encoding="utf-8").read())
    mentioned = names_mentioned(tree)
    offending = sorted(mentioned & WRITES)
    check("the listener mentions no pyserial write or line-control call",
          not offending, f"found {', '.join(offending)}")

    # Non-vacuity: the detector must actually be able to see a write. Without
    # this the check above passes just as well on an empty file.
    probe = ast.parse("con.write(b'x')\n")
    check("the detector can see a write when there is one",
          "write" in names_mentioned(probe) & WRITES)

    # And it must be reading, or it is not doing its job at all.
    check("the listener does read from the port", "read" in mentioned)


def test_sweep_without_a_port_is_not_a_no_op():
    """The defect: --sweep with no --port printed the ports and exited 0.

    Driven through choose_port() with the port list stubbed, so this needs no
    hardware and no adapter plugged in.
    """
    sys.path.insert(0, HERE)
    try:
        import serial_listen
    except ImportError as exc:                     # pragma: no cover
        check("serial_listen imports", False, str(exc))
        return

    real = serial_listen.ports
    try:
        serial_listen.ports = lambda: ["COM4"]
        check("one port and no --port: it is used, not listed and abandoned",
              serial_listen.choose_port(None) == "COM4",
              f"chose {serial_listen.choose_port(None)!r}")
        check("an explicitly named port still wins",
              serial_listen.choose_port("COM9") == "COM9")

        serial_listen.ports = lambda: ["COM3", "COM4"]
        check("several ports and no --port: it refuses rather than guessing",
              serial_listen.choose_port(None) is None)
        check("...but an explicit one is honoured",
              serial_listen.choose_port("COM3") == "COM3")

        serial_listen.ports = lambda: []
        check("no ports at all: it refuses",
              serial_listen.choose_port(None) is None)
    finally:
        serial_listen.ports = real


def test_main_exits_non_zero_when_it_did_nothing():
    """An operator reads the exit code; a no-op must not report success."""
    sys.path.insert(0, HERE)
    import serial_listen

    real = serial_listen.ports
    try:
        serial_listen.ports = lambda: []
        rc = serial_listen.main(["--sweep"])
        check("a sweep with nothing to sweep exits non-zero", rc == 1,
              f"exited {rc}")

        serial_listen.ports = lambda: ["COM3", "COM4"]
        rc = serial_listen.main(["--sweep"])
        check("an ambiguous sweep exits non-zero too", rc == 1, f"exited {rc}")

        # --ports is a listing, not a run; it is allowed to succeed.
        serial_listen.ports = lambda: []
        check("--ports still exits 0, because listing is its whole job",
              serial_listen.main(["--ports"]) == 0)
    finally:
        serial_listen.ports = real


#: Modules the standard library provides, so an import of one proves nothing
#: about requirements.txt. Only third-party names have to be declared.
STDLIB = set(sys.stdlib_module_names) | {"__future__"}

#: Imported only under a try/except that handles the absence, or only by the
#: repo's own tooling. Each needs a reason, the way .coveragerc demands one.
UNDECLARED_OK = {
    # The gateway's own packages and test helpers.
    "ampedge",
}


def test_every_third_party_import_is_declared():
    """A gateway that pip-installs clean must also RUN clean.

    THE DEFECT THIS CAUGHT. pymodbus declares pyserial as an optional extra
    (`pyserial>=3.5; extra == "serial"`), so plain `pip install pymodbus` does
    not bring it. edge/requirements.txt listed pymodbus and not pyserial, which
    meant Modbus RTU — the only route into a controller with no Ethernet, and
    the route the first moulding floor needs — raised on a fresh plant PC. It
    worked here because this laptop happened to have pyserial already.

    That is the worst shape a dependency bug can take: invisible in the repo,
    visible only at a customer's site, on the machine nobody can SSH into.
    """
    req = os.path.join(HERE, "requirements.txt")
    declared = set()
    for line in io.open(req, encoding="utf-8"):
        line = line.split("#")[0].strip()
        if not line:
            continue
        name = line.split(">")[0].split("<")[0].split("=")[0].split("[")[0]
        declared.add(name.strip().lower().replace("-", "_"))
    # Import name vs distribution name, where they differ.
    aliases = {"pyserial": "serial", "pyyaml": "yaml", "paho_mqtt": "paho"}
    importable = {aliases.get(d, d) for d in declared} | declared

    # WHAT IS SCANNED: the code that SHIPS to a plant PC -- the ampedge package
    # and the standalone commissioning tools beside it. NOT test_*.py: the
    # end-to-end suites import models, database, mqtt_service and tenancy on
    # purpose, because the last hop they prove is the real AMP handler, and CI
    # installs backend/requirements.txt for exactly that. Those imports are the
    # architectural boundary being tested, not a missing dependency, and a
    # guard that flagged them would be read as noise and then ignored.
    shipped = []
    for fname in sorted(os.listdir(HERE)):
        if fname.endswith(".py") and not fname.startswith("test_"):
            shipped.append(os.path.join(HERE, fname))
    pkg = os.path.join(HERE, "ampedge")
    for root, _dirs, files in os.walk(pkg):
        shipped.extend(os.path.join(root, f) for f in sorted(files)
                       if f.endswith(".py"))

    missing = {}
    for path in shipped:
        fname = os.path.relpath(path, HERE).replace("\\", "/")
        tree = ast.parse(io.open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots = [node.module.split(".")[0]]
            else:
                continue
            for root in roots:
                low = root.lower()
                if low in STDLIB or low in importable or low in UNDECLARED_OK:
                    continue
                if os.path.exists(os.path.join(HERE, root + ".py")):
                    continue          # a sibling module in edge/
                if os.path.isdir(os.path.join(HERE, root)):
                    continue          # a package inside edge/
                missing.setdefault(low, set()).add(fname)

    check("every third-party import in edge/ is in requirements.txt",
          not missing,
          "; ".join(f"{m} (in {', '.join(sorted(f))})"
                    for m, f in sorted(missing.items())))
    check("pyserial is declared, because pymodbus only offers it as an extra",
          "pyserial" in declared, f"declared: {sorted(declared)}")
    # Non-vacuity: the parser must have actually read the file.
    check("requirements.txt was parsed, not silently empty",
          len(declared) >= 5, f"{len(declared)} packages")
    # Non-vacuity: it must have actually walked the shipped code.
    check("the shipped gateway code was actually scanned",
          len(shipped) >= 5, f"{len(shipped)} files")


def main():
    print("=" * 74)
    print("THE SERIAL LISTENER IS READ-ONLY, AND NEVER A SILENT NO-OP")
    print("=" * 74)
    test_it_never_writes()
    test_sweep_without_a_port_is_not_a_no_op()
    test_main_exits_non_zero_when_it_did_nothing()
    test_every_third_party_import_is_declared()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL CHECKS PASSED - serial_listen.py reads, and says so honestly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
