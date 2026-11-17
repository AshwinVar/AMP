"""A contended first allocation must not throw away the caller's half-built document.

WHAT THIS PINS
--------------
doc_numbers.allocate reserves a document number INSIDE the caller's open
transaction and does not commit — so by the time a handler asks for a GRN or an
issue-slip number, it has usually ALREADY written the header that number is for.
The function's docstring states exactly this ("a request that fails afterwards
rolls the reservation back with everything else rather than burning a number on a
write that never happened").

The first allocation for a (tenant, doc_type) has no counter row yet, so it
INSERTs one. Two requests racing to make that first slip both read "no row"
before either commits; one INSERT wins, the other trips the UNIQUE(tenant_code,
doc_type) constraint and recovers by reading the winner's row. The recovery used
to begin with a bare `db.rollback()`, which rolls back the WHOLE transaction —
discarding the GRN/slip the handler had already written. The handler then went on
to attach the freshly allocated number to a row that no longer existed.

The existing concurrency test (test_tenant_document_numbers.py §7) commits the
first allocator BEFORE the second runs, so the second takes the row-exists branch
and the INSERT-collision recovery never executes. This suite drives the recovery
directly and asserts the caller's pending write survives it.

HOW THE RACE IS FORCED DETERMINISTICALLY
----------------------------------------
_highest_existing is called in exactly the gap between allocate's "no counter
row" SELECT and its INSERT. Monkeypatching it to create the winning counter row
as a side effect reproduces the interleaving without threads — the same
deterministic-interleaving technique the existing suite uses.

Run: DATABASE_URL="sqlite:///./ci.db" python test_document_number_race_keeps_caller_writes.py
"""
import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import doc_numbers
import models
from database import Base

# A private engine (same reasoning as test_tenant_document_numbers): this suite
# creates and drops its own schema. A PostgreSQL DATABASE_URL is honoured so the
# savepoint recovery is proved against the engine production actually runs.
_URL = os.environ.get("DATABASE_URL", "")
if _URL.startswith("postgresql"):
    engine = create_engine(_URL)
else:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
SessionLocal = sessionmaker(bind=engine)

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def _sentinel(tenant, slip_no):
    """A minimally-valid issue slip — the caller's in-progress work."""
    return models.MaterialIssueSlip(
        tenant_code=tenant, slip_no=slip_no, requested_qty=1, requested_by="tester")


def _reset_schema():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _with_racing_first_allocation(winner_next_value):
    """Install a _highest_existing that, on its first call, inserts the WINNING
    counter row (as another allocator would) so allocate's own INSERT collides.

    Returns a restore() to undo the monkeypatch."""
    real = doc_numbers._highest_existing
    state = {"fired": False}

    def racing_highest(db, model, column, prefix, tenant):
        if not state["fired"]:
            state["fired"] = True
            # The other allocator wins the race to create the counter row and
            # commits its reservation: next_value already advanced past its own
            # first number. A separate session is unnecessary — flushing it into
            # this same transaction before allocate's INSERT is enough to make the
            # UNIQUE constraint trip, which is the condition under test.
            db.add(models.DocumentSequence(
                tenant_code=tenant, doc_type="MIS", next_value=winner_next_value))
            db.flush()
        return real(db, model, column, prefix, tenant)

    doc_numbers._highest_existing = racing_highest
    return lambda: setattr(doc_numbers, "_highest_existing", real)


def test_recovery_keeps_the_callers_pending_document():
    _reset_schema()
    print()
    print("=" * 74)
    print("A CONTENDED FIRST ALLOCATION PRESERVES THE CALLER'S PENDING WRITE")
    print("=" * 74)

    db = SessionLocal()
    # The caller writes its document FIRST, then asks for a number — exactly the
    # order the real handlers use (db.add(grn) ... allocate ... db.commit()).
    db.add(_sentinel("FACTORY_A", "SENTINEL-1"))

    restore = _with_racing_first_allocation(winner_next_value=5001)
    try:
        number = doc_numbers.allocate(
            db, "FACTORY_A", "MIS", models.MaterialIssueSlip, "slip_no", "MIS", start=5000)
    finally:
        restore()

    # The recovery takes the winner's row, so the number is the winner's and is
    # never a value the winner already issued (independently derived: the winner's
    # counter sat at next_value=5001, so the loser reads 5001 and advances it).
    check("the recovery issues the winner's next number, never a reused one",
          number == "MIS-5001", f"got {number}")

    db.commit()

    # THE POINT. The slip the caller had already written must have survived the
    # number allocation. A bare rollback inside allocate discarded it (and, on
    # this interleaving, the winner row the recovery then reads — a NoResultFound
    # crash); a savepoint touches only the collided INSERT.
    fresh = SessionLocal()
    survived = (fresh.query(models.MaterialIssueSlip)
                .filter(models.MaterialIssueSlip.slip_no == "SENTINEL-1").first())
    check("the caller's half-built document survived the allocation",
          survived is not None,
          "the pending slip was rolled back with the number reservation")

    # And the counter advanced, so the NEXT allocation does not hand out 5001
    # again (the reservation really was kept, not merely read).
    seq = (fresh.query(models.DocumentSequence)
           .filter(models.DocumentSequence.tenant_code == "FACTORY_A",
                   models.DocumentSequence.doc_type == "MIS").one())
    check("the counter advanced past the issued number", seq.next_value == 5002,
          f"next_value={seq.next_value}")
    fresh.close()
    db.close()


def test_happy_path_first_allocation_unchanged():
    """The uncontended first allocation (the overwhelmingly common case) still
    seeds and returns its number — the savepoint wrapper must be invisible when
    nothing collides."""
    _reset_schema()
    print()
    print("=" * 74)
    print("CONTROL: AN UNCONTENDED FIRST ALLOCATION IS UNCHANGED")
    print("=" * 74)

    db = SessionLocal()
    db.add(_sentinel("FACTORY_A", "SENTINEL-2"))
    first = doc_numbers.allocate(
        db, "FACTORY_A", "MIS", models.MaterialIssueSlip, "slip_no", "MIS", start=5000)
    second = doc_numbers.allocate(
        db, "FACTORY_A", "MIS", models.MaterialIssueSlip, "slip_no", "MIS", start=5000)
    db.commit()

    check("first number is seeded from start", first == "MIS-5001", f"got {first}")
    check("second number is consecutive", second == "MIS-5002", f"got {second}")

    fresh = SessionLocal()
    survived = (fresh.query(models.MaterialIssueSlip)
                .filter(models.MaterialIssueSlip.slip_no == "SENTINEL-2").first())
    check("the caller's pending write committed normally", survived is not None)
    fresh.close()
    db.close()


if __name__ == "__main__":
    test_recovery_keeps_the_callers_pending_document()
    test_happy_path_first_allocation_unchanged()
    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("All checks passed.")
