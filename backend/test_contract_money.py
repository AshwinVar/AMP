"""Money, percentages and the SLA/credit evaluation of a statement (ADR-0021).

WHAT THIS PINS
--------------
AMP computes an SLA credit from the OEM-attributed downtime of a statement. It
never moves money, but the number it prints is the number two companies accept,
so it must be EXACT and it must never invent a result the data does not hold:

  * Decimal text in, Decimal text out. No float anywhere. Money is quantized to
    paise ONCE, with ROUND_HALF_UP, and rendered by exactly one function.
  * A tier comparison is integer cross-multiplication, not a comparison of
    rounded percentages: an availability that DISPLAYS as 97.00 but is below 97
    still earns the 97 tier's credit.
  * Percentages are intensive. No covered time, too little measured time, or no
    attributable time is `not_evaluable` with availability and credit None —
    never 0.
  * Disputed time makes the credit pending: the amount is None and the range
    brackets every way the disputes could resolve.

Run: DATABASE_URL="sqlite:///./ci.db" python test_contract_money.py
"""
import ast
import io
import os
import sys
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import canonical  # noqa: E402
import contract_money as cm  # noqa: E402
from contract_money import MoneyError  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def _refused(fn, *args):
    try:
        fn(*args)
    except MoneyError as e:
        return str(e)
    return None


def _terms(target="97.00", tiers=(("97.00", "5.00"), ("95.00", "10.00")),
           min_measured="90.00", fee="40000.00"):
    return SimpleNamespace(
        currency="INR", period_fee=Decimal(fee), sla_target_pct=Decimal(target),
        min_measured_pct=Decimal(min_measured),
        credit_tiers=tuple(SimpleNamespace(below_pct=Decimal(b), credit_pct=Decimal(c))
                           for b, c in tiers))


def _totals(available=0, oem=0, factory=0, disputed=0, unmeasured=0, covered=None):
    total = available + oem + factory + disputed + unmeasured
    return {"covered_seconds": total if covered is None else covered,
            "available_seconds": available, "oem_seconds": oem,
            "factory_seconds": factory, "disputed_seconds": disputed,
            "unmeasured_seconds": unmeasured}


# --------------------------------------------------------------------------
# 1. parsing and rendering decimal text
# --------------------------------------------------------------------------

def test_D_accepts_only_two_place_decimal_text():
    assert cm.D("40000.00") == Decimal("40000.00")
    assert cm.D("0.00") == Decimal("0")
    assert cm.D("999999999999.99") == Decimal("999999999999.99")
    refused = ["40000", "40000.0", "40000.000", "-1.00", "+1.00", " 1.00", "1.00 ",
               "1e3", "1.00\n", "", ".50", "1,000.00", "1000000000000.00",
               # Unicode digits: \d would accept these, and Decimal parses them.
               "١٢.٠٠", "１.00",
               "NaN", "Infinity"]
    for text in refused:
        assert _refused(cm.D, text), f"D accepted {text!r}"
    for value in (1.0, 1, Decimal("1.00"), None, True, b"1.00"):
        assert _refused(cm.D, value), f"D accepted {type(value).__name__}"
    print(f"PASS D: two-place ASCII decimal text only ({len(refused)} texts and 6 "
          "non-strings refused)")


def test_decimal_text_is_the_one_renderer():
    assert cm.decimal_text(Decimal("97.00")) == "97.00"
    assert cm.decimal_text(Decimal("97")) == "97.00"
    assert cm.decimal_text(Decimal("97.5")) == "97.50"
    assert cm.decimal_text(Decimal("0")) == "0.00"
    assert cm.decimal_text(Decimal("-0.00")) == "0.00"
    assert cm.decimal_text(Decimal("2000.01")) == "2000.01"
    assert cm.decimal_text(Decimal("1E+2")) == "100.00"
    for text in ("0.00", "12.34", "999999999999.99", "40000.10"):
        assert cm.decimal_text(cm.D(text)) == text
    for bad in (Decimal("97.001"), Decimal("-1.00"), Decimal("NaN"),
                Decimal("Infinity"), Decimal("1000000000000.00")):
        assert _refused(cm.decimal_text, bad), f"decimal_text rendered {bad!r}"
    for bad in (97.0, 97, "97.00", None, True):
        assert _refused(cm.decimal_text, bad), f"decimal_text rendered {bad!r}"
    print("PASS decimal_text: exact two places, refuses what it would have to round")


