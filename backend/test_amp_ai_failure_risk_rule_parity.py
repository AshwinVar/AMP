"""The rule baseline adapter scores exactly what the live rule scorer scores.

WHY
---
The failure-risk model is only worth shipping if it beats the rule-based scorer
AMP already runs (``predictive_engine`` via ``ai.prediction``), on the same
rows. That comparison is only honest if the "rule" column really is the live
rule. ``amp_ai/failure_risk/baseline_rule.py`` runs the engine on a
MachineHistory; this suite seeds the same history into a database and checks
the adapter against the live path, counter for counter and score for score.

THE DIFFERENCES THAT ARE INTENDED (and asserted as such)
--------------------------------------------------------
  * ``ai.prediction._history_aggregates`` has NO upper time bound; the adapter
    counts ``[as_of - 30d, as_of)``. With no row after ``as_of`` they agree;
    one later row shows the live path counting the future.
  * status and utilisation come from the last MachineEvent before ``as_of``
    (the live path reads the Machine row, which is the same thing when the
    writers keep them in step, as the test sets them).
  * no work orders on either side (open-order pressure is not in the history).

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_failure_risk_rule_parity.py
"""
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
from ai import prediction
from amp_ai.failure_risk import baseline_rule as R
from amp_ai.failure_risk import history as H
from amp_ai.failure_risk import synthetic as S
from database import Base
from predictive_engine import calculate_predictive_risk

T = "RULEPARITY"
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def seed(db, fleet, as_of, *, extra_future=False):
    """Seed every row stamped before as_of; return {db machine id: history re-keyed to that id}."""
    histories = {}
    for mh in fleet.histories:
        state = H.status_at(mh, as_of)
        machine = models.Machine(tenant_code=T, site="", name=mh.name,
                                 status=state[0] if state else "Idle",
                                 utilization=state[1] if state else 0, downtime="0 min")
        db.add(machine)
        db.flush()
        for ts, old, new, util in mh.events:
            if ts < as_of:
                db.add(models.MachineEvent(tenant_code=T, machine_id=machine.id, machine_name=mh.name,
                                           old_status=old, new_status=new, utilization=util,
                                           source="synthetic", created_at=ts))
        for ts, reason, duration in mh.downtime:
            if ts < as_of:
                db.add(models.DowntimeLog(tenant_code=T, machine_id=machine.id, reason=reason,
                                          duration=duration, created_at=ts))
        for ts, planned, runtime, ideal, total, good, rejected in mh.production:
            if ts < as_of:
                db.add(models.ProductionRecord(tenant_code=T, machine_id=machine.id, planned_minutes=planned,
                                               runtime_minutes=runtime, ideal_cycle_time_seconds=ideal,
                                               total_count=total, good_count=good, rejected_count=rejected,
                                               created_at=ts))
        if extra_future:
            db.add(models.DowntimeLog(tenant_code=T, machine_id=machine.id, reason="Breakdown",
                                      duration="9 hrs", created_at=as_of + timedelta(hours=1)))
        histories[machine.id] = H.MachineHistory(
            machine.id, mh.name, events=mh.events, downtime=mh.downtime, production=mh.production,
            inspections=mh.inspections, maintenance=mh.maintenance)
    db.commit()
    return histories


def live_rows(db, as_of):
    token = tenancy.set_current_tenant(T)
    try:
        aggregates = prediction._history_aggregates(db, as_of - timedelta(days=prediction.RISK_WINDOW_DAYS))
        machines = db.query(models.Machine).all()
        rows = calculate_predictive_risk(machines, (), (), (), [], aggregates=aggregates)
    finally:
        tenancy.reset_current_tenant(token)
    return aggregates, {row["machine_id"]: row for row in rows}


