"""The contract terms document: parsed fail-closed, hashed one way (ADR-0020).

WHAT THIS PINS
--------------
Both parties accept a terms version by its SHA-256. So the parser must:

  * refuse every malformed field BY NAME, rather than defaulting it — a default
    the OEM never wrote is a term the factory never saw;
  * produce one canonical document per meaning, so key order, whitespace and
    equivalent spellings ("040000.00", "No Material") cannot give one set of
    terms two hashes;
  * derive the down-status keys from machine_status instead of re-listing
    them, and require the generic reasons AMP itself writes ("Breakdown" from
    MQTT, "Unknown" from the reason normaliser) so an auto-generated reason can
    never be read as the factory explaining a stop.

Run: DATABASE_URL="sqlite:///./ci.db" python test_contract_terms.py
"""
import ast
import copy
import io
import json
import os
import sys
import unicodedata
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import canonical  # noqa: E402
import contract_terms as ct  # noqa: E402
import machine_status  # noqa: E402
from contract_terms import TermsError  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def example():
    return {
        "schema": 1, "currency": "INR", "period_months": 1, "period_fee": "40000.00",
        "timezone": "Asia/Kolkata", "term_months": 12,
        "coverage": {"mode": "weekly",
                     "windows": [{"days": [0, 1, 2, 3, 4, 5], "start": "08:00",
                                  "end": "20:00"}],
                     "excluded_dates": ["2026-10-20"]},
        "sla_target_pct": "97.00",
        "credit_tiers": [{"below_pct": "97.00", "credit_pct": "5.00"},
                         {"below_pct": "95.00", "credit_pct": "10.00"}],
        "min_measured_pct": "90.00",
        "trusted_sources": ["mqtt"],
        "status_defaults": {"Breakdown": "OEM", "Maintenance": "FACTORY",
                            "Offline": "DISPUTED"},
        "reason_map": {"no material": "FACTORY", "power failure": "FACTORY",
                       "motor overheating": "OEM"},
        "generic_reasons": ["breakdown", "unknown"],
        "reason_lead_seconds": 600, "termination_notice_days": 30,
        "covered_installations": [{"installation_id": 12, "serial_number": "AER-0042"}],
    }


def _field_of(raw):
    try:
        ct.parse(raw)
    except TermsError as e:
        assert isinstance(e.field, str) and e.field, e
        assert str(e).startswith(e.field), str(e)
        return e.field
    return None


def _set(path, value):
    def mutate(doc):
        cur = doc
        for key in path[:-1]:
            cur = cur[key]
        if value is _DELETE:
            del cur[path[-1]]
        else:
            cur[path[-1]] = value
        return doc
    return mutate


_DELETE = object()


# --------------------------------------------------------------------------
# 1. a valid document
# --------------------------------------------------------------------------

def test_the_example_parses_into_typed_terms():
    t = ct.parse(example())
    assert t.schema == 1 and t.currency == "INR"
    assert t.period_months == 1 and t.term_months == 12
    assert t.period_fee == Decimal("40000.00") and type(t.period_fee) is Decimal
    assert t.sla_target_pct == Decimal("97.00")
    assert t.min_measured_pct == Decimal("90.00")
    assert [(x.below_pct, x.credit_pct) for x in t.credit_tiers] == [
        (Decimal("97.00"), Decimal("5.00")), (Decimal("95.00"), Decimal("10.00"))]
    assert t.timezone == "Asia/Kolkata" and str(t.zone) == "Asia/Kolkata"
    assert t.coverage.mode == "weekly"
    w = t.coverage.windows[0]
    assert w.days == (0, 1, 2, 3, 4, 5) and w.start_minute == 480 and w.end_minute == 1200
    assert t.coverage.excluded_dates == (date(2026, 10, 20),)
    assert t.trusted_sources == ("mqtt",)
    assert dict(t.status_defaults) == {"Breakdown": "OEM", "Maintenance": "FACTORY",
                                       "Offline": "DISPUTED"}
    assert dict(t.reason_map)["no material"] == "FACTORY"
    assert t.generic_reasons == ("breakdown", "unknown")
    assert t.reason_lead_seconds == 600 and t.termination_notice_days == 30
    assert [(c.installation_id, c.serial_number) for c in t.covered_installations] \
        == [(12, "AER-0042")]
    # JSON text parses to the same terms.
    assert ct.parse(json.dumps(example())) == t
    assert ct.parse(json.dumps(example()).encode("utf-8")) == t
    # 24x7 and an all-day window ending at 24:00 are valid.
    doc = example()
    doc["coverage"] = {"mode": "24x7"}
    assert ct.parse(doc).coverage.mode == "24x7"
    doc = example()
    doc["coverage"]["windows"] = [{"days": [6], "start": "00:00", "end": "24:00"}]
    assert ct.parse(doc).coverage.windows[0].end_minute == 1440
    doc = example()
    doc["credit_tiers"] = []
    assert ct.parse(doc).credit_tiers == ()
    print("PASS the plan's example parses into typed, immutable terms")


