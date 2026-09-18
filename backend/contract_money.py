"""Money, percentages and the SLA/credit evaluation of a statement (ADR-0021).

WHAT THIS MODULE IS
-------------------
A downtime attribution statement reports availability against the contract's SLA
target and the credit the contract's own schedule gives for the OEM-attributed
downtime. AMP COMPUTES that figure. It never invoices, charges or moves money,
and it does no usage or pay-per-output billing (a later phase, out of scope).

The attribution is the differentiator, not the arithmetic. Metering and shared
usage ledgers already exist (SteamChain, PayperChain, Linxfour, Rockwell
US10747201B2) and nothing here claims novelty for them. A freedom-to-operate
review is needed before commercial launch.

THE RULES, EACH IN ONE PLACE
----------------------------
    D(text)             the only parser of decimal text: ^[0-9]{1,12}\\.[0-9]{2}$
    decimal_text(d)     the only renderer of a Decimal as text (exactly 2 places)
    quantize_money(d)   the only rounding of money: 0.01, ROUND_HALF_UP
    pct_display(n, d)   the only rounding of a percentage: 0.01, half-up, exact
                        integer arithmetic; no denominator -> None, never "0.00"
    evaluate_sla(...)   the SLA state machine and the credit

There is no float anywhere in this module (test_contract_money asserts it, and
refuses '/' outright because int / int is a float). Money never lives in a
Numeric/Float column either: SQLite stores NUMERIC as REAL.

THE SLA, IN ORDER (seconds, all integers)
-----------------------------------------
    U A O F D   unmeasured, available, OEM, factory, disputed
    covered     U + A + O + F + D
    base        covered - U - F       (factory stops and no-data are not the
                                       machine's availability to lose)

    1. covered == 0                                   not_evaluable "no covered time"
    2. (covered - U) * 10000 < min_measured_h * covered
                                                      not_evaluable "insufficient measurement"
    3. base == 0                                      not_evaluable "no attributable time"
    4. D > 0                                          pending_disputes
    5. otherwise availability = (base - O) / base,    met | breached

A tier applies when availability < below_pct, decided by integer
cross-multiplication, (base - O) * 10000 < below_h * base, never by comparing
rounded percentages: 96.9999% displays as 97.00 and is still below 97.

PENDING DISPUTES. The amount is None and the statement cannot be accepted. The
credit range brackets every way the disputed seconds can resolve: range_max when
all resolve OEM (lowest availability), range_min when all resolve AVAILABLE
(highest). Resolving to FACTORY or UNMEASURED lands between them, unless it
makes the period not evaluable, in which case there is no credit at all.
`availability_if_disputes_factory` is None when every attributable second is
disputed, because that outcome leaves no base.

Run the tests: DATABASE_URL="sqlite:///./ci.db" python backend/test_contract_money.py
"""
import re
from decimal import ROUND_HALF_UP, Decimal, localcontext

# ASCII digits only: \d would accept Arabic-Indic and full-width digits, and
# Decimal() parses them happily.
DECIMAL_TEXT = re.compile(r"\A[0-9]{1,12}\.[0-9]{2}\Z")

CENT = Decimal("0.01")

# Enough significant digits that no operation here is ever rounded by the
# context: 12 integer digits x 5 digits of percentage, with room to spare.
_PRECISION = 60

NOT_EVALUABLE = "not_evaluable"
PENDING_DISPUTES = "pending_disputes"
MET = "met"
BREACHED = "breached"
SLA_STATES = (NOT_EVALUABLE, PENDING_DISPUTES, MET, BREACHED)

# The per-bucket totals a statement carries, in seconds.
TOTAL_KEYS = ("covered_seconds", "available_seconds", "oem_seconds",
              "factory_seconds", "disputed_seconds", "unmeasured_seconds")


class MoneyError(ValueError):
    """A value cannot be parsed, rendered or evaluated exactly."""


def D(text):
    """Parse decimal text with exactly two places. The only parser."""
    if type(text) is not str or DECIMAL_TEXT.match(text) is None:
        raise MoneyError(
            f"{text!r} is not decimal text with 1-12 digits and exactly 2 places")
    return Decimal(text)


def decimal_text(d):
    """Render a Decimal as text with exactly two places. The only renderer.

    Refuses rather than rounds: a value with a third significant place has not
    been quantized, and silently rounding it here would give the text and the
    arithmetic two different numbers."""
    if type(d) is not Decimal:
        raise MoneyError(f"decimal_text renders a Decimal, not {type(d).__name__}")
    if not d.is_finite():
        raise MoneyError(f"decimal_text: {d!r} is not finite")
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        hundredths = d.scaleb(2)
        if hundredths != hundredths.to_integral_value():
            raise MoneyError(
                f"decimal_text: {d!r} has more than two places; quantize it first")
        n = int(hundredths)
    if n < 0:
        raise MoneyError(f"decimal_text: {d!r} is negative")
    text = f"{n // 100}.{n % 100:02d}"
    if DECIMAL_TEXT.match(text) is None:
        raise MoneyError(f"decimal_text: {d!r} has more than 12 integer digits")
    return text


def quantize_money(d):
    """Quantize money to 0.01 with ROUND_HALF_UP. The only rounding of money."""
    if type(d) is not Decimal:
        raise MoneyError(f"quantize_money takes a Decimal, not {type(d).__name__}")
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return d.quantize(CENT, rounding=ROUND_HALF_UP)


def _count(value, name):
    if type(value) is not int or value < 0:
        raise MoneyError(f"{name} must be a non-negative int, not {value!r}")
    return value


