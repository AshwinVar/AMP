"""A non-finite per-tenant rate is no rate — it never 500s a money surface.

TenantConfig.unit_value_gbp is the one per-tenant ₹ margin, read through the
single shared helper tenancy.tenant_unit_value and multiplied into every money
figure (the management summary's estimated_loss_value, ai/cost, ai/recovery,
risk_radar). The column is a Float with NO database constraint, and the write
path (PATCH /tenant-config) used to let a non-finite value through: float()
accepts "inf"/"nan"/"Infinity"/"NaN", and the only numeric guard was `val < 0`,
which +inf and nan both pass (neither is < 0). Stored, that rate reached
loss_value.whole(units x inf) -> OverflowError (and whole(units x nan) ->
ValueError), 500-ing the cost-of-losses, recovery, scorecard and
management-summary reads for the whole tenant.

Two guards, pinned here:
  * the WRITE boundary refuses a non-finite rate (test_tenant_unit_value.py
    ::test_validation_rejects_non_finite), the same finite check
    part_spec_routes._positive keeps; and
  * the READ chokepoint, tenancy.tenant_unit_value, coalesces a non-finite
    STORED value (a raw-SQL / migration / pre-guard legacy row) to None, so a bad
    rate behaves exactly like an unset one — units everywhere, ₹ nowhere — and no
    consumer crashes.

Run:  python backend/test_unit_value_must_be_finite.py     (exit 0 = pass)
"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import analytics_engine
import models
import tenancy
from ai import cost, recovery
from database import Base

TENANT = "DEFAULT"
INF = float("inf")
NAN = float("nan")


def _db(rate):
    """A session whose TenantConfig holds `rate` directly — simulating a row
    written by raw SQL / a migration / a path that predates the write guard."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    db.add(models.TenantConfig(tenant_code=TENANT, plan="Pro", unit_value_gbp=rate))
    db.commit()
    return db


def _plant(db):
    """M1 (SMT): 40 min down, 10 scrapped. M2 (IC): 30 min down, no scrap.
    Pooled run rate = good / runtime = (160 + 90) / (160 + 90) = 1.0 unit/min,
    so downtime = 70 units, scrap = 10 units, lost = 80 good units. The same
    independently-derived fixture test_loss_money_needs_the_tenant_rate.py pins."""
    at = datetime.utcnow() - timedelta(hours=2)
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=80, line="SMT"))
    db.add(models.Machine(id=2, name="M2", status="Running", utilization=80, line="IC"))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=200, runtime_minutes=160,
                                   ideal_cycle_time_seconds=30, total_count=170, good_count=160,
                                   rejected_count=10, created_at=at))
    db.add(models.ProductionRecord(machine_id=2, planned_minutes=120, runtime_minutes=90,
                                   ideal_cycle_time_seconds=30, total_count=90, good_count=90,
                                   rejected_count=0, created_at=at))
    db.commit()


# ── The read chokepoint reads a non-finite stored rate as unset ─────────────


def test_tenant_unit_value_coalesces_non_finite_to_none():
    for bad in (INF, -INF, NAN):
        db = _db(bad)
        assert tenancy.tenant_unit_value(db, TENANT) is None, f"{bad!r} should read as unset"
    # a real finite rate (including a real 0) is returned unchanged.
    assert tenancy.tenant_unit_value(_db(3.0), TENANT) == 3.0
    assert tenancy.tenant_unit_value(_db(0), TENANT) == 0
    print("PASS tenant_unit_value reads inf/-inf/nan as unset, keeps a finite rate (0 included)")


# ── A legacy non-finite row leaves every money surface units-only, never 500 ─


def test_management_summary_survives_a_non_finite_rate():
    for bad in (INF, NAN):
        db = _db(bad)
        _plant(db)
        rate = tenancy.tenant_unit_value(db, TENANT)          # the real production path
        s = analytics_engine.build_management_summary(
            machines=[SimpleNamespace(id=1, name="M1", status="Running"),
                      SimpleNamespace(id=2, name="M2", status="Running")],
            downtime_logs=[SimpleNamespace(machine_id=1, reason="Jam", duration="70m")],
            shifts=[],
            production_records=db.query(models.ProductionRecord).all(),
            unit_value_gbp=rate)
        # The units the data DOES support are still there; only the money is None.
        assert s["estimated_loss_units"] == 70, s                 # 70 downtime min x 1.0 unit/min
        assert s["unit_value_gbp"] is None, s
        assert s["estimated_loss_value"] is None, s
    print("PASS management summary with an inf/nan rate reports 70 lost units and no ₹ (no 500)")


def test_cost_summary_survives_a_non_finite_rate():
    # ai/cost's priced path gates on `unit_value is None` and then apportions the
    # per-machine / per-line ₹ breakdown — so an inf that reached it (not None)
    # would apportion a None cost and crash. The chokepoint makes it read as unset.
    for bad in (INF, NAN):
        db = _db(bad)
        _plant(db)
        s = cost.build_cost_summary(db, TENANT)
        assert s["unit_value_gbp"] is None and s["priced"] is False, s
        assert s["downtime_minutes"] == 70 and s["rejected_units"] == 10, s
        assert s["downtime_lost_units"] == 70 and s["lost_units"] == 80, s
        for k in ("loss_cost", "downtime_cost", "scrap_cost"):
            assert s[k] is None, f"{k} = {s[k]!r} with a non-finite (so unusable) rate"
        # the breakdowns the apportion path builds exist and carry no ₹.
        assert [m["lost_units"] for m in s["by_machine"]] == [50, 30], s
        assert all(row["cost"] is None for row in s["by_machine"] + s["by_line"]), s
    print("PASS cost summary with an inf/nan rate reports 80 lost units and no ₹ (apportion safe)")


def test_recovery_summary_survives_a_non_finite_rate():
    for bad in (INF, NAN):
        db = _db(bad)
        _plant(db)
        r = recovery.build_recovery_summary(db, TENANT)
        assert r["unit_value_gbp"] is None, r
        assert r["recoverable_value_window"] is None, r
    print("PASS recovery summary with an inf/nan rate carries no ₹ (no 500)")


# ── A finite rate still prices everything, to prove the guard is a no-op there ─


def test_a_finite_rate_still_prices_the_losses():
    db = _db(3.0)
    _plant(db)
    s = cost.build_cost_summary(db, TENANT)
    # 70 downtime units + 10 scrap units, each x ₹3 -> ₹210 + ₹30 = ₹240.
    assert s["unit_value_gbp"] == 3.0 and s["priced"] is True, s
    assert s["downtime_cost"] == 210 and s["scrap_cost"] == 30 and s["loss_cost"] == 240, s
    print("PASS a finite ₹3 rate still prices the losses at ₹240 (guard is a no-op)")


if __name__ == "__main__":
    test_tenant_unit_value_coalesces_non_finite_to_none()
    test_management_summary_survives_a_non_finite_rate()
    test_cost_summary_survives_a_non_finite_rate()
    test_recovery_summary_survives_a_non_finite_rate()
    test_a_finite_rate_still_prices_the_losses()
    print("ALL NON-FINITE UNIT-VALUE TESTS PASSED")
