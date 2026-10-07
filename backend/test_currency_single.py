"""One tenant, one currency — the product must not print money two ways.

THE BUG. The cost-of-losses family was dollars and the unit-value family was
pounds, with no conversion and no currency column anywhere. ai/cost.py's rate
comments said "$ lost per minute" while the tenant's own rate column is
`unit_value_gbp`; the weekly report emitted a "$" Cost-of-losses section and then
a "£.../yr to recover" alert in the same markdown; and the always-rendered
overview column put ScorecardStrip's "$ Cost of losses" about sixty lines above
RecoverySnapshot's "£ / good unit". lib/modules.ts even used "£" as the Costing
nav icon directly above a card whose first three stats were "$".

ADR-0010 (accepted) makes one per-tenant £/good-unit rate the single money basis
and warns against figures that "silently disagree", so GBP is canonical and the
dollar literals were the defect.

The subtle part is a CROSS-STACK coupling. ai/scorecard.py ships the symbol as a
KPI's `unit` token, and four consumers branch on it to choose prefix-vs-suffix
formatting: ai/report.py, ai/assistant.py, and frontend ScorecardStrip.tsx (twice).
If the producer and a consumer disagree the money does not merely look
inconsistent — it falls through to the suffix branch and renders "49740£". So
these tests assert the RENDERED strings, not just the constant.

Run:  python backend/test_currency_single.py     (exit 0 = pass)
"""

import sys

# This suite PRINTS the currency symbol it guards (via CURRENCY), so it is the
# first to die on a cp1252 console. Same reason as the suites it pins below.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import io
import json
import os
import re
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from ai import assistant, cost, report, scorecard
from currency import CURRENCY, money, signed_money, unit_rate
from database import Base

HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(HERE, "..", "frontend")


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed_losses(db):
    """One machine with real downtime and scrap, so loss_cost is non-zero.

    Priced at the tenant's own unit value, ₹45 (ADR-0010: no rate, no money
    figure at all). This week:
    40 min down at 97 good / 440 run minutes ≈ 9 units, + 3 rejects = 12 units = ₹540.
    The number matters: a fixture with loss_cost == 0 would render "₹0" and still
    pass a naive "no $" assertion while telling us nothing about the formatting path.
    """
    now = datetime.utcnow()
    db.add(models.TenantConfig(tenant_code="DEFAULT", plan="Pro", unit_value_gbp=45))
    db.add(models.Machine(id=1, name="M1", status="Running", utilization=90, line="SMT"))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=440,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=97,
                                   rejected_count=3, created_at=now))
    db.add(models.ProductionRecord(machine_id=1, planned_minutes=480, runtime_minutes=420,
                                   ideal_cycle_time_seconds=30, total_count=100, good_count=90,
                                   rejected_count=10, created_at=now - timedelta(days=8)))
    db.commit()
    return db


def _read(path):
    with io.open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_the_two_stacks_agree_on_the_symbol():
    """backend/currency.py and frontend/lib/money.ts must declare the same symbol.

    They cannot be independent: the scorecard's `unit` token crosses the wire and
    ScorecardStrip compares it to its own constant.
    """
    ts = _read(os.path.join(FRONTEND, "lib", "money.ts"))
    m = re.search(r'export const CURRENCY = "(.+?)"', ts)
    assert m, "lib/money.ts must export a CURRENCY string constant"
    assert m.group(1) == CURRENCY, (
        f"frontend CURRENCY {m.group(1)!r} != backend {CURRENCY!r} — the scorecard "
        f"`unit` token would miss ScorecardStrip's comparison and render as a suffix")
    print("PASS backend and frontend declare the same currency symbol")


