"""The OEM fleet and claims lists take their page in SQL (measured first).

WHY THIS EXISTS
---------------
/oem/fleet and /oem/claims always paged their RESPONSE -- {total, limit,
offset, rows} -- but not their QUERY: every installation of the manufacturer
was hydrated, counted with len() and sliced in Python. oem_perf.py measured
it: 48.7 ms for a 100-row page at 10,000 machines, 6.0 ms at 1,000, growing
with the fleet, for a page whose size never changes. The service desk opens
that screen every morning.

Now oem_sharing.installations_query hands the fleet over as a query, the
handlers take the page with OFFSET/LIMIT and the whole count beside it
(paging.cached_count), and the claims list's derived state -- a Pending
invitation whose deadline has passed reads Expired -- is written in SQL
(_claim_state_is) so that filter, too, runs in the database.

WHAT IS PINNED
--------------
  1  the fleet page hydrates the page, not the fleet: the SELECT carries a
     LIMIT, 100 rows come back of 250, the total says 250
  2  offset walks the fleet; `customer` narrows the page AND the total
  3  another manufacturer's fleet is invisible to the page and its total
  4  claims: Expired lists exactly the Pending-past-deadline claims, Pending
     exactly the live ones, Revoked exactly the revoked; total per filter;
     the page is taken in SQL
  5  installations_for still returns every row, ordered, for its other callers

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_oem_fleet_pages_in_sql.py
"""
import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import models  # noqa: E402
import oem_claims  # noqa: E402
import oem_routes  # noqa: E402
import oem_sharing  # noqa: E402
import paging  # noqa: E402
from database import Base  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)(), engine


def seed(db):
    alpha = models.MachineModel(oem_code="OEM_ALPHA", model_code="A-1", name="Alpha One")
    beta = models.MachineModel(oem_code="OEM_BETA", model_code="B-1", name="Beta One")
    db.add_all([alpha, beta])
    db.flush()
    rows = []
    for i in range(250):
        rows.append(models.MachineInstallation(
            oem_code="OEM_ALPHA", serial_number=f"SN-A-{i:04d}", model_id=alpha.id,
            factory_tenant_code=f"FACTORY_{i % 5}", site="Plant 1", status="Active"))
    for i in range(40):
        rows.append(models.MachineInstallation(
            oem_code="OEM_BETA", serial_number=f"SN-B-{i:04d}", model_id=beta.id,
            factory_tenant_code="FACTORY_0", site="Plant 1", status="Active"))
    db.add_all(rows)
    db.flush()
    first = db.query(models.MachineInstallation).filter_by(oem_code="OEM_ALPHA").first()
    now = datetime.utcnow()
    claims = []
    for i in range(12):                       # live: Pending, deadline ahead
        claims.append(models.MachineClaim(oem_code="OEM_ALPHA", installation_id=first.id,
                                          token_hash=f"live-{i}", status=oem_claims.PENDING,
                                          expires_at=now + timedelta(days=3)))
    for i in range(7):                        # stale: Pending, deadline passed
        claims.append(models.MachineClaim(oem_code="OEM_ALPHA", installation_id=first.id,
                                          token_hash=f"stale-{i}", status=oem_claims.PENDING,
                                          expires_at=now - timedelta(minutes=1)))
    for i in range(4):                        # revoked
        claims.append(models.MachineClaim(oem_code="OEM_ALPHA", installation_id=first.id,
                                          token_hash=f"rev-{i}", status=oem_claims.REVOKED,
                                          expires_at=now + timedelta(days=3)))
    claims.append(models.MachineClaim(oem_code="OEM_BETA", installation_id=first.id,
                                      token_hash="beta-1", status=oem_claims.PENDING,
                                      expires_at=now - timedelta(minutes=1)))   # not ALPHA's
    db.add_all(claims)
    db.commit()


