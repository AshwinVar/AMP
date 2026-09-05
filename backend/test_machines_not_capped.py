"""GET /machines must return EVERY machine. A row cap here corrupts three KPIs.

WHY THIS TEST EXISTS
--------------------
`machines_routes.py` is the only list endpoint with no row cap, and that stands
out: `/inventory/items` caps at 500, `/agent-actions` at 300, `/work-orders` at
200, `/downtime-logs` at 100. Measured, `/machines` is also the worst grower of
the five — 8.6x from 10 to 1000 machines. Every instinct says add a `.limit()`.

Adding one would be a correctness defect, not a performance fix.

The dashboard does not merely RENDER this array; it reduces it into three
headline numbers client-side (`app/dashboard/page.tsx:1755-1762`):

    const running   = machines.filter(m => m.status === "Running").length
    const breakdown = machines.filter(m => m.status === "Breakdown").length
    const avgUtilization = machines.reduce((s, m) => s + m.utilization, 0)
                           / machines.length

A cap would not shorten a visible list — that would at least be noticeable. It
would print an UNDERSTATED breakdown count and a mean over an arbitrary,
id-ordered subset, with nothing on screen indicating anything was dropped. A
plant with 250 machines and a cap of 200 would show a confident, wrong "3
machines down" while five more sat broken past the cut.

That is the ADR-0010 honesty rule, and this same file already enforces it twice
on the WRITE side: `machines_routes.py` canonicalises status on create and on
PATCH precisely so a non-canonical value cannot drop a machine out of
`breakdown_count`, and clamps utilization because "a percentage the data can't
support" must not reach a display. A read-side cap would reintroduce, silently,
the exact class of bug the write side is guarded against.

WHAT ABOUT THE PERFORMANCE, THEN
--------------------------------
It was measured and it does not justify the risk. At 1000 machines one user
waits 23.9 ms of service time (docs/PERFORMANCE.md); the 184 ms figure quoted
elsewhere is queueing under 8 concurrent clients, not user latency. AMP's
factories are two orders of magnitude below that, and `tenancy.py` scopes every
SELECT to one tenant, so 1000 rows would have to be a single customer's fleet.

A lighter projection was considered and rejected: the response already goes
through `response_model=List[schemas.MachineResponse]`, which emits 6 fields and
already drops 2 columns. Exactly one emitted field (`line`) is unread by the
frontend. Dropping one short string is an unmeasurable win in exchange for a
breaking schema change.

IF THIS EVER NEEDS TO CHANGE
----------------------------
Pagination, with the frontend computing those three KPIs server-side or over the
full set — not a bare cap. The point of this test is that the change must be
deliberate: `.limit()` here fails CI with the reason attached, rather than
shipping quietly and printing wrong numbers.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_machines_not_capped.py
"""
import ast
import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import machines_routes
import models
import tenancy
from database import Base

HERE = os.path.dirname(os.path.abspath(__file__))
T = "NOCAP"
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def seed(n):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    Session = sessionmaker(bind=engine)
    db = Session()
    tok = tenancy.set_current_tenant(None)
    db.add(models.TenantConfig(tenant_code=T))
    for i in range(n):
        db.add(models.Machine(
            tenant_code=T, name=f"M{i:04d}", site="P1",
            # A spread of statuses so a truncated read produces a WRONG count,
            # not merely a shorter list. Breakdowns are seeded at the END of the
            # id order on purpose: an id-ordered cap drops exactly those.
            status="Breakdown" if i >= n - 5 else "Running",
            utilization=10 if i >= n - 5 else 90, downtime="0 min"))
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()
    return Session


def get_machines(Session):
    db = Session()
    tok = tenancy.set_current_tenant(T)
    try:
        return machines_routes.get_machines(
            db=db, current_user={"tenant_code": T, "username": "u", "role": "Admin"})
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def main():
    print("=" * 74)
    print("1. EVERY MACHINE COMES BACK, AT A SIZE PAST EVERY SIBLING'S CAP")
    print("=" * 74)
    # 600 clears the largest cap in the codebase (inventory's 500), so a cap
    # copied from a sibling endpoint would be caught here.
    N = 600
    Session = seed(N)
    rows = get_machines(Session)
    check(f"all {N} machines returned", len(rows) == N, f"got {len(rows)}")

    print()
    print("=" * 74)
    print("2. THE KPIs THE DASHBOARD DERIVES ARE STILL RIGHT")
    print("=" * 74)
    # Recomputed exactly as app/dashboard/page.tsx:1755-1762 does, because that
    # is what a cap would corrupt — silently.
    running = len([m for m in rows if m.status == "Running"])
    breakdown = len([m for m in rows if m.status == "Breakdown"])
    avg = round(sum(m.utilization for m in rows) / len(rows)) if rows else 0
    expected_avg = round((90 * (N - 5) + 10 * 5) / N)
    check("breakdown count is complete", breakdown == 5, f"got {breakdown}, expected 5")
    check("running count is complete", running == N - 5, f"got {running}")
    check("average utilization is over the WHOLE fleet",
          avg == expected_avg, f"got {avg}, expected {expected_avg}")

    print()
    print("=" * 74)
    print("3. NO ROW CAP IN THE HANDLER — the static half of the guard")
    print("=" * 74)
    # Section 1 catches a cap larger than 600 only if the fixture grows. This
    # catches ANY .limit() on the handler, whatever its size, without needing
    # to seed past it.
    with open(os.path.join(HERE, "machines_routes.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "get_machines"), None)
    check("get_machines still exists", fn is not None, "renamed?")
    limits = [n for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr in ("limit", "offset", "slice")] if fn else []
    check("get_machines has no .limit()/.offset()", not limits,
          "a cap here silently understates breakdown count and skews "
          "avgUtilization — see this file's docstring. Paginate deliberately "
          "instead, and move the KPI derivation off the truncated array.")

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
