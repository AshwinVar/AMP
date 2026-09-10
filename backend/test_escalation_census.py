"""The escalation card counted rows it then showed in no bucket.

THE DEFECT
----------
`/analytics/escalations` publishes `total` beside a status breakdown, and
`EscalationSection.tsx:114-117` renders them on one row:

    Total | Open | In Progress | Resolved

The breakdown reads three exact strings:

    "open":        status_counts.get("Open", 0)
    "in_progress": status_counts.get("In Progress", 0)
    "resolved":    status_counts.get("Resolved", 0)

`ai/agents.py` writes two more. Every agent-raised escalation starts life as
`"Proposed"` (`:306` for repeated downtime, `:389` from the briefing), and
`:131` writes `"Cancelled"` when a human REJECTS one in the Approvals Inbox:

    item.status = "Open" if approve else "Cancelled"

So an escalation waiting on a human decision, and every escalation a human
declined, are counted in Total and appear in none of the three buckets beside
it — permanently. `status` is also `Column(String, default="Open")` WITHOUT
`nullable=False`, so a NULL goes the same way.

WHAT MAKES THIS ONE WORTH READING TWICE
----------------------------------------
The existing suite knows. `test_analytics_routes.py:956`:

    # the named status buckets + the two unnamed-status rows (Cancelled, NULL)
    # also account for every row — nothing double-counted, nothing dropped.
    assert out["open"] + out["in_progress"] + out["resolved"] + 2 == out["total"]

It encodes the gap as a literal `+ 2` and calls the result "nothing dropped". A
census assertion that adds a constant to make the arithmetic work is not
checking the census; it is documenting the hole. That is how this survived a
suite which, on this very endpoint, looks thorough — the same lesson as
`started + paused + completed == 4` in #568, but stated out loud in a comment.

That assertion is rewritten here to require a real partition.

NOT A DEFECT, having been checked: the severity row. `critical/high/medium/low`
sit beside the same total, but `severity` is `Column(String, nullable=False)`
and every writer emits one of those four — ai/agents.py "High",
factory_ops_routes "High"/"Medium", factory_simulator Critical/High/Medium, and
core_routes' `alert.get("severity", "Medium")` over generate_alerts, which only
produces High/Medium. ("Info" and "Warning" exist in this codebase, but they are
NOTIFICATION severities, not escalation ones.) So that half genuinely
partitions and is left alone.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_escalation_census.py
"""
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import analytics_routes
import models
import tenancy
from database import Base

T = "ESCCEN"
USER = {"tenant": T, "username": "tester", "role": "Admin"}
failures = []

# (title, status, severity) — one row per status word any writer emits.
ROWS = [
    ("E-OPEN", "Open", "High"),
    ("E-WORKING", "In Progress", "High"),
    ("E-DONE", "Resolved", "Low"),
    ("E-PROPOSED", "Proposed", "Critical"),   # ai/agents.py:306 / :389
    ("E-REJECTED", "Cancelled", "Medium"),    # ai/agents.py:131, on rejection
    ("E-NULL", None, "Medium"),
]


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def seed():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    db = sessionmaker(bind=engine)()
    tok = tenancy.set_current_tenant(None)
    db.add(models.TenantConfig(tenant_code=T))
    now = datetime.utcnow()
    for title, status, severity in ROWS:
        e = models.Escalation(tenant_code=T, title=title, severity=severity,
                              owner="Ops", department="Maintenance",
                              source="Smart alert", created_at=now)
        if status is not None:
            e.status = status
        db.add(e)
    db.commit()
    # status is Column(String, default="Open"): a SQLAlchemy column default is
    # applied on INSERT when the attribute is None, so constructing with
    # status=None silently stores "Open" and the NULL case is never exercised.
    db.execute(text("UPDATE escalations SET status = NULL WHERE title = 'E-NULL'"))
    db.commit()
    assert db.execute(text("SELECT status FROM escalations WHERE title='E-NULL'")
                      ).scalar() is None, "fixture failed to store a real NULL"
    tenancy.reset_current_tenant(tok)
    return db


def main():
    db = seed()
    tok = tenancy.set_current_tenant(T)
    a = analytics_routes.get_escalation_analytics(db, USER)

    print("=" * 74)
    print("1. THE STATUS ROW ADDS UP TO THE TOTAL")
    print("=" * 74)
    buckets = ("open", "in_progress", "resolved", "proposed", "cancelled", "other")
    parts = sum(a[b] for b in buckets)
    check(f"total = {a['total']}", a["total"] == len(ROWS), str(a["total"]))
    check(f"open+in_progress+resolved+proposed+cancelled+other == total "
          f"({parts} vs {a['total']})", parts == a["total"],
          " ".join(f"{b}={a[b]}" for b in buckets))

    print()
    print("=" * 74)
    print("2. THE TWO STATES THE AGENTS WRITE ARE REPORTED")
    print("=" * 74)
    check("an escalation awaiting a human decision is counted",
          a["proposed"] == 1, str(a["proposed"]))
    check("...and one a human declined is not lost", a["cancelled"] == 1,
          str(a["cancelled"]))
    check("a NULL status lands in 'other' rather than vanishing",
          a["other"] == 1, str(a["other"]))

    print()
    print("=" * 74)
    print("3. NOTHING ELSE MOVED")
    print("=" * 74)
    check("open still counts only the one Open row", a["open"] == 1, str(a["open"]))
    check("in_progress still counts the one", a["in_progress"] == 1,
          str(a["in_progress"]))
    check("resolved still counts the one", a["resolved"] == 1, str(a["resolved"]))

    print()
    print("=" * 74)
    print("4. THE SEVERITY ROW WAS ALREADY A PARTITION — AND STILL IS")
    print("=" * 74)
    # Checked rather than assumed: severity is Column(String, nullable=False)
    # and every escalation writer emits one of these four, so this half never
    # had the defect and must not acquire one.
    sev = a["critical"] + a["high"] + a["medium"] + a["low"]
    check(f"critical+high+medium+low == total ({sev} vs {a['total']})",
          sev == a["total"], f"{sev} vs {a['total']}")
    col = models.Escalation.__table__.columns["severity"]
    check("...which holds because severity is NOT NULL", col.nullable is False)

    print()
    print("=" * 74)
    print("5. THE SHARED OPEN RULE IS UNTOUCHED")
    print("=" * 74)
    # #565 made "is this escalation still on the queue" one predicate. That is a
    # DIFFERENT question from "which status word is on the row", and this census
    # must not be mistaken for it: Proposed is open work, Cancelled is not.
    from ai import escalations
    check("a Proposed escalation is still OPEN to the shared rule",
          not escalations._is_closed("Proposed"))
    check("...and a Cancelled one is still closed",
          escalations._is_closed("Cancelled"))
    open_rows = db.query(models.Escalation).filter(escalations.open_clause()).count()
    # Open + In Progress + Proposed + NULL = 4 still on the queue.
    check(f"the open queue is 4, which is not the same question as the census "
          f"({open_rows})", open_rows == 4, str(open_rows))

    tenancy.reset_current_tenant(tok)
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


def test_escalation_census():
    """The pytest entry point — a suite exposing only main() contributes
    nothing to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
