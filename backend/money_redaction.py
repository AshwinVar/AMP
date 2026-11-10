"""Backend money omission — a non-Admin never RECEIVES a rupee figure.

THE SERVER-SIDE HALF OF THE DASHBOARD MONEY GATE. Money on the dashboard — every
rupee figure: part prices, plant revenue, the cost of losses, recovery upside,
the scorecard's loss_cost KPI, SaaS MRR, the per-unit margin rate — is for
Admins. The front-end gate (frontend/lib/money-visibility.tsx, useCanSeeMoney)
already hides money for everyone else, but a UI that hides a value the API still
sends is not a gate: anyone can read the figure straight off the network tab. So
the figures are OMITTED HERE, at the API response boundary, and a non-Admin's
payload simply has no amount in it — which is exactly what the front-end treats
as "hidden". The two halves are defence in depth; this one is the authoritative one.

WHY A HELPER AND NOT A CHANGE TO THE READ-MODELS. The read-models (ai.cost,
ai.recovery, ai.scorecard, ...) are composed by one another and by the Copilot,
the weekly report and the factory simulator — callers that are NOT an HTTP
response to a human and MUST keep every figure (the scorecard reads
cost["unit_value_gbp"] to compute its own KPI; stripping it inside build_cost_summary
would break the scorecard). Redaction therefore happens once, at the HTTP boundary
of each money-returning endpoint, over the finished payload — never inside the
figure's source.

WHAT COUNTS AS ADMIN. The same role source as the rest of the write surface:
auth.require_roles(["Admin"]) gates writes on current_user["role"] == "Admin", and
can_see_money() asks the identical question. Supervisor, Operator and Viewer are
every one of them non-Admin and receive no money — even on an endpoint a
Supervisor may open (e.g. /analytics/management), because access to a page and
sight of its money are two different permissions.

HOW A FIGURE IS REMOVED. Three shapes, three treatments:

  * a named money field (``"loss_cost": 49740``, ``"revenue": ...``, ``"price_per_piece": ...``)
    is OMITTED — the key is deleted, so the amount is simply not there. This is
    the default and covers almost everything (MONEY_KEYS).

  * a name->figure MAP (``"by_type": {"Energy": 1200, ...}``) is omitted whole when
    it is a dict, because its VALUES are the money and its keys are arbitrary
    labels there is no point keeping without them (MONEY_MAP_KEYS). The same key
    as a LIST of ``{"type", "amount"}`` rows is left to the normal walk, which
    strips the ``amount`` inside and keeps the ``type`` labels.

  * a figure whose declared unit IS the currency — an ai.evidence.Fact
    ``{"value": 49740, "unit": "₹", ...}`` or a scorecard KPI
    ``{"value": .., "delta": .., "unit": "₹"}`` — is NULLED in place rather than
    removed, because the surrounding object (id / label / unit / provenance /
    tone / ...) is a required structure the front-end iterates and a Fact with no
    key at all would not round-trip. Only ``value`` and ``delta`` are nulled; the
    rest of the Fact stays.

  * finally, any STRING is scrubbed of an embedded rupee figure — the headline on
    the Command Centre ("Biggest measured loss: … (₹1,23,456)"), the cost-trend
    verdict ("Losses up ₹500 to ₹1,23,456…"), the Daily Brief's sentences — all
    compose money INTO prose, and a sentence is a way to receive a figure too.
    ``₹`` is the one centralised money symbol (currency.py, guarded by
    test_currency_single.py) and never appears except before an amount, so
    matching "₹" + digits is exact: a bare "₹" column label (no digits) is left
    alone, and no non-money text is ever touched.

Response-shaping only. Nothing here deletes a row, unpublishes a figure, or
changes what crosses the MQTT/wire — the amount still lives in the database and
reaches every internal consumer; a non-Admin's HTTP response just does not carry it.
"""
import re

from currency import CURRENCY

