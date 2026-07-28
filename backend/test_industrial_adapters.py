"""Unit tests for industrial_adapters — the PLC/OT protocol classification.

protocol_for() maps a stored device.protocol string to a known protocol key,
which get_adapter() uses to pick the SimulatorAdapter's signal templates. It runs
on every tick_industrial() poll, so a misclassification silently feeds a device
the WRONG protocol's signals (and stamps the wrong source_protocol on the row).

The classifier used to test `key in normalised_protocol` after stripping spaces
and hyphens, so the two-letter key "ab" (Allen-Bradley) matched any protocol
whose name merely CONTAINED the letters a-b — "Fabricated", "Grabber", "Crab"
were all read as Allen-Bradley. These pin the token-based classifier: every real
protocol still resolves correctly, and a name that only happens to contain a
short key's letters no longer false-matches.

Pure functions, stubbed device rows, no DB.

Run:  python backend/test_industrial_adapters.py     (exit 0 = pass)
"""
from types import SimpleNamespace

import industrial_adapters as ia


def _device(protocol):
    return SimpleNamespace(protocol=protocol)


def test_seeded_demo_protocols_round_trip():
    # The seed stores each protocol's display name (meta["name"]) on the device;
    # protocol_for must map that name back to the SAME key it was seeded from, or
    # the device reads a different protocol's signals than it represents. This is
    # an independent invariant: the expected key is the one PROTOCOLS itself
    # declares, derived here from that table, not hand-copied.
    for proto in ia.PROTOCOLS:
        got = ia.protocol_for(_device(proto["name"]))
        assert got == proto["key"], f"{proto['name']!r} -> {got!r}, expected {proto['key']!r}"
    print("PASS every seeded demo protocol name classifies back to its own key")


def test_real_protocol_names_and_variants():
    # Each expected value is derived by hand from the protocol's identity, not
    # from the function under test.
    expected = {
        "OPC UA": "opcua",
        "Modbus TCP": "modbus",
        "Siemens S7": "s7",
        "Allen-Bradley": "ab",
        "Beckhoff ADS": "beckhoff",
        "Omron FINS": "omron",
        # Separator / casing / run-together variants a user might type.
        "OPC-UA": "opcua",
        "opcua": "opcua",
        "OPCUAServer": "opcua",
        "Modbus RTU": "modbus",
        "S7comm": "s7",
        "S7-1200": "s7",
        "Siemens": "s7",       # Siemens without the "S7" token
        "Rockwell": "ab",      # Allen-Bradley's vendor name
        "allen": "ab",
        "AB": "ab",            # someone types the bare key
    }
    for proto, want in expected.items():
        got = ia.protocol_for(_device(proto))
        assert got == want, f"{proto!r} -> {got!r}, expected {want!r}"
    print("PASS real protocol names and separator/casing variants classify correctly")


def test_names_containing_ab_are_not_allen_bradley():
    # The exact regression: a protocol whose name merely CONTAINS the letters "ab"
    # (or "s7") must NOT be forced onto Allen-Bradley / Siemens — it is unrecognised
    # and falls back to the Modbus default. Old code returned "ab" for every one of
    # these; the token classifier returns "modbus".
    for proto in ("Fabricated Protocol", "Grabber", "Crab", "Fabric",
                  "Modbus lab", "tab", "Slab-7", "Grab-line"):
        got = ia.protocol_for(_device(proto))
        assert got == "modbus", f"{proto!r} -> {got!r}, expected 'modbus'"
    print("PASS names that merely contain 'ab'/'s7' are not misread as AB/Siemens")


def test_modbus_wins_over_incidental_ab_letters():
    # A real Modbus device labelled with an "ab"-containing word must stay Modbus,
    # not flip to Allen-Bradley on the incidental letters (the exact page-level
    # symptom of the old bug: a Modbus PLC read through Rockwell templates).
    assert ia.protocol_for(_device("Modbus fabric bridge")) == "modbus"
    print("PASS an 'ab'-containing Modbus label stays Modbus")


def test_empty_and_none_default_to_modbus():
    # No crash on None / empty / whitespace / punctuation-only; all default to the
    # ubiquitous Modbus rather than raising.
    for proto in (None, "", "   ", "---", "/"):
        assert ia.protocol_for(_device(proto)) == "modbus", repr(proto)
    print("PASS empty / None / punctuation-only protocol defaults to modbus, no crash")


def test_get_adapter_reads_that_protocols_signals():
    # get_adapter -> SimulatorAdapter reads exactly the classified protocol's signal
    # templates. An Omron device must yield Omron's signals (line_pressure, ...),
    # never Allen-Bradley's — the observable consequence of correct classification.
    adapter = ia.get_adapter(_device("Omron FINS"))
    assert adapter.protocol_key == "omron"
    names = {name for name, _value, _unit in adapter.read(_device("Omron FINS"))}
    expected = {n for n, _u, _lo, _hi in ia._SIGNAL_TEMPLATES["omron"]}
    assert names == expected, f"{names!r} != {expected!r}"
    print("PASS get_adapter reads the classified protocol's own signal templates")


def test_simulator_values_are_within_template_bounds():
    # Every simulated value sits inside its template's [lo, hi] — the read never
    # emits an out-of-range reading that the ingest/clamp path would then reject.
    for key, templates in ia._SIGNAL_TEMPLATES.items():
        adapter = ia.SimulatorAdapter(key)
        bounds = {name: (lo, hi) for name, _unit, lo, hi in templates}
        for name, value, _unit in adapter.read(_device(key)):
            lo, hi = bounds[name]
            assert lo <= value <= hi, f"{key}:{name} value {value} outside [{lo}, {hi}]"
    print("PASS simulated signal values stay within each template's bounds")


if __name__ == "__main__":
    test_seeded_demo_protocols_round_trip()
    test_real_protocol_names_and_variants()
    test_names_containing_ab_are_not_allen_bradley()
    test_modbus_wins_over_incidental_ab_letters()
    test_empty_and_none_default_to_modbus()
    test_get_adapter_reads_that_protocols_signals()
    test_simulator_values_are_within_template_bounds()
    print("ALL INDUSTRIAL-ADAPTER TESTS PASSED")
