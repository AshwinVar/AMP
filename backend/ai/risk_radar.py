"""The Production Risk Radar: what is likely to become a problem, and why (ADR-0026).

Every risk here is a RULE over measured data, and every risk says which rule and
which measurement produced it. Three things this deliberately does not do:

  * it does not call a threshold a prediction, and it never calls any of this
    machine learning. AMP has one adopted model (failure risk) evaluated on
    synthetic machines only; it is shown on its own model card with that caveat,
    and it is not smuggled in here as a probability;
  * it does not invent a number. A risk AMP cannot size says so;
  * it does not use a likelihood word without the rule that earned it.

THE RULES, EACH WITH ITS THRESHOLD

  Order will miss its date   the units still to ship, divided by the days left,
                             against the plant's own measured good-units-per-day
                             over the last week. Above it: LIKELY. Within 30% of
                             it: POSSIBLE.
  Item runs out              days of cover from the item's own measured burn
                             rate: 3 days or less LIKELY, a week or less POSSIBLE.
  Machine stops              the hand-weighted rule score (predictive_engine):
                             75 or more LIKELY, 55 or more POSSIBLE. Labelled
                             RULE-BASED ASSESSMENT, never a probability.
  Maintenance backlog        a task already overdue is LIKELY to keep slipping;
                             three or more due this week is a WATCH.
  Quality drifting           the fail rate's week-on-week move, against the
                             drift threshold the quality trend already defines;
                             a thin sample is INSUFFICIENT HISTORY, not a risk.

Composes existing read-models; adds no storage.
"""
from datetime import datetime

import models
from ai import evidence as ev
from ai.coverage import build_coverage_summary
from ai.delivery import build_delivery_summary
from ai.maintenance import build_maintenance_forecast
from ai.prediction import assess_from_db
from ai.production import build_production_summary
from ai.quality import build_quality_trend
from currency import CURRENCY
from tenancy import tenant_unit_value

name = "risk_radar"

M, D, R, U = ev.MEASURED, ev.DERIVED, ev.RULE, ev.UNKNOWN
W7 = "last 7 days"
TOP_RISKS = 8

# The rule thresholds, in one place, because every sentence below quotes them.
ORDER_TIGHT = 0.7          # required rate within 30% of measured rate -> POSSIBLE
COVER_LIKELY_DAYS = 3
COVER_POSSIBLE_DAYS = 7
MACHINE_LIKELY_SCORE = 75
MACHINE_POSSIBLE_SCORE = 55
MAINTENANCE_WATCH_COUNT = 3

_ORDER = {ev.LIKELY: 0, ev.POSSIBLE: 1, ev.WATCH: 2}


def _fact(key, label, value, prov, unit="", source="", window=W7, detail=""):
    return ev.Fact(key=key, label=label, value=value, provenance=prov, unit=unit, source=source,
                   window=window, detail=detail).to_dict(key)


def _risk(key, title, detail, likelihood, rule, horizon, module, view, facts,
          units=None, unit_value=None):
    return {"key": key, "title": title, "detail": detail, "likelihood": likelihood, "rule": rule,
            "horizon": horizon, "module": module, "view": view, "facts": facts,
            "impact_units": units,
            "impact_money": (round(units * unit_value) if units is not None and unit_value is not None else None),
            "currency": CURRENCY if unit_value is not None else None}