def _hundredths_half_up(num, den):
    """round_half_up(num / den * 100, 2 places) as an integer of hundredths.

    Exact for any size of integer: floor((2 * num * 10000 + den) / (2 * den))."""
    return (2 * num * 10000 + den) // (2 * den)


def pct_display(num, den):
    """num/den as a percentage with two places, ROUND_HALF_UP; None if den == 0.

    The only rounding of a percentage. It is for DISPLAY: every decision
    (breach, tier, minimum measured) compares the exact integers instead."""
    _count(num, "numerator")
    _count(den, "denominator")
    if den == 0:
        return None
    return decimal_text(Decimal(_hundredths_half_up(num, den)).scaleb(-2))


def _pct_hundredths(pct):
    """A two-place percentage Decimal as an integer of hundredths (97.00 -> 9700)."""
    if type(pct) is not Decimal:
        raise MoneyError(f"percentage must be a Decimal, not {type(pct).__name__}")
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        scaled = pct.scaleb(2)
        if scaled != scaled.to_integral_value():
            raise MoneyError(f"percentage {pct!r} has more than two places")
        return int(scaled)


def _below(num, den, pct):
    """num/den < pct%, exactly: num * 10000 < pct_hundredths * den (den > 0)."""
    return num * 10000 < _pct_hundredths(pct) * den


def _credit_for(num, den, terms):
    """(tier_below_pct, credit_pct) for availability num/den: the largest credit
    among the tiers the availability is below; (None, 0.00) if none applies."""
    best = None
    for tier in terms.credit_tiers:
        if _below(num, den, tier.below_pct):
            if best is None or tier.credit_pct > best.credit_pct:
                best = tier
    if best is None:
        return None, Decimal("0.00")
    return best.below_pct, best.credit_pct


def _amount(terms, credit_pct):
    """period_fee x credit_pct / 100, quantized once. scaleb(-2) is the exact
    division by 100 on a Decimal."""
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return quantize_money((terms.period_fee * credit_pct).scaleb(-2))


def _read_totals(totals):
    try:
        values = {k: _count(totals[k], k) for k in TOTAL_KEYS}
    except KeyError as e:
        raise MoneyError(f"totals is missing {e.args[0]}") from None
    parts = sum(values[k] for k in TOTAL_KEYS[1:])
    if parts != values["covered_seconds"]:
        raise MoneyError(
            f"totals do not add up: buckets sum to {parts}, covered is "
            f"{values['covered_seconds']}")
    return values


def evaluate_sla(totals, terms):
    """The SLA and credit blocks of a statement, canonical-ready (text, int, None).

    `totals` holds TOTAL_KEYS as integer seconds. `terms` provides currency,
    period_fee, sla_target_pct, min_measured_pct and credit_tiers (each with
    below_pct and credit_pct), all Decimals from D()."""
    t = _read_totals(totals)
    covered = t["covered_seconds"]
    unmeasured = t["unmeasured_seconds"]
    oem = t["oem_seconds"]
    disputed = t["disputed_seconds"]
    base = covered - unmeasured - t["factory_seconds"]

    sla = {
        "target_pct": decimal_text(terms.sla_target_pct),
        "min_measured_pct": decimal_text(terms.min_measured_pct),
        "measured_pct": pct_display(covered - unmeasured, covered),
        "base_seconds": base,
        "availability_pct": None,
        "availability_if_disputes_oem": None,
        "availability_if_disputes_factory": None,
        "availability_if_disputes_available": None,
        "state": None,
        "not_evaluable_reason": None,
    }
    credit = {
        "currency": terms.currency,
        "period_fee": decimal_text(terms.period_fee),
        "tier_below_pct": None,
        "credit_pct": None,
        "amount": None,
        "range_min": None,
        "range_max": None,
    }

    def not_evaluable(reason):
        sla["state"] = NOT_EVALUABLE
        sla["not_evaluable_reason"] = reason
        return {"sla": sla, "credit": credit}

    if covered == 0:
        return not_evaluable("no covered time")
    if _below(covered - unmeasured, covered, terms.min_measured_pct):
        return not_evaluable("insufficient measurement")
    if base == 0:
        return not_evaluable("no attributable time")

    if disputed > 0:
        sla["state"] = PENDING_DISPUTES
        worst = base - oem - disputed                    # every dispute -> OEM
        best = base - oem                                # every dispute -> AVAILABLE
        sla["availability_if_disputes_oem"] = pct_display(worst, base)
        sla["availability_if_disputes_available"] = pct_display(best, base)
        # Every dispute -> FACTORY: disputed time leaves the base.
        sla["availability_if_disputes_factory"] = pct_display(worst, base - disputed)
        _, max_pct = _credit_for(worst, base, terms)
        _, min_pct = _credit_for(best, base, terms)
        credit["range_max"] = decimal_text(_amount(terms, max_pct))
        credit["range_min"] = decimal_text(_amount(terms, min_pct))
        return {"sla": sla, "credit": credit}

    up = base - oem
    sla["availability_pct"] = pct_display(up, base)
    sla["state"] = BREACHED if _below(up, base, terms.sla_target_pct) else MET
    tier_below, credit_pct = _credit_for(up, base, terms)
    credit["tier_below_pct"] = None if tier_below is None else decimal_text(tier_below)
    credit["credit_pct"] = decimal_text(credit_pct)
    credit["amount"] = decimal_text(_amount(terms, credit_pct))
    return {"sla": sla, "credit": credit}
