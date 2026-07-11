"""
AMP — end-to-end verification / demo runner.

Drives ONE production cycle through every module against a running AMP
API and prints a PASS/FAIL verdict for each cross-module linkage, so you can
prove the whole system is wired together in about 20 seconds.

Safe to re-run: it tops raw stock up before the run and leaves the target
machine Running afterwards. Uses only the Python standard library — nothing
to pip install.

Run against the live deployment (default):
    python backend/e2e_sim.py

Run against a local backend instead:
    # bash
    AMP_URL=http://localhost:8000 python backend/e2e_sim.py
    # PowerShell
    $env:AMP_URL="http://localhost:8000"; python backend/e2e_sim.py

Override the demo login if needed:
    AMP_USER / AMP_PASS

Exit code 0 = every critical linkage verified, 1 = something broke.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

BASE = os.environ.get("AMP_URL", "https://flowmes-production.up.railway.app").rstrip("/")
USER = os.environ.get("AMP_USER", "gmats")
PASS = os.environ.get("AMP_PASS", "gmats@2026")  # demo-tenant credential
TOKEN = None
CHECKS = []  # (label, ok) — critical linkages that decide the exit code


def api(method, path, body=None, quiet=False):
    """Tiny JSON HTTP helper. Returns parsed JSON, or None on any error."""
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = "Bearer " + TOKEN
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt else {}
    except urllib.error.HTTPError as e:
        if not quiet:
            print(f"    [HTTP {e.code}] {method} {path}: {e.read().decode()[:140]}")
        return None
    except Exception as e:
        if not quiet:
            print(f"    [ERR] {method} {path}: {e}")
        return None


def check(label, ok, detail=""):
    """Record and print a critical PASS/FAIL linkage assertion."""
    CHECKS.append((label, bool(ok)))
    print(f"    [{'PASS' if ok else 'FAIL'}] {label}" + (f"  ->  {detail}" if detail else ""))
    return ok


def find(lst, **kw):
    for x in (lst or []):
        if all(x.get(k) == v for k, v in kw.items()):
            return x
    return None


def item_stock(code):
    it = find(api("GET", "/inventory/items") or [], item_code=code)
    return (it or {}).get("current_stock"), (it or {}).get("id")


def machine_status(mid):
    return (find(api("GET", "/machines") or [], id=mid) or {}).get("status")


def set_status(mid, status):
    # status is a QUERY param on this endpoint, not a JSON body.
    return api("PATCH", f"/machines/{mid}/status?status={status}", quiet=True)


def line():
    print("-" * 70)


print("=" * 70)
print(f" AMP - END-TO-END VERIFICATION   ({BASE})")
print("=" * 70)

# ── Login + health ────────────────────────────────────────────────
login = api("POST", "/login", {"username": USER, "password": PASS})
TOKEN = (login or {}).get("access_token")
check("Auth - token issued", bool(TOKEN),
      f"role={ (login or {}).get('role') } tenant={ (login or {}).get('tenant') }")
if not TOKEN:
    print("\nCannot continue without a token. Is the API up and are the creds right?")
    sys.exit(1)
h = api("GET", "/health") or {}
check("Backend health", h.get("status") == "ok", f"db={h.get('database')}")

machines = api("GET", "/machines") or []
mac = find(machines, name="CNC-01") or (machines[0] if machines else None)
mid, mname = mac["id"], mac["name"]
stamp = datetime.now().strftime("%H%M%S")

# ── 1. Planning: release a work order ─────────────────────────────
line(); print("MODULE 1/7 -PLANNING  (linkage: work order -> machine -> part/BOM)"); line()
wo_no = f"WO-SIM-{stamp}"
wo = api("POST", "/work-orders", {
    "work_order_no": wo_no, "part_number": "SHAFT-001",
    "batch_number": f"BATCH-SIM-{stamp}", "machine_id": mid,
    "target_quantity": 5, "actual_quantity": 0, "status": "In Progress",
    "planned_start": datetime.now().isoformat(),
    "planned_end": (datetime.now() + timedelta(hours=4)).isoformat(),
})
woid = (wo or {}).get("id")
check("Work order released to floor", bool(woid), f"{wo_no}  (5x SHAFT-001 on {mname})")

# ── 2. Operations: operator clocks on ─────────────────────────────
line(); print("MODULE 2/7 -OPERATIONS  (linkage: operator -> work order -> machine -> OEE)"); line()
oj = api("POST", "/operator/executions", {
    "execution_no": f"EXE-SIM-{stamp}", "operator_name": "Rajan Kumar",
    "machine_id": mid, "work_order_id": woid,
    "job_status": "In Progress", "good_count": 0, "rejected_count": 0,
})
check("Operator execution linked to work order",
      bool(oj) and oj.get("work_order_id") == woid, (oj or {}).get("execution_no", "-"))
set_status(mid, "Running")

# ── 3. Machine floor: breakdown -> downtime -> escalation ─────────
line(); print("MODULE 3/7 -MACHINE FLOOR  (linkage: breakdown -> downtime -> escalation -> alert)"); line()
set_status(mid, "Breakdown")
flipped = machine_status(mid) == "Breakdown"
check("Machine status control (query param)", flipped, f"{mname} -> Breakdown")
dt = api("POST", "/downtime-logs", {
    "machine_id": mid, "reason": "Breakdown",
    "duration": "35 min", "notes": "E2E verify: spindle drive fault"})
check("Downtime logged", bool(dt), "35 min")
esc_b = len(api("GET", "/escalations") or [])
api("POST", "/escalations/from-smart-alerts", {})
esc_a = len(api("GET", "/escalations") or [])
print(f"    (info) smart-alert escalations raised: +{esc_a - esc_b}")
set_status(mid, "Running")
print(f"    {mname} recovered -> Running")

# ── 4. Quality: inspection tied to the WO ─────────────────────────
line(); print("MODULE 4/7 -QUALITY  (linkage: inspection -> work order -> defect escalation)"); line()
qi = api("POST", "/quality/inspections", {
    "inspection_no": f"QI-SIM-{stamp}", "work_order_id": woid, "machine_id": mid,
    "inspector": "Kamal Sharma", "inspected_quantity": 5,
    "passed_quantity": 4, "failed_quantity": 1,
    "defect_category": "Dimensional", "rework_quantity": 1,
    "scrap_quantity": 0, "status": "Rework"})
check("Quality inspection linked to work order",
      bool(qi) and qi.get("work_order_id") == woid, "5 inspected / 4 pass / 1 fail")

# ── 5. Inventory / BOM: completion auto-moves stock (the star) ────
line(); print("MODULE 5/7 -INVENTORY / BOM  (linkage: WO complete -> consume raw -> produce finished)"); line()
# make sure there is raw stock to consume, so the run is clean and repeatable
steel, iid = item_stock("RM-STEEL-001")
if iid is not None and (steel or 0) < 100:
    api("PATCH", f"/inventory/items/{iid}", {"current_stock": 820})
    print("    (setup) topped RM-STEEL-001 up to 820 for a clean run")
steel_b, _ = item_stock("RM-STEEL-001")
fg_b, _ = item_stock("FG-SHAFT-001")
txn_b = len(api("GET", "/inventory/transactions") or [])
print(f"    BEFORE  raw steel: {steel_b}   finished shafts: {fg_b}   txns: {txn_b}")
api("PATCH", f"/work-orders/{woid}", {"status": "Completed", "actual_quantity": 5})
time.sleep(1)
steel_a, _ = item_stock("RM-STEEL-001")
fg_a, _ = item_stock("FG-SHAFT-001")
txn_a = len(api("GET", "/inventory/transactions") or [])
print(f"    AFTER   raw steel: {steel_a}   finished shafts: {fg_a}   txns: {txn_a}")
check("BOM consumed raw material", steel_a is not None and steel_a < steel_b,
      f"steel {steel_b} -> {steel_a} ({(steel_a or 0) - (steel_b or 0):+})")
check("BOM produced finished goods", fg_a is not None and fg_a > fg_b,
      f"finished {fg_b} -> {fg_a} ({(fg_a or 0) - (fg_b or 0):+})")
check("Inventory transactions auto-written", txn_a > txn_b, f"+{txn_a - txn_b} booked")

# ── 6. Procurement: low stock -> reorder escalations ─────────────
line(); print("MODULE 6/7 -PROCUREMENT  (linkage: low stock -> escalation -> purchasing)"); line()
lb = len(api("GET", "/escalations") or [])
api("POST", "/inventory/generate-low-stock-escalations", {})
la = len(api("GET", "/escalations") or [])
low = [i for i in (api("GET", "/inventory/items") or [])
       if i.get("current_stock", 0) <= i.get("reorder_level", 0)]
print(f"    (info) {len(low)} items at/below reorder level  ->  +{la - lb} reorder escalations")

# ── 7. Intelligence: exec + predictive + AI rollup ───────────────
line(); print("MODULE 7/7 -INTELLIGENCE  (linkage: all module data -> exec / predictive / AI)"); line()
mgmt = api("GET", "/analytics/management") or {}
check("Exec OEE rollup present", mgmt.get("avg_oee") is not None,
      f"OEE {mgmt.get('avg_oee')}%  (A {mgmt.get('avg_availability')} / "
      f"P {mgmt.get('avg_performance')} / Q {mgmt.get('avg_quality')})")
pred = api("GET", "/analytics/predictive-maintenance") or []
top = pred[0] if pred else {}
check("Predictive risk scored", bool(top.get("risk_score") is not None),
      f"{top.get('machine_name')} -> {top.get('risk_level')} ({top.get('risk_score')}/100)")
ai_b = len(api("GET", "/ai/recommendations") or [])
api("POST", "/ai/generate-recommendations", {})
ai_a = len(api("GET", "/ai/recommendations") or [])
print(f"    (info) AI recommendations generated: +{ai_a - ai_b}  "
      f"(LLM copilot enabled: {(api('GET', '/ai/status') or {}).get('enabled')})")

# ── Verdict ───────────────────────────────────────────────────────
line()
passed = sum(1 for _, ok in CHECKS if ok)
total = len(CHECKS)
print(f" VERIFICATION: {passed}/{total} linkages passed")
for label, ok in CHECKS:
    print(f"   {'v' if ok else 'x'}  {label}")
ok_all = passed == total
print("=" * 70)
print(" RESULT:  " + ("ALL LINKAGES VERIFIED - one work order rippled through every module."
                      if ok_all else "SOME CHECKS FAILED - see [FAIL] lines above."))
print("=" * 70)
sys.exit(0 if ok_all else 1)
