"""Two definitions of "the last 7 days", and a coverage figure that described
neither.

THE DEFECT
----------
`oee_contract` opens by saying what it is for:

    "the problem was that each surface chose its own RECORD SET and its own idea
     of what 'no data' means, so the same factory answered differently depending
     on where you asked."

and states the rule: `window = [now - days, now)`, start inclusive, end
exclusive. But `ai/twin._recent_production` — which feeds ai/oee, ai/cost,
ai/losses and ai/recovery — had its own:

    cutoff = midnight(today - (days - 1))
    filter(created_at >= cutoff)              # and NO upper bound at all

Two windows, both called "the last 7 days":

    record                        oee_contract    twin
    6.9 days old                       IN          out     (twin starts later)
    1 hour in the future               out          IN     (twin has no end)

Measured on one plant at one moment, six good days and one bad shift seven days
back — the day the two windows disagree about:

                              OEE    A    P    Q   records
    oee_contract (canonical)   66   71   98   96        71
    twin / cost / losses       87   89  100   98        52

21 OEE points, from the same rows, at the same instant. That is the same table
`oee_contract`'s own docstring prints as the reason it exists.

THE PART THAT CANNOT BE ARGUED WITH
------------------------------------
`ai/oee.build_oee_summary` uses BOTH — the plant OEE from the twin window and
`coverage` from `oee_contract.coverage(...)`. Coverage exists to say "this
figure was measured from N of M machines", so it was describing a different
record set than the number it qualifies. One machine, one record, one response:

    a record 6.9 days old
        plant OEE : has_data=False, oee=0
        coverage  : 1 of 1 machines reporting, complete=True
        -> "we measured the whole plant, and the answer is nothing"

    a record 1 hour in the future (gateway clock skew)
        plant OEE : has_data=True, oee=85
        coverage  : 0 of 1 machines reporting, complete=False
        -> a plant OEE measured from ZERO machines, which is precisely the
           survivorship failure coverage was built to prevent

THE FIX
-------
`_recent_production` uses `oee_contract.OeeWindow`. Not a new rule — the
existing one, applied where a second copy had grown. Section 4 is a reference
oracle: a factory whose records sit strictly inside both windows reports exactly
the same numbers as before, so this moves the BOUNDARY and nothing else.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_one_production_window.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import oee_contract
import tenancy
from ai.oee import build_oee_summary
from ai.twin import _oee_from_records, _recent_production
from database import Base

T = "ONEWINDOW"
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def seed(offsets):
    """One machine, one production record per offset from now."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    db = sessionmaker(bind=engine)()
    tok = tenancy.set_current_tenant(None)
    db.add(models.TenantConfig(tenant_code=T))
    m = models.Machine(tenant_code=T, name="ONLY-MACHINE", site="S",
                       status="Running", utilization=80, downtime="0 min")
    db.add(m)
    db.flush()
    now = datetime.utcnow()
    for off in offsets:
        db.add(models.ProductionRecord(
            tenant_code=T, machine_id=m.id, planned_minutes=100, runtime_minutes=90,
            ideal_cycle_time_seconds=30, total_count=180, good_count=170,
            rejected_count=10, created_at=now + off))
    db.commit()
    tenancy.reset_current_tenant(tok)
    return db


def contract_ids(db):
    w = oee_contract.OeeWindow(7)
    return {r.id for r in db.query(models.ProductionRecord).filter(
        models.ProductionRecord.created_at >= w.start,
        models.ProductionRecord.created_at < w.end).all()}