def test_the_two_stacks_agree_on_THE_GROUPING_TOO():
    """The same symbol is not the same format.

    The mirror check above compared ₹49,740, which groups identically whichever
    convention you use — so it stayed green while the two stacks printed the SAME
    FIGURE DIFFERENTLY above six digits. Python's f"{n:,}" groups in thousands
    ("14,00,000" vs "1,400,000") and lib/money.ts formats with en-IN, as its
    contract-money section always had, because INR groups in lakhs.

    A plant reading ₹1,400,000 in a report and ₹14,00,000 on the dashboard is
    being shown two numbers. Six digits is not an edge case for a plant turning
    over a few lakh a month, which is this platform's whole market.

    Pinned by example, at the boundaries where the conventions diverge, and
    asserted against the grouping lib/money.ts actually declares rather than
    against a second copy of the rule.
    """
    ts = _read(os.path.join(FRONTEND, "lib", "money.ts"))
    assert 'Intl.NumberFormat("en-IN"' in ts, (
        "lib/money.ts must group with en-IN; backend currency.group() groups in "
        "lakhs and the two must not diverge")

    # Below six digits the two conventions agree, which is exactly why the symbol
    # check could not catch this. Above it, they do not.
    assert money(999) == f"{CURRENCY}999"
    assert money(1000) == f"{CURRENCY}1,000"
    assert money(49740) == f"{CURRENCY}49,740"
    assert money(100000) == f"{CURRENCY}1,00,000", money(100000)
    assert money(1400000) == f"{CURRENCY}14,00,000", money(1400000)
    assert money(123456789) == f"{CURRENCY}12,34,56,789", money(123456789)
    assert money(-1400000) == f"{CURRENCY}-14,00,000", money(-1400000)
    assert signed_money(1400000) == f"{CURRENCY}+14,00,000", signed_money(1400000)

    # The paise path carries into the rupees. Computing the integer part and the
    # fraction separately printed ₹0.00 for a rate of almost one rupee.
    assert unit_rate(0.995) == f"{CURRENCY}1.00", unit_rate(0.995)
    assert unit_rate(2.5) == f"{CURRENCY}2.50", unit_rate(2.5)
    assert unit_rate(1234.56) == f"{CURRENCY}1,234.56", unit_rate(1234.56)
    assert unit_rate(123456.78) == f"{CURRENCY}1,23,456.78", unit_rate(123456.78)
    print("PASS both stacks group in lakhs, and the paise carry into the rupees")


def test_the_scorecard_unit_token_survives_every_consumer():
    """The coupled path, end to end: producer -> `unit` token -> each renderer.

    Asserting the RENDERED string is what makes this test able to fail. Comparing
    only `unit == CURRENCY` would pass even if report.py still tested for "$",
    because the mismatch shows up as suffix formatting, not as a wrong token.
    """
    db = _seed_losses(_fresh_session())
    kpis = {k["key"]: k for k in scorecard.build_scorecard(db, "DEFAULT")["kpis"]}
    loss = kpis["loss_cost"]
    assert loss["unit"] == CURRENCY, loss["unit"]
    assert loss["value"] > 0, "fixture must produce a real loss, else formatting is untested"

    line = report._kpi_line(loss)
    assert f"{CURRENCY}{loss['value']:,}" in line, line
    assert not line.startswith(f"- **Cost of losses**: {loss['value']}"), (
        f"money rendered as a bare number or suffix: {line!r}")
    assert "$" not in line, line

    # The delta magnitude takes a separate branch in both renderers.
    assert loss["delta"] is not None and loss["delta"] != 0, "need a delta to test its branch"
    assert f"{CURRENCY}{abs(loss['delta']):,}" in line, line
    print("PASS the scorecard currency token renders as a prefix in every consumer")


def test_the_nav_icon_follows_the_platform_currency():
    """modules.json is a money surface, and nothing was watching it.

    The Costing nav item's icon is a currency symbol, and it is served from the
    BACKEND manifest — not from lib/modules.ts, whose `icon: CURRENCY` is
    overridden by whatever GET /modules returns. So when the platform moved to
    rupees, every screen in the product kept a "£" in its sidebar: the one
    currency symbol a user sees on every single page was the one nothing checked.

    The sweep below scans money-rendering CODE. This is a data file, which is
    exactly why it was missed, so it is asserted by name here.
    """
    raw = _read(os.path.join(HERE, "modules.json"))
    others = {"£", "$", "€", "¥"} - {CURRENCY}
    found = sorted(c for c in others if c in raw)
    assert not found, (
        f"backend/modules.json carries {found} while the platform prints "
        f"{CURRENCY!r}. That file drives the nav, so the wrong symbol shows on "
        f"every screen in the product.")

    manifest = json.loads(raw)
    icons = {v["key"]: v.get("icon") for pack in manifest["packs"]
             for v in pack.get("views", [])}
    # Non-vacuity: a rename or a restructure must not make this a check of nothing.
    assert "costing" in icons, "the costing view vanished from modules.json"
    assert icons["costing"] == CURRENCY, (
        f"the Costing nav icon is {icons['costing']!r}, not {CURRENCY!r}")
    print(f"PASS the nav manifest carries {CURRENCY} and no other currency symbol")


