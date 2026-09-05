"""MQTT boot: don't cry wolf when unconfigured, don't die forever when refused.

THREE DEFECTS AT BOOT, FOUND BY READING start_mqtt_service
-----------------------------------------------------------
1. IT CRIES WOLF. `MQTT_BROKER` defaults to "127.0.0.1" (mqtt_service.py:36), so
   a deployment that never configured MQTT still dials localhost, fails, and logs
   "FastAPI MQTT connection error: ConnectionRefusedError(111)" on every boot.
   Production has done this since launch. monitoring.py:354-361 already encodes
   the correct semantic and even names this wart — it checks the RAW environment
   "because mqtt_service applies a 127.0.0.1 default, which makes its constant
   unable to answer 'was this deliberately configured?'". The health endpoint is
   right; the log is the thing that lies.

2. ONE REFUSED CONNECT KILLS INGEST PERMANENTLY. paho's `loop_forever()`
   auto-reconnects — but only after a SUCCESSFUL initial connect. The old code
   called `connect()` and `loop_forever()` inside one try; a raised connect skips
   loop_forever entirely, the except swallows it, and the thread exits. Nothing
   restarts it. A broker that is one second slow to accept at cold start means no
   MQTT ingest until someone redeploys — and nothing surfaces that, because the
   health block reports on the thread, which is gone.

3. NO BROKER COULD BE USED EVEN IF ONE EXISTED. `mqtt.Client()` was constructed
   bare: no `username_pw_set`, no `tls_set`, and no MQTT_USERNAME / MQTT_PASSWORD
   / MQTT_TLS anywhere in the backend (grep across ~300 files: zero hits). Every
   production-grade broker requires auth, and docker-compose.yml warns in its own
   comment "never point a production deployment at an anonymous broker". So the
   step everyone assumed was "set MQTT_BROKER on Railway" could not have worked.

WHAT THIS SUITE CAN AND CANNOT PROVE
------------------------------------
It drives `start_mqtt_service` with a fake client, so it proves the CONTRACT:
what is called, with what, how often, and what is logged. It cannot prove a real
broker accepts the credentials — there is no broker here, and no way to get one.
That is stated in the commit rather than implied by a green test.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_mqtt_boot.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mqtt_service

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


class FakeClient:
    """Records what the service does to a paho client, and can refuse to connect."""

    def __init__(self, refuse_times=0):
        self.refuse_times = refuse_times
        self.connect_calls = []
        self.username_pw = None
        self.tls_called = False
        self.looped = False

    def username_pw_set(self, username, password=None):
        self.username_pw = (username, password)

    def tls_set(self, *a, **kw):
        self.tls_called = True

    def connect(self, host, port, keepalive):
        self.connect_calls.append((host, port, keepalive))
        if len(self.connect_calls) <= self.refuse_times:
            raise ConnectionRefusedError(111, "Connection refused")

    def loop_forever(self):
        self.looped = True


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


def run_service(refuse_times=0, **kw):
    """Start the service synchronously so the test can assert on the outcome."""
    made = []

    def factory():
        # Only the FIRST client refuses; a retry that built a fresh refusing
        # client would loop forever regardless of what the code under test does.
        c = FakeClient(refuse_times=refuse_times if not made else 0)
        made.append(c)
        return c

    slept = []
    thread = mqtt_service.start_mqtt_service(
        client_factory=factory, sleep=slept.append, run_inline=True, **kw)
    return thread, (made[0] if made else None), slept


def main():
    print("=" * 74)
    print("1. UNCONFIGURED — say so, and do not dial localhost")
    print("=" * 74)
    with Env(MQTT_BROKER=None):
        thread, client, _ = run_service()
        check("no client is built when MQTT_BROKER is unset", client is None,
              "it dialled anyway")
        check("...and no listener thread is started", thread is None, str(thread))
    with Env(MQTT_BROKER="   "):
        thread, client, _ = run_service()
        check("a blank MQTT_BROKER counts as unset", client is None and thread is None,
              "whitespace was treated as a hostname")

    print()
    print("=" * 74)
    print("2. CONFIGURED — connect, and keep the listener running")
    print("=" * 74)
    with Env(MQTT_BROKER="broker.example", MQTT_PORT="1884"):
        thread, client, _ = run_service()
        check("a client is built when a broker IS configured", client is not None, "none")
        check("...and it connects to the configured host and port",
              client and client.connect_calls[0][:2] == ("broker.example", 1884),
              str(client.connect_calls if client else None))
        check("...and enters the paho loop", bool(client and client.looped), "never looped")

    print()
    print("=" * 74)
    print("3. A REFUSED FIRST CONNECT IS RETRIED, NOT FATAL")
    print("=" * 74)
    # The defect: loop_forever() auto-reconnects only AFTER a successful initial
    # connect, so one refusal used to end ingest for the life of the process.
    with Env(MQTT_BROKER="broker.example", MQTT_CONNECT_RETRIES="4"):
        thread, client, slept = run_service(refuse_times=2)
        check("a broker that refuses twice is still reached",
              bool(client and client.looped),
              f"connects={len(client.connect_calls) if client else 0} looped="
              f"{client.looped if client else None}")
        check("...after exactly 3 attempts", client and len(client.connect_calls) == 3,
              str(len(client.connect_calls) if client else 0))
        check("...backing off between them, not spinning", len(slept) == 2, str(slept))

    with Env(MQTT_BROKER="broker.example", MQTT_CONNECT_RETRIES="3"):
        thread, client, slept = run_service(refuse_times=99)
        check("a broker that never comes up gives up after the configured tries",
              client and len(client.connect_calls) == 3,
              str(len(client.connect_calls) if client else 0))
        check("...and does not enter the loop", not (client and client.looped), "looped anyway")

    print()
    print("=" * 74)
    print("4. CREDENTIALS AND TLS REACH THE CLIENT")
    print("=" * 74)
    # These cases must NOT pass a client_factory. _build_client() is the function
    # that applies credentials, so a factory would bypass exactly what is under
    # test — the first version of this section did, and asserted on a path it had
    # replaced. Swap the paho constructor instead, so the real builder runs.
    #
    # Cannot prove a real broker ACCEPTS these — there is no broker here. Proves
    # only that they are passed, which is what was missing entirely.
    def build(**env):
        made = []
        real = mqtt_service.mqtt.Client
        mqtt_service.mqtt.Client = lambda *a, **kw: (made.append(FakeClient()) or made[-1])
        try:
            with Env(MQTT_BROKER="b", **env):
                mqtt_service.start_mqtt_service(sleep=lambda *_: None, run_inline=True)
        finally:
            mqtt_service.mqtt.Client = real
        return made[0] if made else None

    client = build(MQTT_USERNAME="amp", MQTT_PASSWORD="s3cret", MQTT_TLS=None)
    check("username and password are set on the client",
          client and client.username_pw == ("amp", "s3cret"),
          str(client.username_pw if client else None))
    check("...and TLS is NOT enabled unless asked",
          client and not client.tls_called, "tls_set called without MQTT_TLS")
    client = build(MQTT_USERNAME=None, MQTT_PASSWORD=None, MQTT_TLS="1")
    check("MQTT_TLS=1 enables TLS", bool(client and client.tls_called), "not called")
    check("...and no credentials are invented when none are set",
          client and client.username_pw is None, str(client.username_pw if client else None))
    client = build(MQTT_USERNAME=None, MQTT_PASSWORD=None, MQTT_TLS=None)
    check("CONTROL: an anonymous broker still works (no auth, no TLS)",
          client and client.username_pw is None and not client.tls_called
          and client.looped,
          "the unauthenticated path regressed")

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
