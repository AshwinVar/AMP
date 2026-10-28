"""Backend money omission — a non-Admin never RECEIVES a rupee figure.

THE CONTRACT UNDER TEST (money_redaction.redact_money): the server, not just the
browser, withholds money from everyone but an Admin. For every money-returning
endpoint, a Supervisor / Operator / Viewer response must contain NONE of the
money figures — the key is gone, or (where the structure is required) the figure
is null, and a figure composed into a sentence is scrubbed out of it — while an
Admin's response is returned UNCHANGED, every figure intact.

Three layers:

  Part 1  the helper against the EXACT payload shape of each endpoint (plant
          board, cost summary / trend, recovery, scorecard, command centre, risk
          radar, shortage, root cause, costing, part spec, management, saas),
          plus the over-redaction guard (sensor `*_value` and the `..._records`
          COUNT are NOT money and must survive).

  Part 2  the real route handlers with their read-model stubbed, proving each
          handler actually passes its payload through redact_money (no DB).

  Part 3  the inline-built endpoints (part spec, costing, tenant config, saas)
          against a seeded database, proving the wiring end to end.

Run:  DATABASE_URL="sqlite:///./ci_money_redaction.db" python backend/test_money_redaction.py
      (exit 0 = pass)
"""
import os
import sys

# PRINTING THE CURRENCY SYMBOL MUST NOT KILL A PASSING SUITE (see memory:
# cp1252-console-kills-passing-suites). A Windows console is cp1252 by default and
# '₹' has no code point there; this suite carries the symbol in its
# assertions and failure details, so reconfigure stdout to UTF-8 first.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("DATABASE_URL", "sqlite:///./ci_money_redaction.db")

import json  # noqa: E402

from currency import CURRENCY  # noqa: E402
from money_redaction import (  # noqa: E402
    MONEY_KEYS, can_see_money, redact_money,
)

failures = []
T = "MONEYREDACT-TEST"
ADMIN = {"tenant": T, "role": "Admin", "username": "admin"}
SUPERVISOR = {"tenant": T, "role": "Supervisor", "username": "sup"}
OPERATOR = {"tenant": T, "role": "Operator", "username": "op"}
VIEWER = {"tenant": T, "role": "Viewer", "username": "view"}
NON_ADMINS = [SUPERVISOR, OPERATOR, VIEWER]


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def section(t):
    print("\n" + "=" * 74 + f"\n{t}\n" + "=" * 74)


