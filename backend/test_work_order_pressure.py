"""The risk factor for work-order load could only ever score zero.

THE DEFECT
----------
`predictive_engine` scores machine failure risk out of 100. One of its ten
factors is outstanding production demand:

    if pressure >= 500:
        score += 10
        reasons.append("high active work-order load")

`pressure` sums `target_quantity - actual_quantity` over work orders whose status
is in

    ACTIVE_WORK_ORDER_STATUSES = ("Running", "Delayed")

and AMP writes neither word. `factory_simulator.py:279` writes the book as
["Planned", "In Progress", "In Progress", "In Progress", "Completed", "On Hold"],
and the vocabulary the whole backend writes is exactly Planned / In Progress /
Completed / On Hold (frontend/lib/status-vocab.json, checked against the backend
AST by test_status_vocabulary_parity.py).

Measured on a seeded plant:

    work-order statuses present : ['Completed', 'In Progress', 'On Hold', 'Planned']
    ACTIVE_WORK_ORDER_STATUSES  : ('Running', 'Delayed')
    intersection                : EMPTY

so `pressure` was 0 for every machine, the +10 never fired, and "high active
work-order load" has never appeared as a reason on any machine, ever. The factor
was published, documented and dead.

With the canonical open-set, on that same plant:

    SMT-PickPlace-01     0 -> 10   (pressure 1478)
    IC-Assembly-01       0 -> 10   (pressure 1234)
    IC-FinalQC-01       12 -> 22   (pressure  639)

Three of eight machines, +10 each. NO risk BAND moves on this plant — all three
sit deep inside Low, and the one High machine (SMT-AOI-01, 55) has no open
orders. A machine sitting at 45-54 would cross into High; none is. That is the
honest size of the change: it restores a factor, and on a seeded plant it does
not restate anybody's risk level.

WHY THIS WAS LEFT UNTIL NOW
---------------------------
#579 built `work_order_status.py` for exactly this family and fixed the command
centre. Its docstring names this line as an outstanding caller —
"predictive_engine:33  ('Running', 'Delayed')  the risk factor ... matched
nothing at all" — and it was deliberately deferred, because repairing it changes
PUBLISHED RISK SCORES and deserved its own before/after measurement rather than
riding along with a KPI fix. That measurement is the block above.

THE RULE
--------
A whitelist has to enumerate every word anyone will ever write. The complement
only has to enumerate the endings, and a word nobody thought of defaults to OPEN
— the safe direction for a backlog. `work_order_status` already states it once;
this file pins that the risk engine and its SQL loader both use it and neither
keeps a second copy.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_work_order_pressure.py
"""
import json
import os
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

import models
import predictive_engine as pe
import work_order_status as wos
from database import Base

failures = []

_VOCAB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      os.pardir, "frontend", "lib", "status-vocab.json")


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def _machine(machine_id=1, status="Idle", utilization=70):
    """A machine with no downtime, no events and no rejects, so the ONLY factor
    that can move its score is work-order pressure."""
    return SimpleNamespace(id=machine_id, name=f"M-{machine_id}", status=status,
                           utilization=utilization, line="L1")


def _wo(status, target=900, actual=0, machine_id=1):
    return SimpleNamespace(machine_id=machine_id, status=status,
                           target_quantity=target, actual_quantity=actual)


