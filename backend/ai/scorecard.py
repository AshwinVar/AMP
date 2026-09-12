"""Executive scorecard — the plant on one line (ADR-0007).

One headline KPI per pillar — OEE, good rate, delivery reliability, and the cost
of losses — each with a tone (good / warn / bad) and a change vs the prior 7 days,
so the exec home leads with the numbers that matter and their direction, not a
wall of cards. Composes the pillar read-models only; auto-scoped to the tenant
(ADR-0002); it adds no storage.
"""
from datetime import datetime, timedelta

import models
from analytics_engine import oee_direction
from ai.oee import build_oee_summary
from ai.production import build_production_summary
from ai.delivery import build_delivery_summary
from ai.cost import build_cost_summary, downtime_minutes, DOWNTIME_COST_PER_MIN, SCRAP_COST_PER_UNIT
from currency import CURRENCY
from ai.twin import _oee_from_records, _recent_production
import oee_contract

name = "scorecard"

WINDOW_DAYS = 7


def _tone(value, good, warn, higher_is_better=True) -> str:
    """A KPI's health band. Higher-is-better metrics are good above `good`, warn
    above `warn`, else bad; the flag flips the comparison for cost-like metrics."""
    if value is None:
        return "none"
    if higher_is_better:
        return "good" if value >= good else "warn" if value >= warn else "bad"
    return "good" if value <= good else "warn" if value <= warn else "bad"


def _period_kpis(records) -> dict:
    """OEE, good rate and loss cost over a set of production records — the three
    windowed KPIs, computed the same way as the live cards so the current and
    prior periods compare like-for-like."""
    total = sum(r.total_count or 0 for r in records)
    good = sum(r.good_count or 0 for r in records)
    rejected = sum(r.rejected_count or 0 for r in records)
    # Same per-record downtime basis as the live cost card, so the current period
    # (from build_cost_summary) and this prior period compare like-for-like.
    downtime_min = downtime_minutes(records)
    return {
        "has": bool(records),
        "oee": _oee_from_records(records)["oee"],
        "good_rate": round(good / total * 100) if total else 0,
        "loss_cost": downtime_min * DOWNTIME_COST_PER_MIN + rejected * SCRAP_COST_PER_UNIT,
    }


def _prior_records(db, current):
    """Production records from the 7 days immediately before `current`.

    Derived from the SAME anchor as the current half, so the two tile exactly
    (oee_contract.prior_window). This used to be hand-rolled from calendar
    midnights -- [midnight(today-13), midnight(today-6)) -- against a ROLLING
    current window, which overlapped it by up to 24 hours and let a week be
    compared against itself. See test_week_halves_tile.py.

    Bounded in SQL -- the table grows continuously."""
    return _recent_production(db, days=current.days,
                              now=oee_contract.prior_window(current).end)


def _delta(cur, prior, has_prior, lower_is_better=False):
    """Signed change vs the prior period and its tone (good/bad/flat), or
    (None, None) when there's no prior period to compare against."""
    if not has_prior or cur is None:
        return None, None
    d = cur - prior
    if d == 0:
        return 0, "flat"
    improved = (d < 0) if lower_is_better else (d > 0)
    return d, "good" if improved else "bad"