def _order_risks(delivery, daily_rate, unit_value, today):
    """An order whose remaining units cannot be made in the days it has left, at
    the rate the plant has actually been running."""
    out = []
    for o in (delivery.get("at_risk_orders") or [])[:5]:
        remaining = max(0, (o.get("order_quantity") or 0) - (o.get("dispatched_quantity") or 0))
        if not remaining:
            continue
        days_left = o.get("days_to_due")
        facts = [_fact(f"order.{o['order_no']}.remaining", f"{o['order_no']}: units still to ship", remaining,
                       D, "units", "customer_orders", "now")]
        if days_left is not None:
            facts.append(_fact(f"order.{o['order_no']}.days_left", f"{o['order_no']}: days to the due date",
                               days_left, D, "days", "customer_orders", "now"))
        # An order already past its date is not a forecast; it is a fact, and the
        # rule says so rather than dressing it up as a likelihood.
        if o.get("state") == "late" or (days_left is not None and days_left < 0):
            out.append(_risk(
                f"order.{o['order_no']}", f"{o['order_no']} is already past its date",
                f"{remaining:,} units still to ship"
                + (f" for {o['customer']}" if o.get("customer") else ""),
                ev.LIKELY, "the due date has passed and units are still unshipped",
                "now", "orders", "orders", facts, units=remaining, unit_value=unit_value))
            continue
        if daily_rate is None or days_left is None:
            out.append(_risk(
                f"order.{o['order_no']}", f"{o['order_no']} may miss its date",
                f"{remaining:,} units still to ship for {o.get('customer') or 'the customer'}",
                ev.WATCH, "no measured output rate yet, so AMP cannot say whether the date is reachable",
                "unknown", "orders", "orders", facts, units=remaining, unit_value=unit_value))
            continue
        required = remaining / max(1, days_left)
        facts.append(_fact(f"order.{o['order_no']}.required_rate", f"{o['order_no']}: units a day needed",
                           round(required, 1), D, "units/day", "customer_orders + production_records", "now"))
        facts.append(_fact("plant.measured_rate", "Plant good units a day (measured)", round(daily_rate, 1), M,
                           "units/day", "production_records"))
        if required > daily_rate:
            likelihood, rule = ev.LIKELY, (f"needs {required:.1f} units a day; the plant has been making "
                                           f"{daily_rate:.1f} a day over the {W7}")
        elif required > ORDER_TIGHT * daily_rate:
            likelihood, rule = ev.POSSIBLE, (f"needs {required:.1f} units a day, within 30% of the measured "
                                             f"{daily_rate:.1f} a day, so any stoppage puts it out of reach")
        else:
            likelihood, rule = ev.WATCH, (f"needs {required:.1f} units a day against a measured "
                                          f"{daily_rate:.1f}; comfortable unless something stops")
        out.append(_risk(
            f"order.{o['order_no']}",
            f"{o['order_no']} " + ("will miss its date at the current rate" if likelihood == ev.LIKELY
                                   else "is tight against its date" if likelihood == ev.POSSIBLE
                                   else "is due soon"),
            f"{remaining:,} units still to ship in {days_left} day{'' if days_left == 1 else 's'}"
            + (f" for {o['customer']}" if o.get("customer") else ""),
            likelihood, rule, f"{max(0, days_left)} days", "orders", "orders", facts,
            units=remaining, unit_value=unit_value))
    return out


def _stock_risks(coverage, impact, unit_value):
    """`impact` is {item_code: units of production that cannot be made}, measured
    through the tenant's own bills of materials (ADR-0030).

    ADR-0026 shipped this rule with NO size at all, and said why: *"a stock-out
    carries no units, because AMP has no measured link from a shortage to the
    units not made."* That link now exists, so a risk gets a size when the item
    is in a recipe — and still none when it is not, which is the same refusal as
    before rather than a new guess.
    """
    out = []
    for item in (coverage.get("items") or [])[:5]:
        cover = item.get("days_of_cover")
        if cover is None:
            continue
        if cover <= COVER_LIKELY_DAYS:
            likelihood = ev.LIKELY
        elif cover <= COVER_POSSIBLE_DAYS:
            likelihood = ev.POSSIBLE
        else:
            continue
        empty = cover == 0
        facts = [
            _fact(f"stock.{item['item_code']}.cover", f"{item['item_name']}: days of cover", cover, D, "days",
                  "inventory_items + inventory_transactions", "now"),
            _fact(f"stock.{item['item_code']}.burn", f"{item['item_name']}: measured use a day",
                  item.get("daily_burn"), M, item.get("unit") or "", "inventory_transactions"),
            _fact(f"stock.{item['item_code']}.stock", f"{item['item_name']}: in stock",
                  item.get("current_stock"), M, item.get("unit") or "", "inventory_items", "now"),
        ]
        out.append(_risk(
            f"stock.{item['item_code']}",
            (f"{item['item_name']} is already out of stock" if empty else
             f"{item['item_name']} runs out in {cover} day{'' if cover == 1 else 's'}"),
            (f"projected empty on {item['stockout_date']}" if item.get("stockout_date") and not empty
             else "nothing on hand" if empty else "at the measured rate")
            + (f"; supplier {item['supplier']}" if item.get("supplier") else ""),
            likelihood,
            ("there is no stock on hand" if empty else
             f"days of cover ({cover}) is at or below the "
             f"{COVER_LIKELY_DAYS if likelihood == ev.LIKELY else COVER_POSSIBLE_DAYS}-day threshold, "
             "at the item's own measured use"),
            "now" if empty else f"{cover} days", "inventory", "inventory", facts,
            units=impact.get(item["item_code"]), unit_value=unit_value))
    return out