def _score(work_orders, machine=None):
    machine = machine or _machine()
    rows = pe.calculate_predictive_risk([machine], [], [], [], work_orders)
    return rows[0]


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def main():
    print("=" * 74)
    print("1. OUTSTANDING WORK RAISES THE SCORE, FOR EVERY OPEN STATUS AMP WRITES")
    print("=" * 74)
    # Driven from the shared vocabulary, not a literal list, so a status added to
    # the product is covered here the day it is added.
    vocab = json.load(open(_VOCAB, encoding="utf-8"))["WorkOrder"]["status"]
    open_statuses = [s for s in vocab["options"] + vocab["systemOnly"]
                     if not wos.is_closed(s)]
    check(f"the vocabulary has open states to test ({', '.join(open_statuses)})",
          len(open_statuses) >= 3, str(open_statuses))
    for status in open_statuses:
        row = _score([_wo(status)])
        check(f"{status!r}: 900 outstanding units scores {row['risk_score']} "
              f"with reason present",
              row["risk_score"] >= 10 and "high active work-order load" in row["reasons"],
              f"score={row['risk_score']} reasons={row['reasons']}")

    print()
    print("=" * 74)
    print("2. FINISHED WORK DOES NOT — AND THE THRESHOLD IS REAL")
    print("=" * 74)
    for status in ("Completed", "Cancelled", "  COMPLETED  ", "done"):
        row = _score([_wo(status)])
        check(f"{status!r} carries no pressure",
              "high active work-order load" not in row["reasons"],
              f"reasons={row['reasons']}")
    # The control: without a threshold every open order would score, and section 1
    # would pass for the wrong reason.
    low = _score([_wo("In Progress", target=499, actual=0)])
    check("499 outstanding units is below the 500 threshold",
          "high active work-order load" not in low["reasons"], str(low["reasons"]))
    at = _score([_wo("In Progress", target=500, actual=0)])
    check("...and 500 is not", "high active work-order load" in at["reasons"],
          str(at["reasons"]))
    # Delivered work is not outstanding work.
    done = _score([_wo("In Progress", target=900, actual=900)])
    check("an open order already delivered in full carries no pressure",
          "high active work-order load" not in done["reasons"], str(done["reasons"]))

    print()
    print("=" * 74)
    print("3. A STATUS NOBODY ENUMERATED COUNTS AS OPEN")
    print("=" * 74)
    # The whole point of a complement. An unknown word must default to the safe
    # direction: unfinished work is still work.
    for status in (None, "", "Awaiting Materials", "Quarantined", "Running", "Delayed"):
        row = _score([_wo(status)])
        check(f"{status!r} counts toward pressure",
              "high active work-order load" in row["reasons"], str(row["reasons"]))

    print()
    print("=" * 74)
    print("4. THE SQL LOADER AND THE PYTHON FILTER ARE ONE RULE")
    print("=" * 74)
    # ai/prediction bounds the query in SQL; predictive_engine re-filters in
    # Python. Two implementations of "open" is exactly the defect being fixed, so
    # they are pinned against each other row for row.
    db = _session()
    statuses = ["Planned", "In Progress", "On Hold", "Completed", "Cancelled",
                "  Completed  ", "COMPLETED", "done", None, "", "Awaiting Materials"]
    for i, status in enumerate(statuses, start=1):
        db.add(models.WorkOrder(work_order_no=f"WO-{i}", machine_id=1,
                                part_number="P", batch_number=f"B-{i}",
                                target_quantity=10, actual_quantity=0,
                                status=status))
    db.commit()
    sql_open = {w.work_order_no for w in
                db.query(models.WorkOrder).filter(wos.open_clause()).all()}
    py_open = {w.work_order_no for w in db.query(models.WorkOrder).all()
               if not wos.is_closed(w.status)}
    check(f"open_clause() and is_closed() agree on all {len(statuses)} rows "
          f"({len(sql_open)} open)", sql_open == py_open,
          f"sql-only={sorted(sql_open - py_open)} python-only={sorted(py_open - sql_open)}")
    check("...and that is not a vacuous agreement (some rows are closed)",
          0 < len(sql_open) < len(statuses), f"{len(sql_open)} of {len(statuses)}")
    db.close()

    print()
    print("=" * 74)
    print("5. NO SECOND COPY OF THE RULE SURVIVES")
    print("=" * 74)
    check("predictive_engine no longer defines its own active-status whitelist",
          not hasattr(pe, "ACTIVE_WORK_ORDER_STATUSES"),
          f"ACTIVE_WORK_ORDER_STATUSES = {getattr(pe, 'ACTIVE_WORK_ORDER_STATUSES', None)!r}")
    import ai.prediction as prediction
    check("...and ai.prediction does not import one either",
          not hasattr(prediction, "ACTIVE_WORK_ORDER_STATUSES"),
          "ai.prediction still holds a status whitelist")
    source = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "predictive_engine.py"), encoding="utf-8").read()
    check("...and predictive_engine reads the canonical vocabulary",
          "work_order_status" in source, "no import of work_order_status")

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for failure in failures:
            print(f"  - {failure}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


def test_work_order_pressure():
    """The pytest entry point — a suite exposing only main() contributes nothing
    to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
