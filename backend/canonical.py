"""Canonical bytes, content hashes and acceptance validity for statements (ADR-0021).

WHAT THIS MODULE IS FOR
-----------------------
A downtime attribution statement is accepted by both parties, the OEM and the
factory. Each acceptance records the SHA-256 of the statement content it
accepted and the revision it accepted, and any later change to the statement
must cancel it. Three primitives make that work, and each lives only here:

    canonical_bytes(obj)                 the ONE byte representation of content
    sha256_hex(data) / content_hash(obj) its hash
    acceptance_is_valid(acc, statement)  does this acceptance still count?

WHAT THIS MODULE IS NOT
-----------------------
It is not a ledger, a chain or a signature. Nothing here links one statement, one
revision or one acceptance to another. Integrity is one hash per statement
revision plus the ordinary audit log. Anyone with full database access could
rewrite a statement, its hash and its acceptances together; only the parties' own
exported copies can show that. Prior art for chained usage ledgers exists
(SteamChain, PayperChain, Linxfour, Rockwell US10747201B2) and AMP deliberately
does not build one.

THE CANONICAL FORM
------------------
    json.dumps(value, sort_keys=True, separators=(",", ":"),
               ensure_ascii=False, allow_nan=False).encode("utf-8")

over a value built ONLY from exact built-in types:

    dict      keys must be str; keys are NFC-normalised, and two keys that become
              equal under NFC are refused rather than silently merged
    list      order is content (intervals are ordered) and is preserved
    str       NFC-normalised; a lone surrogate is refused (it has no UTF-8 form)
    int       within +/-(2**53 - 1), so every JSON reader parses it exactly;
              bool is refused (True is an int in Python and `true` in JSON)
    None      null

Everything else is refused, including subclasses of the types above (IntEnum,
str enums, OrderedDict): their JSON rendering is decided by their class, not by
this module. In particular:

    float, Decimal    render money with contract_money.decimal_text first. A
                      float has no single text form (97.0, 97.00000000000001),
                      and Decimal("5.0") and Decimal("5.00") are equal numbers
                      with different text.
    datetime, date    render with canonical.ts first.

Keys sort by Unicode code point (Python string order). Parties verify a statement
by hashing the DOWNLOADED BYTES, never by re-serialising parsed JSON, so no other
language has to reproduce this ordering; parsing the bytes and canonicalising
again in Python is a fixed point, which test_canonical asserts.

Run the tests: DATABASE_URL="sqlite:///./ci.db" python backend/test_canonical.py
"""
import hashlib
import json
import re
import unicodedata
from datetime import date, datetime, timezone

# The largest integer every IEEE-754 JSON reader (a browser included) parses
# without rounding. Seconds, ids and revisions are nowhere near it.
MAX_SAFE_INTEGER = 2 ** 53 - 1

_SURROGATE = re.compile("[\ud800-\udfff]")
_SHA256_HEX = re.compile(r"\A[0-9a-f]{64}\Z")


class CanonicalError(ValueError):
    """A value has no fixed canonical rendering. The message names where."""


def _text(value, path):
    if _SURROGATE.search(value):
        raise CanonicalError(
            f"{path}: lone surrogate in text; it has no UTF-8 encoding")
    return unicodedata.normalize("NFC", value)


def _refusal(value, path):
    kind = type(value).__name__
    if isinstance(value, bool):
        hint = "booleans are not part of statement content; use a string"
    elif isinstance(value, float) or type(value).__name__ == "Decimal":
        hint = "render the number with contract_money.decimal_text first"
    elif isinstance(value, (datetime, date)):
        hint = "render the instant with canonical.ts first"
    else:
        hint = "only dict, list, str, int and None have a canonical form"
    return CanonicalError(f"{path}: {kind} is not allowed in canonical content ({hint})")


def _walk(value, path, open_containers):
    if value is None:
        return None
    kind = type(value)
    if kind is str:
        return _text(value, path)
    if kind is int:
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise CanonicalError(
                f"{path}: integer {value} is outside +/-(2**53 - 1) and would be "
                "rounded by a JSON reader")
        return value
    if kind is list or kind is dict:
        marker = id(value)
        if marker in open_containers:
            raise CanonicalError(f"{path}: cycle; the structure contains itself")
        open_containers.add(marker)
        try:
            if kind is list:
                return [_walk(item, f"{path}[{i}]", open_containers)
                        for i, item in enumerate(value)]
            out = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise CanonicalError(
                        f"{path}: key {key!r} is {type(key).__name__}; keys must be "
                        "str (json.dumps would silently stringify it)")
                norm = _text(key, f"{path}.<key>")
                if norm in out:
                    raise CanonicalError(
                        f"{path}: keys {key!r} and another key collide after NFC "
                        "normalisation")
                out[norm] = _walk(item, f"{path}.{norm}", open_containers)
            return out
        finally:
            open_containers.discard(marker)
    raise _refusal(value, path)


