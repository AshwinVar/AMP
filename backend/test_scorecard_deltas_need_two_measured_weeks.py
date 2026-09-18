"""A week-on-week change needs two measured weeks.

THE DEFECT
----------
The scorecard's KPIs publish None, not a fabricated zero, when their pillar
measured nothing (#585, test_scorecard_no_data.py). The CHANGE beside each value
did not follow the same rule:

* It was computed from the raw figure under the None. A week whose only
  production row recorded nothing published OEE "—" and, in the same tile,
  "▼72 pts" in red: not measured, and fell 72 points. Good rate read "—" and
  "▼90". The same with no rows at all.
* The prior week counted ROWS, not measurements (`has: bool(records)`), the
  count-the-rows rule oee_contract.is_measurable replaced everywhere else
  (test_one_has_data_rule.py). A last week whose only row recorded nothing
  became a 0% OEE and a 0% good rate, so an ordinary week read "▲72 pts" and
  "▲90 pts" in green.
* Cost of losses was "measured" whenever a row existed: a week whose only row
  recorded nothing published losses of 0 in GREEN beside a green "▼33", the
  "it eliminated all its losses" reading #585 removed for a week with no rows.

THE RULE
--------
A KPI's value is measured by its pillar's rule (OEE and losses: the window's
production is measurable; good rate: units were inspected); the prior week is
judged by the same rule; and a change is published only when BOTH weeks were
measured. Stated over the whole payload in section 5: no KPI publishes a change
without a value.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_scorecard_deltas_need_two_measured_weeks.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
from ai.scorecard import build_scorecard
from database import Base

WINDOWED = ("oee", "good_rate", "loss_cost")     # the three KPIs with a weekly change
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
    db = sessionmaker(bind=engine)()
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=80, line="L1"))
    db.commit()
    return db


def _ran(db, days_ago, good=90):
    # 100 planned, 80 run, 100 made at a 48 s ideal cycle: A 80%, P 100%, and
    # Q = good%. 20 downtime minutes at 90/80 a minute = 22 units + 10 scrap.
    db.add(models.ProductionRecord(
        machine_id=1, planned_minutes=100, runtime_minutes=80, ideal_cycle_time_seconds=48,
        total_count=100, good_count=good, rejected_count=100 - good,
        created_at=datetime.utcnow() - timedelta(days=days_ago)))
    db.commit()


def _recorded_nothing(db, days_ago):
    # A row with nothing in it: no minutes planned, nothing made.
    db.add(models.ProductionRecord(
        machine_id=1, planned_minutes=0, runtime_minutes=0, ideal_cycle_time_seconds=48,
        total_count=0, good_count=0, rejected_count=0,
        created_at=datetime.utcnow() - timedelta(days=days_ago)))
    db.commit()


def _kpis(db):
    return {k["key"]: k for k in build_scorecard(db, "DEFAULT")["kpis"]}


def _show(k):
    return f"value={k['value']!r} tone={k['tone']!r} delta={k['delta']!r} delta_tone={k['delta_tone']!r}"


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def check_no_change(label, k, key):
    check(f"{label}: {key} publishes no change", k[key]["delta"] is None
          and k[key]["delta_tone"] is None, _show(k[key]))


def main():
    section("1. THIS WEEK RECORDED NOTHING; LAST WEEK RAN")
    db = _session()
    _recorded_nothing(db, days_ago=2)
    _ran(db, days_ago=9)
    k = _kpis(db)
    for key in WINDOWED:
        check_no_change("empty row this week", k, key)
    check("empty row this week: cost of losses is not measured (no green 0)",
          k["loss_cost"]["value"] is None and k["loss_cost"]["tone"] == "none", _show(k["loss_cost"]))
    db.close()

    section("2. THIS WEEK HAS NO ROWS; LAST WEEK RAN")
    db = _session()
    _ran(db, days_ago=9)
    k = _kpis(db)
    for key in WINDOWED:
        check_no_change("no rows this week", k, key)
    db.close()

    section("3. LAST WEEK RECORDED NOTHING; THIS WEEK RAN")
    db = _session()
    _ran(db, days_ago=2)
    _recorded_nothing(db, days_ago=9)
    k = _kpis(db)
    for key in WINDOWED:
        check_no_change("empty row last week", k, key)
    check("empty row last week: this week's values are still published",
          all(k[key]["value"] is not None for key in WINDOWED),
          {key: k[key]["value"] for key in WINDOWED})
    db.close()

    section("4. CONTROLS: TWO MEASURED WEEKS STILL PUBLISH THEIR CHANGE")
    db = _session()
    _ran(db, days_ago=2, good=90)
    _ran(db, days_ago=9, good=45)
    k = _kpis(db)
    # OEE 80 x 100 x 90 = 72 against 80 x 100 x 45 = 36; good rate 90 against 45.
    check("two measured weeks: OEE +36, improving", (k["oee"]["delta"], k["oee"]["delta_tone"])
          == (36, "good"), _show(k["oee"]))
    check("two measured weeks: good rate +45, improving",
          (k["good_rate"]["delta"], k["good_rate"]["delta_tone"]) == (45, "good"), _show(k["good_rate"]))
    check("two measured weeks: losses fell, and that is good",
          k["loss_cost"]["delta"] is not None and k["loss_cost"]["delta"] < 0
          and k["loss_cost"]["delta_tone"] == "good", _show(k["loss_cost"]))
    db.close()

    db = _session()
    # A measured week with NO losses (ran exactly as planned, nothing scrapped)
    # against a lossy one: losses of 0 are a measured fact, green, and the fall
    # is published.
    db.add(models.ProductionRecord(
        machine_id=1, planned_minutes=100, runtime_minutes=100, ideal_cycle_time_seconds=48,
        total_count=125, good_count=125, rejected_count=0,
        created_at=datetime.utcnow() - timedelta(days=2)))
    db.commit()
    _ran(db, days_ago=9)
    k = _kpis(db)
    check("a measured zero-loss week: losses 0, green",
          (k["loss_cost"]["value"], k["loss_cost"]["tone"]) == (0, "good"), _show(k["loss_cost"]))
    check("...and its fall against a lossy week is published",
          k["loss_cost"]["delta"] is not None and k["loss_cost"]["delta"] < 0, _show(k["loss_cost"]))
    db.close()

    section("5. NO KPI PUBLISHES A CHANGE WITHOUT A VALUE, OVER EVERY FIXTURE ABOVE")
    fixtures = {
        "empty row this week": lambda db: (_recorded_nothing(db, 2), _ran(db, 9)),
        "no rows this week": lambda db: _ran(db, 9),
        "empty row last week": lambda db: (_ran(db, 2), _recorded_nothing(db, 9)),
        "empty rows both weeks": lambda db: (_recorded_nothing(db, 2), _recorded_nothing(db, 9)),
    }
    for name, seed in fixtures.items():
        db = _session()
        seed(db)
        bad = [key for key, kpi in _kpis(db).items()
               if kpi["value"] is None and (kpi["delta"] is not None or kpi["delta_tone"] is not None)]
        check(f"{name}: every KPI without a value has no change", not bad, str(bad))
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


def test_scorecard_deltas_need_two_measured_weeks():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
