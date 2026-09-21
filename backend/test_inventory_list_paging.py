"""A list is a page, and says so: /inventory/items and /inventory/transactions.

Both endpoints always returned the newest 500 items / 300 transactions and
nothing else. A plant with 1,200 stock items saw 500 on the dashboard with no
sign that 700 were missing -- found by the restore drill at scale, whose verify
phase counted 500 of 1,200. Now:

  1  the defaults are unchanged: 500 items, 300 transactions, newest first
  2  every response carries the tenant's TOTAL in X-Total-Count -- the whole
     count, not the page's
  3  limit and offset page through the rest, and a limit past MAX_PAGE is
     clamped, for a request and a direct caller alike
  4  the total, like the page, is the caller's tenant's and nobody else's
  5  an offset past the end is an empty page, with the total unchanged
  6  the browser may read the count: CORS exposes X-Total-Count (the screen
     and the API are on different origins)

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_inventory_list_paging.py
"""
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import Response  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import inventory_routes as ir  # noqa: E402
import models  # noqa: E402
import tenancy  # noqa: E402
from database import Base  # noqa: E402

A, B = "PAGE_A", "PAGE_B"
# A has MORE than MAX_PAGE items, so the clamp is observable: a limit of
# 100,000 must come back as 2,000 rows, not as all of them.
A_ITEMS, B_ITEMS, A_TX = 2203, 7, 350
failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    Session = sessionmaker(bind=engine)
    db = Session()
    tok = tenancy.set_current_tenant(None)
    try:
        for t, n in ((A, A_ITEMS), (B, B_ITEMS)):
            db.add_all([models.InventoryItem(tenant_code=t, item_code=f"{t}-{i:05d}", item_name=f"{t} part {i}",
                                             category="Raw", unit="kg", current_stock=10, reorder_level=1)
                        for i in range(n)])
        db.flush()
        first = db.query(models.InventoryItem).filter(models.InventoryItem.tenant_code == A).first()
        db.add_all([models.InventoryTransaction(tenant_code=A, item_id=first.id, transaction_type="Issue",
                                                quantity=1, reference="test", notes=f"tx {i}")
                    for i in range(A_TX)])
        db.commit()
    finally:
        tenancy.reset_current_tenant(tok)
    return db


def items(db, tenant, **kw):
    resp = Response()
    tok = tenancy.set_current_tenant(tenant)
    try:
        rows = ir.get_inventory_items(resp, kw.get("limit"), kw.get("offset", 0), db, {"tenant": tenant, "role": "Admin"})
    finally:
        tenancy.reset_current_tenant(tok)
    return rows, resp.headers.get(ir.TOTAL_HEADER)


def transactions(db, tenant, **kw):
    resp = Response()
    tok = tenancy.set_current_tenant(tenant)
    try:
        rows = ir.get_inventory_transactions(resp, kw.get("limit"), kw.get("offset", 0), db,
                                             {"tenant": tenant, "role": "Admin"})
    finally:
        tenancy.reset_current_tenant(tok)
    return rows, resp.headers.get(ir.TOTAL_HEADER)


def main():
    db = session()

    print("1. THE DEFAULTS ARE UNCHANGED")
    rows, total = items(db, A)
    check(f"a plant with {A_ITEMS} items still gets a page of {ir.ITEMS_PAGE}", len(rows) == ir.ITEMS_PAGE == 500, str(len(rows)))
    check("newest first", [r.id for r in rows] == sorted((r.id for r in rows), reverse=True))
    tx, tx_total = transactions(db, A)
    check(f"{A_TX} transactions still get a page of {ir.TRANSACTIONS_PAGE}", len(tx) == ir.TRANSACTIONS_PAGE == 300, str(len(tx)))

    print("2. EVERY RESPONSE SAYS HOW MANY THERE ARE")
    check(f"X-Total-Count is the whole count ({A_ITEMS}), not the page's", total == str(A_ITEMS), str(total))
    check("...and for transactions", tx_total == str(A_TX), str(tx_total))

    print("3. LIMIT AND OFFSET PAGE THROUGH THE REST")
    page1, _ = items(db, A, limit=100, offset=0)
    page2, t2 = items(db, A, limit=100, offset=100)
    check("limit=100 gives 100", len(page1) == 100 and len(page2) == 100, f"{len(page1)}, {len(page2)}")
    check("offset=100 continues where the first page stopped, with nothing repeated",
          page2[0].id < page1[-1].id and not ({r.id for r in page1} & {r.id for r in page2}))
    check("the total does not move with the page", t2 == str(A_ITEMS), str(t2))
    # Bounded, never `while True`: an endpoint that ignores the offset serves
    # the same first page forever, and the loop that walks it must stop and
    # FAIL, not spin (the mutation harness sat 20 minutes on exactly that).
    seen = set()
    off = 0
    pages = 0
    expected_pages = -(-A_ITEMS // ir.MAX_PAGE)
    for pages in range(1, expected_pages + 3):
        page, _ = items(db, A, limit=ir.MAX_PAGE, offset=off)
        if not page:
            break
        seen |= {r.id for r in page}
        off += len(page)
    check(f"paging to the end reaches every one of the {A_ITEMS} items in {expected_pages} pages",
          len(seen) == A_ITEMS and pages == expected_pages + 1, f"{len(seen)} items over {pages - 1} pages")
    big, _ = items(db, A, limit=100000)
    check(f"a limit past MAX_PAGE ({ir.MAX_PAGE}) is clamped, even for a direct caller", len(big) == ir.MAX_PAGE == 2000, str(len(big)))
    tiny, _ = items(db, A, limit=0)
    check("a limit of 0 is treated as 1, not as everything", len(tiny) == 1, str(len(tiny)))
    neg, _ = items(db, A, limit=10, offset=-50)
    check("a negative offset is 0", [r.id for r in neg] == [r.id for r in items(db, A, limit=10)[0]])

    print("4. THE TOTAL IS THE CALLER'S TENANT'S")
    b_rows, b_total = items(db, B)
    check(f"{B} sees its {B_ITEMS} items and a total of {B_ITEMS}", len(b_rows) == B_ITEMS and b_total == str(B_ITEMS), f"{len(b_rows)} / {b_total}")
    check("...none of which are A's", all(r.tenant_code == B for r in b_rows))
    b_tx, b_tx_total = transactions(db, B)
    check(f"{B} has no transactions and is told 0", b_tx == [] and b_tx_total == "0", f"{len(b_tx)} / {b_tx_total}")

    print("5. AN OFFSET PAST THE END IS AN EMPTY PAGE")
    none, t = items(db, A, limit=50, offset=A_ITEMS + 10)
    check("empty page, total unchanged", none == [] and t == str(A_ITEMS), f"{len(none)} / {t}")

    print("6. A BROWSER MAY READ THE COUNT")
    # The screen runs on another origin (Vercel) than the API (Railway). A
    # response header a browser script may read must be listed in CORS's
    # expose_headers; otherwise the count is sent and never seen, and the
    # dashboard is back to a page that looks complete.
    import main  # noqa: E402  (registers the app and its middleware)
    cors = [m for m in main.app.user_middleware if m.cls.__name__ == "CORSMiddleware"]
    exposed = list(cors[0].kwargs.get("expose_headers") or []) if cors else []
    check(f"CORS exposes {ir.TOTAL_HEADER} to the browser", ir.TOTAL_HEADER in exposed, str(exposed))

    db.close()
    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(" -", f)
        return 1
    print("ALL INVENTORY PAGING CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