def main():
    print("=" * 74)
    print("1. ONE WINDOW — the two selections are identical")
    print("=" * 74)
    # The whole defect in one assertion. Offsets chosen to straddle BOTH
    # boundaries the two windows used to disagree about.
    OFFSETS = [
        ("6.9 days old — inside the rolling window, before twin's midnight start",
         -timedelta(days=6, hours=22)),
        ("7.5 days old — outside both", -timedelta(days=7, hours=12)),
        ("3 days old — comfortably inside both", -timedelta(days=3)),
        ("1 hour old", -timedelta(hours=1)),
        ("1 hour in the FUTURE — gateway clock skew", timedelta(hours=1)),
    ]
    db = seed([off for _label, off in OFFSETS])
    tok = tenancy.set_current_tenant(T)
    twin = {r.id for r in _recent_production(db, days=7)}
    canon = contract_ids(db)
    check(f"both windows select the same {len(canon)} record(s)", twin == canon,
          f"twin-only={sorted(twin - canon)} contract-only={sorted(canon - twin)}")
    rows = db.query(models.ProductionRecord).order_by(models.ProductionRecord.id).all()
    for (label, _off), r in zip(OFFSETS, rows):
        inside = r.id in canon
        low = label.lower()
        expected = "future" not in low and "outside both" not in low
        check(f"{label[:56]:58} -> {'IN' if inside else 'out'}",
              inside == expected, f"expected {'IN' if expected else 'out'}")

    print()
    print("=" * 74)
    print("2. COVERAGE NOW DESCRIBES THE FIGURE IT QUALIFIES")
    print("=" * 74)
    # The half that cannot be argued with: these two numbers live in the SAME
    # response, and one exists to qualify the other.
    for label, off in (("a record 6.9 days old", -timedelta(days=6, hours=22)),
                       ("a record 1 hour in the FUTURE", timedelta(hours=1))):
        one = seed([off])
        t2 = tenancy.set_current_tenant(T)
        s = build_oee_summary(one, T)
        cov, plant = s["coverage"], s["plant"]
        check(f"{label}: reporting>0 iff the OEE has data",
              (cov["machines_reporting"] > 0) == plant["has_data"],
              f"reporting={cov['machines_reporting']} has_data={plant['has_data']}")
        tenancy.reset_current_tenant(t2)
        one.close()

    print()
    print("=" * 74)
    print("3. A FUTURE-DATED ROW IS EXCLUDED EVERYWHERE")
    print("=" * 74)
    # #550 settled this for the contract; the twin window had no upper bound at
    # all, so a clock-skewed gateway could inflate every read-model downstream
    # of it. One rule now, in one place.
    fut = seed([timedelta(hours=1)])
    t3 = tenancy.set_current_tenant(T)
    check("the twin window excludes it too",
          _recent_production(fut, days=7) == [], str(len(_recent_production(fut, days=7))))
    check("...so the plant reports no data rather than a future number",
          _oee_from_records(_recent_production(fut, days=7))["has_data"] is False)
    tenancy.reset_current_tenant(t3)
    fut.close()

    print()
    print("=" * 74)
    print("4. REFERENCE ORACLE — an ordinary factory is UNCHANGED")
    print("=" * 74)
    # This moves a BOUNDARY. A plant whose records sit strictly inside both
    # windows must report exactly what it reported before, or the change is not
    # a boundary fix, it is a different number.
    inside = seed([-timedelta(days=d, hours=h) for d in range(1, 5) for h in (0, 6, 12, 18)])
    t4 = tenancy.set_current_tenant(T)
    recs = _recent_production(inside, days=7)
    check("every seeded record is selected", len(recs) == 16, str(len(recs)))
    o = _oee_from_records(recs)
    c = oee_contract.as_percentages(oee_contract.plant_oee(inside, T))
    # A=90/100, P=(30*180)/(90*60)=1.0 capped, Q=170/180 -> .9*1*.944 = 85%
    check("the twin path still reports 85%", o["oee"] == 85, str(o["oee"]))
    check("...and the canonical path agrees with it", c["oee"] == o["oee"],
          f"contract={c['oee']} twin={o['oee']}")
    cov = oee_contract.coverage(inside, T, oee_contract.OeeWindow(7))
    check("...with complete coverage", cov["complete"] is True, str(cov))
    tenancy.reset_current_tenant(t4)
    inside.close()

    print()
    print("=" * 74)
    print("5. THE START IS INCLUSIVE HERE TOO, NOT JUST IN THE CONTRACT")
    print("=" * 74)
    # A mutation making this window's start EXCLUSIVE survived the sections
    # above, because none of their records lands exactly on a boundary that
    # moves with the clock. `[start, end)` is the contract's rule and the reason
    # adjacent windows tile; a copy that agreed on the DATES but not on the
    # boundary would be the same defect in a subtler form.
    #
    # Pinning the clock is what makes this testable at all — the seam added in
    # #550 for exactly this reason. Racing the boundary instead would be a test
    # that almost never exercises it.
    real_now = oee_contract._now
    frozen = datetime(2026, 9, 5, 11, 22, 33, 444555)
    oee_contract._now = lambda: frozen
    try:
        boundary = oee_contract.OeeWindow(7).start
        edge = seed([])                       # machine only; records added below
        t5 = tenancy.set_current_tenant(T)
        mid = edge.query(models.Machine).first().id
        edge.add(models.ProductionRecord(
            tenant_code=T, machine_id=mid, planned_minutes=100, runtime_minutes=90,
            ideal_cycle_time_seconds=30, total_count=180, good_count=170,
            rejected_count=10, created_at=boundary))
        edge.commit()
        picked = _recent_production(edge, days=7)
        check("a record exactly ON window.start is INCLUDED", len(picked) == 1,
              f"{len(picked)} record(s); boundary={boundary}")
        check("...so the twin agrees with the contract at the boundary",
              {r.id for r in picked} == contract_ids(edge),
              f"twin={[r.id for r in picked]} contract={sorted(contract_ids(edge))}")
        tenancy.reset_current_tenant(t5)
        edge.close()
    finally:
        oee_contract._now = real_now

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
