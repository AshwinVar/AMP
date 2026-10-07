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

CURRENCY = "₹"


# ── Grouping is part of the currency, not a detail ────────────────────
#
# Python's `f"{n:,}"` groups in thousands: 1400000 -> "1,400,000". An Indian
# plant's own invoices, ledgers and the spreadsheet this platform is replacing
# group in lakhs: "14,00,000". The platform prints rupees, so it groups the way
# the reader counts.
#
# This also puts the two stacks back in step. lib/money.ts formats with en-IN —
# as lib/money.ts's contract-money section has always done, on the stated
# grounds that INR groups in lakhs — so above six digits the backend and the
# frontend had been printing the same figure differently. The mirror check could
# not see it: it compares ₹49,740, which groups identically either way.
def group(n) -> str:
    """Indian digit grouping of a whole number: 1234567 -> '12,34,567'.

    Last three digits, then pairs. Sign is handled by the caller so this stays a
    pure grouping of digits.
    """
    digits = str(abs(int(n)))
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    pairs = []
    while len(head) > 2:
        pairs.insert(0, head[-2:])
        head = head[:-2]
    if head:
        pairs.insert(0, head)
    return ",".join(pairs + [tail])


def money(n) -> str:
    """Format a whole-currency amount: money(49740) -> '₹49,740',
    money(1400000) -> '₹14,00,000'."""
    sign = "-" if int(n) < 0 else ""
    return f"{CURRENCY}{sign}{group(n)}"


def signed_money(n) -> str:
    """Format a delta, sign always shown: signed_money(-500) -> '₹-500',
    signed_money(500) -> '₹+500'.

    Keeps the shape the f"${x:+,}" call sites produced, so the trend verdict
    strings read as before apart from the symbol and the grouping.
    """
    sign = "-" if int(n) < 0 else "+"
    return f"{CURRENCY}{sign}{group(n)}"


def unit_rate(n) -> str:
    """Format a per-unit rate, keeping paise when it has them: unit_rate(12) ->
    '₹12', unit_rate(2.5) -> '₹2.50'. A tenant's unit value is a Float column, and
    money() would print 2.5 as '₹2'.

    Converted to whole paise FIRST, then split. Grouping the integer part and
    formatting the fraction separately loses the carry: 0.995 rounds to '1.00'
    paise-side while the integer part is still 0, printing ₹0.00 for a rate of
    almost one rupee.
    """
    value = float(n)
    if value.is_integer():
        return money(int(value))
    sign = "-" if value < 0 else ""
    whole, paise = divmod(int(round(abs(value) * 100)), 100)
    return f"{CURRENCY}{sign}{group(whole)}.{paise:02d}"
