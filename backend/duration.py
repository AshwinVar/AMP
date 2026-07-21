"""Duration parsing — one correct implementation, shared by the engines.

Downtime durations are free-text ("2 hrs 15 min", "1 hr 10 min", "45 min", or a
bare "90"). This parser reads hours and minutes and sums them to minutes. It used
to be duplicated: analytics_engine had a correct regex version, predictive_engine
a cruder digit-concatenation version that misread any hour format ("1 hr" -> 1
minute, "2 hrs 15 min" -> 215), which silently understated downtime in the
predictive-maintenance risk score. Both now import this one function.
"""
import re


def parse_duration_to_minutes(value: str) -> int:
    if not value:
        return 0

    lower = str(value).lower()
    total = 0

    hour_match = re.search(r"(\d+)\s*h", lower)
    minute_match = re.search(r"(\d+)\s*m", lower)

    if hour_match:
        total += int(hour_match.group(1)) * 60
    if minute_match:
        total += int(minute_match.group(1))

    # No unit at all — treat a bare number as minutes.
    if not hour_match and not minute_match:
        plain = re.sub(r"\D", "", lower)
        total += int(plain) if plain else 0

    return total
