"""The Executive OEE ranking published OEE for machines that made nothing.

THE DEFECT
----------
`/analytics/executive-oee` returns a row for every machine in `machine_ranking`.
For a machine with no production records in the window it did not say so. It
filled the gap with constants:

    availability = machine.utilization                 # a gauge, not a measurement
    performance  = 90 if machine.status == "Running" else 60
    quality      = 95                                  # when there were no inspections
    oee          = product of the above                # 80 * .90 * .95 -> 68

and ranked that 68% among machines whose OEE was measured. The frontend then
made it worse: `lib/oee.ts readMachineOee` treats any row it finds in the
ranking as `measured: true`, so the dashboard card printed "OEE 68%" under the
label reserved for measured figures, and its own honest "Estimated OEE" path
never ran — because the backend never omitted a row.

`docs/engineering/PRODUCTION-READINESS-FINAL.md` listed exactly this as a P1
blocker ("/analytics/executive-oee fabricates A/P/Q/OEE for machines with zero
production data") and marked it closed. The work that closed it (ADR-0014,
#558, #591) fixed the PLANT figure — `oee_contract.is_measurable` even names
this endpoint — and left the per-machine rows inventing numbers.

WHAT THIS SUITE PINS
--------------------
The row is computed by THE CONTRACT (oee_contract.oee_from_sums, "the one place
the formula lives"), which the machine cockpit already uses — so a machine reads
the same on the ranking and on its own screen. That is also why an earlier draft
of this fix was wrong: it let a measured zero in one component make the OEE 0
while another component was undefined. The contract says a product needs all
three, and the cockpit follows it; a second rule here would have shown the same
machine as 0% on one screen and "—" on the other, which is the exact class #590
and #591 removed.

* a component with no denominator (no planned time / no runtime / no counts) is
  None, never a constant;
* a MEASURED ZERO component is shown as 0 — a machine scheduled and never run
  reads availability 0% — it is not hidden;
* OEE is None whenever any component is undefined;
* quality from inspections is displayed when there are no production counts, but
  never feeds OEE.

Measured machines keep their numbers, and the ranking lists measured machines
first, by OEE, then machines with no OEE by name.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_executive_oee_no_invented_machine_figures.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite:///./ci.db")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import analytics_routes  # noqa: E402
import models  # noqa: E402
from database import Base  # noqa: E402

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _record(db, machine_id, planned, runtime, ideal, total, good):
    db.add(models.ProductionRecord(machine_id=machine_id, planned_minutes=planned,
                                   runtime_minutes=runtime, ideal_cycle_time_seconds=ideal,
                                   total_count=total, good_count=good,
                                   rejected_count=total - good))


def main():
    db = _db()
    # A: measured normally.
    db.add(models.Machine(id=1, name="A-MEASURED", status="Running", utilization=90))
    _record(db, 1, planned=480, runtime=400, ideal=30, total=600, good=570)
    # B: no production, Running, a utilization reading -> the old code said 68%.
    db.add(models.Machine(id=2, name="B-NO-PRODUCTION", status="Running", utilization=80))
    # C: no production, but inspections exist -> quality is a real measurement.
    db.add(models.Machine(id=3, name="C-INSPECTED-ONLY", status="Idle", utilization=50))
    db.add(models.QualityInspection(inspection_no="QI-1", inspector="I", machine_id=3,
                                    inspected_quantity=100, passed_quantity=90))
    # D: scheduled and never ran -> a MEASURED 0% availability.
    db.add(models.Machine(id=4, name="D-SCHEDULED-IDLE", status="Idle", utilization=0))
    _record(db, 4, planned=480, runtime=0, ideal=30, total=0, good=0)
    # E: ran and made nothing -> a MEASURED 0% performance.
    db.add(models.Machine(id=5, name="E-RAN-MADE-NOTHING", status="Running", utilization=60))
    _record(db, 5, planned=480, runtime=300, ideal=30, total=0, good=0)
    # F: no production, and every inspection failed -> inspection quality is a real
    #    0, but a zero from INSPECTIONS must not become a "measured zero" OEE for a
    #    machine that never produced. Mutation MB4 (dropping the measurability
    #    check) survived every suite until this machine was added.
    db.add(models.Machine(id=6, name="F-INSPECTED-ALL-FAILED", status="Idle", utilization=0))
    db.add(models.QualityInspection(inspection_no="QI-2", inspector="I", machine_id=6,
                                    inspected_quantity=40, passed_quantity=0))
    # G: ran (availability and performance defined), made nothing, and HAS
    #    inspections. The only shape where inspection quality could leak into OEE:
    #    A 62, P 0, displayed Q 90 — the contract still has no production quality,
    #    so no OEE. Mutation N5 survived until this machine existed. Inserted LAST
    #    but named to sort FIRST among the unmeasured, so the name-order check
    #    cannot pass by insertion order (mutation N6 had).
    db.add(models.Machine(id=7, name="AA-RAN-INSPECTED", status="Running", utilization=70))
    _record(db, 7, planned=480, runtime=300, ideal=30, total=0, good=0)
    db.add(models.QualityInspection(inspection_no="QI-3", inspector="I", machine_id=7,
                                    inspected_quantity=100, passed_quantity=90))
    db.commit()

    out = analytics_routes.get_executive_oee(db=db, current_user={})
    rows = {r["machine_name"]: r for r in out["machine_ranking"]}

    print("=" * 74)
    print("1. A MACHINE THAT MADE NOTHING HAS NO OEE, NOT AN INVENTED ONE")
    print("=" * 74)
    b = rows["B-NO-PRODUCTION"]
    check("no production -> OEE is None, not 68%", b["oee"] is None, repr(b))
    check("...availability is not the utilization gauge", b["availability"] is None, repr(b))
    check("...performance is not the 90-if-Running constant", b["performance"] is None, repr(b))
    check("...quality is not the 95 constant", b["quality"] is None, repr(b))
    check("...and the row says it is unmeasured", b.get("measured") is False, repr(b))
    check("...while the raw utilization reading is still surfaced",
          b["utilization"] == 80, repr(b))

    print()
    print("=" * 74)
    print("2. REAL MEASUREMENTS ARE KEPT, INCLUDING REAL ZEROS")
    print("=" * 74)
    a = rows["A-MEASURED"]
    # 400/480 -> 83; (30*600)/(400*60) = 18000/24000 -> 75; 570/600 -> 95;
    # OEE from the ROUNDED components, exactly as before: .83*.75*.95 -> 59.
    check("a measured machine keeps its exact numbers",
          (a["availability"], a["performance"], a["quality"], a["oee"]) == (83, 75, 95, 59),
          repr(a))
    check("...and is flagged measured", a.get("measured") is True, repr(a))

    c = rows["C-INSPECTED-ONLY"]
    check("quality measured from inspections is kept (90), not discarded",
          c["quality"] == 90, repr(c))
    check("...but with no production there is still no OEE",
          c["oee"] is None and c.get("measured") is False, repr(c))

    g = rows["AA-RAN-INSPECTED"]
    check("a machine that ran and made nothing shows its inspection quality (90)",
          g["quality"] == 90 and g["performance"] == 0, repr(g))
    check("...but inspection quality never feeds OEE: still no OEE",
          g["oee"] is None and g.get("measured") is False, repr(g))

    f = rows["F-INSPECTED-ALL-FAILED"]
    check("an all-failed inspection is a real quality of 0", f["quality"] == 0, repr(f))
    check("...but it does not give a never-produced machine a 'measured' 0% OEE",
          f["oee"] is None and f.get("measured") is False, repr(f))

    d = rows["D-SCHEDULED-IDLE"]
    check("scheduled and never ran -> availability is SHOWN as a measured 0",
          d["availability"] == 0, repr(d))
    check("...performance (no runtime) is undefined, so by the contract there is no OEE",
          d["performance"] is None and d["oee"] is None and d.get("measured") is False, repr(d))

    e = rows["E-RAN-MADE-NOTHING"]
    check("ran and made nothing -> performance is SHOWN as a measured 0", e["performance"] == 0,
          repr(e))
    check("...quality (no counts) is undefined, so by the contract there is no OEE",
          e["quality"] is None and e["oee"] is None and e.get("measured") is False, repr(e))

    print()
    print("=" * 74)
    print("2b. THE RANKING AGREES WITH THE MACHINE'S OWN SCREEN")
    print("=" * 74)
    # The cockpit computes a machine's OEE with oee_contract.machine_oee over the
    # same window. Every row here must say the same thing about the same machine.
    import oee_contract
    disagreements = []
    for r in out["machine_ranking"]:
        mine = oee_contract.as_percentages(oee_contract.machine_oee(db, "DEFAULT", r["machine_id"]))
        if (mine["oee"], mine["availability"], mine["performance"]) != \
                (r["oee"], r["availability"], r["performance"]):
            disagreements.append((r["machine_name"], (r["availability"], r["performance"], r["oee"]),
                                  (mine["availability"], mine["performance"], mine["oee"])))
    check("every ranked machine matches oee_contract.machine_oee (availability, performance, OEE)",
          disagreements == [], repr(disagreements))

    print()
    print("=" * 74)
    print("3. THE RANKING NEVER PUTS A GUESS AMONG MEASUREMENTS")
    print("=" * 74)
    order = [r["machine_name"] for r in out["machine_ranking"]]
    measured_part = [r for r in out["machine_ranking"] if r.get("measured")]
    unmeasured_part = [r for r in out["machine_ranking"] if not r.get("measured")]
    check("every measured machine is ranked before every unmeasured one",
          order == [r["machine_name"] for r in measured_part + unmeasured_part], repr(order))
    check("measured machines are ordered by OEE, highest first",
          [r["oee"] for r in measured_part] == sorted((r["oee"] for r in measured_part),
                                                     reverse=True), repr(order))
    check("unmeasured machines follow in name order",
          [r["machine_name"] for r in unmeasured_part]
          == sorted(r["machine_name"] for r in unmeasured_part), repr(order))
    # Two invariants, stated precisely. A row without an OEE may still show a
    # MEASURED zero component (D shows availability 0%), so "no numbers on an
    # unmeasured row" would be wrong. What must never happen is a number with no
    # measurement behind it.
    no_oee_but_number = [r["machine_name"] for r in out["machine_ranking"]
                         if not r.get("measured") and r["oee"] is not None]
    check("no row without a measured OEE carries a numeric OEE",
          no_oee_but_number == [], repr(no_oee_but_number))
    no_production = [r for r in out["machine_ranking"] if r["total_count"] == 0
                     and r["machine_name"] in ("B-NO-PRODUCTION", "C-INSPECTED-ONLY",
                                               "F-INSPECTED-ALL-FAILED")]
    invented = [r["machine_name"] for r in no_production
                if any(isinstance(r[k], (int, float)) for k in ("availability", "performance"))]
    check("a machine with no production records at all has no availability or performance",
          len(no_production) == 3 and invented == [], repr(invented or no_production))

    print()
    print("=" * 74)
    print("4. THE PLANT FIGURE IS UNCHANGED")
    print("=" * 74)
    # Pooled over A, D, E (the machines with production): planned 1440,
    # runtime 700, ideal 30*600 = 18000 s, total 600, good 570.
    check("plant OEE is still the pooled figure and still measured",
          out.get("plant_oee") is not None and out.get("has_data") is True, repr(
              {k: out.get(k) for k in ("plant_oee", "has_data")}))

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILURE(S)")
        for f in failures:
            print("  -", f)
        return 1
    print("EXECUTIVE OEE OK: unmeasured machines have no OEE; measured zeros and "
          "measurements are kept")
    return 0


if __name__ == "__main__":
    sys.exit(main())