def main():
    db, engine = session()
    seed(db)
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def _log(conn, cursor, statement, params, context, executemany):
        statements.append(statement.lower())

    alpha = {"oem": "OEM_ALPHA", "username": "alpha-admin", "role": "OemAdmin"}
    beta = {"oem": "OEM_BETA", "username": "beta-admin", "role": "OemAdmin"}

    def fleet(principal, **kw):
        statements.clear()
        paging.forget_counts()
        body = oem_routes.fleet(customer=kw.get("customer"), limit=kw.get("limit", 100),
                                offset=kw.get("offset", 0), db=db, principal=principal)
        selects = [s for s in statements if "from machine_installations" in s and "count(" not in s]
        return body, selects

    print("1. THE FLEET PAGE HYDRATES THE PAGE, NOT THE FLEET")
    body, selects = fleet(alpha)
    check("100 rows of a 250-machine fleet, total 250",
          len(body["machines"]) == 100 and body["total"] == 250, f"{len(body['machines'])} / {body['total']}")
    check("the installations SELECT carries a LIMIT (the page is taken in SQL)",
          selects and all("limit" in s for s in selects), str(selects)[:200])
    check("...and only one such SELECT ran, for the page", len(selects) == 1, str(len(selects)))

    print("2. OFFSET WALKS THE FLEET; CUSTOMER NARROWS PAGE AND TOTAL")
    p2, _ = fleet(alpha, offset=200)
    check("offset=200 gives the last 50, total still 250",
          len(p2["machines"]) == 50 and p2["total"] == 250, f"{len(p2['machines'])} / {p2['total']}")
    check("the second page continues after the first (no overlap)",
          {m["serial_number"] for m in body["machines"]}.isdisjoint({m["serial_number"] for m in p2["machines"]}))
    one, _ = fleet(alpha, customer="FACTORY_1")
    check("customer=FACTORY_1: 50 rows and a total of 50, not the fleet's 250",
          len(one["machines"]) == 50 and one["total"] == 50, f"{len(one['machines'])} / {one['total']}")

    print("3. ANOTHER MANUFACTURER'S FLEET IS INVISIBLE TO THE PAGE AND ITS TOTAL")
    b, _ = fleet(beta)
    check("OEM_BETA: its 40 machines, total 40",
          len(b["machines"]) == 40 and b["total"] == 40 and
          all(m["serial_number"].startswith("SN-B-") for m in b["machines"]), f"{len(b['machines'])} / {b['total']}")
    none, _ = fleet(beta, customer="FACTORY_1")
    check("OEM_BETA asking for ALPHA's customer FACTORY_1 gets an empty page and a total of 0",
          none["machines"] == [] and none["total"] == 0, str(none["total"]))

    print("4. CLAIMS: THE DERIVED STATE IS FILTERED, COUNTED AND PAGED IN SQL")

    def claims(principal, status=None, **kw):
        statements.clear()
        paging.forget_counts()
        body = oem_routes.list_claims(status=status, limit=kw.get("limit", 100), offset=kw.get("offset", 0),
                                      db=db, principal=principal)
        selects = [s for s in statements if "from machine_claims" in s and "count(" not in s]
        return body, selects

    allc, selects = claims(alpha)
    check("ALPHA's 23 claims, total 23 (BETA's one is not selected)",
          len(allc["claims"]) == 23 and allc["total"] == 23, f"{len(allc['claims'])} / {allc['total']}")
    check("the claims SELECT carries a LIMIT", selects and all("limit" in s for s in selects), str(selects)[:200])
    exp, _ = claims(alpha, status=oem_claims.EXPIRED)
    check("status=Expired: exactly the 7 Pending claims whose deadline passed, total 7",
          len(exp["claims"]) == 7 and exp["total"] == 7 and all(c["status"] == oem_claims.EXPIRED for c in exp["claims"]),
          f"{len(exp['claims'])} / {exp['total']}")
    pend, _ = claims(alpha, status=oem_claims.PENDING)
    check("status=Pending: exactly the 12 live ones, total 12",
          len(pend["claims"]) == 12 and pend["total"] == 12 and all(c["status"] == oem_claims.PENDING for c in pend["claims"]),
          f"{len(pend['claims'])} / {pend['total']}")
    rev, _ = claims(alpha, status=oem_claims.REVOKED)
    check("status=Revoked: exactly the 4, total 4", len(rev["claims"]) == 4 and rev["total"] == 4,
          f"{len(rev['claims'])} / {rev['total']}")
    small, _ = claims(alpha, status=oem_claims.PENDING, limit=5, offset=10)
    check("limit=5&offset=10 of the 12 live ones: 2 rows, total 12",
          len(small["claims"]) == 2 and small["total"] == 12, f"{len(small['claims'])} / {small['total']}")
    # The SQL derivation agrees with oem_claims.public_state row by row.
    every = db.query(models.MachineClaim).filter_by(oem_code="OEM_ALPHA").all()
    derived = {s: sum(1 for c in every if oem_claims.public_state(c) == s)
               for s in (oem_claims.PENDING, oem_claims.EXPIRED, oem_claims.REVOKED)}
    check("the SQL states agree with oem_claims.public_state for every claim",
          derived == {oem_claims.PENDING: 12, oem_claims.EXPIRED: 7, oem_claims.REVOKED: 4}, str(derived))

    print("5. installations_for STILL LISTS EVERY ROW, IN ORDER, FOR ITS OTHER CALLERS")
    every_inst = oem_sharing.installations_for(db, "OEM_ALPHA")
    check("250 rows, ascending by id", len(every_inst) == 250 and
          [r.id for r in every_inst] == sorted(r.id for r in every_inst), str(len(every_inst)))
    check("narrowed to one customer: 50", len(oem_sharing.installations_for(db, "OEM_ALPHA", tenant_code="FACTORY_2")) == 50)

    event.remove(engine, "before_cursor_execute", _log)
    paging.forget_counts()
    db.close()
    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(" -", f)
        return 1
    print("ALL OEM FLEET/CLAIMS SQL-PAGING CHECKS PASSED")
    return 0


def test_oem_fleet_pages_in_sql():
    """The pytest entry point (the coverage job collects module-level test_ functions)."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    sys.exit(main())
