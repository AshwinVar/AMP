"""Industrial adapter resilience — what happens when a PLC does NOT answer.

test_industrial_adapters.py covers the protocol CLASSIFIER thoroughly and stops
there. Everything after classification is untested: what the factory actually
returns, what a failing read does to the caller, and what the poll loop tells the
rest of the platform. Those are the parts that decide whether one unreachable PLC
costs a reading or costs a whole tick.

Two things this suite states plainly rather than dressing up.

FIRST — there is no real driver. PROTOCOLS advertises six industrial protocols
and names the library an edge agent would use for each, but not one of those
libraries is a dependency and no subclass of ProtocolAdapter other than
SimulatorAdapter exists. get_adapter() therefore returns the simulator for every
device, always. Writing an "OPC UA read" test here would be fiction, so instead
these tests PIN that state: if a real driver ever lands, the pins fail and force
the author to say what the new contract is.

SECOND — tick_industrial has no failure handling of its own. An adapter that
raises propagates straight out, and the only thing catching it is the blanket
try/except in main.py's simulation loop, which also wraps every OTHER tick for
that tenant. So one unreachable PLC does not crash the server, but it does cost
that tenant the rest of its simulation pass, and a scan that dies half-way leaves
partial signals staged in the caller's session. Those consequences are asserted
here, in the caller's own shape, because they are invisible from
industrial_adapters.py alone.

Run:  python backend/test_adapter_resilience.py     (exit 0 = pass)
"""
import os
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

import events
import industrial_adapters as ia
import models
from database import Base

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))


def _device(protocol):
    """A stub row for the pure classifier tests — protocol_for reads one field."""
    return SimpleNamespace(protocol=protocol)


