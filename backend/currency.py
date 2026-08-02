"""The one place the platform's currency symbol is written down.

Before this module the product printed a single tenant's money in TWO currencies.
The cost-of-losses family was dollars — ai/cost.py's rates carried "# $ lost per
minute" comments and every consumer rendered f"${...}" — while the unit-value
family was pounds: models.py's column is literally `unit_value_gbp`, briefing.py
emitted "£.../yr to recover", and lib/modules.ts even used "£" as the Costing nav
icon, directly above a card whose first three stats were all "$". No conversion,
no FX rate, no currency column existed anywhere.

The two collided in one document and on one screen. The weekly report emits a "$"
Cost-of-losses section and then a "£.../yr to recover" alert; the always-rendered
overview column puts ScorecardStrip's "$ Cost of losses" about sixty lines above
RecoverySnapshot's "£ / good unit". Nothing had to be configured to see it.

ADR-0010 settles which one is right rather than leaving it to taste: its accepted
decision is "one configurable per-tenant rate — the margin per good unit", the
column is `unit_value_gbp`, and it warns in as many words against figures that
"silently disagree". So GBP is canonical and the dollar literals were the defect.

Everything money-shaped imports from here, and test_currency_single.py fails the
build if a bare symbol reappears in a money surface or if the frontend's
lib/money.ts drifts from this value.

Deliberately NOT a per-tenant currency yet. That is a real feature — a column, a
PATCH validator, a payload key on every read-model, and a formatter threaded
through both stacks — and it is not what was broken. Centralising first makes it
a one-line change here later instead of a 20-site sweep again.
"""

CURRENCY = "£"


def money(n) -> str:
    """Format a whole-currency amount: money(49740) -> '£49,740'."""
    return f"{CURRENCY}{n:,}"


def signed_money(n) -> str:
    """Format a delta, sign always shown: signed_money(-500) -> '£-500'.

    Matches the shape the f"${x:+,}" call sites produced, so the trend verdict
    strings read exactly as before apart from the symbol.
    """
    return f"{CURRENCY}{n:+,}"
