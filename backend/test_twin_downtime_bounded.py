"""The fleet twin must not read the whole downtime log to show three rows each.

THE DEFECT, AND WHOSE IT IS
---------------------------
`ai/twin.py::_downtime_by_machine` backs `/machine-health`, on the dashboard's
3-second poll. It selected THREE COLUMNS of EVERY downtime row, ordered by id,
and kept the first three per machine in Python. At 200 machines with a month of
history that is 75,000 rows transferred to keep 600.

I wrote it, in #525, to remove an N+1: it replaced a per-machine `.limit(3)`
(three queries per machine) with one query. That trade is a win at 200 rows and
a loss at 75,000 — after the #537 aggregate work it was the whole of
`/machine-health`'s remaining 261 ms, and 200 of those milliseconds were Python
iterating rows it then discarded.

WHY THE #532 GUARD MISSED IT
-----------------------------
`test_growing_table_reads.py` exempts `db.query(Model.col, ...)` on the grounds
that "the row never becomes an ORM object". That is true and it is not the
point: 75,000 projected rows are still 75,000 rows over the wire and 75,000
iterations in Python. The exemption is narrowed in that file, and this suite
pins the behaviour it was hiding here.

WHY A WINDOW FUNCTION IS NOW SAFE
----------------------------------
The original comment says grouping happens in Python "so the behaviour is
identical on SQLite (tests) and PostgreSQL (production)". That was a real
constraint once: window functions need SQLite 3.25+. The interpreter here bundles
3.38, and CI uses the same Python, so `row_number() OVER (PARTITION BY ...)`
runs on both. The database does the per-machine cut instead of the network.

THE PART THAT NEEDED CHECKING
------------------------------
Tenant scoping (ADR-0002) is applied by an ORM hook on selects against a SCOPED
model. Wrapping the select in a subquery and selecting from THAT could bypass
the hook — the outer statement's entity is the subquery, not the model. So the
tenant predicate is written explicitly inside the subquery, and section 2 proves
a second factory's downtime never reaches the first factory's twin. A
performance change that quietly widened tenant visibility would be a far worse
bug than the one it fixed.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_twin_downtime_bounded.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
from ai import twin
from database import Base

A, B = "TWIN_A", "TWIN_B"
failures = []
_sql = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def seed(per_machine=30, n_machines=6):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()

    @event.listens_for(engine, "before_cursor_execute")
    def _cap(conn, cur, statement, params, context, many):
        _sql.append(" ".join(statement.split()))

    Session = sessionmaker(bind=engine)
    db = Session()
    tok = tenancy.set_current_tenant(None)
    for t in (A, B):
        db.add(models.TenantConfig(tenant_code=t))
    now = datetime.utcnow()
    ids = {}
    for t in (A, B):
        ids[t] = []
        for i in range(n_machines):
            m = models.Machine(tenant_code=t, name=f"{t}-M{i}", site="P1",
                               status="Running", utilization=60, downtime="0 min")
            db.add(m)
            db.flush()
            ids[t].append(m.id)
    # Factory B's reasons are distinctive: if one leaks into A's twin, the
    # isolation check below sees a word that cannot legitimately appear.
    for t in (A, B):
        for i in range(n_machines * per_machine):
            db.add(models.DowntimeLog(
                tenant_code=t, machine_id=ids[t][i % n_machines],
                reason=(f"A-reason-{i}" if t == A else f"BONLY-leak-{i}"),
                duration=f"{5 + (i % 50)} min",
                created_at=now - timedelta(minutes=i)))
        db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()
    return Session, ids


def call(Session, tenant, fn, *args, **kw):
    db = Session()
    tok = tenancy.set_current_tenant(tenant)
    try:
        return fn(db, *args, **kw)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def full_scans():
    out = []
    for s in _sql:
        low = s.lower()
        if not low.startswith("select") or "from downtime_logs" not in low:
            continue
        # A bounded read caps rows: a LIMIT, an aggregate, or the window
        # function's rank predicate.
        if "limit" in low or "count(" in low or "row_number" in low or "group by" in low:
            continue
        out.append(s[:110])
    return out


def main():
    print("=" * 74)
    print("1. ORACLE — the same three rows per machine, in the same order")
    print("=" * 74)
    Session, ids = seed()

    # Ground truth computed here: newest-first by id, three per machine.
    db = Session()
    tok = tenancy.set_current_tenant(None)
    expected = {}
    for mid in ids[A]:
        rows = (db.query(models.DowntimeLog)
                  .filter(models.DowntimeLog.machine_id == mid,
                          models.DowntimeLog.tenant_code == A)
                  .order_by(models.DowntimeLog.id.desc()).limit(3).all())
        expected[mid] = [{"reason": r.reason, "duration": r.duration} for r in rows]
    tenancy.reset_current_tenant(tok)
    db.close()

    got = call(Session, A, twin._downtime_by_machine)
    check("every machine gets its three most recent downtime rows",
          {k: v for k, v in got.items() if k in expected} == expected,
          f"\n    expected {list(expected.items())[:1]}\n    got      "
          f"{[(k, v) for k, v in got.items() if k in expected][:1]}")
    check("...and no machine gets more than three",
          all(len(v) <= 3 for v in got.values()),
          str({k: len(v) for k, v in got.items()}))
    check("CONTROL: the fixture really has more history than that",
          len(expected) and all(len(v) == 3 for v in expected.values()),
          str({k: len(v) for k, v in expected.items()}))

    print()
    print("=" * 74)
    print("2. ISOLATION — a subquery must not escape the tenant scope")
    print("=" * 74)
    # The risk this change introduces. ADR-0002 scopes selects against a SCOPED
    # model; selecting from a SUBQUERY of one may not be seen by that hook.
    a_rows = [r for rows in call(Session, A, twin._downtime_by_machine).values()
              for r in rows]
    leaked = [r for r in a_rows if "BONLY" in str(r.get("reason"))]
    check("factory A's twin contains none of factory B's downtime",
          not leaked, f"{len(leaked)} leaked row(s): {leaked[:2]}")
    check("...and A's twin covers only A's machines",
          set(call(Session, A, twin._downtime_by_machine)) <= set(ids[A]),
          str(sorted(set(call(Session, A, twin._downtime_by_machine)) - set(ids[A]))))
    # CONTROL: B must still see its own, or "no leak" could just mean "no rows".
    b_rows = [r for rows in call(Session, B, twin._downtime_by_machine).values()
              for r in rows]
    check("CONTROL: factory B DOES see its own downtime",
          b_rows and all("BONLY" in str(r["reason"]) for r in b_rows),
          f"{len(b_rows)} row(s)")

    print()
    print("=" * 74)
    print("3. BOUNDED — the whole table is not read to show three rows each")
    print("=" * 74)
    _sql.clear()
    call(Session, A, twin._downtime_by_machine)
    bad = full_scans()
    check("no unbounded read of downtime_logs", not bad,
          f"{len(bad)} scan(s): {bad[:1]}")

    print()
    print("=" * 74)
    print("4. FLAT — the work does not grow with the history")
    print("=" * 74)
    small, _ = seed(per_machine=2)
    big, _ = seed(per_machine=60)
    _sql.clear()
    call(small, A, twin._downtime_by_machine)
    n_small = len([s for s in _sql if s.lower().startswith("select")])
    _sql.clear()
    call(big, A, twin._downtime_by_machine)
    n_big = len([s for s in _sql if s.lower().startswith("select")])
    check("same statement count at 2 and 60 rows per machine",
          n_small == n_big, f"{n_small} vs {n_big}")
    check("...and no scan on the larger history", not full_scans(),
          str(full_scans()[:1]))
    check("...and still exactly three rows per machine",
          all(len(v) == 3 for v in call(big, A, twin._downtime_by_machine).values()),
          str({k: len(v) for k, v in call(big, A, twin._downtime_by_machine).items()}))

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