# --------------------------------------------------------------------------
# 2. every invalid field is refused by name
# --------------------------------------------------------------------------

INVALID = [
    # (label, mutation, the field the error must name)
    ("missing field", _set(["timezone"], _DELETE), "timezone"),
    ("unknown field", _set(["contract_value"], "1.00"), "contract_value"),
    ("unknown field credit_cap_pct", _set(["credit_cap_pct"], "10.00"), "credit_cap_pct"),
    ("schema 2", _set(["schema"], 2), "schema"),
    ("schema True", _set(["schema"], True), "schema"),
    ("currency USD", _set(["currency"], "USD"), "currency"),
    ("period_months 2", _set(["period_months"], 2), "period_months"),
    ("period_months '1'", _set(["period_months"], "1"), "period_months"),
    ("period_fee without places", _set(["period_fee"], "40000"), "period_fee"),
    ("period_fee as a number", _set(["period_fee"], 40000.0), "period_fee"),
    ("period_fee negative", _set(["period_fee"], "-1.00"), "period_fee"),
    ("unknown timezone", _set(["timezone"], "Mars/Olympus"), "timezone"),
    ("timezone path", _set(["timezone"], "../../etc/passwd"), "timezone"),
    ("timezone not text", _set(["timezone"], 5), "timezone"),
    ("term not a multiple of the period",
     lambda d: (d.update(period_months=3, term_months=13), d)[1], "term_months"),
    ("term 0", _set(["term_months"], 0), "term_months"),
    ("term 121", _set(["term_months"], 121), "term_months"),
    ("coverage mode", _set(["coverage", "mode"], "shifts"), "coverage.mode"),
    ("coverage extra key", _set(["coverage", "note"], "x"), "coverage.note"),
    ("24x7 with windows",
     lambda d: (d.update(coverage={"mode": "24x7", "windows": []}), d)[1],
     "coverage.windows"),
    ("weekly without windows", _set(["coverage", "windows"], _DELETE), "coverage.windows"),
    ("no windows", _set(["coverage", "windows"], []), "coverage.windows"),
    ("day 7", _set(["coverage", "windows", 0, "days"], [7]), "coverage.windows[0].days"),
    ("day bool", _set(["coverage", "windows", 0, "days"], [True]), "coverage.windows[0].days"),
    ("no days", _set(["coverage", "windows", 0, "days"], []), "coverage.windows[0].days"),
    ("repeated day", _set(["coverage", "windows", 0, "days"], [1, 1]),
     "coverage.windows[0].days"),
    ("start 8:00", _set(["coverage", "windows", 0, "start"], "8:00"),
     "coverage.windows[0].start"),
    ("start 24:00", _set(["coverage", "windows", 0, "start"], "24:00"),
     "coverage.windows[0].start"),
    ("end 24:01", _set(["coverage", "windows", 0, "end"], "24:01"),
     "coverage.windows[0].end"),
    ("end equals start", _set(["coverage", "windows", 0, "end"], "08:00"),
     "coverage.windows[0].end"),
    ("overnight window", _set(["coverage", "windows", 0, "end"], "06:00"),
     "coverage.windows[0].end"),
    ("window extra key", _set(["coverage", "windows", 0, "tz"], "UTC"),
     "coverage.windows[0].tz"),
    ("duplicate window",
     lambda d: (d["coverage"]["windows"].append(dict(d["coverage"]["windows"][0])), d)[1],
     "coverage.windows"),
    ("impossible date", _set(["coverage", "excluded_dates"], ["2026-02-30"]),
     "coverage.excluded_dates[0]"),
    ("date format", _set(["coverage", "excluded_dates"], ["20/10/2026"]),
     "coverage.excluded_dates[0]"),
    ("repeated date", _set(["coverage", "excluded_dates"], ["2026-10-20", "2026-10-20"]),
     "coverage.excluded_dates"),
    ("target above 100", _set(["sla_target_pct"], "100.01"), "sla_target_pct"),
    ("tier below not descending",
     _set(["credit_tiers"], [{"below_pct": "95.00", "credit_pct": "5.00"},
                             {"below_pct": "97.00", "credit_pct": "10.00"}]),
     "credit_tiers[1].below_pct"),
    ("tier credit not ascending",
     _set(["credit_tiers"], [{"below_pct": "97.00", "credit_pct": "10.00"},
                             {"below_pct": "95.00", "credit_pct": "10.00"}]),
     "credit_tiers[1].credit_pct"),
    ("credit above 100", _set(["credit_tiers", 1, "credit_pct"], "100.01"),
     "credit_tiers[1].credit_pct"),
    ("zero credit tier", _set(["credit_tiers", 0, "credit_pct"], "0.00"),
     "credit_tiers[0].credit_pct"),
    ("tier above the target", _set(["credit_tiers", 0, "below_pct"], "98.00"),
     "credit_tiers[0].below_pct"),
    ("tier below 0.00", _set(["credit_tiers"], [{"below_pct": "0.00", "credit_pct": "5.00"}]),
     "credit_tiers[0].below_pct"),
    ("tier extra key", _set(["credit_tiers", 0, "cap"], "1.00"), "credit_tiers[0].cap"),
    ("tiers not a list", _set(["credit_tiers"], {}), "credit_tiers"),
    ("min measured text", _set(["min_measured_pct"], "ninety"), "min_measured_pct"),
    ("no trusted sources", _set(["trusted_sources"], []), "trusted_sources"),
    ("manual is not a span source", _set(["trusted_sources"], ["manual"]),
     "trusted_sources[0]"),
    ("upper-case source", _set(["trusted_sources"], ["MQTT"]), "trusted_sources[0]"),
    ("repeated source", _set(["trusted_sources"], ["mqtt", "mqtt"]), "trusted_sources"),
    ("status_defaults missing Offline", _set(["status_defaults", "Offline"], _DELETE),
     "status_defaults"),
    ("status_defaults with Down", _set(["status_defaults", "Down"], "OEM"),
     "status_defaults.Down"),
    ("status_defaults with Running", _set(["status_defaults", "Running"], "FACTORY"),
     "status_defaults.Running"),
    ("status default AVAILABLE", _set(["status_defaults", "Offline"], "AVAILABLE"),
     "status_defaults.Offline"),
    ("reason mapped to DISPUTED", _set(["reason_map", "no material"], "DISPUTED"),
     "reason_map.no material"),
    ("reason keys colliding after normalisation",
     _set(["reason_map", " No Material "], "OEM"), "reason_map"),
    ("reason map key that is generic", _set(["reason_map", "Breakdown"], "OEM"),
     "reason_map.Breakdown"),
    ("reason map key that is blank", _set(["reason_map", "   "], "OEM"),
     "reason_map.   "),
    ("generic reasons without breakdown", _set(["generic_reasons"], ["unknown"]),
     "generic_reasons"),
    ("generic reasons without unknown", _set(["generic_reasons"], ["breakdown"]),
     "generic_reasons"),
    ("generic reasons repeated", _set(["generic_reasons"], ["breakdown", "Breakdown",
                                                           "unknown"]),
     "generic_reasons"),
    ("lead negative", _set(["reason_lead_seconds"], -1), "reason_lead_seconds"),
    ("lead float", _set(["reason_lead_seconds"], 600.0), "reason_lead_seconds"),
    ("notice 400 days", _set(["termination_notice_days"], 400), "termination_notice_days"),
    ("no covered installations", _set(["covered_installations"], []),
     "covered_installations"),
    ("installation id 0", _set(["covered_installations", 0, "installation_id"], 0),
     "covered_installations[0].installation_id"),
    ("installation id bool", _set(["covered_installations", 0, "installation_id"], True),
     "covered_installations[0].installation_id"),
    ("blank serial", _set(["covered_installations", 0, "serial_number"], " "),
     "covered_installations[0].serial_number"),
    ("repeated installation",
     lambda d: (d["covered_installations"].append(
         {"installation_id": 12, "serial_number": "AER-0043"}), d)[1],
     "covered_installations"),
    ("repeated serial",
     lambda d: (d["covered_installations"].append(
         {"installation_id": 13, "serial_number": "AER-0042"}), d)[1],
     "covered_installations"),
    ("lone surrogate in a serial",
     _set(["covered_installations", 0, "serial_number"], "AER-\ud800"),
     "covered_installations[0].serial_number"),
]


