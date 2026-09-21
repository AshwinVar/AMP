"""Every capped list is a page, and says so (paging.py).

WHY THIS GUARD EXISTS
---------------------
test_growing_table_reads.py makes every polled read of an append-only table
take a `.limit(...)`. That is right, and it is how thirty list endpoints came
to return "the newest N rows" -- and nothing else. A plant with 1,200 stock
items saw 500 on the dashboard with no sign that 700 were missing (#671 found
it, #673 closed it for the two inventory lists). A notification screen counted
the unread rows in the newest 500 and called that the number. The cap was
never the defect; a page that cannot be told from a complete list was.

paging.page() is the one place the honest version lives: the caller's whole
count in X-Total-Count, ?limit= and ?offset= clamped in one function, the
endpoint's old cap as its default page. This file fails the build for a capped
GET that does not go through it.

THE RULE (section 1)
--------------------
In every *_routes.py, a GET handler whose body has a `db.query(...)...
.limit(...)...all()` chain must call `paging.page(...)`, or be listed in
ALLOWED with the reason the cap is not a page: a window that feeds a figure
or a chart (a trend of the last 200 records is a window, not page one of the
records), a body envelope that already carries its own total, or an on-demand
download. As in test_growing_table_reads.py, the walk is over the AST, so a
docstring quoting the pattern is not a finding, and an allowlist entry that
no longer names a real capped read fails the build (section 2).

WHAT THE HELPER PROMISES (sections 4-6)
---------------------------------------
Measured on real handlers, not on the helper alone: the default page is the
old cap; the total is the whole scoped count, not the page's; a handler that
annotates or serialises its rows still does; an endpoint with its own smaller
ceiling keeps it; the notification unread count is over EVERY notification.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_lists_are_pages.py
"""
import ast
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")

HERE = os.path.dirname(os.path.abspath(__file__))

# Capped GET reads that are NOT pages, with the reason. Key: "<file>::<function>".
ALLOWED = {
    # ---- Windows: the rows feed a figure or a chart, and the cap is the
    # window's width. Page two of a KPI's inputs is not a thing.
    "analytics_routes.py::oee_summary":
        "GET /oee/summary -- OEE per machine from the newest 100 records: a window "
        "that feeds a figure, with the record count reported beside it",
    "analytics_routes.py::get_machine_timeline":
        "GET /analytics/machine-timeline -- the newest 200 events as a timeline strip",
    "analytics_routes.py::get_oee_trends":
        "GET /analytics/oee-trends -- one chart point per record over the newest 200",
    "analytics_routes.py::get_shift_kpis":
        "GET /analytics/shift-kpis -- KPIs over the newest 50 shifts",
    "analytics_routes.py::get_smart_alerts":
        "GET /alerts/smart -- alerts derived from the newest 100 production and "
        "downtime records",
    "analytics_routes.py::get_executive_oee":
        "GET /analytics/executive-oee -- the newest 50 shifts feed the executive figures",
    "analytics_routes.py::get_iot_command_center":
        "GET /analytics/iot-command -- a display window of the newest 300 telemetry "
        "rows next to a SQL count() for the headline (the cap does not leak into it)",
    "analytics_routes.py::get_industrial_gateway_analytics":
        "GET /analytics/industrial -- a display window of the newest 500 signals "
        "next to a SQL count() for the headline",
    # ---- A body envelope with its own convention.
    "oem_routes.py::oem_notifications":
        "GET /oem/notifications -- {\"notifications\": [...]} for the OEM portal; the "
        "newest 100 for one OEM, a different screen and a different contract",
    # ---- On-demand downloads.
    "reports_routes.py::export_intelligence_summary":
        "GET /intelligence-summary.txt -- a text download whose alert feed takes the "
        "newest 100 downtime rows; the whole file is the deliverable",
}

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

def _chain(node):
    """Every call in an `a.b().c().d()` chain, outermost first."""
    out = []
    cur = node
    while isinstance(cur, ast.Call):
        out.append(cur)
        f = cur.func
        cur = f.value if isinstance(f, ast.Attribute) else None
    return out


