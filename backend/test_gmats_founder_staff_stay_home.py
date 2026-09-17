"""A founder-workspace Operator read, and a Supervisor changed, a customer's stock.

THE DEFECT
----------
`gmats_inventory_routes._effective_tenant` carried this docstring:

    A founder-workspace ADMIN may view any tenant it asks for; everyone else is
    locked to their own tenant regardless of what they request.

and this code:

    if jwt_tenant == "DEFAULT" and current_user.get("role") == "Admin":
        return requested or "GMATS"
    if jwt_tenant == "DEFAULT":
        return "GMATS"
    return jwt_tenant

A non-Admin login in the founder's own DEFAULT workspace — an Operator demo
account, a Supervisor on the founder's staff — has DEFAULT as its own tenant.
The code did not lock it there. It locked it to GMATS: the pilot customer
(the first client), by name. With no parameters at all, that login read GMATS's item
master, purchase rates and customers, and the Supervisor could stock in, raise
proformas (reserving stock), invoice (deducting it) and issue MINs against it.
`_guard_record` compared by-id writes against the same answer, so the caller's
OWN DEFAULT records returned 403 while GMATS's returned 200.

The UI walked them there too: the dashboard showed the company switcher to any
DEFAULT login, whatever its role, with "GMATS Compressors" as an option.

WHY IT SURVIVED A FIX FOR EXACTLY THIS
--------------------------------------
#500's own commit says it fixed "a DEFAULT Supervisor mutated another customer's
physical stock". What it did was remove the free choice of tenant and hard-code
the one real client instead — closing "any customer" and leaving "that customer".
And GmatsItem, GmatsProforma, GmatsInvoice and GmatsMIN are not in
tenancy.SCOPED_MODELS, so the ADR-0002 ORM hook that backstops every other
module never runs here: this function is the ONLY boundary these tables have.

It was also the dominant defect class of this codebase — one rule, two
implementations. `tenancy.effective_tenant` already stated the rule correctly
("Only a founder-workspace ADMIN may preview another tenant ... ROLE, NOT JUST
WORKSPACE"), including the OEM principal branch, and this module kept a private
copy that disagreed with it.

THE FIX THIS SUITE PINS
-----------------------
`_effective_tenant` delegates to `tenancy.effective_tenant`, so there is one rule
for which tenant a request may act in. Section 4 asserts the two AGREE across a
matrix of workspaces, roles and requests rather than reading the source, so it
fails for any future drift, whichever side it happens on. Section 5 asserts every
/gmats route still passes through one of the two checks, because on these tables
a route that forgets is not scoped by anything else.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_gmats_founder_staff_stay_home.py
"""
import ast
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DATABASE_URL", "sqlite:///./ci.db")

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import gmats_inventory_routes as gmats  # noqa: E402
import models  # noqa: E402
import tenancy  # noqa: E402
from database import Base  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def user(tenant, role):
    return {"sub": f"{role or 'norole'}@{tenant}", "tenant": tenant, "role": role}


FOUNDER_ADMIN = user("DEFAULT", "Admin")
FOUNDER_SUPERVISOR = user("DEFAULT", "Supervisor")
FOUNDER_OPERATOR = user("DEFAULT", "Operator")
FOUNDER_NO_ROLE = {"sub": "legacy", "tenant": "DEFAULT"}   # fail-closed: not an Admin
ACME_ADMIN = user("ACME", "Admin")


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    for iid, tenant, name, stock in ((1, "GMATS", "GMATS Compressor Valve", 40),
                                     (2, "DEFAULT", "Founder Demo Part", 5),
                                     (3, "ACME", "Acme Bracket", 9)):
        db.add(models.GmatsItem(id=iid, tenant_code=tenant, item_code=f"C{iid}",
                                item_name=name, physical_stock=stock, reserved_stock=0,
                                reorder_level=0, purchase_rate=100))
    db.commit()
    return db