def test_each_invalid_field_is_refused_by_name():
    for label, mutate, field in INVALID:
        doc = mutate(copy.deepcopy(example()))
        got = _field_of(doc)
        assert got == field, f"{label}: expected refusal naming {field!r}, got {got!r}"
    print(f"PASS {len(INVALID)} invalid documents each refused, naming the field")


def test_malformed_json_is_refused():
    base = json.dumps(example())
    cases = {
        "not json": "{",
        "top-level list": "[]",
        "NaN": base.replace('"reason_lead_seconds": 600', '"reason_lead_seconds": NaN'),
        "duplicate key": base.replace('"schema": 1', '"schema": 1, "schema": 1'),
        "float fee": base.replace('"period_fee": "40000.00"', '"period_fee": 40000.00'),
    }
    for label, text in cases.items():
        assert _field_of(text) is not None, f"{label} was accepted"
    assert _field_of(None) is not None
    assert _field_of(42) is not None
    print(f"PASS malformed JSON refused ({len(cases)} texts plus non-documents)")


# --------------------------------------------------------------------------
# 3. one meaning, one hash
# --------------------------------------------------------------------------

def test_the_hash_ignores_key_order_whitespace_and_equivalent_spellings():
    h = ct.terms_hash(ct.parse(example()))
    assert canonical.is_sha256_hex(h)
    reordered = json.loads(json.dumps(example()), object_pairs_hook=lambda p: dict(reversed(p)))
    assert ct.terms_hash(ct.parse(reordered)) == h
    spaced = json.dumps(example(), indent=4, sort_keys=True)
    assert ct.terms_hash(ct.parse(spaced)) == h
    doc = example()
    doc["period_fee"] = "040000.00"
    doc["reason_map"] = {"NO MATERIAL": "FACTORY", " Power Failure": "FACTORY",
                         "Motor Overheating ": "OEM"}
    doc["generic_reasons"] = ["Unknown", "BREAKDOWN"]
    doc["coverage"]["windows"][0]["days"] = [5, 4, 3, 2, 1, 0]
    assert ct.terms_hash(ct.parse(doc)) == h
    print("PASS the terms hash is stable under key order, whitespace, case and ordering")


