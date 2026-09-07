"""The operator terminal counted jobs it then showed in no bucket.

THE DEFECT
----------
`/analytics/operator-terminal` publishes `total_jobs` beside a status
breakdown, and `OperatorTerminalSection.tsx:13` renders them on one row:

    Jobs   Started   Paused   Completed   Good   Quality

The breakdown is an exact-string GROUP BY (`analytics_routes.py:1480`):

    "started":   status_counts.get("Started", 0)
    "paused":    status_counts.get("Paused", 0)
    "completed": status_counts.get("Completed", 0)

But `"In Progress"` is a status this system writes, in three places:

    factory_simulator.py:529   random.choice(["Completed", "In Progress",
                                              "Completed", "Paused"])
    factory_simulator.py:1004  job_status="In Progress"
    e2e_sim.py:162             "job_status": "In Progress"

So roughly a quarter of the simulated plant's jobs are counted in `Jobs` and
appear in none of the three buckets beside it. `job_status` is also
`Column(String, default="Started")` WITHOUT nullable=False, and
`OperatorJobExecutionUpdate` types it `Optional[str] = None`, so a client can
PATCH an explicit null — `schemas.py:1142` heals that for the RESPONSE, but the
analytics reads the column, not the response model, so a NULL vanishes from the
breakdown while still counting in the total.

AND THE RULE ALREADY EXISTS, CORRECTLY, ONE DIRECTORY AWAY
-----------------------------------------------------------
`ai/workforce.py:43` states it and even names the missing word:

    # Job statuses that mean the operator has closed the job out. Anything else
    # (Started / In Progress / Paused) is still live work, counted as active.
    DONE_STATUSES = {"completed", "complete", "done", "closed", "finished"}

matched through `_is_done()`, which lowercases and strips. So the workforce
read-model (`OperatorPerformanceCards`) and the operator terminal card describe
the same jobs on the same plant and disagree about them — the same "one rule,
two implementations" shape as #553, #562 and #565.

WHY THE EXISTING TEST DID NOT CATCH IT
---------------------------------------
`test_analytics_routes.py:794` asserts
`started + paused + completed == 4` — on a fixture whose four rows carry only
`"Started"`, `"Paused"` and `"Completed"`. Its own comment says so: "all four
are named". A census assertion over a fixture containing only the vocabulary
the census knows about cannot fail. That test is not wrong, it is narrow; this
file supplies the words it never had.

THE FIX
-------
`ai/workforce.job_status_bucket()` — one mapping, matched lowercased and
trimmed like `_is_done` beside it, with an explicit `other` bucket so an
unrecognised word or a NULL is VISIBLE rather than dropped. Every word mapped
below is one this repository actually writes.

Separately, `OperatorTerminalSection.tsx` renders a `<select>` whose options are
Started / Paused / Completed against `value={row.job_status}`. A row stored as
"In Progress" matches no option, so the control renders with nothing selected
and the operator cannot see what state the job is in. The select now renders
the stored value as an extra option when it is not one of the three, which keeps
the operator's own vocabulary to three choices while showing the truth.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_operator_status_census.py
"""
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import analytics_routes
import models
import tenancy
from ai import workforce
from database import Base

T = "OPCENSUS"
USER = {"tenant": T, "username": "tester", "role": "Admin"}
failures = []