def canonical_bytes(value):
    """The one UTF-8 byte string for `value`. Raises CanonicalError otherwise."""
    walked = _walk(value, "$", set())
    return json.dumps(walked, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_hex(data):
    """Lowercase hex SHA-256 of BYTES.

    A str is refused: its bytes depend on an encoding the caller would be
    guessing, and a stored canonical_json read back as text must be encoded as
    UTF-8 deliberately, at the call site, where a reader can see it."""
    if not isinstance(data, (bytes, bytearray)):
        raise CanonicalError(
            f"sha256_hex hashes bytes, not {type(data).__name__}")
    return hashlib.sha256(bytes(data)).hexdigest()


def content_hash(value):
    """sha256_hex(canonical_bytes(value))."""
    return sha256_hex(canonical_bytes(value))


def is_sha256_hex(value):
    """Exactly 64 lowercase hex characters, as sha256_hex produces."""
    return type(value) is str and _SHA256_HEX.match(value) is not None


def utc_seconds(dt):
    """`dt` as naive UTC, truncated (floored) to the whole second.

    The single truncation rule. Naive datetimes are UTC by AMP convention;
    aware ones are converted. Use it on any timestamp that did not come from a
    whole-second column (DowntimeLog.created_at carries microseconds) before it
    reaches statement content or interval arithmetic."""
    if not isinstance(dt, datetime):
        raise CanonicalError(f"utc_seconds needs a datetime, not {type(dt).__name__}")
    if dt.tzinfo is not None and dt.utcoffset() is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0, tzinfo=None)


def ts(dt):
    """Render an instant as YYYY-MM-DDTHH:MM:SSZ (UTC).

    A fractional second is REFUSED, not dropped: statement intervals carry
    `seconds` computed from their bounds, and a bound that renders differently
    from the value the arithmetic used would publish a statement whose own
    numbers disagree. Truncate explicitly with utc_seconds."""
    if not isinstance(dt, datetime):
        raise CanonicalError(f"ts needs a datetime, not {type(dt).__name__}")
    if dt.tzinfo is not None and dt.utcoffset() is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    if dt.microsecond:
        raise CanonicalError(
            f"ts: {dt.isoformat()} has a fractional second; truncate it with "
            "canonical.utc_seconds first")
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
            f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z")


def _revision(value):
    return type(value) is int and value >= 1


def acceptance_is_valid(acceptance, statement):
    """Does this acceptance still count for this statement? The ONLY copy of the rule.

    Valid if and only if ALL of:
      * it was recorded against this statement (statement_id == statement.id);
      * its content_hash equals the statement's current content_hash;
      * its revision equals the statement's current revision.

    REVISION AS WELL AS HASH (critic finding C1). Revision 1 is accepted, a
    dispute makes revision 2, withdrawing it makes revision 3 whose bytes equal
    revision 1's. On hash alone the old acceptance would count again with nobody
    having re-accepted. Bound to the revision, it does not.

    Fails closed: a missing, malformed or merely-equal-but-invalid value (two
    empty hashes, two revision-0s, True standing in for 1) is never valid.
    Duck-typed over attributes so it applies to ORM rows and plain objects alike.
    """
    if acceptance is None or statement is None:
        return False
    a_statement = getattr(acceptance, "statement_id", None)
    s_id = getattr(statement, "id", None)
    if type(a_statement) is not int or type(s_id) is not int or a_statement != s_id:
        return False
    a_hash = getattr(acceptance, "content_hash", None)
    s_hash = getattr(statement, "content_hash", None)
    if not (is_sha256_hex(a_hash) and is_sha256_hex(s_hash)):
        return False
    a_rev = getattr(acceptance, "revision", None)
    s_rev = getattr(statement, "revision", None)
    if not (_revision(a_rev) and _revision(s_rev)):
        return False
    return a_hash == s_hash and a_rev == s_rev
