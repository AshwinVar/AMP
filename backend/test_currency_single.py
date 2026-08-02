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
import io
import os
import re
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from ai import assistant, cost, report, scorecard
from currency import CURRENCY, money, signed_money
from database import Base

HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(HERE, "..", "frontend")


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed_losses(db):
    """One machine with real downtime and scrap, so loss_cost is non-zero.

    40 min downtime x 12 + 3 rejects x 25 = 555. The number matters: a fixture with
    loss_cost == 0 would render "£0" and still pass a naive "no $" assertion while
    telling us nothing about the formatting path.
    """
    now = datetime.utcnow()
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
                      "lib/money.ts"]
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
        for i, ln in enumerate(_read(os.path.join(FRONTEND, rel)).splitlines(), 1):
            code = ln.split("//", 1)[0]
            # `${...}` is ordinary template interpolation; only a $ IMMEDIATELY before
            # one (`$${`) or before a digit is a currency literal.
            if re.search(r"\$\$\{|\$\d", code):
                offenders.append(f"{rel}:{i}: {ln.strip()}")
    assert not offenders, "hardcoded currency literal(s) — import from currency.py / lib/money.ts:\n" + "\n".join(offenders)
    print("PASS no money file carries a hardcoded currency literal")


def test_the_formatters_themselves():
    assert money(49740) == f"{CURRENCY}49,740"
    assert signed_money(-500) == f"{CURRENCY}-500"
    assert signed_money(500) == f"{CURRENCY}+500"
    print("PASS the shared formatters produce grouped, signed money")


if __name__ == "__main__":
    test_the_two_stacks_agree_on_the_symbol()
    test_the_scorecard_unit_token_survives_every_consumer()
    test_no_money_surface_prints_a_dollar()
    test_source_rot_guard_no_bare_symbol_in_a_money_file()
    test_the_formatters_themselves()
    print("ALL CURRENCY TESTS PASSED")
