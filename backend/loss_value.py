"""Valuing a loss: good units not made, times the tenant's own rate (ADR-0010).

ONE conversion, used by every surface that turns downtime or scrap into money:
the cost-of-losses read-model and trend, the scorecard, and the management
summary's downtime loss.

  * A scrapped unit is one good unit not made.
  * A minute of downtime is valued at the observed RUN RATE: good units per minute
    of run time over the same records. That is what the line was producing while
    it ran, so it is what the stopped minutes would have produced.
  * Money is those units times TenantConfig.unit_value_gbp. No rate means no £,
    ever: a tenant without a rate sees units. A rate of 0 is a real £0.

It replaces two made-up tariffs. ai/cost.py priced downtime at £12 a minute and
scrap at £25 a unit for every tenant, and build_management_summary fell back to
£8 a minute when no rate was set. A plant with a £2 margin and one with a £400
margin were shown the same money. See test_loss_money_needs_the_tenant_rate.py.

Deliberately a leaf module (no model or read-model imports) so analytics_engine
and the ai read-models can both import it without a cycle.
"""
import math


def run_rate(good, runtime_minutes):
    """Good units per minute of run time, or None when nothing ran."""
    if not runtime_minutes or runtime_minutes <= 0:
        return None
    return (good or 0) / runtime_minutes


def downtime_units(downtime_minutes, rate):
    """The good units the downtime would have produced at `rate`, as a float.

    0.0 when there was no downtime. None when there was downtime but no run rate
    to convert it: a plant that never ran lost an unknown amount, not nothing."""
    if not downtime_minutes:
        return 0.0
    if rate is None:
        return None
    return downtime_minutes * rate


def whole(x):
    """Round half up to an int (Python's round() is banker's); None stays None."""
    return None if x is None else int(math.floor(x + 0.5))


def value(units, unit_value):
    """whole(units x the tenant's £ per good unit), or None if either is unknown."""
    if units is None or unit_value is None:
        return None
    return whole(units * unit_value)


def apportion(total, parts):
    """Round a {key: float} breakdown to ints that sum EXACTLY to `total`.

    Largest remainder: floor every part, then hand the units still owed to the
    parts with the largest fractional remainders (ties by key order). A headline
    and the bars under it are rounded from the same floats, and rounding each on
    its own lets the bars miss the headline by a unit or a pound. A reader adds
    them up.

    `total` is whole(sum(parts)) at every call site, so what is owed is never
    negative and never more than one per part."""
    floors = {k: math.floor(v) for k, v in parts.items()}
    owed = total - sum(floors.values())
    order = sorted(parts, key=lambda k: parts[k] - floors[k], reverse=True)
    for k in order[:owed]:
        floors[k] += 1
    return floors