def _is_get_handler(fn):
    for d in fn.decorator_list:
        call = d if isinstance(d, ast.Call) else None
        if call and isinstance(call.func, ast.Attribute) and call.func.attr == "get":
            return True
    return False


def _has_capped_all(fn):
    """Does the function hydrate a `.limit(...)` slice of a query with `.all()`?"""
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "all"):
            continue
        attrs = {c.func.attr for c in _chain(node) if isinstance(c.func, ast.Attribute)}
        if "limit" in attrs and "query" in attrs:
            return True
    return False


def _calls_page(fn):
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "page" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "paging"):
            return True
    return False


def scan(path):
    """(paged, unpaged): the GET handlers that page through paging.page, and
    the GET handlers that hydrate a `.limit(...)` slice with `.all()` without
    it -- the finding."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        tree = ast.parse(fh.read(), filename=path)
    paged, unpaged = [], []
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        if not _is_get_handler(fn):
            continue
        if _calls_page(fn):
            paged.append(fn.name)
        elif _has_capped_all(fn):
            unpaged.append(fn.name)
    return paged, unpaged


def route_files():
    return sorted(p for p in glob.glob(os.path.join(HERE, "*_routes.py"))
                  if not os.path.basename(p).startswith("test_"))


# ---------------------------------------------------------------------------
# Live checks on real handlers
# ---------------------------------------------------------------------------

def _session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import tenancy
    from database import Base
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    return sessionmaker(bind=engine)()


def _as(tenant, fn, **kw):
    """Call a handler as a request for `tenant` would, with a real Response."""
    from fastapi import Response
    import tenancy
    resp = Response()
    tok = tenancy.set_current_tenant(tenant)
    try:
        rows = fn(resp, **kw)
    finally:
        tenancy.reset_current_tenant(tok)
    return rows, resp.headers.get("X-Total-Count")


def main():
    import paging

    print("=" * 74)
    print("1. EVERY CAPPED GET IS A PAGE, OR IS ALLOWED WITH A REASON")
    print("=" * 74)
    found_paged, found_unpaged = {}, {}
    for path in route_files():
        name = os.path.basename(path)
        paged, unpaged = scan(path)
        for fn in paged:
            found_paged[f"{name}::{fn}"] = True
        for fn in unpaged:
            found_unpaged[f"{name}::{fn}"] = True
    unexpected = sorted(k for k in found_unpaged if k not in ALLOWED)
    check(f"scanned {len(route_files())} route modules: {len(found_paged)} GETs page through "
          f"paging.page, {len(found_unpaged)} capped GETs do not, {len(ALLOWED)} of those allowed, "
          f"0 unexplained",
          not unexpected, "capped GETs that neither page nor explain why: " + ", ".join(unexpected))
    check("at least thirty list endpoints go through paging.page", len(found_paged) >= 30,
          str(len(found_paged)))
    if unexpected:
        print()
        print("  A capped list that does not say it is one looks complete and is not.")
        print("  Route it through paging.page(response, query, default, limit, offset),")
        print("  or -- if the cap is a window, an envelope or a download -- add it to")
        print("  ALLOWED with the reason.")

    print()
    print("=" * 74)
    print("2. THE ALLOWLIST IS HONEST")
    print("=" * 74)
    stale = sorted(k for k in ALLOWED if k not in found_unpaged)
    check(f"every one of the {len(ALLOWED)} allowlist entries still names a capped, unpaged GET",
          not stale, "stale (paged, fixed or renamed -- delete these): " + ", ".join(stale))

    print()
    print("=" * 74)
    print("3. THE SCAN DETECTS THE DEFECT IT WAS WRITTEN FOR")
    print("=" * 74)
    import tempfile
    sample = '''
import models
import paging
router = object()

@router.get("/things")
def capped_and_silent(db, current_user):
    """A docstring saying paging.page(...) must NOT count."""
    return db.query(models.Thing).order_by(models.Thing.id.desc()).limit(200).all()

@router.get("/things-paged")
def capped_and_honest(response, limit=None, offset=0, db=None, current_user=None):
    return paging.page(response, db.query(models.Thing).order_by(models.Thing.id.desc()), 200, limit, offset)

@router.get("/things-all")
def uncapped(db, current_user):
    return db.query(models.Thing).all()

@router.post("/things")
def a_post_with_a_cap(db, current_user):
    return db.query(models.Thing).limit(5).all()

def not_a_route(db):
    return db.query(models.Thing).limit(5).all()
'''
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "sample_routes.py")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(sample)
        paged, unpaged = scan(p)
    check("a capped, silent GET IS detected", "capped_and_silent" in unpaged, str(unpaged))
    check("a GET that pages is counted as paged, not as a finding",
          paged == ["capped_and_honest"], str((paged, unpaged)))
    check("an uncapped GET is not this guard's business (test_growing_table_reads owns it)",
          "uncapped" not in paged + unpaged, str((paged, unpaged)))
    check("a POST and a plain function are not this guard's business",
          "a_post_with_a_cap" not in paged + unpaged and "not_a_route" not in paged + unpaged,
          str((paged, unpaged)))
    check("a docstring naming paging.page is NOT a call to it", unpaged == ["capped_and_silent"], str(unpaged))

    print()
    print("=" * 74)
    print("4. THE DEFAULT PAGE IS THE OLD CAP, THE TOTAL IS THE WHOLE COUNT")
    print("=" * 74)
    import models
    import tenancy
    import work_orders_routes as WO
    import factory_ops_routes as FO
    db = _session()
    tok = tenancy.set_current_tenant(None)
    try:
        db.add_all([models.WorkOrder(tenant_code="PG_A", work_order_no=f"WO-{i:04d}", part_number="P",
                                     batch_number="B", target_quantity=1, actual_quantity=0, status="Planned")
                    for i in range(230)])
        db.add_all([models.WorkOrder(tenant_code="PG_B", work_order_no=f"WOB-{i:04d}", part_number="P",
                                     batch_number="B", target_quantity=1, actual_quantity=0, status="Planned")
                    for i in range(3)])
        db.add_all([models.Escalation(tenant_code="PG_A", title=f"E{i}", severity="High", owner="ops",
                                      department="Production", status="Open", source="Manual")
                    for i in range(310)])
        db.add_all([models.Notification(tenant_code="PG_A", notification_type="System", severity="Info",
                                        title=f"N{i}", message="m", status="Read" if i % 2 else "Unread")
                    for i in range(520)])
        db.commit()
        # A NULL status must be written with an UPDATE. Passing status=None to the
        # constructor is NOT a NULL: the column carries default="Unread", and the
        # ORM omits an explicit None from the INSERT so the default applies (the
        # same trap #407 fell into). The first version of this test seeded "NULL"
        # rows that way, and the mutation that stops counting NULL as unread
        # survived it, because no row was NULL.
        db.query(models.Notification).filter(models.Notification.title.in_(
            [f"N{i}" for i in range(0, 520, 6)])).update({"status": None}, synchronize_session=False)
        db.commit()
    finally:
        tenancy.reset_current_tenant(tok)
    user_a = {"tenant": "PG_A", "role": "Admin"}
    user_b = {"tenant": "PG_B", "role": "Admin"}

    rows, total = _as("PG_A", WO.get_work_orders, db=db, current_user=user_a)
    check("work orders: 230 on the book -> a page of 200 (the old cap), total 230",
          len(rows) == 200 and total == "230", f"{len(rows)} / {total}")
    check("newest first, unchanged", [r.id for r in rows] == sorted((r.id for r in rows), reverse=True))
    rows2, total2 = _as("PG_A", WO.get_work_orders, limit=200, offset=200, db=db, current_user=user_a)
    check("offset=200 gives the remaining 30, total still 230", len(rows2) == 30 and total2 == "230",
          f"{len(rows2)} / {total2}")
    rows_b, total_b = _as("PG_B", WO.get_work_orders, db=db, current_user=user_b)
    check("the other tenant sees its 3 and a total of 3, not the neighbour's",
          len(rows_b) == 3 and total_b == "3" and all(r.tenant_code == "PG_B" for r in rows_b),
          f"{len(rows_b)} / {total_b}")
    direct = WO.get_work_orders(db=db, current_user=user_a)
    check("a direct caller with no Response still gets the same page (200)", len(direct) == 200, str(len(direct)))

    print()
    print("=" * 74)
    print("5. A HANDLER THAT ANNOTATES ITS ROWS STILL DOES")
    print("=" * 74)
    esc, esc_total = _as("PG_A", FO.get_escalations, db=db, current_user=user_a)
    check("escalations: a page of 300 of 310, total 310", len(esc) == 300 and esc_total == "310",
          f"{len(esc)} / {esc_total}")
    check("every row still carries the approvals annotation (awaiting_approval)",
          all(hasattr(r, "awaiting_approval") for r in esc))

    print()
    print("=" * 74)
    print("6. THE UNREAD COUNT IS OVER EVERY NOTIFICATION, NOT THE NEWEST PAGE")
    print("=" * 74)
    tok = tenancy.set_current_tenant("PG_A")
    try:
        nulls = db.query(models.Notification).filter(models.Notification.status.is_(None)).count()
        unread_rows = db.query(models.Notification).filter(models.Notification.status == "Unread").count()
    finally:
        tenancy.reset_current_tenant(tok)
    truly_unread = nulls + unread_rows
    check("the seed really holds NULL-status rows (87) beside Unread ones (173)",
          nulls == 87 and unread_rows == 173, f"{nulls} NULL / {unread_rows} Unread")
    page1, n_total = _as("PG_A", FO.get_notifications, db=db, current_user=user_a)
    check("notifications: a page of 500 of 520, total 520", len(page1) == 500 and n_total == "520",
          f"{len(page1)} / {n_total}")
    one, unread_total = _as("PG_A", FO.get_notifications, unread=True, limit=1, db=db, current_user=user_a)
    check(f"?unread=true&limit=1: one row, and X-Total-Count is the unread count over all 520 ({truly_unread})",
          len(one) == 1 and unread_total == str(truly_unread), f"{len(one)} / {unread_total}")
    check("...which counts a NULL status as unread (the read-all and dedup convention), "
          "not just the rows marked Unread",
          unread_total == str(nulls + unread_rows) and unread_total != str(unread_rows), str(unread_total))
    counted_on_page = sum(1 for n in page1 if n.status != "Read")
    check("...and it is NOT the count the screen used to compute over the page",
          counted_on_page != truly_unread, f"page {counted_on_page} vs all {truly_unread}")

    print()
    print("=" * 74)
    print("7. AN ENDPOINT'S OWN SMALLER CEILING SURVIVES THE SHARED HELPER")
    print("=" * 74)
    import enterprise_inventory_routes as EIR
    tok = tenancy.set_current_tenant(None)
    try:
        db.add_all([models.Remnant(tenant_code="PG_A", tag_no=f"R-{i:04d}", item_id=None, original_qty=2,
                                   remaining_qty=1, unit="kg", location="L", status="Available")
                    for i in range(260)])
        db.commit()
    finally:
        tenancy.reset_current_tenant(tok)
    rem, rem_total = _as("PG_A", EIR.get_remnants, limit=10_000, offset=0, db=db, current_user=user_a)
    check("remnants: limit=10000 is clamped to the module's own 200, total 260",
          len(rem) == 200 and rem_total == "260", f"{len(rem)} / {rem_total}")
    rem_d, _ = _as("PG_A", EIR.get_remnants, db=db, current_user=user_a)
    check("...and its default page is still 50", len(rem_d) == EIR._PAGE_DEFAULT == 50, str(len(rem_d)))
    check("the module's clamp alias agrees with paging.clamp",
          EIR._page("abc", -5) == (50, 0) and EIR._page(10_000, 3) == (200, 3), str(EIR._page("abc", -5)))
    check("paging.MAX_PAGE bounds everyone else", paging.clamp(10_000, 500) == (2000, 0))
    db.close()

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


def test_lists_are_pages():
    """The pytest entry point (the coverage job collects module-level test_ functions)."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