def test_the_hash_changes_with_any_term():
    h = ct.terms_hash(ct.parse(example()))
    changes = [
        _set(["period_fee"], "40000.01"),
        _set(["reason_map", "no material"], "OEM"),
        _set(["trusted_sources"], ["mqtt", "simulator"]),
        _set(["coverage", "windows", 0, "end"], "20:01"),
        _set(["coverage", "excluded_dates"], []),
        _set(["status_defaults", "Offline"], "FACTORY"),
        _set(["reason_lead_seconds"], 601),
        _set(["covered_installations", 0, "serial_number"], "AER-0043"),
        _set(["credit_tiers", 1, "credit_pct"], "10.01"),
    ]
    for mutate in changes:
        assert ct.terms_hash(ct.parse(mutate(copy.deepcopy(example())))) != h
    print(f"PASS each of {len(changes)} term changes changes the hash")


def test_the_canonical_document_is_a_fixed_point():
    t = ct.parse(example())
    doc = ct.terms_canonical(t)
    assert ct.parse(doc) == t
    assert ct.terms_canonical(ct.parse(doc)) == doc
    text = ct.terms_json_text(t)
    assert text.encode("utf-8") == canonical.canonical_bytes(doc)
    assert canonical.sha256_hex(text.encode("utf-8")) == ct.terms_hash(t)
    assert ct.parse(text) == t
    print("PASS parse(terms_canonical(t)) == t, and terms_json_text hashes to terms_hash")