def test_no_money_surface_prints_a_dollar():
    """Behavioural sweep over every money-rendering entry point."""
    db = _seed_losses(_fresh_session())
    rendered = {
        "cost summary details": [l["detail"] for l in cost.build_cost_summary(db, "DEFAULT")["losses"]],
        "cost trend verdict": [cost.build_cost_trend(db, "DEFAULT")["verdict"]],
        "weekly report": [report.build_weekly_report(db, "DEFAULT")["markdown"]],
        "assistant cost answer": [assistant._cost(db, "DEFAULT")[0]],
        "assistant digest": [assistant.digest(db, "DEFAULT")["digest"]],
    }
    for label, texts in rendered.items():
        for t in texts:
            assert "$" not in t, f"{label} still prints a dollar: {t!r}"
            assert CURRENCY in t, f"{label} printed no {CURRENCY} at all: {t!r}"
    print("PASS no money surface prints a dollar; all print %s" % CURRENCY)


def _frontend_currency_literals(rel, text):
    """(index, line number, line) for every frontend line that prints a literal "$".

    `${...}` inside a template literal is interpolation, not currency, so only a $
    IMMEDIATELY before one (`$${`) or before a digit counts there. Outside a template
    literal -- in JSX text -- a bare `${m.cost}` renders a "$" followed by the value.
    CostIntelCard printed every machine's loss that way and DigitalTwinSection its
    "Cost:" label, and the old check, which only looked for `$${` and `$<digit>`,
    passed both. Backtick-quoted segments are blanked before the JSX-text check."""
    found = []
    for line_no, ln in enumerate(text.splitlines(), 1):
        code = ln.split("//", 1)[0]
        outside_templates = re.sub(r"`[^`]*`", "``", code)
        if re.search(r"\$\$\{|\$\d", code) or re.search(r"\$\{|\$\d", outside_templates):
            found.append((len(found), line_no, ln.strip()))
    return found


def test_the_frontend_guard_catches_a_dollar_in_jsx_text():
    """The two lines this guard missed, verbatim, must now be caught; ordinary
    template interpolation and the shared formatter must not be."""
    missed = [
        "<span>${m.cost.toLocaleString()} (downtime ${m.downtime_cost.toLocaleString()})</span>",
        "<p>Cost: ${(machine ? costMap.get(machine.id) ?? 0 : 0).toLocaleString()}</p>",
    ]
    fine = [
        "title={`Downtime ${money(m.downtime_cost)}`}",
        "style={{ width: `${w}%` }}",
        "<p>Lost: {lossFigure(row.cost, row.lost_units)}</p>",
        "return `${CURRENCY}${n.toLocaleString()}`;",
    ]
    assert len(_frontend_currency_literals("probe", "\n".join(missed))) == 2
    assert _frontend_currency_literals("probe", "\n".join(fine)) == []
    print("PASS the frontend currency guard catches a JSX-text dollar and ignores template interpolation")


def test_source_rot_guard_no_bare_symbol_in_a_money_file():
    """Catch a NEW hardcoded literal that no current test happens to render.

    The allowlist is one line of ai/assistant.py: "$" there is an intent KEYWORD a
    user may type, not a display symbol, so it stays (alongside "£").
    """
    backend_files = ["ai/cost.py", "ai/report.py", "ai/scorecard.py", "ai/assistant.py",
                     "ai/briefing.py", "ai_copilot.py", "report_generator.py"]
    frontend_files = ["components/CostIntelCard.tsx", "components/CostSnapshot.tsx",
                      "components/ScorecardStrip.tsx", "components/CostingSection.tsx",
                      "components/MoneyStorySnapshot.tsx", "components/RecoverySnapshot.tsx",
                      "components/DigitalTwinSection.tsx", "lib/money.ts"]
    # The single allowed "$" in backend code: ai/assistant.py's intent-keyword tuple,
    # identified by a token unique to it. Those strings are matched against what the
    # USER TYPES, so "$" belongs there next to "£" — someone asking "what's this
    # costing me in $" should still reach the cost answer.
    INTENT_KEYWORDS = '"expensive"'
    offenders = []
    for rel in backend_files:
        for i, ln in enumerate(_read(os.path.join(HERE, rel)).splitlines(), 1):
            code = ln.split("#", 1)[0]
            if "$" in code and INTENT_KEYWORDS not in code:
                offenders.append(f"{rel}:{i}: {ln.strip()}")
    for rel in frontend_files:
        for i, line_no, ln in _frontend_currency_literals(rel, _read(os.path.join(FRONTEND, rel))):
            offenders.append(f"{rel}:{line_no}: {ln}")
    assert not offenders, "hardcoded currency literal(s) — import from currency.py / lib/money.ts:\n" + "\n".join(offenders)
    print("PASS no money file carries a hardcoded currency literal")


