"""No cost-of-losses £ exists without the tenant's own rate (ADR-0010).

ADR-0010's decision: one per-tenant margin per good unit (TenantConfig.
unit_value_gbp) drives every money figure, and "unset means units-only, never a
fabricated £". The dashboard's own money panel says so to the customer: "one rate
drives the whole dashboard, and stays units-only until you set it."

The cost-of-losses family never followed it. ai/cost.py priced every tenant's
downtime at a fixed £12 a minute and every scrapped unit at a fixed £25:

  * GET /cost-summary and /cost-trend, the Cost of losses cards and Trends card;
  * the scorecard's "Cost of losses" KPI and its week-on-week change;
  * the weekly report, the assistant's answers and digest, the copilot context;
  * the digital twin's cost heat map;

and build_management_summary valued downtime at £8 a minute whenever no rate was
set. None of these numbers came from the customer. A plant with a £2 margin
and one with a £400 margin were shown the same £.

THE RULE THESE TESTS PIN
  * A loss is measured in GOOD UNITS NOT MADE: a scrapped unit is one; downtime
    minutes are converted at the window's observed run rate (good units per minute
    of run time), the conversion build_management_summary already used.
  * Money = those units x the tenant's rate. No rate -> every £ is None and the
    surfaces speak in units. A rate of 0 is a real £0.
  * Downtime with no run time in the window has no run rate to convert it, so its
    units (and £) are None, not 0.
  * Every headline still equals the sum of its breakdowns, units and £ alike.

Run:  python backend/test_loss_money_needs_the_tenant_rate.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import analytics_engine
import models
from ai import assistant, cost, report, scorecard, twin
from currency import CURRENCY
from database import Base

TENANT = "DEFAULT"


def _db(rate=None):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    if rate is not None:
        db.add(models.TenantConfig(tenant_code=TENANT, plan="Pro", unit_value_gbp=rate))
    db.commit()
    return db


def _plant(db, hours_ago=2):
    """M1 (SMT): 40 min down, 10 scrapped. M2 (IC): 30 min down, no scrap.
    Pooled run rate = good / runtime = (160 + 90) / (160 + 90) = 1.0 unit/min,
    so downtime = 70 units, scrap = 10 units, lost = 80 good units."""
    at = datetime.utcnow() - timedelta(hours=hours_ago)
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=80, line="SMT"))
    db.add(models.Machine(id=2, name="M2", status="Running", utilization=80, line="IC"))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=200, runtime_minutes=160,
                                   ideal_cycle_time_seconds=30, total_count=170, good_count=160,
                                   rejected_count=10, created_at=at))
    db.add(models.ProductionRecord(machine_id=2, planned_minutes=120, runtime_minutes=90,
                                   ideal_cycle_time_seconds=30, total_count=90, good_count=90,
                                   rejected_count=0, created_at=at))
    db.commit()


def _no_pound(text):
    assert CURRENCY not in text, f"a £ figure with no rate set: {text!r}"


# ── Unset: units only, never a fabricated £ ─────────────────────────────────


def test_cost_summary_without_a_rate_reports_units_and_no_money():
    db = _db()
    _plant(db)
    s = cost.build_cost_summary(db, TENANT)
    assert s["priced"] is False and s["unit_value_gbp"] is None
    assert s["downtime_minutes"] == 70 and s["rejected_units"] == 10
    assert s["downtime_lost_units"] == 70 and s["lost_units"] == 80, s
    for k in ("loss_cost", "downtime_cost", "scrap_cost"):
        assert s[k] is None, f"{k} = {s[k]!r} with no rate set"
    assert all(row["cost"] is None for row in s["by_machine"] + s["by_line"] + s["daily"])
    assert all(v is None for v in s["machine_cost"].values())
    assert all(l["cost"] is None for l in s["losses"])
    for l in s["losses"]:
        _no_pound(l["detail"])
    assert s["biggest"] == "downtime"                    # 70 units > 10 units
    assert [m["lost_units"] for m in s["by_machine"]] == [50, 30]
    print("PASS no rate: the cost summary reports 80 good units lost and no £ at all")


def test_trend_scorecard_report_assistant_twin_speak_units_without_a_rate():
    db = _db()
    _plant(db)
    t = cost.build_cost_trend(db, TENANT)
    assert t["current"]["cost"] is None and t["delta_cost"] is None
    assert t["current"]["lost_units"] == 80
    _no_pound(t["verdict"])

    kpi = {k["key"]: k for k in scorecard.build_scorecard(db, TENANT)["kpis"]}["loss_cost"]
    assert kpi["unit"] != CURRENCY and kpi["value"] == 80, kpi
    assert "unit" in kpi["label"].lower() or "unit" in kpi["unit"], kpi

    rep = report.build_weekly_report(db, TENANT)["markdown"]
    section = rep.split("## Cost of losses", 1)[1].split("##", 1)[0]
    _no_pound(section)
    assert "80" in section, section

    answer, _view = assistant._cost(db, TENANT)
    _no_pound(answer)
    assert "80" in answer, answer
    digest = assistant.digest(db, TENANT)["digest"]
    _no_pound(digest)
    assert "80 good units" in digest, digest

    overlay = {m["machine_id"]: m for m in twin.build_twin_overlay(db, TENANT)["machines"]}
    assert overlay[1]["cost"] is None and overlay[1]["lost_units"] == 50, overlay
    print("PASS no rate: trend, scorecard, weekly report, assistant and twin speak units, never £")


def test_management_summary_has_no_per_minute_proxy():
    machines = [SimpleNamespace(id=1, name="M1", status="Running")]
    downtime = [SimpleNamespace(machine_id=1, reason="Jam", duration="40m")]
    rec = [SimpleNamespace(machine_id=1, planned_minutes=200, runtime_minutes=160, total_count=170,
                           good_count=160, rejected_count=10, ideal_cycle_time_seconds=30)]
    s = analytics_engine.build_management_summary(machines, downtime, [], rec)
    assert s["total_downtime_minutes"] == 40 and s["estimated_loss_units"] == 40, s
    assert s["estimated_loss_value"] is None, f"£{s['estimated_loss_value']} with no rate (the old £8/min proxy)"
    priced = analytics_engine.build_management_summary(machines, downtime, [], rec, unit_value_gbp=3)
    assert priced["estimated_loss_value"] == 120, priced
    stopped = [SimpleNamespace(machine_id=1, planned_minutes=200, runtime_minutes=0, total_count=0,
                               good_count=0, rejected_count=0, ideal_cycle_time_seconds=30)]
    unknown = analytics_engine.build_management_summary(machines, downtime, [], stopped, unit_value_gbp=3)
    assert unknown["estimated_loss_units"] is None and unknown["estimated_loss_value"] is None, unknown
    print("PASS the management summary no longer invents £8 a minute, and no run time means unknown")


# ── Set: £ = units x the tenant's rate, reconciled ───────────────────────────


def test_money_is_units_times_the_tenants_rate():
    db = _db(rate=2.5)
    _plant(db)
    s = cost.build_cost_summary(db, TENANT)
    assert s["priced"] is True and s["unit_value_gbp"] == 2.5
    assert (s["downtime_cost"], s["scrap_cost"], s["loss_cost"]) == (175, 25, 200), s
    assert s["loss_cost"] == sum(m["cost"] for m in s["by_machine"])
    assert s["loss_cost"] == sum(r["cost"] for r in s["by_line"])
    assert s["loss_cost"] == sum(d["cost"] for d in s["daily"])
    assert s["loss_cost"] == sum(s["machine_cost"].values())
    kpi = {k["key"]: k for k in scorecard.build_scorecard(db, TENANT)["kpis"]}["loss_cost"]
    assert kpi["unit"] == CURRENCY and kpi["value"] == 200, kpi
    assert cost.build_cost_trend(db, TENANT)["current"]["cost"] == 200
    print("PASS rate £2.50: 80 lost units -> £200, and every breakdown sums to it")


def test_a_rate_of_zero_is_a_real_zero():
    db = _db(rate=0)
    _plant(db)
    s = cost.build_cost_summary(db, TENANT)
    assert s["priced"] is True and s["loss_cost"] == 0 and s["lost_units"] == 80
    print("PASS rate £0: £0 lost, still 80 units")


def test_fractional_units_still_reconcile_exactly():
    """Run rate 7/3 units a minute makes every machine's downtime units fractional."""
    db = _db(rate=1.37)
    at = datetime.utcnow() - timedelta(hours=3)
    for mid, (down, line) in enumerate([(7, "SMT"), (11, "SMT"), (13, "IC")], start=1):
        db.add(models.Machine(id=mid, name=f"M{mid}", status="Running", utilization=80, line=line))
        db.add(models.ProductionRecord(machine_id=mid, planned_minutes=30 + down, runtime_minutes=30,
                                       ideal_cycle_time_seconds=30, total_count=71, good_count=70,
                                       rejected_count=1, created_at=at))
    db.commit()
    s = cost.build_cost_summary(db, TENANT)
    assert s["lost_units"] == sum(m["lost_units"] for m in s["by_machine"])
    assert s["lost_units"] == sum(r["lost_units"] for r in s["by_line"])
    assert s["lost_units"] == sum(d["lost_units"] for d in s["daily"])
    assert s["loss_cost"] == s["downtime_cost"] + s["scrap_cost"]
    assert s["loss_cost"] == sum(m["cost"] for m in s["by_machine"])
    assert s["loss_cost"] == sum(r["cost"] for r in s["by_line"])
    assert s["loss_cost"] == sum(d["cost"] for d in s["daily"])
    # 31 min x 7/3 = 72.33 -> 72 downtime units, + 3 scrap = 75 units
    assert (s["downtime_lost_units"], s["lost_units"]) == (72, 75), s
    assert abs(s["loss_cost"] - s["lost_units"] * 1.37) <= 1, s
    # Per machine: 16.33 / 25.67 / 30.33 downtime units floor to 16 / 25 / 30 (71). The
    # one unit still owed goes to the LARGEST remainder, M2's .67, so 16 / 26 / 30,
    # plus one scrapped unit each.
    assert {m["machine_id"]: m["lost_units"] for m in s["by_machine"]} == {1: 17, 2: 27, 3: 31}, s["by_machine"]
    print("PASS fractional run rates: units and £ headlines equal the sum of every breakdown")


