"""MQTT ingest resilience — the failure modes a real shop floor actually produces.

test_mqtt_service.py pins the *happy-ish* ingest contract (one DowntimeLog per
breakdown transition, status canonicalisation, count guards). It says nothing
about what happens when the LINK misbehaves, which on a factory network is the
common case: a gateway drops off and reconnects, a broker redelivers a QoS-1
packet it never saw acked, a half-configured edge node publishes a fragment of
JSON, an analog input reads NaN because someone unplugged a sensor.

Those are not hypotheticals. ``mqtt_service.on_message`` wraps everything in one
broad ``except Exception``, so a malformed packet cannot crash the listener — but
that same blanket also means a bug in there is SILENT: rows quietly go missing or
quietly double up and nothing fails loudly. The bug class that costs money is
duplication: a redelivered breakdown packet writing a second DowntimeLog inflates
downtime minutes, MTBF/MTTR and the predictive risk score, and the fix
(transition-gating) is only correct because the previous status is read back from
the DB rather than held in memory — which is exactly what a reconnect would lose.

This suite pins those link-level behaviours:

  * reconnect re-subscribes (recovery is paho's auto-reconnect re-firing
    ``on_connect``; there is no ``on_disconnect`` handler at all — pinned below);
  * every shape of unusable payload is dropped without touching the DB;
  * a redelivered breakdown packet logs downtime ONCE, while a genuine second
    breakdown after a recovery still logs a second time (the control that keeps
    the first assertion from being satisfied by "never logs anything");
  * ingest survives a WebSocket broadcast failure — the DB write is already
    committed before the broadcast, and must stay committed.

Several tests below deliberately pin behaviour that is NOT ideal (production
records are not deduplicated; a broadcast error is swallowed after the sync
``live_ws.broadcast_live_event`` has already run). They are marked as pins: they
record what the code does today so a change to it is a deliberate decision, not
an accident.

Run:  python backend/test_mqtt_resilience.py     (exit 0 = pass)
"""
import io
import json
import warnings
from contextlib import redirect_stdout

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

import models
import mqtt_identity
import mqtt_listener
import mqtt_service
from database import Base

# The real guard, captured before any test swaps it out. The broadcast-failure
# tests need the genuine safe_broadcast, not the no-op stub the fixture installs.
_REAL_SAFE_BROADCAST = mqtt_service.safe_broadcast

# Live events the fixture's stub captured for the test that is running.
_BROADCASTS = []

# Every packet has to be ROUTABLE before any of the payload-robustness questions
# below become reachable: on_message resolves the owning tenant from the topic
# and drops anything it cannot attribute (see test_mqtt_tenant_identity.py).
# These tests are about what happens to a WELL-ADDRESSED message with a hostile
# body, so they all ride on one provisioned tenant.
_TENANT = "MQTT_TEST"
_TOPIC = f"{mqtt_service.TOPIC_PREFIX}/{_TENANT}/-/machines"


class _Msg:
    """Paho message stand-in. on_message reads only .topic and .payload (bytes),
    so a raw-bytes double lets a test put ANY wire content on the topic —
    including bytes that are not valid UTF-8 or not valid JSON."""

    def __init__(self, payload, topic=None):
        self.topic = topic if topic is not None else _TOPIC
        self.payload = payload


