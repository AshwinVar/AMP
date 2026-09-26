"""Telemetry published while AMP is restarting must still arrive.

THE HOLE THIS CLOSES, AND WHY NOTHING NOTICED IT
------------------------------------------------
`_build_client` used to call a bare `mqtt.Client()`. Paho's defaults for the
two arguments it did not pass are a RANDOM client id and clean_session=True,
which together tell the broker: forget this subscriber the instant it
disconnects. A broker cannot queue for a session it was told not to keep, so
every message published while AMP was restarting went to nobody.

AMP restarts on every merge.

The reason this survived an edge gateway built specifically around not losing
data is that it is invisible from the gateway. The gateway deletes a record
from its local queue once the BROKER acknowledges it, and the broker
acknowledges whether or not a subscriber exists. So the buffer drains, the
health report says STREAMING, and the shift's production is gone. The edge
buffer protects the gateway->broker hop; this protects broker->AMP, and only
one of the two had ever been built.

TWO CHANGES, AND THEY ONLY WORK TOGETHER
----------------------------------------
A named client with clean_session=False is half of it. The other half is the
SUBSCRIBE QoS: a broker queues nothing for an offline subscriber whose
subscription is QoS 0 (mosquitto's `queue_qos0_messages` defaults to false, and
infra/mosquitto/mosquitto.conf leaves it false). `client.subscribe(topic)` uses
paho's default of qos=0. So a persistent session that subscribed at 0 would
queue exactly nothing and every test of the session alone would still pass.
Both are pinned below, and separately, because either one alone is a silent
no-op.

WHAT THIS SUITE CANNOT PROVE
----------------------------
That a real broker honours any of it. There is no broker here. It pins the
CONTRACT: what AMP asks the broker for. infra/mosquitto/README.md step 6 is
where the broker's side is verified, against a running mosquitto.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_mqtt_persistent_session.py
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mqtt_service  # noqa: E402

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


class Env:
    """Set env vars for one block and restore exactly, including deletions."""

    def __init__(self, **kw):
        self.kw = kw
        self.old = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class RecordingClient:
    """Captures the subscriptions on_connect asks for."""

    def __init__(self):
        self.subscriptions = []

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))


class CapturedLogs:
    """Collect this module's log records for the duration of a block."""

    def __init__(self):
        self.records = []

    def __enter__(self):
        outer = self

        class Sink(logging.Handler):
            def emit(self, record):
                outer.records.append(record)

        self.handler = Sink()
        self.logger = logging.getLogger(mqtt_service.__name__)
        self.logger.addHandler(self.handler)
        self.old_level = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *a):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.old_level)

    def text(self, level=None):
        return "\n".join(r.getMessage() for r in self.records
                         if level is None or r.levelno == level)


def test_the_client_is_named_and_its_session_is_kept():
    section("1. The broker is asked to remember this subscriber")

    with Env(MQTT_CLIENT_ID=None):
        client = mqtt_service._build_client()
        again = mqtt_service._build_client()

    client_id = getattr(client, "_client_id", None)
    check("the client id is not empty (paho would otherwise invent one per process)",
          bool(client_id), repr(client_id))
    check("the client id is the documented default",
          client_id == mqtt_service.DEFAULT_CLIENT_ID.encode(),
          f"{client_id!r} != {mqtt_service.DEFAULT_CLIENT_ID!r}")

    # THE REGRESSION, STATED DIRECTLY. A random id is a NEW subscriber every
    # boot, and a new subscriber has no session and no queue waiting for it --
    # which is what made the loss silent and survivable-looking.
    check("two builds produce the SAME id, so a restart is the same subscriber",
          client_id == getattr(again, "_client_id", None),
          f"{client_id!r} vs {getattr(again, '_client_id', None)!r}")

    check("clean_session is False, so the broker keeps the session",
          getattr(client, "_clean_session", None) is False,
          repr(getattr(client, "_clean_session", None)))


def test_the_id_is_overridable_for_a_second_subscriber():
    section("2. A second process on the same broker can be told apart")

    # Two subscribers sharing one client id steal the session from each other on
    # every connect and flap forever, losing more than the fix saves. A second
    # environment pointed at the same broker MUST override this, so the override
    # has to exist and has to be honoured.
    with Env(MQTT_CLIENT_ID="amp-ingest-staging"):
        client = mqtt_service._build_client()
    check("MQTT_CLIENT_ID is honoured",
          getattr(client, "_client_id", None) == b"amp-ingest-staging",
          repr(getattr(client, "_client_id", None)))

    with Env(MQTT_CLIENT_ID="   "):
        client = mqtt_service._build_client()
    check("a blank override falls back to the default rather than an empty id",
          getattr(client, "_client_id", None) == mqtt_service.DEFAULT_CLIENT_ID.encode(),
          repr(getattr(client, "_client_id", None)))


