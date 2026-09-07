"""Mutation harness for the cancelled-PO rule (#566).

Every mutation restores some part of the behaviour where a withdrawn purchase
order was treated as late, or weakens the new state so it stops partitioning the
census. Each MUST be caught by test_cancelled_po_not_late.py.

Note the file is CRLF. Multi-line patterns are written with \n and converted, so
they actually apply — a pattern that silently fails to match reports as a
survivor here rather than passing quietly, because a disabled mutation measures
nothing and looks identical to a guard that works.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_cancelled_po.py
"""
import io
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST = os.path.join(HERE, "test_cancelled_po_not_late.py")

MUTATIONS = [
    ("_state no longer recognises a cancelled PO — it falls through to late",
     "ai/supply.py",
     '    if status in CANCELLED_STATUSES:\n        return "cancelled"\n',
     ''),
    ("cancelled ranked BEFORE the receipt test — a delivery that actually "
     "arrived gets reclassified as withdrawn",
     "ai/supply.py",
     '    if status in RECEIVED_STATUSES or (ordered > 0 and received >= ordered):\n'
     '        return "received"\n'
     '    if status in CANCELLED_STATUSES:\n        return "cancelled"\n',
     '    if status in CANCELLED_STATUSES:\n        return "cancelled"\n'
     '    if status in RECEIVED_STATUSES or (ordered > 0 and received >= ordered):\n'
     '        return "received"\n'),
    ("the US spelling drops out of the terminal set",
     "ai/supply.py",
     'CANCELLED_STATUSES = {"cancelled", "canceled"}',
     'CANCELLED_STATUSES = {"cancelled"}'),
    ("status no longer lowercased/trimmed before matching",
     "ai/supply.py",
     '    status = (po.status or "").strip().lower()',
     '    status = po.status or ""'),
    ("a cancelled PO counts as inbound load again (summary)",
     "ai/supply.py",
     '        if p.expected_delivery_date in due_set and _state(p, today) not in _NOT_INBOUND:\n'
     '            due_count[p.expected_delivery_date] += 1\n'
     '    upcoming = [{"date": d.isoformat(), "pos": due_count[d]} for d in upcoming_days]\n'
     '\n    # Delivery reliability, plant-wide',
     '        if p.expected_delivery_date in due_set and _state(p, today) != "received":\n'
     '            due_count[p.expected_delivery_date] += 1\n'
     '    upcoming = [{"date": d.isoformat(), "pos": due_count[d]} for d in upcoming_days]\n'
     '\n    # Delivery reliability, plant-wide'),
    ("a cancelled PO counts as inbound load again (drill-down)",
     "ai/supply.py",
     '        if p.expected_delivery_date in due_set and _state(p, today) not in _NOT_INBOUND:\n'
     '            due_count[p.expected_delivery_date] += 1\n'
     '    upcoming = [{"date": d.isoformat(), "pos": due_count[d]} for d in upcoming_days]\n'
     '\n    # Reliability:',
     '        if p.expected_delivery_date in due_set and _state(p, today) != "received":\n'
     '            due_count[p.expected_delivery_date] += 1\n'
     '    upcoming = [{"date": d.isoformat(), "pos": due_count[d]} for d in upcoming_days]\n'
     '\n    # Reliability:'),
    ("_NOT_INBOUND loses cancelled",
     "ai/supply.py",
     '_NOT_INBOUND = ("received", "cancelled")',
     '_NOT_INBOUND = ("received",)'),
    ("cancelled folded into the late count in the reliability denominator",
     "ai/supply.py",
     '    resolved = totals["received"] + totals["late"]\n\n    by_supplier',
     '    resolved = totals["received"] + totals["late"] + totals["cancelled"]\n\n    by_supplier'),
    ("the summary stops publishing the cancelled count",
     "ai/supply.py",
     '        "late": totals["late"],\n        "cancelled": totals["cancelled"],\n'
     '        "receipt_rate": _pct(received_units, ordered_units),',
     '        "late": totals["late"],\n'
     '        "receipt_rate": _pct(received_units, ordered_units),'),
    ("cancelled POs put back on the chase list",
     "ai/supply.py",
     '        if state in ("late", "at_risk"):\n            due = p.expected_delivery_date\n'
     '            chase.append({\n                "po_no": p.po_no,\n                "supplier": name_,',
     '        if state in ("late", "at_risk", "cancelled"):\n            due = p.expected_delivery_date\n'
     '            chase.append({\n                "po_no": p.po_no,\n                "supplier": name_,'),
]


def run_test():
    env = dict(os.environ, DATABASE_URL="sqlite:///./ci_mut_po.db")
    r = subprocess.run([sys.executable, TEST], cwd=HERE, capture_output=True,
                       text=True, timeout=180, env=env)
    return r.returncode, (r.stdout + r.stderr)


def main():
    print("Baseline (unmutated) must PASS:")
    rc, out = run_test()
    if rc != 0:
        print(out[-2500:])
        print("BASELINE FAILS — fix the code before mutation testing.")
        return 1
    print("  PASS\n")

    caught, survived = 0, []
    for i, (label, rel, find, repl) in enumerate(MUTATIONS, 1):
        path = os.path.join(HERE, rel)
        # newline="" on read AND write, so a CRLF file round-trips byte-exact
        # and the restore does not silently rewrite every line ending.
        original = io.open(path, encoding="utf-8", newline="").read()
        crlf = "\r\n" in original
        f = find.replace("\n", "\r\n") if crlf else find
        r_ = repl.replace("\n", "\r\n") if crlf else repl
        if f not in original:
            survived.append(f"{label}  [PATTERN DID NOT APPLY — unmeasured]")
            print(f"{i:2}. SURVIVED (pattern missing)  {label}")
            continue
        try:
            io.open(path, "w", encoding="utf-8", newline="").write(
                original.replace(f, r_, 1))
            rc, out = run_test()
        finally:
            io.open(path, "w", encoding="utf-8", newline="").write(original)
        if rc != 0:
            caught += 1
            fails = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("FAIL")]
            print(f"{i:2}. caught     {label}")
            print(f"      -> {fails[0][:92] if fails else '(non-zero exit)'}")
        else:
            survived.append(label)
            print(f"{i:2}. SURVIVED   {label}")

    print()
    print("=" * 74)
    print(f"{caught}/{len(MUTATIONS)} mutations caught")
    for s in survived:
        print(f"  SURVIVED: {s}")
    print("=" * 74)
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