def test_rounding_each_row_on_its_own_would_miss_the_headline():
    """Three machines each lose 1.5 units: the headline is 4.5 -> 5, but rounding each
    row on its own gives 2 + 2 + 2 = 6, a card whose rows add up to more than it says."""
    db = _db(rate=1)
    at = datetime.utcnow() - timedelta(hours=3)
    for mid in (1, 2, 3):
        db.add(models.Machine(id=mid, name=f"M{mid}", status="Running", utilization=80, line="SMT"))
        # half a good unit per run minute; 3 min down -> 1.5 units
        db.add(models.ProductionRecord(machine_id=mid, planned_minutes=103, runtime_minutes=100,
                                       ideal_cycle_time_seconds=30, total_count=50, good_count=50,
                                       rejected_count=0, created_at=at))
    db.commit()
    s = cost.build_cost_summary(db, TENANT)
    assert s["lost_units"] == 5 and s["loss_cost"] == 5, s
    assert sum(m["lost_units"] for m in s["by_machine"]) == 5, s["by_machine"]
    assert sum(m["cost"] for m in s["by_machine"]) == 5, s["by_machine"]
    print("PASS 3 x 1.5 units reads 5 on the headline and 5 across the rows, not 6")


def test_a_machine_with_no_line_is_in_the_headline_but_on_no_line():
    """by_line publishes only named lines, but the apportionment must still include
    the unnamed ones, or the named lines would be handed the unnamed machine's loss."""
    db = _db(rate=1)
    _plant(db)
    db.add(models.Machine(id=3, name="M3", status="Running", utilization=80, line=None))
    db.add(models.ProductionRecord(machine_id=3, planned_minutes=150, runtime_minutes=100,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=100,
                                   rejected_count=0, created_at=datetime.utcnow() - timedelta(hours=2)))
    db.commit()
    s = cost.build_cost_summary(db, TENANT)
    # pooled run rate (160 + 90 + 100) / (160 + 90 + 100) = 1: M3 lost 50 units, £50
    lines = {r["line"]: r for r in s["by_line"]}
    assert set(lines) == {"SMT", "IC"}, lines
    assert s["loss_cost"] == 130 and lines["SMT"]["cost"] == 50 and lines["IC"]["cost"] == 30, s["by_line"]
    assert sum(r["cost"] for r in s["by_line"]) == s["loss_cost"] - 50
    print("PASS a machine with no line counts in the headline and is not handed to a named line")


