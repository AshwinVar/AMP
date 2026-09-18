"""The statement-integrity primitives (ADR-0021): canonical bytes, hash, acceptance.

WHAT THESE PRIMITIVES ARE FOR
-----------------------------
A downtime attribution statement is accepted by BOTH parties. Each acceptance
records the SHA-256 of the exact statement content it accepted, and the revision
it accepted. Any later change to the statement must invalidate the acceptance.
That only works if "the statement content" has exactly one byte representation:

  * the same content must always produce the same bytes (key order, whitespace,
    Unicode normalisation form cannot leak into the hash), and
  * different content must never be squeezed into the same bytes (a float that
    prints as 97.0 or 97.00000000001, a Decimal whose exponent varies, a key
    that collides after normalisation).

So `canonical_bytes` refuses every value whose rendering is not fixed by this
module. Money arrives pre-rendered by contract_money.decimal_text; timestamps
arrive pre-rendered by canonical.ts.

WHY REVISION AS WELL AS HASH (critic finding C1)
------------------------------------------------
Revision 1 is accepted, a dispute is raised (revision 2), then withdrawn
(revision 3). Revision 3's bytes equal revision 1's, so a hash-only check would
silently revive the old acceptance with nobody re-accepting. An acceptance is
valid only for the hash AND the revision it named.

THE GOLDEN HASH BELOW WAS NOT PRODUCED BY PYTHON. It was computed by coreutils
`sha256sum` over bytes written with printf, so this suite does not merely assert
that hashlib agrees with itself.

Run: DATABASE_URL="sqlite:///./ci.db" python test_canonical.py
"""
import ast
import io
import json
import os
import random
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import IntEnum
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import canonical  # noqa: E402
from canonical import CanonicalError  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

H1 = "a" * 64
H2 = "b" * 64


def _raises(fn, *args):
    try:
        fn(*args)
    except CanonicalError as e:
        return str(e)
    return None


# --------------------------------------------------------------------------
# 1. canonical_bytes: one content, one byte string
# --------------------------------------------------------------------------

def test_golden_vector_matches_an_independent_sha256():
    content = {"c": {"z": "ü", "y": -3}, "b": "é", "a": [1, None, "x"]}
    expected = (b'{"a":[1,null,"x"],"b":"\xc3\xa9","c":{"y":-3,"z":"\xc3\xbc"}}')
    got = canonical.canonical_bytes(content)
    assert got == expected, got
    # sha256sum of exactly those bytes (coreutils, not hashlib).
    assert canonical.sha256_hex(got) == \
        "165ee20e8105e05e5b83020bcafe4743832c737b61368ec68b74ce25541070d4"
    assert canonical.content_hash(content) == canonical.sha256_hex(got)
    print("PASS golden vector: sorted keys, no whitespace, raw UTF-8, independent SHA-256")


def test_insertion_order_does_not_change_the_bytes():
    a = {"period": {"start": "2026-09-01T00:00:00Z", "end": "2026-10-01T00:00:00Z"},
         "machines": [{"installation_id": 12, "seconds": 3600}], "schema": "s/1"}
    b = {"schema": "s/1", "machines": [{"seconds": 3600, "installation_id": 12}],
         "period": {"end": "2026-10-01T00:00:00Z", "start": "2026-09-01T00:00:00Z"}}
    assert canonical.canonical_bytes(a) == canonical.canonical_bytes(b)
    # CONTROL: list order IS content (intervals are ordered), so it must matter.
    c = {"x": [1, 2]}
    d = {"x": [2, 1]}
    assert canonical.canonical_bytes(c) != canonical.canonical_bytes(d)
    print("PASS dict insertion order is erased; list order is preserved")


def test_output_has_no_insignificant_whitespace():
    got = canonical.canonical_bytes({"a": [1, 2], "b": {"c": "d e"}})
    assert got == b'{"a":[1,2],"b":{"c":"d e"}}', got
    print("PASS separators are ',' and ':' with no padding")


def test_strings_are_nfc_normalised_in_values_and_keys():
    composed = "café"          # e-acute as one code point
    decomposed = "café"       # e followed by a combining acute
    assert composed != decomposed
    assert canonical.canonical_bytes({"r": composed}) == \
        canonical.canonical_bytes({"r": decomposed})
    assert canonical.canonical_bytes({composed: 1}) == \
        canonical.canonical_bytes({decomposed: 1})
    # ANGSTROM SIGN normalises to LATIN CAPITAL A WITH RING ABOVE.
    assert canonical.canonical_bytes(["Å"]) == \
        ("[\"" + unicodedata.normalize("NFC", "Å") + "\"]").encode("utf-8")
    assert canonical.canonical_bytes(["Å"]) == '["Å"]'.encode("utf-8")
    print("PASS NFC normalisation applies to values and to keys")