def section_parity(fleet, day):
    as_of = S.as_of_for_day(day)
    print(f"\n{'-' * 74}\nas_of = {as_of} (day {day})")
    db = session()
    histories = seed(db, fleet, as_of)
    aggregates, live = live_rows(db, as_of)

    counter_mismatch = []
    for mid, h in histories.items():
        mine = R.rule_aggregates(h, as_of)
        for key in ("downtime_minutes", "downtime_events", "breakdown_events", "rejects", "totals"):
            if mine[key] != aggregates[key].get(mid, 0):
                counter_mismatch.append((mid, key, mine[key], aggregates[key].get(mid, 0)))
    check("per-machine counters equal ai.prediction._history_aggregates", not counter_mismatch, repr(counter_mismatch[:3]))

    score_mismatch = []
    reasons_mismatch = []
    for mid, h in histories.items():
        row = R.rule_row(h, as_of)
        if R.rule_score(h, as_of) != float(live[mid]["risk_score"]):
            score_mismatch.append((mid, R.rule_score(h, as_of), live[mid]["risk_score"]))
        if row["reasons"] != live[mid]["reasons"] or row["risk_level"] != live[mid]["risk_level"]:
            reasons_mismatch.append(mid)
    check(f"rule_score equals the live engine's risk_score for all {len(histories)} machines",
          not score_mismatch, repr(score_mismatch[:3]))
    check("reasons and risk level match too", not reasons_mismatch, repr(reasons_mismatch[:3]))
    scores = {live[m]["risk_score"] for m in live}
    check("the fixture exercises more than one score level", len(scores) >= 3, repr(sorted(scores)))
    fired = {reason for m in live for reason in live[m]["reasons"]}
    check("the fixture fires the history-driven rules (downtime, events, rejects)",
          {"high accumulated downtime", "frequent downtime events"} <= fired, repr(sorted(fired)))
    return fired


def section_intended_difference(fleet):
    print(f"\n{'-' * 74}\nThe one intended difference: the live path has no upper time bound")
    as_of = S.as_of_for_day(200)
    db = session()
    histories = seed(db, fleet, as_of, extra_future=True)
    aggregates, _ = live_rows(db, as_of)
    differs = [mid for mid, h in histories.items()
               if aggregates["downtime_events"].get(mid, 0) != R.rule_aggregates(h, as_of)["downtime_events"]]
    check("a downtime row after as_of is counted by the live path and not by the adapter",
          len(differs) == len(histories), f"{len(differs)} of {len(histories)}")


def section_contract():
    print(f"\n{'-' * 74}\nContract")
    check("RULE_WINDOW_DAYS is ai.prediction.RISK_WINDOW_DAYS", R.RULE_WINDOW_DAYS == prediction.RISK_WINDOW_DAYS)
    empty = H.MachineHistory(1, "EMPTY")
    as_of = S.as_of_for_day(200)
    row = R.rule_row(empty, as_of)
    check("an unknown status reads like the Machine column defaults (no status points, utilisation 0)",
          row["utilization"] == 0 and row["status"] is None and "low utilization below 40%" in row["reasons"])
    broken = H.MachineHistory(2, "B", events=[(as_of - timedelta(hours=1), "Running", "Breakdown", 0)])
    check("a machine in Breakdown at as_of gets the rule's +35", R.rule_row(broken, as_of)["risk_score"] >= 35)


def main():
    print("=" * 74)
    print("AMP-native AI failure risk: rule baseline adapter parity")
    print("=" * 74)
    fleet = S.generate_fleet(24, 260, seed=97)
    fired = set()
    for day in (150, 223, 259):
        fired |= section_parity(fleet, day)
    check("across the dates, the rejects and breakdown-transition rules fire as well",
          {"repeated breakdown transitions"} <= fired and ({"high reject rate", "moderate reject rate"} & fired),
          repr(sorted(fired)))
    section_intended_difference(fleet)
    section_contract()
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


def test_amp_ai_failure_risk_rule_parity():
    """pytest entry point; CI runs this file as a standalone script."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