def _machine_risks(db):
    out = []
    for row in sorted(assess_from_db(db), key=lambda r: -r["risk_score"])[:5]:
        score = int(row["risk_score"])
        if score >= MACHINE_LIKELY_SCORE:
            likelihood = ev.LIKELY
        elif score >= MACHINE_POSSIBLE_SCORE:
            likelihood = ev.POSSIBLE
        else:
            continue
        reasons = list(row.get("reasons") or [])
        facts = [_fact(f"machine.{row['machine_id']}.score", f"{row['machine_name']}: rule risk score", score, R,
                       "/100", "predictive_engine", "now",
                       detail="hand-weighted rule points, not machine learning")]
        for i, reason in enumerate(reasons[:3], 1):
            facts.append(_fact(f"machine.{row['machine_id']}.reason_{i}", f"{row['machine_name']}: factor {i}",
                               str(reason), R, source="predictive_engine", window="now"))
        out.append(_risk(
            f"machine.{row['machine_id']}", f"{row['machine_name']} is likely to stop"
            if likelihood == ev.LIKELY else f"{row['machine_name']} is showing risk factors",
            "; ".join(reasons[:2]) or "no single factor stands out",
            likelihood,
            f"the rule score is {score}, at or above the {MACHINE_LIKELY_SCORE if likelihood == ev.LIKELY else MACHINE_POSSIBLE_SCORE} "
            "threshold; this is a hand-weighted rule, not a trained model",
            "now", "machines", "machinehealth", facts))
    return out


def _maintenance_risks(forecast):
    out = []
    if forecast.get("overdue"):
        first = (forecast.get("overdue_tasks") or [{}])[0]
        out.append(_risk(
            "maintenance.overdue", f"{forecast['overdue']} maintenance task(s) already overdue",
            (f"oldest: {first.get('task_type')} on {first.get('machine')}" if first else ""),
            ev.LIKELY, "a task past its planned date stays past it until someone does it",
            "now", "cmms", "cmms",
            [_fact("maint.overdue", "Overdue tasks", forecast["overdue"], D, "tasks", "maintenance_tasks", "now")]))
    if (forecast.get("due_next_7") or 0) >= MAINTENANCE_WATCH_COUNT:
        out.append(_risk(
            "maintenance.crunch", f"{forecast['due_next_7']} maintenance tasks fall due this week",
            f"peak day: {forecast.get('peak', {}).get('date', 'unknown')}",
            ev.WATCH, f"{MAINTENANCE_WATCH_COUNT} or more tasks due inside seven days is a crew-capacity watch",
            "7 days", "cmms", "cmms",
            [_fact("maint.due_next_7", "Tasks due this week", forecast["due_next_7"], M, "tasks",
                   "maintenance_tasks", "next 7 days")]))
    return out


