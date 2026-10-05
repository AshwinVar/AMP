"""Mutation harness for the GMATS proforma-issue rule.

Every mutation either puts a piece of the retired tax-invoice flow back, or
weakens the deduction / its inverse / the boundary around them. Each MUST be
caught — backend mutations by test_gmats_proforma_issue.py, frontend ones by
components/GmatsInventory.test.tsx, which is the only place a button label or a
print window can be seen at all.

A pattern that does not apply reports as a SURVIVOR, not a pass: a disabled
mutation measures nothing and looks exactly like a guard that works.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_gmats_proforma_issue.py
"""
import io
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(os.path.dirname(HERE), "frontend")
BACKEND_TEST = os.path.join(HERE, "test_gmats_proforma_issue.py")
ROUTES = "gmats_inventory_routes.py"
SCREEN = os.path.join("..", "frontend", "components", "GmatsInventory.tsx")

MUTATIONS = [
    # ── the deduction itself ──────────────────────────────────────────
    ("issuing no longer takes the stock off the shelf", ROUTES,
     '            item.physical_stock -= l.qty                              # exact (guard guarantees >= 0)\n',
     ''),
    ("issuing no longer clears the reservation", ROUTES,
     '            item.reserved_stock = max(0, item.reserved_stock - l.qty)  # clear reservation\n',
     ''),
    ("the over-issue guard is gone, so the deduction clamps again", ROUTES,
     '            if qty > item.physical_stock:\n',
     '            if False:\n'),
    ("an issued proforma stays Open, so it can be issued twice", ROUTES,
     '    p.status = "Issued"\n    db.commit()\n    _audit(db, current_user, p.tenant_code, "gmats_issue_proforma"',
     '    p.status = "Open"\n    db.commit()\n    _audit(db, current_user, p.tenant_code, "gmats_issue_proforma"'),
    ("issuing writes the retired tax-invoice status", ROUTES,
     '    p.status = "Issued"\n    db.commit()',
     '    p.status = "Invoiced"\n    db.commit()'),
    ("a cancelled or already-issued proforma can be issued", ROUTES,
     '    if not p or p.status != "Open":\n        raise HTTPException(status_code=400, detail="Only open proformas can be issued")',
     '    if not p:\n        raise HTTPException(status_code=400, detail="Only open proformas can be issued")'),
    # The anchor runs on into the comment below the query: cancel_proforma has a
    # byte-identical guard+query pair, and it appears FIRST in the file, so a
    # shorter pattern would silently mutate the wrong handler and measure nothing.
    ("another company can issue this company's proforma", ROUTES,
     '    _guard_record(current_user, p.tenant_code)\n'
     '    lines = db.query(models.GmatsProformaLine).filter(models.GmatsProformaLine.proforma_id == pid).all()\n'
     '    # Guard the TOTAL physical needed per item BEFORE deducting',
     '    lines = db.query(models.GmatsProformaLine).filter(models.GmatsProformaLine.proforma_id == pid).all()\n'
     '    # Guard the TOTAL physical needed per item BEFORE deducting'),

    # ── the inverse ───────────────────────────────────────────────────
    ("undo puts the stock back but forgets the reservation", ROUTES,
     '            item.reserved_stock += l.qty      # and still spoken for — the proforma is Open again\n',
     ''),
    ("undo forgets to put the stock back", ROUTES,
     '            item.physical_stock += l.qty      # back on the shelf\n',
     ''),
    ("undo cancels the document instead of reopening it", ROUTES,
     '    p.status = "Open"\n    db.commit()\n    _audit(db, current_user, p.tenant_code, "gmats_undo_issue"',
     '    p.status = "Cancelled"\n    db.commit()\n    _audit(db, current_user, p.tenant_code, "gmats_undo_issue"'),
    ("anything can be undone, including an Open proforma (stock invented)", ROUTES,
     '    if p.status not in ISSUED_STATUSES:\n',
     '    if False:\n'),
    ("a legacy issue cannot be undone at all", ROUTES,
     'ISSUED_STATUSES = ("Issued", "Invoiced")',
     'ISSUED_STATUSES = ("Issued",)'),
    ("undo leaves the legacy tax-invoice row pointing at an Open proforma", ROUTES,
     '    legacy = db.query(models.GmatsInvoice).filter(models.GmatsInvoice.proforma_id == p.id).all()\n',
     '    legacy = []\n'),
    ("undo matches the legacy invoice on the wrong column", ROUTES,
     'models.GmatsInvoice.proforma_id == p.id',
     'models.GmatsInvoice.id == p.id'),
    ("another company can undo this company's issue", ROUTES,
     '    _guard_record(current_user, p.tenant_code)\n    if p.status not in ISSUED_STATUSES:',
     '    if p.status not in ISSUED_STATUSES:'),

    # ── the listing the issued tab reads ──────────────────────────────
    ("the status filter is ignored, so the issued list shows everything", ROUTES,
     '    if wanted:\n        q = q.filter(models.GmatsProforma.status.in_(wanted))\n',
     ''),

    # ── the screen ────────────────────────────────────────────────────
    ("the button offers a tax invoice again", SCREEN,
     'Generate Proforma Invoice →', 'Generate Tax Invoice →'),
    ("the button posts to the retired tax-invoice endpoint", SCREEN,
     'await apiPost(`/gmats/proformas/${pid}/issue`, {});',
     'await apiPost(`/gmats/proformas/${pid}/invoice`, {});'),
    ("the issued list drops the documents issued before the change", SCREEN,
     'status=Issued,Invoiced', 'status=Issued'),
    ("the tab is called Tax Invoice again", SCREEN,
     '"Stock", "Proforma (Reserve)", "Issued"', '"Stock", "Proforma (Reserve)", "Tax Invoice"'),
]