def _flatten_keys(obj):
    """Every dict key anywhere in a nested payload."""
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            out |= _flatten_keys(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out |= _flatten_keys(v)
    return out


def _has_money_figure_in_text(obj):
    """True if any string anywhere still carries a '₹<number>' figure."""
    import re
    pat = re.compile(r"₹\s?[+-]?\d")
    if isinstance(obj, str):
        return bool(pat.search(obj))
    if isinstance(obj, dict):
        return any(_has_money_figure_in_text(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(_has_money_figure_in_text(v) for v in obj)
    return False


# ──────────────────────────────────────────────────────────────────────────
# PART 1 — the helper against each endpoint's real payload shape
# ──────────────────────────────────────────────────────────────────────────

def test_admin_sees_everything_unchanged():
    payload = {"loss_cost": 49740, "revenue": 123.0,
               "cost": {"loss_cost": 49740, "lost_units": 80}}
    for role in (ADMIN,):
        out = redact_money(payload, role)
        check("an Admin gets the payload back unchanged (same object)",
              out is payload)


def test_roles_resolve_to_admin_or_not():
    check("Admin can see money", can_see_money(ADMIN) is True)
    for u in NON_ADMINS:
        check(f"{u['role']} cannot see money", can_see_money(u) is False)
    check("a missing principal cannot see money", can_see_money(None) is False)
    check("a role-less principal cannot see money", can_see_money({}) is False)


def test_plant_board_day_shape():
    # ai/plant_board.py day(): shift_rate points carry revenue + rate_per_hour;
    # `priced` is a flag (kept), parts/shots are counts (kept).
    pb = {"date": "2026-10-28", "acceptable_fraction": 0.8,
          "production": [{"machine": "M1", "total": 500, "shots": 500}],
          "shift_rate": [{"machine": "M1", "priced": True,
                          "points": [{"shift": 1, "parts": 10, "revenue": 123.45,
                                      "rate_per_hour": 15.4}]}]}
    for u in NON_ADMINS:
        v = redact_money(pb, u)
        pt = v["shift_rate"][0]["points"][0]
        check(f"[{u['role']}] plant-board point drops revenue + rate_per_hour, keeps parts",
              "revenue" not in pt and "rate_per_hour" not in pt and pt["parts"] == 10)
        check(f"[{u['role']}] plant-board keeps the `priced` flag and production counts",
              v["shift_rate"][0]["priced"] is True and v["production"][0]["total"] == 500)


def test_plant_board_month_shape():
    pm = {"year": 2026, "month": 10,
          "daily": [{"day": "2026-10-01", "parts": 5, "shots": 5, "kg": 1.2, "revenue": 50.0}],
          "itemwise_production": [{"part": "clip", "total": 100, "good": 98}],
          "shift_rate_by_machine": [{"machine": "M1", "revenue": 999.0, "rate_per_hour": 5.0}],
          "total_rate_per_hour": 5.0, "total_revenue": 999.0}
    for u in NON_ADMINS:
        v = redact_money(pm, u)
        check(f"[{u['role']}] month drops total_revenue + total_rate_per_hour",
              "total_revenue" not in v and "total_rate_per_hour" not in v)
        check(f"[{u['role']}] month daily keeps parts/kg, drops revenue",
              "revenue" not in v["daily"][0] and v["daily"][0]["parts"] == 5 and v["daily"][0]["kg"] == 1.2)
        check(f"[{u['role']}] month per-machine shift rate drops revenue + rate_per_hour",
              "revenue" not in v["shift_rate_by_machine"][0] and "rate_per_hour" not in v["shift_rate_by_machine"][0])
        check(f"[{u['role']}] month keeps itemwise output (units, not money)",
              v["itemwise_production"][0]["total"] == 100)


def test_cost_summary_shape():
    cs = {"has_data": True, "days": 7, "priced": True, "unit_value_gbp": 2.5,
          "loss_cost": 49740, "downtime_cost": 30000, "scrap_cost": 19740,
          "downtime_minutes": 120, "lost_units": 80, "recorded_total": 2000,
          "losses": [{"key": "downtime", "units": 60, "cost": 30000}],
          "by_line": [{"line": "SMT", "lost_units": 40, "cost": 20000, "downtime_cost": 15000, "scrap_cost": 5000}],
          "by_machine": [{"machine_id": 1, "name": "M1", "lost_units": 40, "cost": 20000}],
          "machine_cost": {1: 20000, 2: 10000}, "machine_lost_units": {1: 40, 2: 20},
          "daily": [{"date": "2026-10-28", "cost": 500, "lost_units": 2}],
          "by_type": [{"type": "Energy", "amount": 1200}]}
    for u in NON_ADMINS:
        v = redact_money(cs, u)
        leaked = _flatten_keys(v) & {"unit_value_gbp", "loss_cost", "downtime_cost",
                                     "scrap_cost", "recorded_total", "machine_cost", "amount", "cost"}
        check(f"[{u['role']}] cost summary leaks no money key", not leaked, str(leaked))
        check(f"[{u['role']}] cost summary keeps units + the priced flag",
              v["lost_units"] == 80 and v["downtime_minutes"] == 120 and v["priced"] is True
              and v["by_line"][0]["lost_units"] == 40 and v["machine_lost_units"] == {1: 40, 2: 20})
        check(f"[{u['role']}] by_type list keeps the label, drops the amount",
              v["by_type"][0]["type"] == "Energy" and "amount" not in v["by_type"][0])


def test_cost_trend_shape_and_verdict():
    ct = {"days": 14, "priced": True, "unit_value_gbp": 2.5, "direction": "worsening",
          "delta_units": 20, "delta_cost": 500,
          "verdict": f"Losses up {CURRENCY}500 to {CURRENCY}1,23,456 (49%) week on week.",
          "current": {"cost": 123456, "lost_units": 80},
          "series": [{"date": "d", "cost": 10, "lost_units": 2, "downtime_cost": 6, "scrap_cost": 4}],
          "worsening_machines": [{"name": "M1", "cost": 100, "prior_cost": 50, "delta_cost": 50, "delta_units": 5}],
          "drivers": [{"key": "downtime", "cost": 80, "prior_cost": 40, "delta_cost": 40, "delta_units": 4}]}
    for u in NON_ADMINS:
        v = redact_money(ct, u)
        leaked = _flatten_keys(v) & {"unit_value_gbp", "delta_cost", "cost", "prior_cost",
                                     "downtime_cost", "scrap_cost"}
        check(f"[{u['role']}] cost trend leaks no money key", not leaked, str(leaked))
        check(f"[{u['role']}] cost trend verdict has no rupee figure left",
              not _has_money_figure_in_text(v["verdict"]) and CURRENCY in v["verdict"], v["verdict"])
        check(f"[{u['role']}] cost trend keeps direction + unit deltas",
              v["direction"] == "worsening" and v["delta_units"] == 20
              and v["worsening_machines"][0]["delta_units"] == 5 and v["series"][0]["lost_units"] == 2)


def test_recovery_shape():
    rec = {"has_data": True, "oee": 72, "gap_points": 13,
           "recoverable_units_window": 125, "recoverable_units_per_year": 6518,
           "recoverable_value_window": 6200, "recoverable_value_per_year": 300000,
           "unit_value_gbp": 2.5, "lever_recoverable_units_per_year": 5000,
           "lever_recoverable_value_per_year": 12500,
           "components": [{"key": "availability", "gap_points": 13}]}
    for u in NON_ADMINS:
        v = redact_money(rec, u)
        leaked = _flatten_keys(v) & {"recoverable_value_window", "recoverable_value_per_year",
                                     "unit_value_gbp", "lever_recoverable_value_per_year"}
        check(f"[{u['role']}] recovery leaks no money key", not leaked, str(leaked))
        check(f"[{u['role']}] recovery keeps the recoverable UNITS + gap",
              v["recoverable_units_window"] == 125 and v["lever_recoverable_units_per_year"] == 5000
              and v["gap_points"] == 13)


def test_scorecard_kpi_shape():
    sc = {"has_data": True, "kpis": [
        {"key": "oee", "label": "Plant OEE", "value": 72, "unit": "%", "delta": 3, "delta_tone": "good"},
        {"key": "loss_cost", "label": "Cost of losses", "value": 49740, "unit": CURRENCY,
         "delta": -500, "delta_tone": "good", "tone": "warn"}]}
    for u in NON_ADMINS:
        v = redact_money(sc, u)
        oee, loss = v["kpis"][0], v["kpis"][1]
        check(f"[{u['role']}] scorecard keeps the OEE KPI value + delta (unit %)",
              oee["value"] == 72 and oee["delta"] == 3)
        check(f"[{u['role']}] scorecard nulls the rupee KPI value + delta, keeps the tile",
              loss["value"] is None and loss["delta"] is None and loss["unit"] == CURRENCY
              and loss["key"] == "loss_cost" and loss["tone"] == "warn")


def test_command_centre_shape():
    cc = {"state": "ok",
          "headline": f"Plant OEE 72%. Biggest measured loss: Downtime ({CURRENCY}1,23,456).",
          "problems": [{"title": "Downtime", "impact_units": 10, "impact_money": 5000,
                        "currency": CURRENCY, "rank_basis": "measured loss"}],
          "cost": {"state": "ok", "priced": True, "currency": CURRENCY, "unit_value": 2.5,
                   "loss_cost": 49740, "lost_units": 80, "downtime_minutes": 120,
                   "facts": [{"id": "F1", "key": "losses.cost", "label": "Cost of losses",
                              "value": 49740, "unit": CURRENCY, "provenance": "derived"},
                             {"id": "F2", "key": "losses.lost_units", "label": "Good units not made",
                              "value": 80, "unit": "units", "provenance": "derived"}]}}
    for u in NON_ADMINS:
        v = redact_money(cc, u)
        check(f"[{u['role']}] command-centre problem drops impact_money, keeps impact_units",
              "impact_money" not in v["problems"][0] and v["problems"][0]["impact_units"] == 10)
        check(f"[{u['role']}] command-centre headline has no rupee figure",
              not _has_money_figure_in_text(v["headline"]), v["headline"])
        cblock = v["cost"]
        check(f"[{u['role']}] command-centre cost BLOCK survives, its money removed",
              isinstance(cblock, dict) and "unit_value" not in cblock and "loss_cost" not in cblock
              and cblock["lost_units"] == 80 and cblock["downtime_minutes"] == 120 and cblock["state"] == "ok")
        check(f"[{u['role']}] command-centre money fact nulled, non-money fact kept",
              cblock["facts"][0]["value"] is None and cblock["facts"][1]["value"] == 80)


def test_risk_radar_shape():
    rr = {"risks": [{"title": "Order short", "impact_units": 40, "impact_money": 800,
                     "currency": CURRENCY, "basis": "rule"}]}
    for u in NON_ADMINS:
        v = redact_money(rr, u)
        check(f"[{u['role']}] risk radar drops impact_money, keeps the rule + units",
              "impact_money" not in v["risks"][0] and v["risks"][0]["impact_units"] == 40
              and v["risks"][0]["basis"] == "rule")


def test_shortage_shape():
    sh = {"units_at_risk": 40, "money_at_risk": 800, "priced": True,
          "shortages": [{"item": "resin", "units_at_risk": 40, "money_at_risk": 800, "currency": CURRENCY,
                         "facts": [{"id": "F1", "value": 800, "unit": CURRENCY, "provenance": "derived"}]}]}
    for u in NON_ADMINS:
        v = redact_money(sh, u)
        leaked = _flatten_keys(v) & {"money_at_risk"}
        check(f"[{u['role']}] shortage leaks no money_at_risk", not leaked)
        check(f"[{u['role']}] shortage keeps units_at_risk + nulls the money fact",
              v["units_at_risk"] == 40 and v["shortages"][0]["units_at_risk"] == 40
              and v["shortages"][0]["facts"][0]["value"] is None)


def test_root_cause_shape():
    rc = {"gap_units": 50, "unattributed_units": 5, "currency": CURRENCY,
          "measured_loss_money": f"{CURRENCY}1,234", "attributed_money": f"{CURRENCY}900",
          "causes": [{"cause_label": "scrap", "units": 20, "money": 500, "basis": "measured"}],
          "biggest": {"gap_units": 50, "gap_money": f"{CURRENCY}1,234"}}
    for u in NON_ADMINS:
        v = redact_money(rc, u)
        leaked = _flatten_keys(v) & {"measured_loss_money", "attributed_money", "money", "gap_money"}
        check(f"[{u['role']}] root cause leaks no money key", not leaked, str(leaked))
        check(f"[{u['role']}] root cause keeps the unit gap + attribution",
              v["gap_units"] == 50 and v["causes"][0]["units"] == 20 and v["causes"][0]["cause_label"] == "scrap"
              and v["biggest"]["gap_units"] == 50)


def test_costing_rollup_shape():
    co = {"total_cost_records": 3, "manual_cost_total": 500, "production_units": 1000,
          "cost_per_good_unit": 0.5, "supplier_receipt_units": 200,
          "by_type": {"Energy": 1200, "Labour": 800}, "by_department": {"Production": 2000}}
    for u in NON_ADMINS:
        v = redact_money(co, u)
        leaked = _flatten_keys(v) & {"manual_cost_total", "cost_per_good_unit", "by_type", "by_department"}
        check(f"[{u['role']}] costing leaks no money key / spend map", not leaked, str(leaked))
        check(f"[{u['role']}] costing keeps the COUNTS (records, units, receipts)",
              v["total_cost_records"] == 3 and v["production_units"] == 1000
              and v["supplier_receipt_units"] == 200)


def test_part_spec_shapes():
    specs = {"specs": [{"part_code": "ELE", "part_name": "clip", "material": "PP",
                        "part_weight_g": 0.33, "cavities": 56, "price_per_piece": 0.09}],
             "materials": ["PP"]}
    audit = {"entries": [{"actor": "a", "changes": {"price_per_piece": {"old": 0.09, "new": 0.12},
                                                    "cavities": {"old": 56, "new": 56}}}]}
    for u in NON_ADMINS:
        v = redact_money(specs, u)
        check(f"[{u['role']}] part spec drops price_per_piece, keeps the rest of the spec",
              "price_per_piece" not in v["specs"][0] and v["specs"][0]["cavities"] == 56
              and v["specs"][0]["part_code"] == "ELE" and v["materials"] == ["PP"])
        va = redact_money(audit, u)
        ch = va["entries"][0]["changes"]
        check(f"[{u['role']}] part spec audit drops a price change, keeps a cavity change",
              "price_per_piece" not in ch and "cavities" in ch)


def test_management_summary_shape():
    mg = {"avg_oee": 83, "avg_availability": 90, "unit_value_gbp": 2.5,
          "estimated_loss_value": 49740, "coverage": None}
    for u in NON_ADMINS:
        v = redact_money(mg, u)
        leaked = _flatten_keys(v) & {"unit_value_gbp", "estimated_loss_value"}
        check(f"[{u['role']}] management summary leaks no money key", not leaked, str(leaked))
        check(f"[{u['role']}] management summary keeps OEE + availability",
              v["avg_oee"] == 83 and v["avg_availability"] == 90)


def test_saas_shape():
    sa = {"total_tenants": 5, "trial": 1, "active": 3, "monthly_recurring_revenue": 50000, "total_seats": 20}
    for u in NON_ADMINS:
        v = redact_money(sa, u)
        check(f"[{u['role']}] saas drops MRR, keeps tenant counts + seats",
              "monthly_recurring_revenue" not in v and v["total_tenants"] == 5 and v["total_seats"] == 20)


def test_final_executive_summary_shape():
    fe = {"machine_count": 10, "work_orders": 4, "total_cost_records": 9,
          "low_stock_items": 2, "total_cost": 123456}
    for u in NON_ADMINS:
        v = redact_money(fe, u)
        check(f"[{u['role']}] final-exec drops total_cost, keeps total_cost_records COUNT",
              "total_cost" not in v and v["total_cost_records"] == 9 and v["machine_count"] == 10)


def test_does_not_over_redact_non_money():
    # Sensor readings and the "...records" COUNT are NOT money and must survive —
    # the exact substring traps the MONEY_KEYS set is written to avoid.
    nm = {"signal_value": 3.3, "numeric_value": 7, "measured_value": 50, "baseline_value": 12,
          "total_cost_records": 9, "runs": 4, "priced": False, "currency": None,
          "run_rate": 0.42, "delta_pct": 12, "coverage": {"complete": True}}
    for u in NON_ADMINS:
        v = redact_money(nm, u)
        check(f"[{u['role']}] non-money fields are left untouched", v == nm, str(v))


def test_never_mutates_input_or_shares_state():
    payload = {"loss_cost": 49740, "nested": {"cost": 500, "lost_units": 5}}
    snapshot = json.dumps(payload, sort_keys=True)
    redact_money(payload, VIEWER)
    check("redacting for a non-Admin does not mutate the caller's payload",
          json.dumps(payload, sort_keys=True) == snapshot)


# ──────────────────────────────────────────────────────────────────────────
# PART 2 — the real route handlers, read-model stubbed (proves the wiring)
# ──────────────────────────────────────────────────────────────────────────

# A payload carrying money of every shape the endpoints emit. Money values are
# distinctive so their mere appearance in a non-Admin response is a provable leak.
CANON = {
    "has_data": True, "priced": True, "direction": "worsening", "lost_units": 80,
    "loss_cost": 491740, "downtime_cost": 301000, "scrap_cost": 190740,
    "unit_value_gbp": 2.5, "recoverable_value_per_year": 377000,
    "lever_recoverable_value_per_year": 125500, "impact_money": 59000,
    "money_at_risk": 880000, "revenue": 123400.0, "rate_per_hour": 150500.0,
    "total_revenue": 999900.0, "measured_loss_money": f"{CURRENCY}1,23,456",
    "verdict": f"Losses up {CURRENCY}500 to {CURRENCY}1,23,456 week on week.",
    "headline": f"Biggest measured loss: Downtime ({CURRENCY}1,23,456).",
    "kpis": [{"key": "loss_cost", "value": 491740, "unit": CURRENCY, "delta": -500}],
    "cost": {"loss_cost": 491740, "lost_units": 80, "state": "ok",
             "facts": [{"value": 491740, "unit": CURRENCY, "provenance": "derived"}]},
    "by_type": {"Energy": 120000},
}
# The distinctive money-value tokens (as JSON would render them) that must NOT
# appear in a non-Admin payload.
MONEY_TOKENS = ["491740", "301000", "190740", "2.5", "377000", "125500", "59000",
                "880000", "123400", "150500", "999900", "1,23,456", "120000"]


def _assert_handler_redacts(label, call):
    """call(current_user) -> payload. Admin gets CANON back unchanged; every
    non-Admin gets a payload with not one money token nor money key in it."""
    admin_out = call(ADMIN)
    check(f"{label}: Admin receives the money unchanged",
          admin_out is CANON or admin_out.get("loss_cost") == 491740, str(type(admin_out)))
    for u in NON_ADMINS:
        out = call(u)
        blob = json.dumps(out, ensure_ascii=False)
        present = [tok for tok in MONEY_TOKENS if tok in blob]
        check(f"{label}: [{u['role']}] carries no money value", not present, str(present))
        bad_keys = _flatten_keys(out) & (MONEY_KEYS | {"by_type"})
        # `cost` may survive as a BLOCK (its inner money already stripped); that is
        # the one money-named key allowed through, and only as a dict.
        check(f"{label}: [{u['role']}] carries no money key", not bad_keys, str(bad_keys))
        check(f"{label}: [{u['role']}] no rupee figure left in any string",
              not _has_money_figure_in_text(out))


def _patch(module, name):
    """Context-less monkeypatch: returns (set, restore) for module.name -> CANON."""
    orig = getattr(module, name)
    return (lambda: setattr(module, name, lambda *a, **k: dict(_deepcopy(CANON))),
            lambda: setattr(module, name, orig))


def _deepcopy(obj):
    return json.loads(json.dumps(obj))


def test_read_model_handlers_are_wired():
    import ai.brief
    import ai.briefing
    import ai.command_centre
    import ai.cost
    import ai.recovery
    import ai.risk_radar
    import ai.root_cause
    import ai.scorecard
    import ai.shortage
    import ai.twin
    import read_model_routes as RM

    cases = [
        ("/recovery-summary", RM.get_recovery_summary, ai.recovery, "build_recovery_summary"),
        ("/command-centre", RM.get_command_centre, ai.command_centre, "build_command_centre"),
        ("/daily-brief", RM.get_daily_brief, ai.brief, "build_daily_brief"),
        ("/shortage-impact", RM.get_shortage_impact, ai.shortage, "build_shortage_impact"),
        ("/root-cause", RM.get_root_cause, ai.root_cause, "explain_production_gap"),
        ("/risk-radar", RM.get_risk_radar, ai.risk_radar, "build_risk_radar"),
        ("/briefing", RM.get_briefing, ai.briefing, "build_briefing"),
        ("/cost-summary", RM.get_cost_summary, ai.cost, "build_cost_summary"),
        ("/cost-trend", RM.get_cost_trend, ai.cost, "build_cost_trend"),
        ("/scorecard", RM.get_scorecard, ai.scorecard, "build_scorecard"),
        ("/twin-overlay", RM.get_twin_overlay, ai.twin, "build_twin_overlay"),
    ]
    for label, handler, module, fn in cases:
        orig = getattr(module, fn)
        setattr(module, fn, lambda *a, **k: _deepcopy(CANON))
        try:
            _assert_handler_redacts(label, lambda u: handler(db=None, current_user=u))
        finally:
            setattr(module, fn, orig)


def test_analytics_handlers_are_wired():
    import ai.plant_board
    import analytics_routes as AR

    for label, handler, fn in [
        ("/analytics/plant-board", lambda u: AR.plant_board_day(on="", db=None, current_user=u), "day"),
        ("/analytics/plant-board/month",
         lambda u: AR.plant_board_month(year=2026, month=10, db=None, current_user=u), "month"),
    ]:
        orig = getattr(ai.plant_board, fn)
        setattr(ai.plant_board, fn, lambda *a, **k: _deepcopy(CANON))
        try:
            _assert_handler_redacts(label, handler)
        finally:
            setattr(ai.plant_board, fn, orig)

    # /analytics/summary wraps the PURE module-level analytics_summary.
    orig = AR.analytics_summary
    AR.analytics_summary = lambda *a, **k: _deepcopy(CANON)
    try:
        _assert_handler_redacts("/analytics/summary",
                                lambda u: AR.analytics_summary_endpoint(db=None, current_user=u))
    finally:
        AR.analytics_summary = orig


# ──────────────────────────────────────────────────────────────────────────
# PART 3 — the inline-built endpoints against a seeded database
# ──────────────────────────────────────────────────────────────────────────

def _fresh_db():
    import database
    import models
    models.Base.metadata.create_all(bind=database.engine)
    db = database.SessionLocal()
    for M in (models.PartSpec, models.TenantConfig, models.CostRecord):
        try:
            db.query(M).filter(M.tenant_code == T).delete()
        except Exception:
            db.rollback()
    db.commit()
    return db


def test_part_spec_list_handler_is_wired():
    from datetime import date
    import models
    import part_spec_routes as PSR
    db = _fresh_db()
    try:
        db.add(models.PartSpec(tenant_code=T, part_code="ELE-CLIP", part_name="clip",
                               material="PP", part_weight_g=0.33, cavities=56, active_cavities=56,
                               ideal_cycle_time_s=14.0, price_per_piece=0.09,
                               effective_from=date(2026, 1, 1)))
        db.commit()
        admin = PSR.list_part_specs(db=db, current_user=ADMIN)
        check("part-specs: Admin sees price_per_piece",
              admin["specs"] and admin["specs"][0]["price_per_piece"] == 0.09)
        for u in NON_ADMINS:
            out = PSR.list_part_specs(db=db, current_user=u)
            check(f"part-specs: [{u['role']}] row has no price_per_piece, keeps the spec",
                  out["specs"] and "price_per_piece" not in out["specs"][0]
                  and out["specs"][0]["part_code"] == "ELE-CLIP")
    finally:
        db.close()


def test_tenant_config_handler_is_wired():
    import models
    import platform_routes as PR
    db = _fresh_db()
    try:
        db.add(models.TenantConfig(tenant_code=T, unit_value_gbp=12.5))
        db.commit()
        admin = PR.tenant_config(db=db, current_user=ADMIN)
        check("tenant-config: Admin sees unit_value_gbp", admin.get("unit_value_gbp") == 12.5)
        for u in NON_ADMINS:
            out = PR.tenant_config(db=db, current_user=u)
            check(f"tenant-config: [{u['role']}] has no unit_value_gbp, keeps tenant_code",
                  "unit_value_gbp" not in out and out["tenant_code"] == T)
    finally:
        db.close()


def test_costing_rollup_handler_is_wired():
    import models
    import tenancy
    import costing_routes as CR
    db = _fresh_db()
    tok = tenancy.set_current_tenant(T)   # inside the test, so the pytest autouse reset can't unbind it
    try:
        db.add(models.CostRecord(tenant_code=T, cost_no="C1", cost_type="Energy",
                                 description="power", amount=500, department="Production"))
        db.commit()
        admin = CR.get_costing_analytics(db=db, current_user=ADMIN)
        check("costing: Admin sees manual_cost_total", "manual_cost_total" in admin)
        for u in NON_ADMINS:
            out = CR.get_costing_analytics(db=db, current_user=u)
            check(f"costing: [{u['role']}] has no money keys, keeps the record COUNT",
                  "manual_cost_total" not in out and "cost_per_good_unit" not in out
                  and "by_type" not in out and "total_cost_records" in out)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def test_saas_analytics_handler_is_wired():
    import models
    import saas_routes as SR
    db = _fresh_db()
    code = "MONEYREDACT-CO"
    db.query(models.CompanyTenant).filter(models.CompanyTenant.company_code == code).delete()
    db.commit()
    orig_scope = SR._registry_scope
    # Scope the registry aggregate to our one seeded company, so MRR is deterministic
    # regardless of whatever else shares the (single-process pytest) database.
    SR._registry_scope = lambda q, cu: q.filter(models.CompanyTenant.company_code == code)
    try:
        db.add(models.CompanyTenant(company_code=code, company_name="Co",
                                    monthly_fee=50000, subscription_status="Active", seats=3))
        db.commit()
        admin = SR.get_saas_analytics(db=db, current_user=ADMIN)
        check("saas: Admin sees MRR = 50000", admin.get("monthly_recurring_revenue") == 50000)
        for u in NON_ADMINS:
            out = SR.get_saas_analytics(db=db, current_user=u)
            check(f"saas: [{u['role']}] has no MRR, keeps the tenant count",
                  "monthly_recurring_revenue" not in out and "total_tenants" in out)
    finally:
        SR._registry_scope = orig_scope
        db.query(models.CompanyTenant).filter(models.CompanyTenant.company_code == code).delete()
        db.commit()
        db.close()


def main():
    section("PART 1 — the helper against each endpoint's payload shape")
    test_admin_sees_everything_unchanged()
    test_roles_resolve_to_admin_or_not()
    test_plant_board_day_shape()
    test_plant_board_month_shape()
    test_cost_summary_shape()
    test_cost_trend_shape_and_verdict()
    test_recovery_shape()
    test_scorecard_kpi_shape()
    test_command_centre_shape()
    test_risk_radar_shape()
    test_shortage_shape()
    test_root_cause_shape()
    test_costing_rollup_shape()
    test_part_spec_shapes()
    test_management_summary_shape()
    test_saas_shape()
    test_final_executive_summary_shape()
    test_does_not_over_redact_non_money()
    test_never_mutates_input_or_shares_state()

    section("PART 2 — real route handlers, read-model stubbed (wiring)")
    test_read_model_handlers_are_wired()
    test_analytics_handlers_are_wired()

    section("PART 3 — inline-built endpoints against a seeded database")
    test_part_spec_list_handler_is_wired()
    test_tenant_config_handler_is_wired()
    test_costing_rollup_handler_is_wired()
    test_saas_analytics_handler_is_wired()

    print("\n" + "=" * 74)
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