def test_keys_that_collide_after_normalisation_are_refused():
    msg = _raises(canonical.canonical_bytes, {"café": 1, "café": 2})
    assert msg is not None, "two distinct keys were silently merged into one"
    assert "collide" in msg.lower(), msg
    print("PASS keys that become equal under NFC are refused, not merged")


def test_lone_surrogates_are_refused():
    for bad in ("\ud800", "a\udfffb", "\udc00x"):
        msg = _raises(canonical.canonical_bytes, {"reason": bad})
        assert msg is not None, f"lone surrogate accepted in a value: {bad!r}"
        assert "surrogate" in msg.lower(), msg
        msg = _raises(canonical.canonical_bytes, {bad: "x"})
        assert msg is not None, f"lone surrogate accepted in a key: {bad!r}"
    # CONTROL: an astral character is a valid scalar value, not a surrogate.
    assert canonical.canonical_bytes(["\U0001f527"]) == \
        '["\U0001f527"]'.encode("utf-8")
    print("PASS lone surrogates refused in values and keys; astral characters kept")


def test_every_value_without_a_fixed_rendering_is_refused():
    class Level(IntEnum):
        LOW = 1

    class Label(str):
        pass

    refused = {
        "float": 97.0,
        "float nan": float("nan"),
        "float inf": float("inf"),
        "Decimal": Decimal("40000.00"),
        "datetime": datetime(2026, 9, 1),
        "date": date(2026, 9, 1),
        "bool True": True,
        "bool False": False,
        "tuple": (1, 2),
        "set": {1},
        "bytes": b"x",
        "object": object(),
        "IntEnum": Level.LOW,
        "str subclass": Label("x"),
    }
    for label, value in refused.items():
        top = _raises(canonical.canonical_bytes, value)
        nested = _raises(canonical.canonical_bytes, {"a": [{"b": value}]})
        assert top is not None, f"{label} accepted at top level"
        assert nested is not None, f"{label} accepted when nested"
        assert "$.a[0].b" in nested, f"{label}: error does not name the path: {nested}"
    for key in (1, None, 1.5, True, ("t",)):
        assert _raises(canonical.canonical_bytes, {key: "v"}) is not None, \
            f"non-str key {key!r} accepted (json.dumps would have stringified it)"
    # The two refusals a builder will actually hit say where to go instead.
    assert "decimal_text" in _raises(canonical.canonical_bytes, Decimal("1.00"))
    assert "decimal_text" in _raises(canonical.canonical_bytes, 1.0)
    assert "canonical.ts" in _raises(canonical.canonical_bytes, datetime(2026, 1, 1))
    print(f"PASS {len(refused)} value kinds refused at top level and nested, "
          "with the path named")


def test_integers_are_limited_to_the_interoperable_range():
    safe = 2 ** 53 - 1
    assert canonical.canonical_bytes([safe, -safe, 0]) == \
        f"[{safe},{-safe},0]".encode("ascii")
    for big in (2 ** 53, -(2 ** 53), 10 ** 30):
        assert _raises(canonical.canonical_bytes, [big]) is not None, big
    print("PASS integers within +/-(2^53-1) accepted; larger ones refused")


def test_control_characters_escape_deterministically():
    got = canonical.canonical_bytes({"t": 'a"b\\c\n\t\r\x01\x7f/'})
    assert got == b'{"t":"a\\"b\\\\c\\n\\t\\r\\u0001\x7f/"}', got
    print("PASS quotes, backslashes and control characters have one escaping")


def test_null_and_empty_containers():
    assert canonical.canonical_bytes(None) == b"null"
    assert canonical.canonical_bytes({}) == b"{}"
    assert canonical.canonical_bytes([]) == b"[]"
    assert canonical.canonical_bytes({"a": None, "b": [], "c": {}, "d": ""}) == \
        b'{"a":null,"b":[],"c":{},"d":""}'
    print("PASS null, empty list, empty object and empty string render once")


def test_cycles_are_refused_not_recursed_forever():
    loop = {"a": []}
    loop["a"].append(loop)
    msg = _raises(canonical.canonical_bytes, loop)
    assert msg is not None and "cycle" in msg.lower(), msg
    # CONTROL: the SAME object twice, not nested in itself, is not a cycle.
    shared = {"k": 1}
    assert canonical.canonical_bytes([shared, shared]) == b'[{"k":1},{"k":1}]'
    print("PASS a self-containing structure is refused; a shared one is not")