def names(rows):
    return sorted(r["item_name"] for r in rows)


def refused(fn):
    try:
        fn()
    except HTTPException as e:
        return e.status_code == 403
    return False


def main():
    print("=" * 74)
    print("1. A FOUNDER-WORKSPACE NON-ADMIN SEES ITS OWN WORKSPACE, NOT A CUSTOMER'S")
    print("=" * 74)
    db = _db()
    # The route's own default for ?tenant= is "GMATS", so calling with no
    # parameter is exactly what a bare GET /gmats/items does.
    for label, who in (("Operator", FOUNDER_OPERATOR), ("Supervisor", FOUNDER_SUPERVISOR),
                       ("login with no role claim", FOUNDER_NO_ROLE)):
        got = names(gmats.gmats_items(tenant="GMATS", db=db, current_user=who))
        check(f"a founder-workspace {label} does NOT get GMATS's item master",
              "GMATS Compressor Valve" not in got, repr(got))
        check(f"...it gets its own DEFAULT workspace's items",
              got == ["Founder Demo Part"], repr(got))

    got = names(gmats.gmats_items(tenant="ACME", db=db, current_user=FOUNDER_OPERATOR))
    check("asking for another customer by name changes nothing for a non-Admin",
          got == ["Founder Demo Part"], repr(got))

    summary = gmats.gmats_summary(tenant="GMATS", db=db, current_user=FOUNDER_OPERATOR)
    check("the stock summary is scoped the same way as the list",
          summary.get("items") == 1 and summary.get("total_physical") == 5, repr(summary))

    print()
    print("=" * 74)
    print("2. ...AND CANNOT WRITE TO A CUSTOMER'S STOCK, BUT CAN WRITE TO ITS OWN")
    print("=" * 74)
    db = _db()
    check("a founder-workspace Supervisor's stock-in on a GMATS item is refused",
          refused(lambda: gmats.gmats_stock_in(1, {"qty": 7}, db=db,
                                               current_user=FOUNDER_SUPERVISOR)),
          "the write was allowed")
    gmats_item = db.get(models.GmatsItem, 1)
    check("...and GMATS's physical stock is untouched",
          gmats_item.physical_stock == 40, repr(gmats_item.physical_stock))

    # The mirror image of the defect: the old rule 403'd the caller's OWN rows.
    try:
        own = gmats.gmats_stock_in(2, {"qty": 3}, db=db, current_user=FOUNDER_SUPERVISOR)
    except HTTPException as e:
        own = {"physical_stock": f"HTTP {e.status_code}"}
    check("the same Supervisor CAN stock in to its own workspace's item",
          own["physical_stock"] == 8, repr(own.get("physical_stock")))

    created = gmats.gmats_create_item({"tenant": "GMATS", "item_code": "X1",
                                       "item_name": "Planted Part"},
                                      db=db, current_user=FOUNDER_SUPERVISOR)
    planted = db.query(models.GmatsItem).filter(models.GmatsItem.item_code == "X1").one()
    check("a create naming GMATS lands in the caller's own workspace, not GMATS",
          planted.tenant_code == "DEFAULT", repr(planted.tenant_code))

    print()
    print("=" * 74)
    print("3. WHAT MUST STILL WORK")
    print("=" * 74)
    db = _db()
    got = names(gmats.gmats_items(tenant="GMATS", db=db, current_user=FOUNDER_ADMIN))
    check("the founder ADMIN still previews GMATS when it asks to",
          got == ["GMATS Compressor Valve"], repr(got))
    out = gmats.gmats_stock_in(1, {"qty": 2}, db=db, current_user=FOUNDER_ADMIN)
    check("...and may act on that previewed customer's record",
          out["physical_stock"] == 42, repr(out.get("physical_stock")))

    got = names(gmats.gmats_items(tenant="GMATS", db=db, current_user=ACME_ADMIN))
    check("a client Admin is locked to its own tenant whatever it asks for",
          got == ["Acme Bracket"], repr(got))
    check("...and cannot touch GMATS's record by id",
          refused(lambda: gmats.gmats_stock_in(1, {"qty": 1}, db=db,
                                               current_user=ACME_ADMIN)),
          "the write was allowed")

    print()
    print("=" * 74)
    print("4. ONE RULE: THIS MODULE AGREES WITH tenancy.effective_tenant")
    print("=" * 74)
    # Asserted by BEHAVIOUR across a matrix, not by reading the source for a
    # call. A future edit on either side that makes them disagree fails here,
    # whichever side it is made on.
    oem = {"sub": "oem-user", "tenant": "DEFAULT", "role": "Admin",
           "principal": "oem", "oem": "COMPAIR"}
    disagreements = []
    for claims in (FOUNDER_ADMIN, FOUNDER_SUPERVISOR, FOUNDER_OPERATOR, FOUNDER_NO_ROLE,
                   ACME_ADMIN, user("ACME", "Operator"), user("GMATS", "Supervisor"), oem):
        for requested in (None, "", "GMATS", "ACME", "DEFAULT"):
            mine = gmats._effective_tenant(claims, requested)
            theirs = tenancy.effective_tenant(claims.get("tenant") or "DEFAULT", requested,
                                              claims.get("role"), claims=claims)
            if mine != theirs:
                disagreements.append((claims["sub"], requested, mine, theirs))
    check("the GMATS rule and the platform rule give the same answer for every "
          "workspace x role x request", disagreements == [], repr(disagreements[:6]))
    # Defence in depth, NOT a reachable hole: auth.get_current_user rejects any
    # principal="oem" token with 403 before a /gmats handler runs (auth.py). This
    # calls the function directly, past that dependency, to pin that the tables
    # with no ORM backstop would still bind the sentinel if that ever changed.
    check("defence in depth: an OEM principal would bind its sentinel, never a factory",
          gmats._effective_tenant(oem, "GMATS") == tenancy.effective_tenant(
              "DEFAULT", "GMATS", "Admin", claims=oem)
          and gmats._effective_tenant(oem, "GMATS") not in ("GMATS", "DEFAULT"),
          repr(gmats._effective_tenant(oem, "GMATS")))

    print()
    print("=" * 74)
    print("5. EVERY /gmats ROUTE PASSES THROUGH THE BOUNDARY")
    print("=" * 74)
    # GmatsItem & co. are not in tenancy.SCOPED_MODELS, so the ORM hook that
    # backstops every other module never runs on these tables. A route that
    # calls neither check is not scoped by anything.
    check("the Gmats* models are still outside SCOPED_MODELS (if this changes, "
          "this section's premise does too)",
          not any(m.__name__.startswith("Gmats") for m in tenancy.SCOPED_MODELS),
          "a Gmats model became auto-scoped")
    tree = ast.parse(io.open(os.path.join(HERE, "gmats_inventory_routes.py"),
                             encoding="utf-8").read())

    def calls(fn, name):
        return any(isinstance(n, ast.Call) and (getattr(n.func, "id", "") == name
                                                or getattr(n.func, "attr", "") == name)
                   for n in ast.walk(fn))

    routes, unguarded = [], []
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        if any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") in
               ("get", "post", "patch", "put", "delete") for d in fn.decorator_list):
            routes.append(fn.name)
            if not (calls(fn, "_effective_tenant") or calls(fn, "_guard_record")):
                unguarded.append(fn.name)
    check("the guard still finds the routes (a matcher that matches nothing "
          "reports all-clear)", len(routes) >= 18, f"found {len(routes)}")
    check("every /gmats route calls _effective_tenant or _guard_record",
          unguarded == [], f"unscoped routes: {unguarded}")

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILURE(S)")
        for f in failures:
            print("  -", f)
        return 1
    print("GMATS TENANCY OK: founder-workspace staff stay in their own workspace; "
          "one rule decides who may preview a customer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