# --------------------------------------------------------------------------
# 4. reason keys, status keys, required generic reasons
# --------------------------------------------------------------------------

def test_reason_key_normalises_nfc_case_and_space():
    assert ct.reason_key("No Material") == "no material"
    assert ct.reason_key("  NO MATERIAL  ") == "no material"
    assert ct.reason_key("Café stop") == ct.reason_key("CAFÉ STOP") == "café stop"
    assert ct.reason_key("STRASSE") == ct.reason_key("Straße") == "strasse"
    assert ct.reason_key(None) == "unknown"
    assert ct.reason_key("") == "unknown"
    assert ct.reason_key("   ") == "unknown"
    for tricky in ("ǰ", "J̌", "ΐ", "ﬁ", "ẞ", "Å", "Å"):
        once = ct.reason_key(tricky)
        assert ct.reason_key(once) == once, (tricky, once)
        assert unicodedata.normalize("NFC", once) == once
    for bad in (5, b"x", ["x"]):
        try:
            ct.reason_key(bad)
        except TermsError:
            continue
        raise AssertionError(f"reason_key accepted {bad!r}")
    print("PASS reason_key: NFC + casefold + strip, idempotent, None/blank -> 'unknown'")


def test_down_status_keys_are_derived_from_the_vocabulary():
    assert ct.down_status_keys() == ("Breakdown", "Maintenance", "Offline")
    assert set(ct.AVAILABLE_STATUSES) == {"Running", "Idle"}
    assert set(ct.AVAILABLE_STATUSES) | set(ct.down_status_keys()) == \
        set(machine_status.VALID_MACHINE_STATUSES)
    assert "Down" not in ct.down_status_keys()
    # Derived, not re-listed: the module never spells the down statuses.
    path = os.path.join(HERE, "contract_terms.py")
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    literals = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    functions = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "down_status_keys" in functions, "structural scan did not find down_status_keys"
    for status in ("Breakdown", "Maintenance", "Offline", "Running"):
        assert status not in literals, f"contract_terms.py spells {status!r} as a literal"
    print("PASS down status keys = vocabulary minus Running/Idle, derived not re-listed")


def test_the_generic_reasons_amp_writes_itself_are_required():
    assert set(ct.REQUIRED_GENERIC_REASONS) == {"breakdown", "unknown"}
    # MQTT writes its own DowntimeLog reason on a transition into Breakdown.
    # Every literal reason it writes must be a required generic reason, or an
    # auto-generated log would read as the factory explaining the stop.
    path = os.path.join(HERE, "mqtt_service.py")
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "reason" and isinstance(kw.value, ast.Constant) \
                        and isinstance(kw.value.value, str):
                    found.append(kw.value.value)
    assert found, "structural scan found no literal reason= in mqtt_service.py"
    for reason in found:
        assert ct.reason_key(reason) in ct.REQUIRED_GENERIC_REASONS, reason
    assert ct.MQTT_AUTO_REASON in found
    print(f"PASS mqtt_service writes {sorted(set(found))}; each is a required generic reason")


def test_contract_terms_contains_no_float():
    path = os.path.join(HERE, "contract_terms.py")
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    nodes = list(ast.walk(tree))
    functions = {n.name for n in nodes if isinstance(n, ast.FunctionDef)}
    for required in ("parse", "terms_canonical", "terms_hash", "reason_key",
                     "down_status_keys"):
        assert required in functions, f"structural scan did not find {required}"
    bad = [n.lineno for n in nodes
           if (isinstance(n, ast.Constant) and type(n.value) is float)
           or (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "float")
           or (isinstance(n, (ast.BinOp, ast.AugAssign)) and isinstance(n.op, ast.Div))]
    assert not bad, f"float literal, float() or '/' at lines {bad}"
    print(f"PASS contract_terms.py: {len(functions)} functions found, no float")


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"ALL {len(tests)} CONTRACT TERMS TESTS PASSED")