#: Response keys whose value is ALWAYS a rupee figure (a number, None, or a list
#: / dict of figures). Omitted by deletion for a non-Admin. Exact names, never
#: substrings: ``total_cost`` is money but ``total_cost_records`` is a COUNT, and
#: ``unit_value`` is money but ``signal_value`` / ``numeric_value`` /
#: ``measured_value`` are sensor readings — a substring match would redact those.
MONEY_KEYS = frozenset({
    # Plant board — shift rate + monthly rollup (ai/plant_board.py)
    "revenue", "total_revenue", "rate_per_hour", "total_rate_per_hour",
    # Cost of losses (ai/cost.py) and the costing rollup (costing_routes.py).
    # Bare ``cost`` is NOT here — it collides (a scalar loss figure vs the Command
    # Centre's ``cost`` BLOCK); it is handled by MONEY_FIGURE_OR_BLOCK below.
    "downtime_cost", "scrap_cost", "loss_cost", "machine_cost",
    "prior_cost", "delta_cost", "total_cost", "manual_cost_total",
    "cost_per_good_unit", "recorded_total", "amount",
    # Recovery opportunity (ai/recovery.py)
    "recoverable_value_window", "recoverable_value_per_year",
    "lever_recoverable_value_per_year",
    # Command Centre / Risk Radar — impact priced in money; the cost block's rate
    "impact_money", "unit_value",
    # The per-unit margin RATE (tenant_configs.unit_value_gbp), carried by the
    # cost / recovery / management payloads and read straight off the tenant config
    "unit_value_gbp",
    # Management summary (analytics_engine.build_management_summary)
    "estimated_loss_value", "lost_value",
    # Smart inventory shortage (ai/shortage.py)
    "money_at_risk",
    # Root-cause explorer (ai/root_cause.py) — numeric and money-formatted strings
    "money", "measured_loss_money", "attributed_money", "gap_money",
    # Part master (part_spec_routes.py) — the price that turns shots into rupees
    "price_per_piece", "price",
    # SaaS registry (saas_routes.py)
    "monthly_recurring_revenue",
})

#: Keys whose value, WHEN A MAPPING, is a {label: rupee-figure} map with nothing
#: non-money worth keeping once the figures go — omitted whole. The same key as a
#: LIST (``[{"type", "amount"}]``) is handled by the ordinary walk instead, which
#: keeps the labels and strips the ``amount``.
MONEY_MAP_KEYS = frozenset({"by_type", "by_department"})

#: A key that is a money FIGURE when its value is a scalar (ai/cost.py's by_line /
#: by_machine / daily rows, ai/twin.py's overlay: ``"cost": 49740``) but a
#: structural BLOCK with non-money siblings when its value is a dict/list (the
#: Command Centre's ``"cost": {state, priced, lost_units, facts, loss_cost, ...}``).
#: Scalar -> omit the figure; container -> descend, so the block's own money
#: (loss_cost, unit_value, the ₹ facts) is stripped and its units / state kept.
MONEY_FIGURE_OR_BLOCK = frozenset({"cost"})

#: The sibling figure fields of a currency-united object (Fact / KPI), nulled in
#: place when ``unit == CURRENCY`` so the object's required shape survives.
_FIGURE_SIBLINGS = ("value", "delta")

#: ``₹`` then an optional sign and a grouped number — the only shape
#: currency.money() / signed_money() ever emit. ``₹`` is money-only (currency.py),
#: so this matches a figure and never ordinary prose; a lone "₹" is left alone.
_MONEY_IN_TEXT = re.compile(r"₹\s?[+-]?\d[\d,]*(?:\.\d+)?")

#: What an embedded figure becomes, so the sentence still reads.
_REDACTED_FIGURE = CURRENCY + "—"  # ₹—


def can_see_money(current_user) -> bool:
    """True only for an Admin — the same role source as the write surface
    (auth.require_roles(["Admin"]) reads current_user["role"]). Supervisor,
    Operator and Viewer are all non-Admin and see no money. Fail-closed: a
    missing / malformed principal is not an Admin."""
    if not isinstance(current_user, dict):
        return False
    return current_user.get("role") == "Admin"


def _scrub_text(s: str) -> str:
    return _MONEY_IN_TEXT.sub(_REDACTED_FIGURE, s)


def _redact(obj):
    """A money-free copy of ``obj`` (see the module docstring for the four cases).
    Rebuilds rather than mutates, so a shared source structure — a module-level
    default, a cached read-model — is never scribbled on."""
    if isinstance(obj, dict):
        currency_united = obj.get("unit") == CURRENCY
        out = {}
        for key, value in obj.items():
            if key in MONEY_KEYS:
                continue                                  # omit the figure
            if key in MONEY_MAP_KEYS and isinstance(value, dict):
                continue                                  # omit the {label: figure} map
            if key in MONEY_FIGURE_OR_BLOCK:
                if isinstance(value, (dict, list)):
                    out[key] = _redact(value)             # a block: strip its money, keep the rest
                # else a scalar figure -> omit
                continue
            if currency_united and key in _FIGURE_SIBLINGS:
                out[key] = None                           # null the figure, keep the shape
                continue
            out[key] = _redact(value)
        return out
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_redact(v) for v in obj)
    if isinstance(obj, str):
        return _scrub_text(obj)
    return obj


def redact_money(payload, current_user):
    """Return ``payload`` with every rupee figure removed — unless the caller is
    an Admin, who receives it unchanged.

    Call it at the response boundary of a money-returning endpoint:
    ``return redact_money(ai.cost.build_cost_summary(db, tenant), current_user)``.
    Safe on any payload — a dict, a list, or one with no money in it (then an
    inert copy). Never mutates the input."""
    if can_see_money(current_user):
        return payload
    return _redact(payload)