def test_quantize_money_is_round_half_up_to_paise():
    cases = {"2.005": "2.01", "2.004999": "2.00", "0.005": "0.01", "0.125": "0.13",
             "2000.005": "2000.01", "16.6665": "16.67", "7": "7.00"}
    for raw, want in cases.items():
        got = cm.decimal_text(cm.quantize_money(Decimal(raw)))
        assert got == want, (raw, got, want)
    for bad in (2.005, 2, "2.005"):
        assert _refused(cm.quantize_money, bad)
    print("PASS quantize_money: ROUND_HALF_UP (2.005 -> 2.01, where half-even gives 2.00)")


def test_pct_display_is_exact_half_up_and_none_without_a_denominator():
    assert cm.pct_display(1, 3) == "33.33"
    assert cm.pct_display(2, 3) == "66.67"
    assert cm.pct_display(1, 8) == "12.50"
    assert cm.pct_display(0, 5) == "0.00"
    assert cm.pct_display(5, 5) == "100.00"
    # 1/20000 = 0.005% exactly: half-up gives 0.01, half-even would give 0.00.
    assert cm.pct_display(1, 20000) == "0.01"
    # 5/20000 = 0.025%: half-up 0.03, half-even would give 0.02.
    assert cm.pct_display(5, 20000) == "0.03"
    # Very large integers stay exact (no float, no context rounding).
    big = 2 ** 52
    assert cm.pct_display(big - 1, big) == "100.00"
    assert cm.pct_display(0, 0) is None
    assert cm.pct_display(7, 0) is None
    for bad in ((True, 2), (1, True), (1.0, 2), (-1, 2), (1, -2)):
        assert _refused(cm.pct_display, *bad), f"pct_display accepted {bad!r}"
    print("PASS pct_display: exact integer half-up; no denominator is None, never 0")


# --------------------------------------------------------------------------
# 2. evaluate_sla: states in order
# --------------------------------------------------------------------------

def test_no_covered_time_is_not_evaluable():
    out = cm.evaluate_sla(_totals(), _terms())
    assert out["sla"]["state"] == "not_evaluable"
    assert out["sla"]["not_evaluable_reason"] == "no covered time"
    assert out["sla"]["availability_pct"] is None
    assert out["sla"]["measured_pct"] is None
    assert out["credit"]["amount"] is None and out["credit"]["credit_pct"] is None
    print("PASS no covered time: not_evaluable, availability None, credit None (not 0)")


def test_insufficient_measurement_is_an_exact_integer_comparison():
    # covered 10000, min 90.00%: 1000 unmeasured is exactly 90.00% measured.
    at_min = cm.evaluate_sla(_totals(available=9000, unmeasured=1000), _terms())
    assert at_min["sla"]["state"] == "met", at_min
    assert at_min["sla"]["measured_pct"] == "90.00"
    below = cm.evaluate_sla(_totals(available=8999, unmeasured=1001), _terms())
    assert below["sla"]["state"] == "not_evaluable"
    assert below["sla"]["not_evaluable_reason"] == "insufficient measurement"
    assert below["sla"]["availability_pct"] is None
    assert below["credit"]["amount"] is None
    assert below["credit"]["credit_pct"] is None
    # 89.995% measured DISPLAYS as 90.00 (half-up) but is below the minimum.
    tricky = cm.evaluate_sla(_totals(available=179990, unmeasured=20010), _terms())
    assert tricky["sla"]["measured_pct"] == "90.00", tricky["sla"]["measured_pct"]
    assert tricky["sla"]["state"] == "not_evaluable", tricky["sla"]
    # Everything unmeasured: not evaluable, and the machine did not "fail".
    dark = cm.evaluate_sla(_totals(unmeasured=86400), _terms())
    assert dark["sla"]["state"] == "not_evaluable"
    assert dark["credit"]["amount"] is None
    print("PASS min_measured_pct: exactly-at passes, one second below and a "
          "displays-as-90.00 case are not evaluable; credit None")


