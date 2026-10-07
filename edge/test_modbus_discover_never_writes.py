"""The commissioning tool must never write to a customer's controller.

It is used by somebody standing next to a running injection moulding machine,
on a shared RS-485 line, to find which register is the shot counter. A write
there could change a setpoint on a machine running unattended. "Read only" is
the whole basis on which it is safe to point at a stranger's plant, so it is
asserted from the source rather than trusted to review.

Read from the AST, not with a grep: a grep for "write" matches a comment saying
it never writes, which is precisely the line that would be there.

Run: python edge/test_modbus_discover_never_writes.py
"""
import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "modbus_discover.py")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

failures = []

# Every pymodbus call that puts anything on the wire as a write. Named in full
# so a new one cannot slip in under a pattern this file happens not to match.
WRITES = {
    "write_coil", "write_coils", "write_register", "write_registers",
    "mask_write_register", "readwrite_registers", "write_file_record",
    "report_slave_id", "execute",
}
# The only two the tool is allowed to make.
READS = {"read_holding_registers", "read_input_registers"}


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def main():
    tree = ast.parse(io.open(SOURCE, encoding="utf-8").read())

    # EVERY ATTRIBUTE NAME THE FILE MENTIONS, not only the ones in call
    # position. The tool reads via `fn = cli.read_holding_registers` and then
    # calls `fn(...)`, so a call-only walker sees neither the read nor -- the
    # part that matters -- a WRITE bound the same way. Any mention of the name
    # is enough to fail this.
    #
    # String constants handed to getattr() count too: getattr(cli, "write_coil")
    # is a write that no attribute node would reveal.
    named = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            named.add(node.attr)
        elif isinstance(node, ast.Name):
            named.add(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            named.add(node.value)
    called = named

    print("=" * 74)
    print("A COMMISSIONING TOOL POINTED AT SOMEBODY ELSE'S RUNNING MACHINE")
    print("=" * 74)

    offending = sorted(called & WRITES)
    check("it calls no pymodbus write, anywhere in the file",
          not offending, f"calls {offending}")

    # Non-vacuity: if the tool stopped reading too, the check above would pass
    # while proving nothing about a file that no longer talks to a controller.
    reads = sorted(called & READS)
    check("...and it does still read, so this is not a check of nothing",
          bool(reads), f"found no read call among {sorted(called)[:12]}")

    # The docstring is the promise a person reads before running it next to a
    # machine. If the behaviour ever changes, that sentence must change with it.
    doc = ast.get_docstring(tree) or ""
    check("the module docstring states READ ONLY",
          "READ ONLY" in doc.upper(), doc[:80])

    print(f"\n  reads used: {reads}")


if __name__ == "__main__":
    main()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED - the discovery tool cannot write")
