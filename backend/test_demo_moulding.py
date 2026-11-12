"""The MERIDIAN moulding demo seeds a correct, money-ready, isolated plant.

What this pins:

  * THE SCOPE IS BUILT — 13 machines, a priced config, three role logins, and a
    plant-week of hourly production.
  * IT IS REPRODUCIBLE — same fixed `now` + the fixed DEMO_SEED gives byte-for-byte
    the same production (a demo you rehearse is the one on screen). A mutation that
    drops `random.seed` turns this red.
  * IT IS CALIBRATED on the real node's rhythm — the busiest hour of the day is in
    the evening (17-23 IST), not flat and not random. Flattening HOUR_WEIGHT fails
    this.
  * IT IS MONEY-READY — AMP's own Command Centre prices the losses in rupees off
    the tenant's configured unit value. The demo's most persuasive surface works.
  * EVERY PLANTED PROBLEM FIRES — a machine down, a quality spike, stock
    shortages, plans behind, a late order, overdue maintenance.
  * IT TOUCHES ONLY THE DEMO SCOPE — a reseed on production must not disturb a real
    tenant. A DEFAULT-tenant machine planted first survives the reseed. This is the
    property that makes it safe to run beside live customers.
  * IT REFUSES WITHOUT A PASSWORD — a public demo login with no password is a real
    account; `seed` fails fast, before any wipe.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_demo_moulding.py
"""
import os
from datetime import datetime, date, timedelta

os.environ.setdefault("DEMO_PASSWORD", "moulding-suite-password")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import database
import demo_moulding
import models
import tenancy
from database import Base

_URL = os.environ.get("DATABASE_URL", "")
engine = (create_engine(_URL) if _URL.startswith("postgresql") else
          create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool))
SessionLocal = sessionmaker(bind=engine)

T = demo_moulding.DEMO_TENANT
IST = timedelta(hours=5, minutes=30)
FIXED_NOW = datetime(2026, 11, 6, 9, 30, 0)     # pins the history window exactly

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def session(tenant=None):
    db = SessionLocal()
    return db, tenancy.set_current_tenant(tenant)


def _prod_signature(db):
    """(row count, total shots) for the demo — the reproducibility fingerprint."""
    rows = (db.query(models.ProductionRecord)
              .filter(models.ProductionRecord.tenant_code == T).all())
    return len(rows), sum(int(r.total_count or 0) for r in rows)


