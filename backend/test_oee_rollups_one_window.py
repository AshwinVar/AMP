"""Three OEE rollups measured all time while every other surface measured a week.

THE DEFECT
----------
`oee_contract.OeeWindow` is THE window: `/oee-summary`, the machine cockpit, the
scorecard, the recovery card and the cost card all pool the canonical rolling
seven days. Three rollups in `analytics_routes.py` pooled `production_records`
with **no date filter at all**:

    analytics_summary            plant OEE on the exec home
    /analytics/management        plant OEE on the management dashboard
    /analytics/executive-oee     plant OEE + the machine ranking

so they published a LIFETIME figure under the same name.

Measured on one machine that ran terribly a year ago and beautifully this week:

    Executive ranking (lifetime)   35%
    Machine cockpit (7 days)       83%

Forty-eight points apart, for the same machine, on two screens the user clicks
between. The ranking that decides which machine looks worst is ranking on
history nobody can act on.

THE COMMENTS ALREADY CLAIMED OTHERWISE
--------------------------------------
`/analytics/executive-oee`'s own plant rollup says:

    "Plant rollup is POOLED (ratio of sums) — the single standardised OEE
     definition (analytics_engine.pooled_oee), so /analytics/executive-oee agrees
     with /oee-summary and every other surface."

It cannot agree with `/oee-summary` while measuring a different span. The
standard was applied to the FORMULA and not to the WINDOW.

And the same file had already made this exact argument once, about shifts, three
hundred lines earlier — bounding `shift_data` to the most recent 50 "so the two
per-shift attainment surfaces reconcile on ONE basis instead of disagreeing
(shift-kpis last-50 vs this endpoint all-time)". The reasoning was right and was
applied to one column of one endpoint.

THE FIX
-------
All three pool the canonical window. Production, quality and downtime are bounded
by the same `OeeWindow`, so a plant OEE is a plant OEE wherever it is read.

WHAT MOVES: these three published figures now describe the last seven days
rather than all history. That is the point — and it is a real change to a
flagship number, which is why this is its own PR with its own measurement,
exactly as #583 was.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_oee_rollups_one_window.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

import analytics_routes
import models
import oee_contract
from ai.oee import build_oee_summary
from database import Base

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


ADMIN = {"sub": "admin", "role": "Admin", "tenant": "DEFAULT"}


def _machine(db, name="PRESS-01"):
    m = models.Machine(name=name, status="Running", utilization=80,
                       downtime="0 min", line="SMT")
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _run(db, machine_id, when, planned=480, runtime=470, total=1000, good=995):
    db.add(models.ProductionRecord(
        machine_id=machine_id, planned_minutes=planned, runtime_minutes=runtime,
        ideal_cycle_time_seconds=12, total_count=total, good_count=good,
        rejected_count=total - good, created_at=when))
    db.commit()


def _plant_figures(db):
    """The plant OEE each surface publishes, by name."""
    return {
        "/oee-summary": build_oee_summary(db, "DEFAULT")["plant"]["oee"],
        "analytics_summary": analytics_routes.analytics_summary(
            db=db, current_user=ADMIN)["avg_oee"],
        "/analytics/management": analytics_routes.get_management_dashboard(
            db=db, current_user=ADMIN)["avg_oee"],
        "/analytics/executive-oee": analytics_routes.get_executive_oee(
            db=db, current_user=ADMIN)["plant_oee"],
    }


def main():
    print("=" * 74)
    print("1. ONE PLANT, ONE PLANT OEE")
    print("=" * 74)
    db = _session()
    m = _machine(db)
    # Ran terribly a year ago, beautifully this week. Any surface measuring all
    # time reports a different plant than any surface measuring the week.
    _run(db, m.id, datetime.utcnow() - timedelta(days=365),
         runtime=30, total=1000, good=50)
    _run(db, m.id, datetime.utcnow() - timedelta(days=2),
         runtime=470, total=2000, good=1990)

    figures = _plant_figures(db)
    for name, value in figures.items():
        print(f"      {name:28s} {value}%")
    canonical = figures["/oee-summary"]
    for name, value in figures.items():
        check(f"{name} agrees with the canonical window ({value} vs {canonical})",
              value == canonical, "this surface is measuring a different span")
    check("...and the canonical figure is not zero, so that is not agreement "
          "on nothing", canonical > 0, str(canonical))
    db.close()

    print()
    print("=" * 74)
    print("2. THE MACHINE RANKING AGREES WITH THE MACHINE COCKPIT")
    print("=" * 74)
    # The click-through: a user picks the worst machine off the ranking and opens
    # its cockpit. The two must describe the same machine.
    from ai import twin
    db = _session()
    m = _machine(db)
    _run(db, m.id, datetime.utcnow() - timedelta(days=365),
         runtime=30, total=1000, good=50)
    _run(db, m.id, datetime.utcnow() - timedelta(days=2),
         runtime=470, total=2000, good=1990)
    ranking = analytics_routes.get_executive_oee(db=db, current_user=ADMIN)["machine_ranking"]
    row = next(r for r in ranking if r["machine_id"] == m.id)
    cockpit = twin.build_machine_detail(db, "DEFAULT", m.id)["oee"]["oee"]
    check(f"the ranking and the cockpit agree ({row['oee']} vs {cockpit})",
          row["oee"] == cockpit,
          "the same machine reads differently on the two screens a user clicks between")
    db.close()

    print()
    print("=" * 74)
    print("3. HISTORY OUTSIDE THE WINDOW IS EXCLUDED, IN BOTH DIRECTIONS")
    print("=" * 74)
    db = _session()
    m = _machine(db)
    window = oee_contract.OeeWindow(7)
    _run(db, m.id, window.start - timedelta(hours=2), runtime=0, total=500, good=0)
    _run(db, m.id, datetime.utcnow() + timedelta(days=2), runtime=0, total=500, good=0)
    _run(db, m.id, datetime.utcnow() - timedelta(days=1))
    figures = _plant_figures(db)
    check(f"a run before the window does not drag the rollups down "
          f"({figures['/analytics/executive-oee']}%)",
          figures["/analytics/executive-oee"] == figures["/oee-summary"],
          str(figures))
    check("...and neither does a future-dated one",
          len(set(figures.values())) == 1, str(figures))
    db.close()

    print()
    print("=" * 74)
    print("3b. DOWNTIME AND QUALITY ARE WINDOWED TOO")
    print("=" * 74)
    # The rollups pool three tables, and all three have to be the same week.
    # Mutation testing earned this section: bounding production alone left the
    # downtime and quality mutations alive, because no fixture here had created
    # a DowntimeLog or a QualityInspection at all.
    db = _session()
    m = _machine(db)
    window = oee_contract.OeeWindow(7)
    db.add(models.DowntimeLog(machine_id=m.id, reason="Ancient", duration="9000 min",
                              created_at=window.start - timedelta(days=200)))
    db.add(models.DowntimeLog(machine_id=m.id, reason="Bearing", duration="30 min",
                              created_at=datetime.utcnow() - timedelta(days=1)))
    # The other end: a stoppage dated in the future must not be in this week's
    # Pareto either. Mutation testing showed the upper bound surviving without it.
    db.add(models.DowntimeLog(machine_id=m.id, reason="ClockSkew", duration="600 min",
                              created_at=datetime.utcnow() + timedelta(days=3)))
    db.commit()
    payload = analytics_routes.get_executive_oee(db=db, current_user=ADMIN)
    reasons = {row["reason"]: row for row in payload["downtime_pareto"]}
    check(f"a 200-day-old stoppage is not in the downtime Pareto "
          f"({sorted(reasons)})", "Ancient" not in reasons, str(reasons))
    check("...nor is one dated in the future", "ClockSkew" not in reasons, str(reasons))
    check("...while this week's is", "Bearing" in reasons, str(reasons))
    db.close()

    # Quality drives the no-production fallback, so a year-old inspection could
    # hand a machine that has not run this week a fabricated OEE.
    db = _session()
    m = _machine(db)
    db.add(models.QualityInspection(
        inspection_no="QI-OLD", machine_id=m.id, inspector="X",
        inspected_quantity=1000, passed_quantity=1000, failed_quantity=0,
        created_at=oee_contract.OeeWindow(7).start - timedelta(days=300)))
    db.commit()
    payload = analytics_routes.get_executive_oee(db=db, current_user=ADMIN)
    row = next(r for r in payload["machine_ranking"] if r["machine_id"] == m.id)
    check(f"a year-old inspection does not feed this week's quality "
          f"(quality {row['quality']})", row["quality"] != 100,
          f"the ranking used an inspection from outside the window: {row}")
    db.close()

    print()
    print("=" * 74)
    print("4. AN EMPTY PLANT IS STILL EMPTY, NOT WRONG")
    print("=" * 74)
    db = _session()
    _machine(db)
    figures = _plant_figures(db)
    check("every rollup returns a figure without crashing",
          all(v is not None for v in figures.values()), str(figures))
    # NOT "and they agree". `analytics_summary` deliberately falls back to a
    # utilization-derived ESTIMATE when there is no production, and publishes 68
    # where the others publish 0. That is existing, intended behaviour (#558) and
    # it is FLAGGED: every one of them returns has_data False, which is the
    # contract the frontend reads. The first version of this section asserted
    # agreement and failed on that fallback — asserting my expectation over the
    # codebase's documented one. What matters here is that an unmeasured figure
    # says it is unmeasured.
    flags = {
        "analytics_summary": analytics_routes.analytics_summary(
            db=db, current_user=ADMIN)["has_data"],
        "/analytics/management": analytics_routes.get_management_dashboard(
            db=db, current_user=ADMIN)["has_data"],
        "/analytics/executive-oee": analytics_routes.get_executive_oee(
            db=db, current_user=ADMIN)["has_data"],
    }
    check("...and every one of them says it has no data",
          not any(flags.values()), str(flags))
    db.close()

    print()
    print("=" * 74)
    print("5. THE ROLLUPS ARE STILL BOUNDED IN SQL")
    print("=" * 74)
    # The whole point of #405/#370/#372 was that these endpoints must not hydrate
    # growing tables. A date bound makes them read LESS, never more — but it has
    # to be in the query, not a Python filter after the fact.
    import os
    source = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "analytics_routes.py"), encoding="utf-8").read()
    check(f"the production rollups carry a created_at lower bound "
          f"({source.count('models.ProductionRecord.created_at >= ')} sites)",
          source.count("models.ProductionRecord.created_at >= ") >= 3,
          "a rollup is not bounded in SQL")
    check("...and an upper bound, so future-dated rows cannot leak in",
          source.count("models.ProductionRecord.created_at < ") >= 3,
          "a rollup has no upper bound")

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


def test_oee_rollups_one_window():
    """The pytest entry point — a suite exposing only main() contributes nothing
    to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