def build_scorecard(db, tenant: str) -> dict:
    """Four headline KPIs — OEE, good rate, delivery reliability, cost of losses —
    each with a tone and a change vs the prior 7 days, composed from the pillar
    read-models (ADR-0007). Delivery reliability is order-state based, so it has
    no weekly delta."""
    # ONE anchor for the whole request: the headline and the "last week" it is
    # subtracted from must abut, not be built from two separate utcnow() calls.
    window = oee_contract.OeeWindow(WINDOW_DAYS)
    oee = build_oee_summary(db, tenant, now=window.end)["plant"]
    prod = build_production_summary(db, tenant)
    delivery = build_delivery_summary(db, tenant)
    cost = build_cost_summary(db, tenant)
    prior = _period_kpis(_prior_records(db, window))

    # Delivery reliability — of the orders that have come due (delivered or late),
    # the share actually delivered. Reuses the delivery read-model's own definition
    # so the scorecard, the delivery summary and the per-customer drill-down all
    # reconcile. This is NOT an on-time rate: customer_orders carries no dispatch
    # timestamp, so punctuality isn't computable, and not-yet-due (on-track /
    # at-risk) orders are held out of the denominator rather than counted as
    # successes. None when no order has come due yet -> the strip shows "—".
    reliability = delivery["reliability_rate"] if delivery["resolved"] else None
    # OEE week-over-week uses the SHARED direction (with its dead-band), so a small
    # move can't read "down"/red here while the recovery card's badge says "flat".
    if prior["has"] and oee["oee"] is not None:
        oee_d = oee["oee"] - prior["oee"]
        oee_dt = {"up": "good", "down": "bad", "flat": "flat"}[oee_direction(oee["oee"], prior["oee"])]
    else:
        oee_d, oee_dt = None, None
    good_d, good_dt = _delta(prod["good_rate"], prior["good_rate"], prior["has"])
    cost_d, cost_dt = _delta(cost["loss_cost"], prior["loss_cost"], prior["has"], lower_is_better=True)

    # A KPI WITH NO DATA UNDER IT PUBLISHES None, NOT ZERO.
    #
    # Zero means "measured, and it was zero". None means "nothing to measure",
    # and the strip already renders it as an em dash -- which is exactly what
    # delivery reliability below has always done when no order has come due.
    # Three of the four KPIs were outside that contract, so a plant that had not
    # run reported OEE 0% in RED, good rate 0% in RED, and cost of losses 0 in
    # GREEN: "it ran catastrophically, everything it made was scrap, and it
    # eliminated all its losses". None of that happened; the plant was idle.
    #
    # This is reachable in spite of the payload-level has_data below, because
    # that is an OR across three pillars: one pillar with data publishes the
    # whole strip, including the pillars that have none. A plant that dispatched
    # an order this week and produced nothing -- a shutdown, a holiday week, a
    # tenant mid-onboarding -- is precisely that case. See
    # test_scorecard_no_data.py.
    # Each KPI asks ITS OWN pillar, and asks the right question of it. A good
    # RATE needs units inspected, not rows: `runs > 0` is the count-the-rows rule
    # oee_contract.is_measurable was written to replace ("a row that recorded
    # nothing satisfies all four and satisfies none of the definitions"), and a
    # production record with total_count 0 is exactly that row. Mutation testing
    # earned this: swapping OEE's basis for `prod["runs"] > 0` survived until a
    # fixture held a row that recorded nothing.
    measured_oee = oee["has_data"]
    measured_prod = prod["total"] > 0
    measured_cost = cost["has_data"]

    def _kpi(value, measured, lo=None, hi=None, tone=None):
        """value+tone when the pillar was measured; (None, "none") when it was not.

        The tone is the STRING "none", not Python None: that is the existing
        contract `_tone(None, ...)` already returns for delivery reliability, it
        is in ScorecardStrip's `toneCls` map (slate, i.e. uncoloured) and in its
        TS union. Returning Python None here would have rendered
        `toneCls[null]` -> undefined and put the literal class "undefined" on the
        element -- a new convention where one already existed."""
        if not measured:
            return None, _tone(None, lo, hi)
        return value, (tone if tone is not None else _tone(value, lo, hi))

    oee_v, oee_tone = _kpi(oee["oee"], measured_oee, 85, 70)
    good_v, good_tone = _kpi(prod["good_rate"], measured_prod, 98, 95)
    cost_v, cost_tone = _kpi(cost["loss_cost"], measured_cost,
                             tone=("good" if cost["loss_cost"] == 0 else "warn"))

    kpis = [
        {"key": "oee", "label": "Plant OEE", "value": oee_v, "unit": "%",
         "tone": oee_tone, "delta": oee_d, "delta_tone": oee_dt},
        {"key": "good_rate", "label": "Good rate", "value": good_v, "unit": "%",
         "tone": good_tone, "delta": good_d, "delta_tone": good_dt},
        # key stays "on_time" so the strip still drills into the orders view; the
        # displayed label and value are the honest delivery-reliability number.
        {"key": "on_time", "label": "Delivery reliability", "value": reliability, "unit": "%",
         "tone": _tone(reliability, 95, 85), "delta": None, "delta_tone": None},
        # `unit` is the DISPLAY token four consumers branch on to decide prefix-vs-suffix
        # formatting: ai/report.py, ai/assistant.py and frontend ScorecardStrip.tsx all
        # test it against the currency symbol. It must come from currency.CURRENCY, not a
        # literal, or the strip silently falls through to suffix formatting ("49740£").
        {"key": "loss_cost", "label": "Cost of losses", "value": cost_v, "unit": CURRENCY,
         "tone": cost_tone, "delta": cost_d, "delta_tone": cost_dt},
    ]
    return {
        "has_data": oee["has_data"] or prod["runs"] > 0 or delivery["total"] > 0,
        "kpis": kpis,
    }
