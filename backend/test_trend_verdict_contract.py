"""Trend verdict contract test (ADR-0007).

The Trends view renders one uniform "this week vs last week" card per trend
read-model from just three shared fields — ``direction``, ``verdict`` and
``tone`` — so it never couples to each read-model's metric-specific keys
(delta_minutes / delta_pts / delta_cost). This pins that shared contract across
all three trends (downtime, quality, cost): every one must return a non-empty
verdict string, a tone the UI knows how to colour, and a string direction. A
trend that dropped or renamed one of these would silently break the card.

Run:  python backend/test_trend_verdict_contract.py     (exit 0 = pass)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from ai import cost, downtime, oee, quality

# The tones the Trends card maps to colours (toneClasses in TrendsSection.tsx).
# Anything else would fall through to the neutral style, hiding a real signal.
_TONES = {"good", "warn", "bad"}

_TRENDS = [
    ("oee", oee.build_oee_trend),
    ("downtime", downtime.build_downtime_trend),
    ("quality", quality.build_quality_trend),
    ("cost", cost.build_cost_trend),
]


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_every_trend_returns_the_verdict_card_contract():
    db = _fresh_session()   # empty tenant — the trends must still return the shape
    for name, build in _TRENDS:
        t = build(db, "DEFAULT")
        assert "direction" in t and isinstance(t["direction"], str) and t["direction"], \
            f"{name}-trend: missing/empty direction"
        assert isinstance(t.get("verdict"), str) and t["verdict"], \
            f"{name}-trend: missing/empty verdict"
        assert t.get("tone") in _TONES, \
            f"{name}-trend: tone {t.get('tone')!r} not one the card can colour {_TONES}"
    print(f"PASS all {len(_TRENDS)} trends return the (direction, verdict, tone) card contract")


if __name__ == "__main__":
    test_every_trend_returns_the_verdict_card_contract()
    print("TREND VERDICT CONTRACT OK")
