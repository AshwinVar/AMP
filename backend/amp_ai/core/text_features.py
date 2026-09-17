"""Text normalisation and signed hashed n-gram features for the native copilot.

FROZEN SPEC (a shipped intent model depends on every detail here)
-----------------------------------------------------------------
normalize_text(s):
  1. Unicode NFKC, then ``str.lower()``.
  2. Apostrophes (' ` and U+2018 U+2019 U+02BC) are DELETED, so "what's" and
     "whats" become the same token.
  3. '-' is kept ("line-2").
  4. Every other punctuation (P*) or symbol (S*) character, and every
     separator (Z*) or control (Cc) character, becomes a space. Invisible
     format characters (Cf, e.g. zero-width space) are deleted.
  5. Whitespace runs collapse to one space; the result is stripped.

hashed_features(s, dim, word_ngrams, char_ngrams):
  * text = normalize_text(s); tokens = text.split(" ").
  * word n-grams for n in [lo, hi]: feature "w{n}:" + the n tokens joined by " ".
  * char n-grams for n in [lo, hi] over " " + text + " ": feature "c{n}:" + slice.
  * each feature adds its ``stable_hash`` sign (+1/-1) at its ``stable_hash``
    index; entries that cancel to exactly 0 are dropped;
  * the vector is L2-normalised; keys are returned in ascending order.
  * ``None`` for either n-gram range disables that family.
  An empty or punctuation-only text yields ``{}``.
"""
import math
import unicodedata

from .rng import stable_hash

__all__ = ["normalize_text", "hashed_features"]

_APOSTROPHES = frozenset(["'", "`", chr(0x2018), chr(0x2019), chr(0x02BC)])


def normalize_text(s) -> str:
    if not isinstance(s, str):
        raise TypeError(f"text must be str, got {type(s).__name__}")
    folded = unicodedata.normalize("NFKC", s).lower()
    out = []
    for ch in folded:
        if ch in _APOSTROPHES:
            continue
        if ch == "-":
            out.append(ch)
            continue
        category = unicodedata.category(ch)
        if category == "Cf":
            continue
        if category[0] in ("P", "S", "Z") or category == "Cc":
            out.append(" ")
        else:
            out.append(ch)
    return " ".join("".join(out).split())


def _ngram_range(value, label):
    if value is None:
        return None
    try:
        lo, hi = value
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a (lo, hi) pair or None") from None
    for v in (lo, hi):
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ValueError(f"{label} bounds must be positive ints, got {value!r}")
    if lo > hi:
        raise ValueError(f"{label} lower bound exceeds upper bound: {value!r}")
    return lo, hi


def hashed_features(s, dim=8192, word_ngrams=(1, 2), char_ngrams=(4, 4)) -> dict[int, float]:
    if isinstance(dim, bool) or not isinstance(dim, int) or dim < 2:
        raise ValueError(f"dim must be an int >= 2, got {dim!r}")
    words = _ngram_range(word_ngrams, "word_ngrams")
    chars = _ngram_range(char_ngrams, "char_ngrams")
    text = normalize_text(s)
    counts = {}

    def add(feature):
        index, sign = stable_hash(feature, dim)
        counts[index] = counts.get(index, 0.0) + sign

    if text and words:
        tokens = text.split(" ")
        for n in range(words[0], words[1] + 1):
            for i in range(len(tokens) - n + 1):
                add(f"w{n}:" + " ".join(tokens[i:i + n]))
    if text and chars:
        padded = f" {text} "
        for n in range(chars[0], chars[1] + 1):
            for i in range(len(padded) - n + 1):
                add(f"c{n}:" + padded[i:i + n])

    nonzero = {i: v for i, v in counts.items() if v != 0.0}
    norm = math.sqrt(math.fsum(v * v for v in nonzero.values()))
    if norm == 0.0:
        return {}
    return {i: nonzero[i] / norm for i in sorted(nonzero)}