def test_downtime_with_no_run_time_is_unknown_not_zero():
    db = _db(rate=5)
    at = datetime.utcnow() - timedelta(hours=1)
    db.add(models.Machine(id=1, name="M1", status="Breakdown", utilization=0, line="SMT"))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=0,
                                   ideal_cycle_time_seconds=30, total_count=0, good_count=0,
                                   rejected_count=0, created_at=at))
    db.commit()
    s = cost.build_cost_summary(db, TENANT)
    assert s["downtime_minutes"] == 480
    assert s["downtime_lost_units"] is None and s["lost_units"] is None, s
    assert s["downtime_cost"] is None and s["loss_cost"] is None, s
    print("PASS 480 min down with no run time: lost units and £ are unknown, not 0")


if __name__ == "__main__":
    test_cost_summary_without_a_rate_reports_units_and_no_money()
    test_trend_scorecard_report_assistant_twin_speak_units_without_a_rate()
    test_management_summary_has_no_per_minute_proxy()
    test_money_is_units_times_the_tenants_rate()
    test_a_rate_of_zero_is_a_real_zero()
    test_fractional_units_still_reconcile_exactly()
    test_rounding_each_row_on_its_own_would_miss_the_headline()
    test_a_machine_with_no_line_is_in_the_headline_but_on_no_line()
    test_downtime_with_no_run_time_is_unknown_not_zero()
    print("ALL LOSS-MONEY TESTS PASSED")