FRONTEND_FILES = {SCREEN}


def run_backend():
    env = dict(os.environ, DATABASE_URL="sqlite:///./ci_mut_gmats.db", PYTHONDONTWRITEBYTECODE="1",
               PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, BACKEND_TEST], cwd=HERE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600, env=env)
    return r.returncode, (r.stdout + r.stderr)


def run_frontend():
    r = subprocess.run(["npx", "vitest", "run", "components/GmatsInventory.test.tsx"],
                       cwd=FRONTEND, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=900, shell=(os.name == "nt"))
    return r.returncode, (r.stdout + r.stderr)


def main():
    print("Baseline (unmutated) must PASS:")
    for label, run in (("backend", run_backend), ("frontend", run_frontend)):
        rc, out = run()
        if rc != 0:
            print(out[-2500:])
            print(f"BASELINE FAILS ({label}) — fix the code before mutation testing.")
            return 1
        print(f"  PASS  {label}")
    print()

    caught, survived = 0, []
    for i, (label, rel, find, repl) in enumerate(MUTATIONS, 1):
        path = os.path.join(HERE, rel)
        run = run_frontend if rel in FRONTEND_FILES else run_backend
        # newline="" on read AND write, so a CRLF file round-trips byte-exact.
        original = io.open(path, encoding="utf-8", newline="").read()
        crlf = "\r\n" in original
        f = find.replace("\n", "\r\n") if crlf else find
        r_ = repl.replace("\n", "\r\n") if crlf else repl
        if f not in original:
            survived.append(f"{label}  [PATTERN DID NOT APPLY — unmeasured]")
            print(f"{i:2}. SURVIVED (pattern missing)  {label}")
            continue
        try:
            io.open(path, "w", encoding="utf-8", newline="").write(original.replace(f, r_, 1))
            rc, out = run()
        finally:
            io.open(path, "w", encoding="utf-8", newline="").write(original)
        if rc != 0:
            caught += 1
            lines = [ln.strip() for ln in out.splitlines()
                     if ln.strip().startswith(("AssertionError", "E   ", "×", "FAIL")) or "Error:" in ln]
            print(f"{i:2}. caught     {label}")
            print(f"      -> {lines[0][:100] if lines else '(non-zero exit)'}")
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