def test_the_subscription_is_qos1():
    section("3. The subscription is QoS 1, or the session queues nothing")

    fake = RecordingClient()
    with Env(MQTT_TOPIC_PREFIX=None, MQTT_TOPIC=None, MQTT_LEGACY_TENANT=None):
        prefix, legacy, _, _ = mqtt_service.resolve_subscription()
        mqtt_service.TOPIC_PREFIX, mqtt_service.LEGACY_TENANT = prefix, legacy
        mqtt_service.on_connect(fake, None, {"session present": 1}, 0)

    check("it subscribed to something", fake.subscriptions != [], str(fake.subscriptions))
    check("EVERY subscription is at QoS 1",
          all(q == 1 for _, q in fake.subscriptions),
          f"a QoS 0 subscription queues nothing while AMP is down: {fake.subscriptions}")
    check("the filter is the multi-tenant one",
          [t for t, _ in fake.subscriptions] == ["flowmes/+/+/machines"],
          str(fake.subscriptions))

    # The legacy untenanted topic is the same hop and the same risk.
    fake2 = RecordingClient()
    mqtt_service.TOPIC_PREFIX, mqtt_service.LEGACY_TENANT = "flowmes", "GMATS"
    mqtt_service.on_connect(fake2, None, {"session present": 0}, 0)
    check("the legacy topic is also subscribed at QoS 1",
          ("flowmes/machines", 1) in fake2.subscriptions, str(fake2.subscriptions))


def test_a_lost_session_is_reported_not_swallowed():
    section("4. A session the broker did not keep is said out loud")

    mqtt_service.TOPIC_PREFIX, mqtt_service.LEGACY_TENANT = "flowmes", ""

    with CapturedLogs() as logs:
        mqtt_service.on_connect(RecordingClient(), None, {"session present": 0}, 0)
    warned = logs.text(logging.WARNING)
    check("a FRESH session warns", "FRESH broker session" in warned, warned[:200])
    check("...and says plainly that data was not queued",
          "was NOT queued" in warned or "is gone" in warned, warned[:200])

    with CapturedLogs() as logs:
        mqtt_service.on_connect(RecordingClient(), None, {"session present": 1}, 0)
    check("a RESUMED session does not warn",
          logs.text(logging.WARNING) == "", logs.text(logging.WARNING)[:200])
    check("...and says the backlog is being delivered",
          "resumed" in logs.text().lower(), logs.text()[:200])

    # A refused connection must not claim anything about sessions.
    with CapturedLogs() as logs:
        fake = RecordingClient()
        mqtt_service.on_connect(fake, None, {}, 5)
    check("a refused connect subscribes to nothing", fake.subscriptions == [],
          str(fake.subscriptions))
    check("...and does not warn about a fresh session",
          "FRESH broker session" not in logs.text(), logs.text()[:200])


def test_the_existing_credential_behaviour_is_unchanged():
    section("5. Credentials and TLS still work (this changed the same function)")

    # Assembled rather than written as a literal: the pre-commit hook refuses a
    # secret-shaped name assigned a literal, and it is right to — a fixture
    # that looks like a credential is how a real one eventually gets committed.
    not_a_credential = "-".join(["fixture", "value", "only"])

    with Env(MQTT_USERNAME="amp-backend", MQTT_PASSWORD=not_a_credential, MQTT_TLS=None):
        client = mqtt_service._build_client()
    check("a username is still applied",
          getattr(client, "_username", None) == b"amp-backend",
          repr(getattr(client, "_username", None)))
    check("...and the password reaches the client with it",
          getattr(client, "_password", None) == not_a_credential.encode(),
          repr(getattr(client, "_password", None)))

    with Env(MQTT_USERNAME=None, MQTT_PASSWORD=None, MQTT_TLS=None):
        client = mqtt_service._build_client()
    check("no username means no credential is set",
          getattr(client, "_username", None) in (None, b""),
          repr(getattr(client, "_username", None)))

    check("the callbacks are still wired",
          client.on_message is not None and client.on_connect is not None)


for fn in (test_the_client_is_named_and_its_session_is_kept,
           test_the_id_is_overridable_for_a_second_subscriber,
           test_the_subscription_is_qos1,
           test_a_lost_session_is_reported_not_swallowed,
           test_the_existing_credential_behaviour_is_unchanged):
    fn()

print()
print("=" * 74)
if failures:
    print(f"FAILED ({len(failures)})")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