def _session():
    """Fresh in-memory DB per test; the industrial tables come from the models."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _add_device(db, protocol="Modbus TCP", status="Online", code="PLC-01"):
    device = models.IndustrialDevice(
        device_code=code, device_name="Line A PLC", device_type="PLC",
        protocol=protocol, ip_address="192.168.10.22:502", status=status,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


class _UnreachableAdapter(ia.ProtocolAdapter):
    """A driver whose PLC does not answer — the everyday OT failure."""

    def read(self, device):
        raise ConnectionError("PLC unreachable: 192.168.10.22:502 connection refused")


class _HalfScanAdapter(ia.ProtocolAdapter):
    """A driver whose link drops PART WAY through a scan.

    Written as a generator on purpose: a real driver streams tags as it reads
    them, so tick_industrial has already persisted some of the scan into the
    session by the time the link dies.
    """

    def read(self, device):
        yield ("flow_rate", 42, "L/min")
        yield ("tank_level", 61, "%")
        raise ConnectionResetError("ADS link dropped mid-scan")


# ── What actually exists today ─────────────────────────────────────────

def test_get_adapter_returns_the_simulator_for_every_protocol():
    """Pins the honest state: there is exactly one adapter implementation.

    Failure this catches: a reader (or a demo) believing AMP is talking to a real
    PLC. Every protocol in PROTOCOLS, and every unrecognised string, resolves to
    SimulatorAdapter — generated values, no hardware, no network. If a real
    driver is added and get_adapter starts returning it, this test fails and the
    author must state the new contract instead of it changing silently.
    """
    for proto in ia.PROTOCOLS:
        adapter = ia.get_adapter(_device(proto["name"]))
        assert type(adapter) is ia.SimulatorAdapter, (proto["name"], type(adapter))
        assert adapter.protocol_key == proto["key"], (proto["name"], adapter.protocol_key)

    for unknown in (None, "", "Profinet", "EtherCAT"):
        adapter = ia.get_adapter(_device(unknown))
        assert type(adapter) is ia.SimulatorAdapter, (unknown, type(adapter))

    # The class hierarchy says the same thing: one concrete adapter ships. Filter
    # to classes defined in industrial_adapters, since this file's own failure
    # doubles subclass ProtocolAdapter too.
    shipped = [c for c in ia.ProtocolAdapter.__subclasses__() if c.__module__ == ia.__name__]
    assert shipped == [ia.SimulatorAdapter], shipped
    print("PASS get_adapter returns SimulatorAdapter for every protocol - no real driver exists")


def test_no_real_protocol_driver_library_is_a_dependency():
    """Pins WHY the simulator is the only adapter: none of the drivers are installed.

    PROTOCOLS names the library an edge agent needs per protocol (asyncua,
    pymodbus, python-snap7, pycomm3, pyads, aphyt/fins). Not one is in
    requirements.txt, so a "real" read is not merely unimplemented — it is
    impossible in this deployment. The library names are read out of PROTOCOLS
    rather than hard-coded here, so the check cannot drift from the table it is
    asserting about. Adding a driver dependency fails this test, which is the
    point: it should come with the adapter that uses it.
    """
    with open(os.path.join(_BACKEND_DIR, "requirements.txt"), encoding="utf-8") as fh:
        requirements = fh.read().lower()

    for protocol in ia.PROTOCOLS:
        # "aphyt / fins" names two candidate packages; check both.
        for library in (part.strip() for part in protocol["library"].split("/")):
            assert library and library not in requirements, \
                f"{protocol['name']} driver {library!r} is now a dependency - is there an adapter for it?"

    # CONTROL: the check can find a package that IS present, so "not in
    # requirements" above is a real absence and not a broken read of the file.
    assert "paho-mqtt" in requirements
    print("PASS none of the six advertised protocol driver libraries is a dependency")


def test_the_base_adapter_read_is_unimplemented():
    """Pins the contract a real edge-agent driver has to fill.

    ProtocolAdapter.read raises NotImplementedError rather than returning an
    empty list. That matters: an empty list would look like a device that is
    connected and reporting nothing, and the connectivity dashboard would show a
    healthy PLC producing no signals rather than an unimplemented driver.
    """
    base = ia.ProtocolAdapter("modbus")
    try:
        base.read(_device("Modbus TCP"))
    except NotImplementedError:
        pass
    else:
        raise AssertionError("ProtocolAdapter.read must not silently return")
    print("PASS the base adapter's read is unimplemented rather than silently empty")


# ── Classifier behaviour on input it was not designed for ──────────────

def test_unknown_protocol_strings_fall_back_to_modbus():
    """Pins the fallback for input the classifier does not recognise.

    The first entry matters most: "MQTT" is the DEFAULT value of
    IndustrialDevice.protocol, so every device created without an explicit
    protocol is classified as Modbus and read through Modbus templates. That is
    the shipped behaviour, asserted here against the column default itself so it
    cannot drift, because it is surprising enough to be worth writing down.

    The rest are the shapes a free-text field really collects: a bare port
    number, a homoglyph (a Cyrillic capital O in place of the Latin one, which
    the a-z0-9 tokeniser drops), and unbounded junk. None may raise, and none may
    be forced onto a protocol it does not name.
    """
    default = models.IndustrialDevice.__table__.columns["protocol"].default.arg
    assert default == "MQTT", default
    assert ia.protocol_for(_device(default)) == "modbus"

    for proto in (
        "502",                                  # someone typed the port
        "ОPC UA",                          # Cyrillic O homoglyph, not OPC UA
        "unknown-protocol-" + "x" * 300,        # unbounded free text
        "  \t\n ",                              # whitespace of several kinds
        "!@#$%^&*()",                           # punctuation only
    ):
        assert ia.protocol_for(_device(proto)) == "modbus", repr(proto[:40])
    print("PASS unknown protocol text (including the 'MQTT' column default) falls back to modbus")


def test_ambiguous_protocol_strings_resolve_by_alias_table_order():
    """Pins which protocol wins when a name mentions TWO of them.

    Gateway devices are routinely labelled "Modbus to OPC UA gateway", so this is
    not a contrived input. The winner is decided by the order of
    _PROTOCOL_ALIASES, NOT by which name appears first in the string — asserted
    by classifying both word orders and getting the same answer, then checking
    that answer is the earlier of the two in the alias table. Anything that
    reordered the table (say, to put modbus first as the default) would silently
    re-point every gateway device at a different protocol's signal templates.
    """
    order = [key for key, _aliases in ia._PROTOCOL_ALIASES]
    assert order.index("opcua") < order.index("modbus"), order
    assert order.index("s7") < order.index("ab"), order

    for text, want in (
        ("Modbus to OPC UA gateway", "opcua"),
        ("OPC UA to Modbus gateway", "opcua"),      # same answer, opposite word order
        ("Siemens Allen-Bradley bridge", "s7"),
        ("Allen-Bradley Siemens bridge", "s7"),     # same answer, opposite word order
    ):
        got = ia.protocol_for(_device(text))
        assert got == want, f"{text!r} -> {got!r}, expected {want!r}"
    print("PASS an ambiguous protocol name resolves by alias-table order, not word order")


def test_a_non_string_protocol_raises_rather_than_misclassifying():
    """Pins the classifier's input contract: str or None, nothing else.

    protocol_for does `(device.protocol or "").lower()`, so anything that is
    neither a string nor None raises AttributeError. In practice device.protocol
    is a String column so this cannot happen through the ORM — but the contract is
    worth pinning, because the alternative (coercing with str()) would classify
    str(502) or str(["Modbus"]) as a protocol name and quietly pick a driver from
    it. A loud failure at the boundary is the better behaviour, so it is asserted
    rather than left to chance.
    """
    for bad in (502, 4.0, ["Modbus TCP"], {"protocol": "Modbus TCP"}):
        try:
            ia.protocol_for(_device(bad))
        except AttributeError:
            continue
        raise AssertionError(f"{bad!r} was accepted as a protocol string")
    print("PASS a non-string protocol raises at the boundary instead of being coerced")


# ── Polling: failure, blast radius and partial scans ───────────────────

def test_offline_devices_are_not_polled():
    """Pins: a device marked Offline is skipped, and an empty floor is not an error.

    Failure this catches: tick_industrial polling every row regardless of status,
    which would fabricate live signals for a PLC that is known to be down and
    make the connectivity dashboard claim a dead device is reporting.
    """
    Session = _session()
    db = Session()
    _add_device(db, status="Offline", code="PLC-OFF")
    ia.tick_industrial(db)
    assert db.query(models.IndustrialSignal).count() == 0

    # ...and a floor with no devices at all returns quietly rather than raising
    # on random.choice of an empty list. Its own DB, so it cannot disturb the
    # device the control below re-reads.
    empty = _session()()
    ia.tick_industrial(empty)
    assert empty.query(models.IndustrialSignal).count() == 0
    empty.close()

    # CONTROL: flipping the same device Online DOES produce signals, so the zeros
    # above are the status filter working and not a fixture that never polls.
    device = db.query(models.IndustrialDevice).first()
    device.status = "Online"
    db.commit()
    ia.tick_industrial(db)
    assert db.query(models.IndustrialSignal).count() == 3, db.query(models.IndustrialSignal).count()
    db.close()
    print("PASS an Offline device (and an empty floor) is skipped; Online is polled")


def test_an_adapter_failure_propagates_and_costs_the_rest_of_the_tick():
    """Pins the real blast radius of one unreachable PLC.

    tick_industrial does not guard adapter.read, so a ConnectionError propagates
    to the caller. main.py's simulation loop wraps the whole per-tenant tick in
    one try/except + rollback, so the server survives — but tick_industrial sits
    THIRD in that sequence, so everything after it (tick_production, the
    utilization drift, the tenant's commit) is skipped for that pass. One PLC
    refusing a connection therefore freezes that tenant's whole simulated
    factory, not just its signals.

    The caller's shape is reproduced here rather than described, so the cost is
    asserted rather than asserted-about.
    """
    Session = _session()
    db = Session()
    _add_device(db)
    original = ia.get_adapter
    try:
        ia.get_adapter = lambda device: _UnreachableAdapter("modbus")

        try:
            ia.tick_industrial(db)
        except ConnectionError:
            pass
        else:
            raise AssertionError("tick_industrial swallowed the adapter failure")
        assert db.query(models.IndustrialSignal).count() == 0

        # main.py _simulation_loop's shape: one try/except around the whole tick.
        reached_the_next_tick = False
        try:
            ia.tick_industrial(db)
            reached_the_next_tick = True     # stands in for tick_production() and the rest
        except Exception:
            db.rollback()
        assert reached_the_next_tick is False, "the failure should have skipped the rest of the pass"
    finally:
        ia.get_adapter = original

    # CONTROL: with the real (simulator) adapter the identical block runs to
    # completion, so the False above is the failure propagating and not the test
    # shape never reaching the flag.
    reached_the_next_tick = False
    try:
        ia.tick_industrial(db)
        reached_the_next_tick = True
    except Exception:
        db.rollback()
    assert reached_the_next_tick is True
    assert db.query(models.IndustrialSignal).count() == 3
    db.close()
    print("PASS an adapter failure propagates out of the tick and costs the rest of the pass")


def test_a_scan_that_dies_half_way_leaves_partial_signals_staged():
    """Pins: tick_industrial has no transaction guard, so the CALLER owns the mess.

    A link that drops mid-scan leaves the signals read so far already added to the
    caller's session, unflushed. Whether they land depends entirely on what the
    caller does next: main.py rolls back, so nothing is written; a caller that
    simply carries on writes a half-scan into industrial_signals, where it is
    indistinguishable from a complete one. Both branches are exercised so the
    dependency on the caller is explicit rather than incidental.
    """
    Session = _session()
    original = ia.get_adapter
    try:
        ia.get_adapter = lambda device: _HalfScanAdapter("modbus")

        # (a) The caller rolls back, as main.py's simulation loop does.
        db = Session()
        _add_device(db)
        try:
            ia.tick_industrial(db)
        except ConnectionResetError:
            pass
        else:
            raise AssertionError("the mid-scan failure should have propagated")

        staged = [obj for obj in db.new if isinstance(obj, models.IndustrialSignal)]
        assert len(staged) == 2, len(staged)      # the two tags read before the drop
        db.rollback()
        assert db.query(models.IndustrialSignal).count() == 0
        db.close()

        # (b) A caller that does NOT roll back persists the half-scan on its next
        #     commit. This is the hazard the rollback above is preventing.
        Session = _session()
        db = Session()
        _add_device(db)
        try:
            ia.tick_industrial(db)
        except ConnectionResetError:
            pass
        db.commit()
        rows = db.query(models.IndustrialSignal).all()
        assert len(rows) == 2, len(rows)
        assert {r.signal_name for r in rows} == {"flow_rate", "tank_level"}, rows
        db.close()
    finally:
        ia.get_adapter = original
    print("PASS a mid-scan failure stages a partial scan that only the caller's rollback discards")


# ── What the poll loop tells the rest of the platform ──────────────────

def test_a_tick_publishes_no_domain_event():
    """Pins what the industrial path does NOT do: it publishes nothing.

    tick_industrial writes IndustrialSignal rows and stops. It raises no
    DowntimeStarted, appends no EventLog entry, and therefore reaches none of the
    AI subscribers (ai/agents.py, ai/subscribers.py) or ai/insights.py, which read
    the event log. A PLC reporting an alarming value is invisible to every one of
    them. That is a gap, not a bug in the code as written — pinned so anyone
    wiring OT signals into the event backbone knows they are adding a capability
    rather than fixing one.
    """
    Session = _session()
    db = Session()
    _add_device(db)
    ia.tick_industrial(db)

    assert db.query(models.IndustrialSignal).count() == 3     # the poll did happen
    assert db.query(models.EventLog).count() == 0             # ...and told nobody

    # CONTROL: the event backbone DOES append to this very DB when something
    # publishes, so the zero above is silence from the industrial path and not an
    # EventLog table that never receives anything in this fixture. A private bus
    # is used so no globally-registered subscriber is invoked.
    bus = events.EventBus()
    bus.publish(events.DowntimeStarted(
        tenant_code="DEFAULT", machine_id=None, reason="Breakdown", duration="10 min"), db)
    db.commit()
    assert db.query(models.EventLog).count() == 1
    db.close()
    print("PASS a poll writes signals and publishes no domain event (control: the bus works here)")


def test_signal_rows_are_stamped_with_the_raw_protocol_not_the_classified_key():
    """Pins a divergence worth knowing about when reading signal history.

    The adapter is chosen by the CLASSIFIED key, but source_protocol on the row is
    the device's RAW free text. A device left on the "MQTT" column default is
    therefore read through Modbus templates while every row it produces is stamped
    "MQTT" — a protocol that is not in PROTOCOLS at all. Anyone grouping signal
    history by source_protocol is grouping by what was typed, not by what was read.
    """
    Session = _session()
    db = Session()
    device = _add_device(db, protocol="MQTT")
    assert ia.get_adapter(device).protocol_key == "modbus"

    ia.tick_industrial(db)
    rows = db.query(models.IndustrialSignal).all()
    assert {r.source_protocol for r in rows} == {"MQTT"}, {r.source_protocol for r in rows}
    assert {r.signal_name for r in rows} == {n for n, *_rest in ia._SIGNAL_TEMPLATES["modbus"]}, rows
    db.close()
    print("PASS signal rows carry the raw device protocol while the read used the classified key")


def test_the_signal_table_is_pruned_back_to_its_bound():
    """Pins the only thing stopping industrial_signals growing without limit.

    Every poll appends and nothing else deletes, so a floor of devices polled
    every 45 seconds would fill the table indefinitely. The tick trims to 1,000
    once it passes 1,200, oldest first. Both halves are asserted — the trim fires,
    and it removes the OLD rows rather than an arbitrary 200 — because deleting
    the newest would leave the connectivity dashboard permanently showing stale
    readings while the row count looked healthy.
    """
    Session = _session()
    db = Session()
    device = _add_device(db)
    db.add_all([
        models.IndustrialSignal(
            device_id=device.id, signal_name="stale_filler", signal_value="1",
            numeric_value=1, unit="x", quality="Good", source_protocol="Modbus TCP")
        for _ in range(1199)
    ])
    db.commit()

    ia.tick_industrial(db)      # 1199 + 3 = 1202 -> over the 1200 bound

    assert db.query(models.IndustrialSignal).count() == 1000, db.query(models.IndustrialSignal).count()
    fresh = db.query(models.IndustrialSignal).filter(
        models.IndustrialSignal.signal_name != "stale_filler").all()
    assert len(fresh) == 3, [r.signal_name for r in fresh]     # the newest rows survived
    db.close()

    # CONTROL: below the bound nothing is deleted, so the 1,000 above is the trim
    # firing and not a fixture that loses rows on every tick.
    db = _session()()
    _add_device(db)
    ia.tick_industrial(db)
    ia.tick_industrial(db)
    assert db.query(models.IndustrialSignal).count() == 6, db.query(models.IndustrialSignal).count()
    db.close()
    print("PASS the signal table trims to 1000 past 1200, oldest first, and not before")


if __name__ == "__main__":
    test_get_adapter_returns_the_simulator_for_every_protocol()
    test_no_real_protocol_driver_library_is_a_dependency()
    test_the_base_adapter_read_is_unimplemented()
    test_unknown_protocol_strings_fall_back_to_modbus()
    test_ambiguous_protocol_strings_resolve_by_alias_table_order()
    test_a_non_string_protocol_raises_rather_than_misclassifying()
    test_offline_devices_are_not_polled()
    test_an_adapter_failure_propagates_and_costs_the_rest_of_the_tick()
    test_a_scan_that_dies_half_way_leaves_partial_signals_staged()
    test_a_tick_publishes_no_domain_event()
    test_signal_rows_are_stamped_with_the_raw_protocol_not_the_classified_key()
    test_the_signal_table_is_pruned_back_to_its_bound()
    print("ADAPTER RESILIENCE OK: simulator-only today, failure blast radius pinned")
