"""Duration parsing — one correct implementation, shared by the engines.

Downtime durations are free-text ("2 hrs 15 min", "1 hr 10 min", "45 min", a
bare "90", or a decimal "1.5 hrs"). This parser reads hours and minutes and sums
them to minutes. It used to be duplicated: analytics_engine had a correct regex
version, predictive_engine a cruder digit-concatenation version that misread any
hour format ("1 hr" -> 1 minute, "2 hrs 15 min" -> 215), which silently
understated downtime in the predictive-maintenance risk score. Both now import
this one function.

The number is matched as a decimal (\\d+(?:\\.\\d+)?) so "1.5 hrs" is 90 minutes,
not 300. An integer-only `\\d+` skipped the "1." and matched the "5" in "1.5 h",
reading a 90-minute stop as 5 hours — a 3x overstatement that flowed straight
into every downtime rollup, the cost-of-losses £ and the risk score. Operators
type this field free-hand, so decimals are real input.
"""
import re
from functools import lru_cache

_NUM = r"(\d+(?:\.\d+)?)"


# Memoised because the aggregate paths call this once per DISTINCT duration
# GROUP rather than once per row, and a factory types the same handful of
# durations over and over -- ~11,000 groups over ~55 distinct strings at 200
# machines with a month of history.
#
# Worth being precise about the size of this. I predicted it would remove most
# of /machine-health's Python time; it did not. Measured, it took that endpoint
# from 282.8 ms to 261.8 ms -- about 8%. The real cost was elsewhere (a
# projection scan in ai/twin.py). Kept because it is free and correct, not
# because it was the fix.
#
# Safe to cache: a pure function of its argument -- no clock, no database, no
# globals -- so the same string always yields the same number. Bounded at 4096
# because `value` is FREE TEXT an operator types, and an unbounded cache keyed
# on user input grows without limit.
@lru_cache(maxsize=4096)
def parse_duration_to_minutes(value: str) -> int:
    if not value:
        return 0

    lower = str(value).lower()
    total = 0.0

    hour_match = re.search(_NUM + r"\s*h", lower)
    minute_match = re.search(_NUM + r"\s*m", lower)

    if hour_match:
        total += float(hour_match.group(1)) * 60
    if minute_match:
        total += float(minute_match.group(1))

    # No unit at all — treat a bare number (integer or decimal) as minutes.
    if not hour_match and not minute_match:
        plain = re.search(_NUM, lower)
        total += float(plain.group(1)) if plain else 0.0

    return round(total)