# One job per status word any writer in this repo produces, plus an unknown one
# and a NULL. The NULL is written in SQL — see seed().
ROWS = [
    ("OJ-STARTED", "Started", 10, 0),
    ("OJ-INPROGRESS", "In Progress", 20, 0),   # factory_simulator.py:529/1004, e2e_sim.py:162
    ("OJ-PAUSED", "Paused", 30, 0),
    ("OJ-COMPLETED", "Completed", 40, 0),
    ("OJ-WEIRD", "Awaiting inspection", 0, 0),  # nothing writes this; `other` must exist
    ("OJ-NULL", None, 0, 0),
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
    for no, status, good, rejected in ROWS:
        # started_at inside ai/workforce's 7-day window, so the two
        # surfaces are reading the same rows and section 3 compares
        # like with like rather than an empty set against a full one.
        row = models.OperatorJobExecution(tenant_code=T, execution_no=no,
                                          operator_name="Rajan",
                                          started_at=datetime.utcnow(),
                                          good_count=good, rejected_count=rejected)
        if status is not None:
            row.job_status = status
        db.add(row)
    db.commit()
    # job_status is Column(String, default="Started"); a SQLAlchemy column
    # default is applied on INSERT when the attribute is None, so constructing
    # with job_status=None silently stores "Started" and the NULL case is never
    # exercised (the trap from #407/#562).
    db.execute(text("UPDATE operator_job_executions SET job_status = NULL "
                    "WHERE execution_no = 'OJ-NULL'"))
    db.commit()
    assert db.execute(text("SELECT job_status FROM operator_job_executions "
                           "WHERE execution_no='OJ-NULL'")).scalar() is None, \
        "fixture failed to store a real NULL"
    tenancy.reset_current_tenant(tok)
    return db


def main():
    db = seed()
    tok = tenancy.set_current_tenant(T)
    a = analytics_routes.get_operator_terminal_analytics(db, USER)

    print("=" * 74)
    print("1. THE PARTS ADD UP TO THE TOTAL")
    print("=" * 74)
    buckets = ("started", "paused", "completed", "other")
    parts = sum(a[b] for b in buckets)
    check(f"total_jobs = {a['total_jobs']} (every seeded row)",
          a["total_jobs"] == len(ROWS), str(a["total_jobs"]))
    check(f"started+paused+completed+other == total_jobs "
          f"({parts} vs {a['total_jobs']})",
          parts == a["total_jobs"], " ".join(f"{b}={a[b]}" for b in buckets))

    print()
    print("=" * 74)
    print("2. THE WORD THE SIMULATOR ACTUALLY WRITES")
    print("=" * 74)
    check("'In Progress' is live work, so it counts as started (2)",
          a["started"] == 2, str(a["started"]))
    check("Paused is paused", a["paused"] == 1, str(a["paused"]))
    check("Completed is completed", a["completed"] == 1, str(a["completed"]))
    check("an unrecognised word and a NULL land in 'other' (2), rather than "
          "being counted in the total and shown nowhere",
          a["other"] == 2, str(a["other"]))

    print()
    print("=" * 74)
    print("3. THE TERMINAL CARD AND THE WORKFORCE READ-MODEL AGREE")
    print("=" * 74)
    # ai/workforce.py splits the same rows into completed / active. The two
    # surfaces render for the same plant, so their idea of "closed out" has to
    # be the same one.
    w = workforce.build_operator_summary(db, T)
    check(f"the workforce read-model sees the same six jobs ({w['jobs']})",
          w["jobs"] == len(ROWS), str(w["jobs"]))
    check("...and the same one completed job",
          w["completed"] == a["completed"],
          f"workforce={w['completed']} terminal={a['completed']}")
    check("...so everything else is active, on both counts",
          w["active"] == a["total_jobs"] - a["completed"],
          f"workforce active={w['active']} "
          f"terminal not-completed={a['total_jobs'] - a['completed']}")

    print()
    print("=" * 74)
    print("4. THE VOCABULARY IS ONE MAPPING, MATCHED FORGIVINGLY")
    print("=" * 74)
    check("'In Progress' -> started",
          workforce.job_status_bucket("In Progress") == "started",
          workforce.job_status_bucket("In Progress"))
    check("case and padding do not decide it",
          workforce.job_status_bucket("  PAUSED  ") == "paused",
          workforce.job_status_bucket("  PAUSED  "))
    check("the DONE_STATUSES synonyms all bucket as completed",
          {workforce.job_status_bucket(s) for s in workforce.DONE_STATUSES} == {"completed"},
          str({s: workforce.job_status_bucket(s) for s in workforce.DONE_STATUSES}))
    check("NULL -> other", workforce.job_status_bucket(None) == "other",
          workforce.job_status_bucket(None))
    check("an unknown word -> other, never silently dropped",
          workforce.job_status_bucket("Awaiting inspection") == "other",
          workforce.job_status_bucket("Awaiting inspection"))
    # The bucket rule and _is_done() must not become a fifth and a sixth
    # implementation of each other.
    disagree = [s for s in ("Started", "In Progress", "Paused", "Completed",
                            "complete", "done", "closed", "finished", None,
                            "Awaiting inspection")
                if workforce._is_done(s) != (workforce.job_status_bucket(s) == "completed")]
    check("job_status_bucket agrees with _is_done on every word, both ways",
          not disagree, str(disagree))

    print()
    print("=" * 74)
    print("5. NOTHING ELSE MOVED")
    print("=" * 74)
    check("good_count is still the sum (100)", a["good_count"] == 100,
          str(a["good_count"]))
    check("rejected_count is still 0", a["rejected_count"] == 0,
          str(a["rejected_count"]))
    check("quality_rate is still 100%", a["quality_rate"] == 100,
          str(a["quality_rate"]))

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


def test_operator_status_census():
    """The pytest entry point — a suite exposing only main() contributes
    nothing to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
