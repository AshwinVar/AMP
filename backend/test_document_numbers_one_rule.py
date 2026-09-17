"""No backend module numbers a document from a row count.

``f"INV-{7000 + count + 1}"`` looks like a sequence and is a population. Delete a
row and the count drops, so the next document takes a number still printed on a
live one. That shipped three separate times:

  * enterprise inventory (MIS / GRN / CC): an IntegrityError 500 (ADR-0012);
  * GMATS tax invoices, MINs, proformas: no constraint, so two live invoices
    were simply stored with the same number (ADR-0012 addendum);
  * the simulator's QI / EXE ticks: every tick after one deletion hit the
    constraint and rolled back the rest of that tenant's tick.

Each was fixed by calling doc_numbers.allocate, the one rule. This guard reads
every backend module and fails on any ``*_no`` field (keyword or assignment)
whose f-string adds to a count, so a fourth copy fails CI instead of production.
A count in a display label ("Breakdown Alert #3") is not a document number and
is not flagged.

Run:  python backend/test_document_numbers_one_rule.py     (exit 0 = pass)
"""
import ast
import glob
import os

BACKEND = os.path.dirname(os.path.abspath(__file__))


def _adds_to_a_count(value):
    if not isinstance(value, ast.JoinedStr):
        return False
    for part in value.values:
        if not isinstance(part, ast.FormattedValue):
            continue
        for add in ast.walk(part.value):
            if not (isinstance(add, ast.BinOp) and isinstance(add.op, ast.Add)):
                continue
            for sub in ast.walk(add):
                if isinstance(sub, ast.Name) and sub.id == "count":
                    return True
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "count"):
                    return True
    return False


def count_derived_numbers(source):
    """Line numbers where a ``*_no`` field is built by adding to a count."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.keyword) and (node.arg or "").endswith("_no"):
            if _adds_to_a_count(node.value):
                hits.append(node.value.lineno)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id if isinstance(t, ast.Name) else getattr(t, "attr", "") for t in targets]
            if any(n.endswith("_no") for n in names) and _adds_to_a_count(node.value):
                hits.append(node.lineno)
    return sorted(set(hits))


def _backend_modules():
    paths = []
    for pattern in ("*.py", "ai/*.py", "amp_ai/*.py", "amp_ai/**/*.py"):
        paths.extend(glob.glob(os.path.join(BACKEND, pattern), recursive=True))
    return sorted({p for p in paths if not os.path.basename(p).startswith("test_")})


def test_no_module_numbers_a_document_from_a_row_count():
    modules = _backend_modules()
    assert len(modules) >= 100, f"found only {len(modules)} backend modules"
    names = {os.path.basename(p) for p in modules}
    for expected in ("gmats_inventory_routes.py", "enterprise_inventory_routes.py", "factory_simulator.py"):
        assert expected in names, f"the scan no longer reaches {expected}"
    offenders = {}
    for path in modules:
        with open(path, encoding="utf-8") as fh:
            hits = count_derived_numbers(fh.read())
        if hits:
            offenders[os.path.relpath(path, BACKEND)] = hits
    assert not offenders, ("document numbers built from a row count (use doc_numbers.allocate): "
                           f"{offenders}")
    print(f"PASS none of {len(modules)} backend modules numbers a document from a row count")


def test_the_guard_catches_what_it_claims_to():
    probe = '''
def a(db, count):
    return Invoice(invoice_no=f"INV-{7000 + count + 1}")
def b(db, M):
    return Note(min_no=f"MIN-{4000 + db.query(M).count() + 1}")
def c(db, count):
    slip_no = f"MIS-{5000 + count + 1}"
def d(db, n):
    return Plan(plan_no=f"PP-{2000 + n}")
def e(db, count, rows):
    return Escalation(title=f"Breakdown Alert #{count + 1}", notes=f"{count} rows")
'''
    assert count_derived_numbers(probe) == [3, 5, 7], count_derived_numbers(probe)
    print("PASS the guard flags invoice_no / min_no / slip_no built from a count; ignores labels and seed loops")


if __name__ == "__main__":
    test_no_module_numbers_a_document_from_a_row_count()
    test_the_guard_catches_what_it_claims_to()
    print("ALL DOCUMENT-NUMBER RULE TESTS PASSED")