def test_no_attributable_time_is_not_evaluable():
    out = cm.evaluate_sla(_totals(factory=3600), _terms())
    assert out["sla"]["state"] == "not_evaluable"
    assert out["sla"]["not_evaluable_reason"] == "no attributable time"
    assert out["sla"]["base_seconds"] == 0
    assert out["sla"]["availability_pct"] is None
    assert out["credit"]["amount"] is None
    print("PASS base == 0: not_evaluable 'no attributable time', availability None")


def test_a_tier_boundary_does_not_credit_one_second_below_does():
    exactly = cm.evaluate_sla(_totals(available=9700, oem=300), _terms())
    assert exactly["sla"]["availability_pct"] == "97.00"
    assert exactly["sla"]["state"] == "met"
    assert exactly["credit"]["credit_pct"] == "0.00"
    assert exactly["credit"]["amount"] == "0.00"
    assert exactly["credit"]["tier_below_pct"] is None
    below = cm.evaluate_sla(_totals(available=9699, oem=301), _terms())
    assert below["sla"]["availability_pct"] == "96.99"
    assert below["sla"]["state"] == "breached"
    assert below["credit"]["tier_below_pct"] == "97.00"
    assert below["credit"]["credit_pct"] == "5.00"
    assert below["credit"]["amount"] == "2000.00"
    print("PASS tier boundary: exactly 97.00% earns nothing, 96.99% earns 5%")


def test_displays_as_the_target_but_is_below_it_still_credits():
    # 969999/1000000 = 96.9999% -> displays 97.00, is below 97.
    out = cm.evaluate_sla(_totals(available=969_999, oem=30_001), _terms())
    assert out["sla"]["availability_pct"] == "97.00", out["sla"]
    assert out["sla"]["state"] == "breached"
    assert out["credit"]["credit_pct"] == "5.00"
    print("PASS 96.9999% displays 97.00 and still breaches and credits (integer compare)")


def test_the_largest_matching_credit_applies():
    out = cm.evaluate_sla(_totals(available=9400, oem=600), _terms())
    assert out["sla"]["availability_pct"] == "94.00"
    assert out["credit"]["tier_below_pct"] == "95.00"
    assert out["credit"]["credit_pct"] == "10.00"
    assert out["credit"]["amount"] == "4000.00"
    # Breached with no tier matching: a measured zero credit, not None.
    gap = _terms(tiers=(("90.00", "5.00"),))
    mid = cm.evaluate_sla(_totals(available=9600, oem=400), gap)
    assert mid["sla"]["state"] == "breached"
    assert mid["credit"]["credit_pct"] == "0.00" and mid["credit"]["amount"] == "0.00"
    print("PASS deepest tier wins; a breach no tier covers credits 0.00 (evaluated)")


def test_credit_amount_rounds_half_up_to_paise():
    out = cm.evaluate_sla(_totals(available=9000, oem=1000), _terms(fee="40000.10"))
    assert out["credit"]["credit_pct"] == "10.00"
    assert out["credit"]["amount"] == "4000.01", out["credit"]      # 4000.010
    half = cm.evaluate_sla(_totals(available=9600, oem=400),
                           _terms(fee="40000.10"))
    assert half["credit"]["credit_pct"] == "5.00"
    assert half["credit"]["amount"] == "2000.01", half["credit"]    # 2000.005
    tiny = cm.evaluate_sla(_totals(available=9600, oem=400), _terms(fee="0.10"))
    assert tiny["credit"]["amount"] == "0.01", tiny["credit"]         # 0.005
    print("PASS credit = fee x pct / 100, quantized once, ROUND_HALF_UP (2000.005 -> 2000.01)")


def test_factory_time_is_excluded_from_the_base():
    out = cm.evaluate_sla(_totals(available=9000, oem=500, factory=500), _terms())
    assert out["sla"]["base_seconds"] == 9500
    assert out["sla"]["availability_pct"] == "94.74"   # 9000/9500
    unmeasured = cm.evaluate_sla(_totals(available=9000, oem=500, unmeasured=500),
                                 _terms())
    assert unmeasured["sla"]["base_seconds"] == 9500
    assert unmeasured["sla"]["measured_pct"] == "95.00"
    print("PASS base = covered - unmeasured - factory; unmeasured never counts as up or down")