def _quality_risk(trend):
    if not trend.get("drifting"):
        return None
    thin = trend.get("thin_sample")
    facts = [
        _fact("quality.delta", "Fail-rate change week on week", trend.get("delta_pts"), D, "pts",
              "quality_inspections", "this week vs last"),
        _fact("quality.current", "Fail rate this week", (trend.get("current") or {}).get("fail_rate"), D, "%",
              "quality_inspections"),
    ]
    return _risk(
        "quality.drift", "The fail rate is drifting up",
        f"{trend.get('drifting_count', 0)} machine(s) moved beyond the drift threshold",
        ev.WATCH if thin else ev.POSSIBLE,
        (f"the week-on-week move is beyond the {trend.get('drift_threshold_pts')}-point drift threshold"
         + (" on a thin sample, so it may be noise" if thin else "")),
        "7 days", "quality", "quality", facts)


def build_risk_radar(db, tenant: str, now=None) -> dict:
    """What is likely to become a problem, by rule, with the measurement behind it.

    ONE instant for every rule. `now` used to reach only `today` (the order
    rule's own arithmetic), the shortage link and `generated_at`, while the five
    read-models this composes each read the wall clock — so a caller replaying
    a past day (the brief, proactive restraint, a test at a fixed NOW) got that
    day's arithmetic over today's states: an order due on the replayed day came
    back LIKELY / "the due date has passed" because delivery had judged it
    against the real date. Every read-model now takes the same `now`.
    """
    today = (now or datetime.utcnow()).date()
    production = build_production_summary(db, tenant, now=now)
    delivery = build_delivery_summary(db, tenant, now=now)
    coverage = build_coverage_summary(db, tenant, now=now)
    forecast = build_maintenance_forecast(db, tenant, now=now)
    quality = build_quality_trend(db, tenant, now=now)
    unit_value = tenant_unit_value(db, tenant)
    daily_rate = (production["good"] / production["days"]) if production["runs"] else None
    # What each shortage would actually stop, through the tenant's own bills of
    # materials (ADR-0030). An item in no recipe is absent from this map and its
    # risk stays unsized, exactly as it was before the link existed.
    from ai.shortage import build_shortage_impact      # lazy: composes inventory, BOM and orders
    stock_impact = {s["item_code"]: s["units_at_risk"]
                    for s in build_shortage_impact(db, tenant, now=now)["shortages"]
                    if s["units_at_risk"]}

    risks = (_order_risks(delivery, daily_rate, unit_value, today)
             + _stock_risks(coverage, stock_impact, unit_value)
             + _machine_risks(db)
             + _maintenance_risks(forecast))
    drift = _quality_risk(quality)
    if drift:
        risks.append(drift)
    risks.sort(key=lambda r: (_ORDER.get(r["likelihood"], 3), -(r["impact_units"] or 0), r["key"]))
    risks = risks[:TOP_RISKS]

    machines = db.query(models.Machine).filter(models.Machine.tenant_code == tenant).count()
    if not machines and not delivery["total"] and not coverage["total_items"]:
        state, headline = ev.NO_DATA, "Nothing is set up yet, so there is nothing to watch."
    elif daily_rate is None:
        state = ev.INSUFFICIENT_HISTORY
        headline = ("No production has been recorded this week, so AMP cannot say whether the orders on the "
                    "book are reachable.")
    else:
        state = ev.OK
        likely = [r for r in risks if r["likelihood"] == ev.LIKELY]
        headline = (f"{len(likely)} thing(s) likely to become a problem" if likely
                    else "Nothing is likely to become a problem on the rules AMP checks")
        if risks:
            headline += f"; {len(risks)} on the radar in all."
        else:
            headline += "."
    return {
        "generated_at": (now or datetime.utcnow()).isoformat(),
        "state": state,
        "headline": headline,
        "measured_rate_per_day": round(daily_rate, 1) if daily_rate is not None else None,
        "risks": risks,
        "note": ("Every risk here is a rule over measured data, and each says which rule and which "
                 "measurement produced it. None of it is a prediction from a trained model."),
    }