def test_the_formatters_themselves():
    assert money(49740) == f"{CURRENCY}49,740"
    assert signed_money(-500) == f"{CURRENCY}-500"
    assert signed_money(500) == f"{CURRENCY}+500"
    print("PASS the shared formatters produce grouped, signed money")


def test_a_suite_that_prints_the_symbol_survives_a_windows_console():
    """A PASSING suite must not be killed by the act of saying so.

    '₹' has no cp1252 code point, and a Windows console is cp1252 by
    default. When the platform moved from pounds to rupees, 11 green suites
    began exiting non-zero on the print that announced their own success —
    UnicodeEncodeError, after every assertion had held. CI's runners are UTF-8,
    so CI stayed green and only a developer ever saw it. An invariant that holds
    on the build machine and fails on the machine it is read on is not much of
    an invariant.

    So: any backend suite carrying THE CURRENCY SYMBOL must make stdout tolerant
    before it prints. One line, and it cannot rot silently because this asserts
    it is there.

    SCOPED TO THE SYMBOL, not to non-ASCII generally, and that was measured
    rather than assumed. Forty other suites carry a character cp1252 cannot
    encode -- box-drawing rules in comment separators, the deliberately
    malformed input of test_canonical.py, arrows inside docstrings. Every one of
    them was run on a cp1252 console and every one exited zero, because those
    characters never reach a print. A guard that flagged them would demand forty
    edits for no defect and teach everyone to add the line as a ritual. This
    file owns the currency symbol; it guards the currency symbol.
    """
    missing, examined = [], 0
    for name in sorted(os.listdir(HERE)):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        text = _read(os.path.join(HERE, name))
        if CURRENCY not in text:
            continue
        examined += 1
        if "sys.stdout.reconfigure" not in text:
            missing.append(name)
    # A rename, a moved directory or a currency that stopped being printed must
    # not quietly turn this into a check of nothing.
    assert examined >= 20, (
        f"only {examined} suites were found to carry {CURRENCY} — expected 20+. "
        f"Either the suite directory moved or the money surfaces stopped naming "
        f"the currency, and this guard is now checking nothing.")
    # A rename or a moved directory must not turn this into a check of nothing.
    assert len(
        [n for n in os.listdir(HERE) if n.startswith("test_") and n.endswith(".py")]
    ) > 300, "the suite directory moved; this guard is reading the wrong place"
    assert not missing, (
        "these suites carry a non-ASCII character but never make stdout "
        "tolerant, so they die on a cp1252 console AFTER passing:\n  "
        + "\n  ".join(missing)
        + '\n\nAdd, after the module docstring:\n'
          '    import sys\n'
          '    if hasattr(sys.stdout, "reconfigure"):\n'
          '        sys.stdout.reconfigure(encoding="utf-8", errors="replace")')
    print(f"PASS all {examined} suites printing {CURRENCY} survive a cp1252 console")


if __name__ == "__main__":
    test_the_two_stacks_agree_on_the_symbol()
    test_the_two_stacks_agree_on_THE_GROUPING_TOO()
    test_the_scorecard_unit_token_survives_every_consumer()
    test_the_nav_icon_follows_the_platform_currency()
    test_no_money_surface_prints_a_dollar()
    test_the_frontend_guard_catches_a_dollar_in_jsx_text()
    test_source_rot_guard_no_bare_symbol_in_a_money_file()
    test_the_formatters_themselves()
    test_a_suite_that_prints_the_symbol_survives_a_windows_console()
    print("ALL CURRENCY TESTS PASSED")
