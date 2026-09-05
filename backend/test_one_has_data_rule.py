"""A week the plant did not run is not a week the plant ran badly.

THE DEFECT
----------
`oee_contract` states the rule and says why:

    has_data is True only when the window contains something measurable:
    planned minutes > 0 OR total count > 0. It previously meant "at least one
    row exists", so an empty shift rendered as a measured 0% OEE.

    Reporting 0% for an unscheduled weekend is a fabricated loss.

Every caller OUTSIDE that module used "at least one row exists":

    analytics_routes.py   record_count > 0              /analytics/summary
    analytics_routes.py   bool(production_by_machine)   /analytics/executive-oee
    analytics_engine.py   len(records) > 0              pooled_oee -> twin/cost/losses/recovery
    analytics_engine.py   record_count > 0              build_management_summary

So the same records answered two ways:

    case                              oee_contract        analytics_engine
    a row that recorded nothing       unmeasured          OEE 0%
    unplanned only (shutdown week)    availability None   OEE 0%
    a normal shift                    58%                 58%   (agree)

This is the second half of the OEE split-brain. #556 closed the WINDOW half —
the two surfaces selected different ROWS and answered 21 points apart. This one
is narrower and more embarrassing: the same rows, and one surface invents a
loss the data does not contain.

WHAT THIS CHANGES, AND WHAT IT DOES NOT
----------------------------------------
`has_data` now comes from `oee_contract.is_measurable` at all four sites. The
RETURN TYPE is untouched — these functions still hand back integers, not the
contract's None-for-undefined. That was the deliberate choice recorded in the
handover: the narrow fix is where the fabricated 0% actually reaches a customer,
and migrating every consumer to None is an interface change AND a product
question about whether a screen shows "—" or "0%".

Section 4 is a reference oracle: any window with real production is unchanged,
so this only moves windows that were never measurable.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_one_has_data_rule.py
"""
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import analytics_engine
import analytics_routes
import models
import oee_contract
import tenancy
from database import Base

T = "HASDATA"
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


class R:
    """A production record, as the pooling functions consume one."""
    def __init__(self, planned, runtime, ideal, total, good, rejected=0):
        self.planned_minutes, self.runtime_minutes = planned, runtime
        self.ideal_cycle_time_seconds, self.total_count = ideal, total
        self.good_count, self.rejected_count = good, rejected


def seed(records):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    db = sessionmaker(bind=engine)()
    tok = tenancy.set_current_tenant(None)
    db.add(models.TenantConfig(tenant_code=T))
    m = models.Machine(tenant_code=T, name="LINE-1", site="S", status="Running",
                       utilization=80, downtime="0 min")
    db.add(m)
    db.flush()
    now = datetime.utcnow()
    for r in records:
        db.add(models.ProductionRecord(
            tenant_code=T, machine_id=m.id, planned_minutes=r.planned_minutes,
            runtime_minutes=r.runtime_minutes,
            ideal_cycle_time_seconds=r.ideal_cycle_time_seconds,
            total_count=r.total_count, good_count=r.good_count,
            rejected_count=r.rejected_count, created_at=now))
    db.commit()
    tenancy.reset_current_tenant(tok)
    return db


EMPTY_ROW = [R(0, 0, 0, 0, 0)]            # a row that recorded nothing
UNPLANNED = [R(0, 120, 30, 200, 190)]     # ran, but nothing was scheduled
NORMAL = [R(480, 400, 30, 600, 560, 40)]  # an ordinary shift