def main():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    database.SessionLocal = SessionLocal

    print("=" * 74)
    print("1. THE SCOPE IS BUILT")
    print("=" * 74)
    db, tok = session(None)
    demo_moulding.seed(db, now=FIXED_NOW)
    tenancy.reset_current_tenant(tok)
    db.close()

    db, tok = session(None)
    machines = db.query(models.Machine).filter(models.Machine.tenant_code == T).count()
    check("13 machines seeded", machines == 13, str(machines))
    cfg = db.query(models.TenantConfig).filter(models.TenantConfig.tenant_code == T).first()
    check("config present and priced", cfg is not None and cfg.unit_value_gbp == demo_moulding.DEMO_UNIT_VALUE,
          str(getattr(cfg, "unit_value_gbp", None)))
    roles = {u.role for u in db.query(models.User).filter(models.User.tenant_code == T).all()}
    check("three role logins exist", roles == {"Admin", "Supervisor", "Operator"}, str(roles))
    n1, shots1 = _prod_signature(db)
    check("a plant-week of hourly production", 12000 < n1 < 30000, str(n1))
    tenancy.reset_current_tenant(tok)
    db.close()

    print()
    print("=" * 74)
    print("2. IT IS REPRODUCIBLE (same now + same seed -> identical production)")
    print("=" * 74)
    db, tok = session(None)
    demo_moulding.seed(db, now=FIXED_NOW)
    n2, shots2 = _prod_signature(db)
    tenancy.reset_current_tenant(tok)
    db.close()
    check("row count identical on reseed", n1 == n2, f"{n1} vs {n2}")
    check("total shots identical on reseed", shots1 == shots2, f"{shots1} vs {shots2}")

    print()
    print("=" * 74)
    print("3. IT IS CALIBRATED — the busiest hour is in the evening")
    print("=" * 74)
    db, tok = session(None)
    by_hour = {}
    for r in db.query(models.ProductionRecord).filter(models.ProductionRecord.tenant_code == T).all():
        h = (r.created_at + IST).hour
        by_hour[h] = by_hour.get(h, 0) + int(r.total_count or 0)
    busiest = max(by_hour, key=by_hour.get)
    quietest = min(by_hour, key=by_hour.get)
    check("peak output is an evening hour (17-23 IST)", 17 <= busiest <= 23, f"peak at {busiest}:00")
    check("the trough is NOT in the evening", not (17 <= quietest <= 23), f"trough at {quietest}:00")
    tenancy.reset_current_tenant(tok)
    db.close()

    print()
    print("=" * 74)
    print("4. IT IS MONEY-READY — the Command Centre prices losses in rupees")
    print("=" * 74)
    from ai.command_centre import build_command_centre
    from currency import CURRENCY
    db, tok = session(T)
    cc = build_command_centre(db, T)
    cost = cc.get("cost", {})
    check("losses are priced", cost.get("priced") is True, str(cost))
    check("the currency is the platform currency", cost.get("currency") == CURRENCY, str(cost.get("currency")))
    check("a loss figure was computed", (cost.get("loss_cost") or 0) > 0, str(cost.get("loss_cost")))
    problems = cc.get("problems") or []
    check("the Command Centre surfaces problems", len(problems) >= 4, str(len(problems)))
    tenancy.reset_current_tenant(tok)
    db.close()

    print()
    print("=" * 74)
    print("5. EVERY PLANTED PROBLEM FIRES")
    print("=" * 74)
    db, tok = session(None)
    down = db.query(models.Machine).filter(models.Machine.tenant_code == T,
                                           models.Machine.status == "Breakdown").count()
    check("one machine is down now", down == 1, str(down))
    qi = db.query(models.QualityInspection).filter(models.QualityInspection.tenant_code == T).all()
    spike = [q for q in qi if q.inspected_quantity and q.failed_quantity / q.inspected_quantity > 0.10]
    check("a quality spike (>10% fail) exists", len(spike) >= 1, str(len(spike)))
    check("the spike is one defect on one machine",
          bool(spike) and len({q.defect_category for q in spike}) == 1
          and spike[0].defect_category == demo_moulding.PROBLEM_QUALITY_DEFECT,
          str({q.defect_category for q in spike}))
    short = db.query(models.InventoryItem).filter(
        models.InventoryItem.tenant_code == T,
        models.InventoryItem.current_stock <= models.InventoryItem.reorder_level).count()
    check("three items at or below reorder", short == 3, str(short))
    behind = db.query(models.ProductionPlan).filter(models.ProductionPlan.tenant_code == T,
                                                    models.ProductionPlan.status == "Behind").count()
    check("at least two plans behind target", behind >= 2, str(behind))
    late = db.query(models.CustomerOrder).filter(models.CustomerOrder.tenant_code == T,
                                                 models.CustomerOrder.due_date < date.today()).count()
    check("at least one order past due", late >= 1, str(late))
    overdue = db.query(models.MaintenanceTask).filter(
        models.MaintenanceTask.tenant_code == T, models.MaintenanceTask.status == "Open",
        models.MaintenanceTask.planned_date < date.today()).count()
    check("an overdue maintenance task exists", overdue >= 1, str(overdue))
    dominant = db.query(models.DowntimeLog).filter(
        models.DowntimeLog.tenant_code == T,
        models.DowntimeLog.reason == demo_moulding.PROBLEM_DOWNTIME_REASON).count()
    check("the breakdown has a dominant downtime reason", dominant >= 4, str(dominant))
    tenancy.reset_current_tenant(tok)
    db.close()

    print()
    print("=" * 74)
    print("6. IT TOUCHES ONLY THE DEMO SCOPE (a real tenant survives a reseed)")
    print("=" * 74)
    db, tok = session(None)
    db.add(models.Machine(tenant_code="DEFAULT", site="", name="REAL-CNC-01",
                          status="Running", utilization=70))
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()
    db, tok = session(None)
    demo_moulding.seed(db, now=FIXED_NOW)
    tenancy.reset_current_tenant(tok)
    db.close()
    db, tok = session(None)
    survived = db.query(models.Machine).filter(models.Machine.tenant_code == "DEFAULT",
                                               models.Machine.name == "REAL-CNC-01").count()
    check("the DEFAULT tenant's machine is untouched", survived == 1, str(survived))
    tenancy.reset_current_tenant(tok)
    db.close()

    print()
    print("=" * 74)
    print("7. IT REFUSES WITHOUT A PASSWORD (and wipes nothing when it does)")
    print("=" * 74)
    saved = os.environ.pop("DEMO_PASSWORD", None)
    db, tok = session(None)
    try:
        demo_moulding.seed(db, now=FIXED_NOW)
        refused = False
    except demo_moulding.DemoRefused:
        refused = True
    tenancy.reset_current_tenant(tok)
    db.close()
    if saved is not None:
        os.environ["DEMO_PASSWORD"] = saved
    check("seeding without DEMO_PASSWORD raises DemoRefused", refused)
    db, tok = session(None)
    still = db.query(models.Machine).filter(models.Machine.tenant_code == T).count()
    check("the refusal wiped nothing", still == 13, str(still))
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


def test_demo_moulding():
    """Pytest entry point. CI's coverage job collects module-level ``test_*``
    functions only, so without this the whole suite counts as untested and can
    push the coverage floor down (see test_demo_reset_repeatable)."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