def _setup():
    """Fresh in-memory DB per test, with the live broadcast captured not sent."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    mqtt_service.SessionLocal = sessionmaker(bind=engine)
    # Provision the tenant these packets address. Without it ingest correctly
    # rejects every message as belonging to an unknown customer, and every test
    # below would pass vacuously by writing nothing.
    db = mqtt_service.SessionLocal()
    db.add(models.TenantConfig(tenant_code=_TENANT))
    db.commit()
    db.close()
    _BROADCASTS.clear()
    mqtt_service.safe_broadcast = _BROADCASTS.append
    return mqtt_service.SessionLocal


def _deliver(msg):
    # The handler emits progress lines containing non-ASCII arrow glyphs; capture
    # stdout so a cp1252 console cannot raise UnicodeEncodeError mid-handler and
    # turn a passing test red. Harmless whichever way the handler reports
    # (print or logger) — the tests assert on the DB and the broadcast, never on
    # the text.
    with redirect_stdout(io.StringIO()):
        mqtt_service.on_message(None, None, msg)


def _send(**fields):
    """Deliver one telemetry packet for PRESS-01 with the given fields overridden."""
    payload = {"machine": "PRESS-01", "status": "Running", "utilization": 50, "downtime": "30 min"}
    payload.update(fields)
    _deliver(_Msg(json.dumps(payload).encode()))


def _counts(Session):
    db = Session()
    try:
        return {
            "machines": db.query(models.Machine).count(),
            "events": db.query(models.MachineEvent).count(),
            "production": db.query(models.ProductionRecord).count(),
            "downtime": db.query(models.DowntimeLog).count(),
        }
    finally:
        db.close()


def _machine(Session, name="PRESS-01"):
    db = Session()
    try:
        return db.query(models.Machine).filter(models.Machine.name == name).first()
    finally:
        db.close()


class _RecordingClient:
    """Paho client stand-in for the connect callbacks — records subscribe() calls."""

    def __init__(self):
        self.subscribed = []

    def subscribe(self, topic):
        self.subscribed.append(topic)


# ── Link lifecycle: reconnect and re-subscription ──────────────────────

def test_reconnect_restores_the_subscription():
    """Pins: after a drop, the re-fired on_connect subscribes AGAIN.

    Failure this catches: a subscription that is only established once. Paho's
    loop_forever reconnects on its own and re-invokes on_connect, but the broker
    does NOT remember the old subscription for a clean session — if on_connect
    stopped subscribing on a later call (e.g. behind a 'first run' guard), the
    client would sit connected and silent, and the shop floor would go dark with
    no error anywhere.
    """
    client = _RecordingClient()
    with redirect_stdout(io.StringIO()):
        mqtt_service.on_connect(client, None, {}, 0)   # first connect
        mqtt_service.on_connect(client, None, {}, 0)   # paho's automatic reconnect

    expected = mqtt_identity.topic_filters(mqtt_service.TOPIC_PREFIX,
                                          mqtt_service.LEGACY_TENANT)
    assert client.subscribed == expected * 2, client.subscribed

    # CONTROL: a REFUSED connection must not subscribe. Without this, the
    # assertion above would also pass for an on_connect that subscribes
    # unconditionally — including on a connection that was never established.
    refused = _RecordingClient()
    with redirect_stdout(io.StringIO()):
        for rc in (1, 3, 5):        # bad protocol / server unavailable / not authorised
            mqtt_service.on_connect(refused, None, {}, rc)
    assert refused.subscribed == [], refused.subscribed
    print("PASS MQTT re-subscribes on every successful (re)connect, never on a refused one")


def test_the_listener_registers_no_disconnect_handler():
    """Pins the CURRENT recovery design: there is no on_disconnect callback.

    Recovery rests entirely on paho's loop_forever auto-reconnect re-firing
    on_connect. That is worth stating in a test rather than leaving implicit,
    because it is the reason the reconnect test above is the whole story: there
    is no place where a disconnect could flush buffered state, mark machines
    stale, or alert. If someone adds an on_disconnect handler, this test fails
    and they must decide what it does to the ingest guarantees rather than
    bolting it on silently.
    """
    with warnings.catch_warnings():
        # paho 2.x warns that the v1 callback API is deprecated; the handler
        # signatures in mqtt_service are v1, which is what is under test here.
        warnings.simplefilter("ignore", DeprecationWarning)
        client = mqtt_listener.build_client()

    assert client.on_connect is mqtt_service.on_connect
    assert client.on_message is mqtt_service.on_message
    assert client.on_disconnect is None, client.on_disconnect
    print("PASS the listener wires connect/message only - reconnect recovery is paho's, not ours")


# ── Unusable payloads ──────────────────────────────────────────────────

def test_unusable_payload_bytes_write_nothing():
    """Pins: bytes that are not decodable/parseable JSON are dropped intact.

    Failure this catches: a half-configured edge node publishing a fragment, a
    keepalive, or latin-1 bytes causing a partial write (a machine row created
    before the parse blew up) or an exception escaping on_message and killing the
    paho network loop for every other device on the topic.
    """
    Session = _setup()
    for wire in (
        b"",                                    # empty keepalive-style publish
        b"   ",                                 # whitespace only
        b"not json at all",                     # plain text
        b'{"machine": "PRESS-01"',              # truncated mid-object
        b'{"machine": "PRESS-01",}',            # trailing comma (not valid JSON)
        b"\xff\xfe\x00PRESS",                   # not decodable as UTF-8
    ):
        _deliver(_Msg(wire))

    assert _counts(Session) == {"machines": 0, "events": 0, "production": 0, "downtime": 0}, _counts(Session)

    # CONTROL: the same handler and the same DB DO ingest a well-formed packet,
    # so "nothing was written" above is the guard working, not a broken fixture.
    _send(status="Running")
    assert _counts(Session)["machines"] == 1, _counts(Session)
    print("PASS MQTT drops undecodable / unparseable payloads and still ingests a good one")


def test_json_that_is_not_an_object_writes_nothing():
    """Pins: valid JSON of the wrong SHAPE is dropped, not half-applied.

    A gateway that publishes a bare array of readings, or just the machine name
    as a JSON string, produces valid JSON with no .get(). Before the broad guard
    that would raise AttributeError mid-handler; either way nothing may be
    written. Listed separately from the parse failures above because it exercises
    a different branch: json.loads SUCCEEDS here.
    """
    Session = _setup()
    for wire in (b"[]", b'[{"machine": "PRESS-01"}]', b'"PRESS-01"', b"42", b"null", b"true"):
        _deliver(_Msg(wire))

    assert _counts(Session) == {"machines": 0, "events": 0, "production": 0, "downtime": 0}, _counts(Session)
    print("PASS MQTT drops JSON that is not an object (array / string / number / null)")


def test_a_packet_without_a_machine_name_writes_nothing():
    """Pins: no machine name means no row anywhere — including no anonymous machine.

    Failure this catches: get_or_create_machine being reached with a falsy name
    and creating a Machine called "" or None, which then appears on every
    dashboard as a ghost asset that nobody can trace to a device.
    """
    Session = _setup()
    for payload in ({"status": "Running"}, {"machine": "", "status": "Running"},
                    {"machine": None, "status": "Running"}, {"machine": 0, "status": "Running"}):
        _deliver(_Msg(json.dumps(payload).encode()))

    assert _counts(Session) == {"machines": 0, "events": 0, "production": 0, "downtime": 0}, _counts(Session)

    # CONTROL: a named packet on the same DB creates exactly one machine.
    _send(status="Running")
    assert _counts(Session)["machines"] == 1, _counts(Session)
    print("PASS MQTT writes no ghost machine for a packet with no machine name")


def test_wrong_field_types_keep_the_last_good_reading():
    """Pins: a structurally-valid packet with wrong-typed fields degrades, never corrupts.

    A misconfigured gateway can publish status/utilization/counts as arrays or
    objects (a whole tag block pasted into a scalar field). Each guard must map
    that to 'no usable value' and leave the machine's last good reading in place,
    rather than writing str(dict) as a status, raising on float(list), or
    aborting the whole message.
    """
    Session = _setup()
    _send(status="Running", utilization=63)             # establish a known-good state
    before = _counts(Session)

    _send(status=["Running"], utilization={"value": 70},
          total_count=[], good_count={}, rejected_count="x")

    machine = _machine(Session)
    assert machine.status == "Running", machine.status
    assert machine.utilization == 63, machine.utilization

    after = _counts(Session)
    assert after["production"] == 0, after                    # no record from garbage counts
    assert after["events"] == before["events"], (before, after)   # no phantom transition
    print("PASS MQTT keeps the last good status/utilization when fields arrive wrong-typed")


def test_nan_utilization_keeps_the_last_value_and_still_applies_status():
    """Pins: a NaN reading skips only the utilization, not the whole message.

    A disconnected analog input reads NaN, and JSON permits the bare NaN token —
    json.loads (which on_message calls) decodes it to float('nan'). NaN passes
    float() but round(nan) raises ValueError, so without the isfinite guard in
    clamp_utilization the exception aborted the entire packet: the machine's
    status change and its breakdown DowntimeLog were lost along with the
    (rightly-rejected) utilization. The wire bytes carry the literal token here
    so the test exercises the same decode path a real gateway would.
    """
    Session = _setup()
    _send(status="Running", utilization=71)

    _deliver(_Msg(
        b'{"machine": "PRESS-01", "status": "Breakdown", "downtime": "12 min", '
        b'"utilization": NaN}'
    ))

    machine = _machine(Session)
    assert machine.utilization == 71, machine.utilization     # last good value untouched
    assert machine.status == "Breakdown", machine.status      # rest of the packet applied
    assert _counts(Session)["downtime"] == 1, _counts(Session)
    print("PASS MQTT rejects a NaN utilization and still applies the status and downtime")


def test_an_unknown_status_mid_breakdown_is_not_a_recovery():
    """Pins: an unrecognised status leaves the machine DOWN, so no phantom re-entry.

    This is where the unknown-status guard meets the duplicate-log guard. A
    machine sits in Breakdown; the gateway publishes "faulted" (not a machine
    state). If that were written raw, the machine's stored status would no longer
    be "Breakdown", and the very next genuine Breakdown packet would look like a
    fresh transition and write a SECOND DowntimeLog for one stoppage — inflating
    the breakdown count and MTBF/MTTR for an event that never ended.
    """
    Session = _setup()
    _send(status="Breakdown")        # Idle -> Breakdown: one log
    _send(status="faulted")          # unknown: ignored, machine stays Breakdown
    _send(status="Breakdown")        # not a transition: still no second log

    assert _machine(Session).status == "Breakdown"
    assert _counts(Session)["downtime"] == 1, _counts(Session)

    # CONTROL: a REAL recovery followed by a real breakdown does log again, so the
    # assertion above is transition-gating and not a blanket "only ever one log".
    _send(status="Running")
    _send(status="Breakdown")
    assert _counts(Session)["downtime"] == 2, _counts(Session)
    print("PASS an unknown status mid-breakdown is not read as a recovery (no duplicate log)")


# ── Duplicate delivery ─────────────────────────────────────────────────

def test_a_redelivered_breakdown_packet_logs_downtime_once():
    """Pins the money bug: QoS-1 redelivery must not double-count a stoppage.

    An MQTT broker redelivers a packet whose PUBACK it never saw — the SAME bytes
    arrive twice, seconds apart, with nothing in the payload marking the second
    as a repeat. Because on_message gates the DowntimeLog on the TRANSITION
    (old_status != status), the second copy is a no-op. If it gated on status
    alone, one stoppage would be counted twice in downtime minutes, breakdown
    counts, MTBF/MTTR and the predictive risk score.

    The identical _Msg OBJECT is delivered twice here — not two equal payloads —
    so the test really is a redelivery and not two similar events.
    """
    Session = _setup()
    packet = _Msg(json.dumps(
        {"machine": "PRESS-01", "status": "Breakdown", "utilization": 0, "downtime": "45 min"}
    ).encode())

    _deliver(packet)
    _deliver(packet)     # broker redelivery of the very same publish
    _deliver(packet)     # and again, for a broker that keeps retrying

    counts = _counts(Session)
    assert counts["downtime"] == 1, counts
    assert counts["events"] == 1, counts        # one Idle -> Breakdown timeline entry too
    assert counts["machines"] == 1, counts      # created once, reused

    # CONTROL: a genuine SECOND breakdown, after a recovery, must still be logged.
    # Without this the assertion above is also satisfied by code that never logs a
    # second breakdown at all — which would hide every repeat failure on the asset.
    _send(status="Running")
    _deliver(packet)
    counts = _counts(Session)
    assert counts["downtime"] == 2, counts
    assert counts["events"] == 3, counts        # Idle->Bd, Bd->Running, Running->Bd
    print("PASS a redelivered breakdown packet logs once; a real second breakdown logs again")


def test_a_redelivered_production_packet_is_recorded_twice():
    """Pins a KNOWN gap, honestly: production counts are NOT deduplicated.

    Unlike the downtime path there is no transition gate and no idempotency key on
    ProductionRecord, so a redelivered production packet writes a second row and
    double-counts the output in every OEE window. This test does not claim that is
    correct — it records it, so that adding a dedupe key (a packet id, or a
    unique constraint on machine + reading timestamp) is a deliberate change with
    a test to update, rather than something a future reader assumes already works.
    """
    Session = _setup()
    packet = _Msg(json.dumps({
        "machine": "PRESS-01", "status": "Running", "utilization": 80,
        "total_count": 100, "good_count": 95, "rejected_count": 5,
    }).encode())

    _deliver(packet)
    _deliver(packet)

    db = Session()
    records = db.query(models.ProductionRecord).all()
    db.close()
    assert len(records) == 2, len(records)
    assert [r.total_count for r in records] == [100, 100]
    print("PASS PIN: a redelivered production packet is recorded twice (no dedupe key today)")


# ── Disconnect recovery ────────────────────────────────────────────────

def test_messages_after_a_reconnect_are_still_ingested():
    """Pins: a reconnect changes nothing about ingest, because no state is in RAM.

    After a drop, paho hands the callbacks a reconnected client and re-fires
    on_connect. on_message keeps nothing between calls — it opens its own session
    and reads the machine's previous status back from the DB — so ingest resumes
    exactly where it left off. That is also why the duplicate guard survives a
    reconnect: if the previous status were cached per-connection it would be lost
    on the drop, and the first packet after reconnect would re-log a stoppage
    that was already open.
    """
    Session = _setup()
    _send(status="Breakdown")                    # one stoppage, logged once
    assert _counts(Session)["downtime"] == 1

    reconnected = _RecordingClient()             # paho's post-drop client
    with redirect_stdout(io.StringIO()):
        mqtt_service.on_connect(reconnected, None, {}, 0)
    assert reconnected.subscribed == mqtt_identity.topic_filters(
        mqtt_service.TOPIC_PREFIX, mqtt_service.LEGACY_TENANT)

    # The machine is still down; the first packet after the reconnect repeats that
    # and must NOT re-log the stoppage.
    _send(status="Breakdown")
    assert _counts(Session)["downtime"] == 1, _counts(Session)

    # ...and genuinely new telemetry after the reconnect IS ingested — the control
    # that stops this test passing for a handler that silently ignores everything
    # post-reconnect.
    _send(status="Running", utilization=88, total_count=40, good_count=38, rejected_count=2)
    machine = _machine(Session)
    assert machine.status == "Running", machine.status
    assert machine.utilization == 88, machine.utilization
    assert _counts(Session)["production"] == 1, _counts(Session)
    print("PASS ingest resumes across a reconnect and the duplicate guard survives it")


# ── Broadcast failure must not lose the ingest ─────────────────────────

def test_a_broadcast_failure_does_not_lose_the_ingest():
    """Pins: a dead WebSocket layer costs the live view, never the DB write.

    Everything the message produced — the machine update, the breakdown's
    DowntimeLog and the ProductionRecord — is committed BEFORE safe_broadcast is
    called, and safe_broadcast swallows the error, so a broken/absent WS layer
    degrades to 'the dashboard does not animate' rather than 'telemetry is lost'.
    The downtime and production rows are asserted deliberately: they are written
    in the commit immediately preceding the broadcast, so if that commit ever
    moved to AFTER the broadcast, on_message's broad except would roll them away
    on any WS error and a whole stoppage would vanish while the machine row
    (committed earlier) still looked fine.

    Three callee shapes are exercised because safe_broadcast branches on them:

      * a generic error       -> the outer except Exception;
      * a RuntimeError        -> the loop-retry branch, which fails again;
      * a SYNCHRONOUS callee  -> what production actually has. live_ws's
        broadcast_live_event is a plain def, and safe_broadcast now calls it
        plainly. It used to wrap the call in asyncio.run(...), which was handed
        the function's None and raised ValueError on EVERY message — swallowed,
        so the symptom was a permanent error line rather than a visible failure.
        The delivery that did happen ran on a throwaway event loop instead of
        the server's. Fixed in live_ws.broadcast_live_event (the thread-to-loop
        bridge); the shapes are pinned in test_live_broadcast_bridge.py. What
        this case still guards is the invariant that outlives the fix: whatever
        the callee does, a broadcast problem must never cost the committed row.
    """
    Session = _setup()
    mqtt_service.safe_broadcast = _REAL_SAFE_BROADCAST      # exercise the real guard
    original = mqtt_service.broadcast_live_event
    sync_calls = []

    def _raise_generic(event):
        raise OSError("broadcast socket gone")

    def _raise_runtime(event):
        raise RuntimeError("Event loop is closed")

    def _sync_callee(event):                                # production's shape
        sync_calls.append(event)

    try:
        # A fresh machine per callee, so each packet is a real Idle -> Breakdown
        # transition and must produce exactly one downtime row of its own.
        for i, callee in enumerate((_raise_generic, _raise_runtime, _sync_callee)):
            mqtt_service.broadcast_live_event = callee
            _send(machine=f"PRESS-0{i}", status="Breakdown", utilization=0, downtime="20 min",
                  total_count=30, good_count=28, rejected_count=2)
            assert _machine(Session, f"PRESS-0{i}").status == "Breakdown", callee.__name__
    finally:
        mqtt_service.broadcast_live_event = original
        mqtt_service.safe_broadcast = _BROADCASTS.append

    counts = _counts(Session)
    assert counts == {"machines": 3, "events": 3, "production": 3, "downtime": 3}, counts
    assert len(sync_calls) == 1, sync_calls     # the sync callee did receive its event
    print("PASS a failing (or synchronous) broadcast never loses the committed ingest")


def test_a_working_broadcast_actually_delivers_the_event():
    """CONTROL for the test above: safe_broadcast is not a no-op.

    Without this, 'the DB survived a broadcast failure' would also hold for a
    safe_broadcast that never calls anything at all — the live feed could be
    permanently dead and every failure test would stay green.

    THE CALLEE IS SYNCHRONOUS, which is what production actually has.
    It used to be `async def` here, justified by the shape `asyncio.run` expects
    — but `asyncio.run` was itself the bug: `live_ws.broadcast_live_event` is a
    plain function, so wrapping it handed `asyncio.run` a None and raised
    ValueError on every message. Both the real function and mqtt_service's
    import fallback are plain defs now, and this stub matches them. A stub of a
    shape production does not have is a test that passes for the wrong reason.
    The thread-to-loop hand-off it replaced is covered by
    test_live_broadcast_bridge.py.
    """
    Session = _setup()
    mqtt_service.safe_broadcast = _REAL_SAFE_BROADCAST
    original = mqtt_service.broadcast_live_event
    delivered = []

    def _record(event):
        delivered.append(event)

    try:
        mqtt_service.broadcast_live_event = _record
        _send(status="Breakdown", utilization=0)
    finally:
        mqtt_service.broadcast_live_event = original
        mqtt_service.safe_broadcast = _BROADCASTS.append

    assert len(delivered) == 1, delivered
    event = delivered[0]
    assert event["event"] == "machine_update"
    assert event["machine"]["name"] == "PRESS-01"
    assert event["timeline"] == {"old_status": "Idle", "new_status": "Breakdown"}, event["timeline"]
    print("PASS a working broadcast really does deliver the machine_update event")


def test_the_live_event_carries_a_tenant_and_no_null_counts():
    """Pins the payload shape the WS layer depends on: tenant present, counts numeric.

    live_ws.ConnectionManager.broadcast routes on payload["tenant_code"] and only
    sends to connections on that tenant (ADR-0002) — a payload missing it would
    match no connection and vanish, or match the wrong one. And the production
    block coalesces rejected readings to 0: a raw None would reach the browser as
    JSON null and render as a blank or NaN tile rather than 'nothing was
    recorded'.

    This assertion used to read `== "DEFAULT"`, and it passed. It was pinning
    the bug: the machine was auto-created with no tenant, so the column default
    supplied one, and the live event told the WS layer to deliver one customer's
    telemetry to whoever happened to be on DEFAULT. The tenant now comes from
    the topic the broker authorised.
    """
    Session = _setup()
    _send(status="Running", total_count="--", good_count=5, rejected_count=None)

    assert len(_BROADCASTS) == 1, _BROADCASTS
    event = _BROADCASTS[0]
    assert event["tenant_code"] == _TENANT, event["tenant_code"]
    assert event["production"] == {"total_count": 0, "good_count": 5, "rejected_count": 0}, event["production"]
    assert _counts(Session)["production"] == 0, _counts(Session)   # nothing was actually recorded

    # CONTROL: a valid reading reports its real numbers, so the zeros above are the
    # coalesce doing its job rather than the block always being zeroed.
    _BROADCASTS.clear()
    _send(status="Running", total_count=60, good_count=57, rejected_count=3)
    assert _BROADCASTS[0]["production"] == {"total_count": 60, "good_count": 57, "rejected_count": 3}
    print("PASS the live event is tenant-stamped and never carries a null production count")


if __name__ == "__main__":
    test_reconnect_restores_the_subscription()
    test_the_listener_registers_no_disconnect_handler()
    test_unusable_payload_bytes_write_nothing()
    test_json_that_is_not_an_object_writes_nothing()
    test_a_packet_without_a_machine_name_writes_nothing()
    test_wrong_field_types_keep_the_last_good_reading()
    test_nan_utilization_keeps_the_last_value_and_still_applies_status()
    test_an_unknown_status_mid_breakdown_is_not_a_recovery()
    test_a_redelivered_breakdown_packet_logs_downtime_once()
    test_a_redelivered_production_packet_is_recorded_twice()
    test_messages_after_a_reconnect_are_still_ingested()
    test_a_broadcast_failure_does_not_lose_the_ingest()
    test_a_working_broadcast_actually_delivers_the_event()
    test_the_live_event_carries_a_tenant_and_no_null_counts()
    print("MQTT RESILIENCE OK: reconnect, bad payloads, redelivery and broadcast failure")