def main():
    print("=" * 74)
    print("1. THE RULE HAS ONE HOME")
    print("=" * 74)
    check("a row that recorded nothing is NOT measurable",
          oee_contract.is_measurable(0, 0) is False)
    check("scheduled time alone makes it measurable",
          oee_contract.is_measurable(480, 0) is True)
    check("units made alone make it measurable — an unplanned run IS data",
          oee_contract.is_measurable(0, 200) is True)
    check("None sums are treated as zero, not as a crash",
          oee_contract.is_measurable(None, None) is False)

    print()
    print("=" * 74)
    print("2. A WINDOW THAT RECORDED NOTHING NO LONGER REPORTS 0%")
    print("=" * 74)
    # The defect. `pooled_oee` feeds the twin, cost, losses and recovery
    # read-models, so this one call site reaches most of the product.
    o = analytics_engine.pooled_oee(EMPTY_ROW)
    check("pooled_oee: has_data is False", o["has_data"] is False, str(o["has_data"]))
    c = oee_contract.oee_from_sums(0, 0, 0, 0, 0)
    check("...agreeing with the contract on the same sums",
          o["has_data"] == c["has_data"], f"engine={o['has_data']} contract={c['has_data']}")
    check("no rows at all is still no data",
          analytics_engine.pooled_oee([])["has_data"] is False)

    print()
    print("=" * 74)
    print("3. EVERY SURFACE THAT PUBLISHES A PLANT OEE AGREES")
    print("=" * 74)
    # All four call sites, through their real endpoints, on the same factory.
    db = seed(EMPTY_ROW)
    tok = tenancy.set_current_tenant(T)
    summ = analytics_routes.analytics_summary(db, {"tenant": T})
    check("/analytics/summary publishes has_data=False",
          summ["has_data"] is False, str(summ.get("has_data")))
    check("...for a window that HAS a row — the flag, not the row count, is what"
          " distinguishes it",
          db.query(models.ProductionRecord).count() == 1,
          str(db.query(models.ProductionRecord).count()))
    exec_oee = analytics_routes.get_executive_oee(db, {"tenant": T})
    check("/analytics/executive-oee publishes has_data=False",
          exec_oee["has_data"] is False, str(exec_oee.get("has_data")))
    mgmt = analytics_engine.build_management_summary(
        db.query(models.Machine).all(), [], [], list(db.query(models.ProductionRecord)))
    check("build_management_summary publishes has_data=False",
          mgmt["has_data"] is False, str(mgmt.get("has_data")))
    check("...while avg_oee is still 0 — WHICH IS THE POINT: the number"+
          " alone cannot tell the two apart",
          mgmt["avg_oee"] == 0, str(mgmt["avg_oee"]))
    # build_management_summary has TWO paths and they must not disagree. The
    # call above takes the row LIST; callers that aggregate in SQL hand in
    # `production_sums` instead and reach a different has_data expression.
    # Without this, a mutation reverting that second branch survived — the
    # fixture simply never went down it.
    mgmt_sql = analytics_engine.build_management_summary(
        db.query(models.Machine).all(), [], [], [],
        production_sums=(0, 0, 0, 0, 0, 1))     # one row, nothing measurable
    check("...and the SQL-aggregated path says the same",
          mgmt_sql["has_data"] is False, str(mgmt_sql.get("has_data")))
    # The property that actually matters: the contract and the engine cannot
    # disagree about whether this factory produced anything.
    contract = oee_contract.plant_oee(db, T)
    engine = analytics_engine.pooled_oee(list(db.query(models.ProductionRecord)))
    check("the contract and the engine agree there is nothing to measure",
          contract["has_data"] == engine["has_data"] is False,
          f"contract={contract['has_data']} engine={engine['has_data']}")
    tenancy.reset_current_tenant(tok)
    db.close()

    print()
    print("=" * 74)
    print("4. REFERENCE ORACLE — REAL PRODUCTION IS UNCHANGED")
    print("=" * 74)
    # This must move ONLY windows that were never measurable. A shift with real
    # numbers has to report exactly what it reported before.
    normal = analytics_engine.pooled_oee(NORMAL)
    check("a normal shift still has data", normal["has_data"] is True)
    # A=400/480=.833  P=(30*600)/(400*60)=.75  Q=560/600=.933 -> 58%
    check("...and still reports 58%", normal["oee"] == 58, str(normal["oee"]))
    unplanned = analytics_engine.pooled_oee(UNPLANNED)
    check("an unplanned run still counts as data — units WERE made",
          unplanned["has_data"] is True, str(unplanned["has_data"]))
    db = seed(NORMAL)
    tok = tenancy.set_current_tenant(T)
    summ = analytics_routes.analytics_summary(db, {"tenant": T})
    check("/analytics/summary reports the same 58%", summ["avg_oee"] == 58,
          str(summ["avg_oee"]))
    check("...and the contract agrees",
          oee_contract.as_percentages(oee_contract.plant_oee(db, T))["oee"] == 58,
          str(oee_contract.as_percentages(oee_contract.plant_oee(db, T))["oee"]))
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


if __name__ == "__main__":
    raise SystemExit(main())