def _random_value(rng, depth=0):
    kinds = ["int", "str", "none"] + (["list", "dict"] if depth < 4 else [])
    kind = rng.choice(kinds)
    if kind == "int":
        return rng.randint(-(2 ** 53 - 1), 2 ** 53 - 1)
    if kind == "none":
        return None
    if kind == "str":
        alphabet = "abc ééÅ\"\\\n\x00\U0001f527हि"
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 8)))
    if kind == "list":
        return [_random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    out = {}
    for _ in range(rng.randint(0, 4)):
        key = "".join(rng.choice("abcxyzéह") for _ in range(rng.randint(1, 5)))
        out[key] = _random_value(rng, depth + 1)
    return out


def test_the_canonical_form_is_a_fixed_point():
    """Parsing canonical bytes and canonicalising again changes nothing.

    This is what lets either party re-derive the hash from a downloaded copy
    rather than trusting AMP's stored one."""
    rng = random.Random(20260917)
    checked = 0
    for _ in range(400):
        value = _random_value(rng)
        try:
            first = canonical.canonical_bytes(value)
        except CanonicalError:
            continue   # an NFC key collision generated at random; refused, fine
        again = canonical.canonical_bytes(json.loads(first.decode("utf-8")))
        assert again == first, (value, first, again)
        checked += 1
    assert checked > 300, f"only {checked} random structures exercised"
    print(f"PASS canonical(parse(canonical(x))) == canonical(x) for {checked} "
          "random structures")


# --------------------------------------------------------------------------
# 2. sha256_hex and content_hash
# --------------------------------------------------------------------------

def test_sha256_hex_hashes_bytes_only():
    # sha256sum of zero bytes (coreutils).
    assert canonical.sha256_hex(b"") == \
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert canonical.sha256_hex(bytearray(b"")) == canonical.sha256_hex(b"")
    msg = _raises(canonical.sha256_hex, "{}")
    assert msg is not None, "a str was hashed; its encoding is the caller's guess"
    assert _raises(canonical.sha256_hex, None) is not None
    print("PASS sha256_hex takes bytes, refuses str and None")


def test_is_sha256_hex_is_strict():
    assert canonical.is_sha256_hex("0" * 64)
    assert canonical.is_sha256_hex(canonical.sha256_hex(b"x"))
    for bad in ("", "0" * 63, "0" * 65, "A" * 64, "g" * 64, " " + "0" * 63,
                "0" * 64 + "\n", None, 0, b"0" * 64):
        assert not canonical.is_sha256_hex(bad), repr(bad)
    print("PASS only 64 lowercase hex characters count as a content hash")


# --------------------------------------------------------------------------
# 3. ts and utc_seconds: one rendering of an instant
# --------------------------------------------------------------------------

def test_ts_renders_whole_second_utc_with_z():
    assert canonical.ts(datetime(2026, 9, 1, 18, 30, 0)) == "2026-09-01T18:30:00Z"
    ist = timezone(timedelta(hours=5, minutes=30))
    assert canonical.ts(datetime(2026, 9, 2, 0, 0, 0, tzinfo=ist)) == \
        "2026-09-01T18:30:00Z"
    assert canonical.ts(datetime(999, 1, 2, 3, 4, 5)) == "0999-01-02T03:04:05Z"
    print("PASS ts: naive is UTC, aware is converted, the year is zero-padded")


def test_ts_refuses_what_it_would_have_to_guess_about():
    msg = _raises(canonical.ts, datetime(2026, 9, 1, 0, 0, 0, 500000))
    assert msg is not None, "a fractional second was silently dropped from content"
    assert "utc_seconds" in msg, msg
    for bad in (date(2026, 9, 1), "2026-09-01T00:00:00Z", None, 1725148800):
        assert _raises(canonical.ts, bad) is not None, repr(bad)
    print("PASS ts refuses fractional seconds, dates, strings and epoch numbers")


def test_utc_seconds_truncates_and_normalises():
    assert canonical.utc_seconds(datetime(2026, 9, 1, 0, 0, 0, 999999)) == \
        datetime(2026, 9, 1, 0, 0, 0)
    ist = timezone(timedelta(hours=5, minutes=30))
    got = canonical.utc_seconds(datetime(2026, 9, 1, 5, 30, 1, 1, tzinfo=ist))
    assert got == datetime(2026, 9, 1, 0, 0, 1) and got.tzinfo is None, got
    assert canonical.ts(canonical.utc_seconds(datetime(2026, 9, 1, 0, 0, 0, 7))) == \
        "2026-09-01T00:00:00Z"
    assert _raises(canonical.utc_seconds, date(2026, 9, 1)) is not None
    print("PASS utc_seconds floors to the second and returns naive UTC")


# --------------------------------------------------------------------------
# 4. acceptance_is_valid: hash AND revision AND statement
# --------------------------------------------------------------------------

def _stmt(h=H1, rev=1, sid=7):
    return SimpleNamespace(id=sid, content_hash=h, revision=rev)


def _acc(h=H1, rev=1, sid=7):
    return SimpleNamespace(statement_id=sid, content_hash=h, revision=rev)


def test_an_acceptance_of_exactly_this_revision_is_valid():
    assert canonical.acceptance_is_valid(_acc(), _stmt()) is True
    assert canonical.acceptance_is_valid(_acc(H2, 4, 9), _stmt(H2, 4, 9)) is True
    print("PASS same statement, same hash, same revision -> valid")


def test_a_changed_statement_invalidates_the_acceptance():
    assert canonical.acceptance_is_valid(_acc(H1, 1), _stmt(H2, 2)) is False
    assert canonical.acceptance_is_valid(_acc(H1, 1), _stmt(H2, 1)) is False
    print("PASS a different content hash -> invalid")


def test_the_hash_returning_to_old_bytes_does_not_revive_an_acceptance():
    """C1: accept rev 1, dispute (rev 2), withdraw (rev 3, same bytes as rev 1)."""
    rev1 = _stmt(H1, 1)
    acceptance = _acc(H1, 1)
    assert canonical.acceptance_is_valid(acceptance, rev1) is True
    rev3 = _stmt(H1, 3)
    assert canonical.acceptance_is_valid(acceptance, rev3) is False
    # Re-accepting revision 3 is what makes it valid again.
    assert canonical.acceptance_is_valid(_acc(H1, 3), rev3) is True
    print("PASS identical bytes at a later revision do not revive the old acceptance")


def test_an_acceptance_of_another_statement_is_not_valid_here():
    assert canonical.acceptance_is_valid(_acc(sid=8), _stmt(sid=7)) is False
    assert canonical.acceptance_is_valid(_acc(sid=None), _stmt(sid=None)) is False
    print("PASS an acceptance only counts for the statement it was recorded against")


def test_malformed_values_never_match_each_other():
    cases = {
        "no acceptance": (None, _stmt()),
        "no statement": (_acc(), None),
        "both hashes empty": (_acc(h=""), _stmt(h="")),
        "both hashes None": (_acc(h=None), _stmt(h=None)),
        "both hashes uppercase": (_acc(h="A" * 64), _stmt(h="A" * 64)),
        "both hashes short": (_acc(h="a" * 32), _stmt(h="a" * 32)),
        "both revisions None": (_acc(rev=None), _stmt(rev=None)),
        "both revisions 0": (_acc(rev=0), _stmt(rev=0)),
        "both revisions negative": (_acc(rev=-1), _stmt(rev=-1)),
        "both revisions True": (_acc(rev=True), _stmt(rev=True)),
        "revision True vs 1": (_acc(rev=True), _stmt(rev=1)),
        "both revisions '1'": (_acc(rev="1"), _stmt(rev="1")),
        "revision 1.0 vs 1": (_acc(rev=1.0), _stmt(rev=1)),
        "statement id True vs 1": (_acc(sid=True), _stmt(sid=1)),
        "missing attributes": (SimpleNamespace(), SimpleNamespace()),
    }
    for label, (a, s) in cases.items():
        assert canonical.acceptance_is_valid(a, s) is False, label
    print(f"PASS {len(cases)} malformed pairs are invalid even when they are equal")


# --------------------------------------------------------------------------
# 5. structural: nothing in canonical.py can produce a float
# --------------------------------------------------------------------------

def test_canonical_module_contains_no_float():
    path = os.path.join(HERE, "canonical.py")
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    nodes = list(ast.walk(tree))
    assert len(nodes) > 200, f"parsed only {len(nodes)} nodes; wrong file?"
    functions = {n.name for n in nodes if isinstance(n, ast.FunctionDef)}
    for required in ("canonical_bytes", "sha256_hex", "content_hash", "ts",
                     "utc_seconds", "acceptance_is_valid", "is_sha256_hex"):
        assert required in functions, f"structural scan did not find {required}"
    float_literals = [n.lineno for n in nodes
                      if isinstance(n, ast.Constant) and type(n.value) is float]
    float_calls = [n.lineno for n in nodes
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "float"]
    assert not float_literals, f"float literal(s) at lines {float_literals}"
    assert not float_calls, f"float() call(s) at lines {float_calls}"
    print(f"PASS canonical.py: {len(functions)} functions found, no float literal "
          "or float() call")


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"ALL {len(tests)} STATEMENT-INTEGRITY TESTS PASSED")