def test_disputes_make_the_credit_pending_with_an_honest_range():
    out = cm.evaluate_sla(_totals(available=9500, oem=200, disputed=300), _terms())
    sla, credit = out["sla"], out["credit"]
    assert sla["state"] == "pending_disputes"
    assert sla["availability_pct"] is None
    assert credit["amount"] is None and credit["credit_pct"] is None
    assert sla["availability_if_disputes_oem"] == "95.00"          # 9500/10000
    assert sla["availability_if_disputes_factory"] == "97.94"      # 9500/9700
    assert sla["availability_if_disputes_available"] == "98.00"    # 9800/10000
    # range_max: every disputed second resolves OEM (95.00% -> 5%; not < 95).
    assert credit["range_max"] == "2000.00", credit
    # range_min: every disputed second resolves AVAILABLE (98.00% -> met).
    assert credit["range_min"] == "0.00", credit
    print("PASS pending_disputes: amount None; range spans all-OEM to all-AVAILABLE")


def test_all_attributable_time_disputed():
    out = cm.evaluate_sla(_totals(disputed=3600, factory=100), _terms())
    sla, credit = out["sla"], out["credit"]
    assert sla["state"] == "pending_disputes"
    assert sla["availability_if_disputes_factory"] is None      # base == D
    assert sla["availability_if_disputes_oem"] == "0.00"
    assert sla["availability_if_disputes_available"] == "100.00"
    assert credit["range_max"] == "4000.00"
    assert credit["range_min"] == "0.00"
    print("PASS base == D: availability-if-factory None; range still bounded")


def test_totals_must_add_up_and_be_plain_ints():
    assert _refused(cm.evaluate_sla, _totals(available=10, covered=11), _terms())
    bad = _totals(available=10)
    bad["oem_seconds"] = True
    assert _refused(cm.evaluate_sla, bad, _terms())
    neg = _totals(available=10)
    neg["oem_seconds"] = -1
    neg["covered_seconds"] = 9
    assert _refused(cm.evaluate_sla, neg, _terms())
    missing = _totals(available=10)
    del missing["disputed_seconds"]
    assert _refused(cm.evaluate_sla, missing, _terms())
    print("PASS evaluate_sla refuses totals that do not add up, bools, negatives, gaps")


def test_output_is_canonical_ready():
    for totals in (_totals(), _totals(available=9699, oem=301),
                   _totals(available=9500, oem=200, disputed=300),
                   _totals(unmeasured=5)):
        out = cm.evaluate_sla(totals, _terms())
        canonical.canonical_bytes(out)      # raises on any Decimal/float/bool
    print("PASS evaluate_sla output has a canonical form in every state")


# --------------------------------------------------------------------------
# 3. structural: no float in contract_money
# --------------------------------------------------------------------------

def test_contract_money_contains_no_float():
    path = os.path.join(HERE, "contract_money.py")
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    nodes = list(ast.walk(tree))
    functions = {n.name for n in nodes if isinstance(n, ast.FunctionDef)}
    for required in ("D", "decimal_text", "quantize_money", "pct_display",
                     "evaluate_sla"):
        assert required in functions, f"structural scan did not find {required}"
    # '/' is refused outright: on two ints it yields a float, and the AST cannot
    # see types. The module divides with // on integers and scaleb on Decimals.
    floats = [n.lineno for n in nodes
              if (isinstance(n, ast.Constant) and type(n.value) is float)
              or (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                  and n.func.id == "float")
              or (isinstance(n, (ast.BinOp, ast.AugAssign))
                  and isinstance(n.op, ast.Div))]
    assert not floats, f"float literal, float() or '/' at lines {floats}"
    print(f"PASS contract_money.py: {len(functions)} functions found; no float "
          "literal, float() call or '/' division")


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"ALL {len(tests)} CONTRACT MONEY TESTS PASSED")
