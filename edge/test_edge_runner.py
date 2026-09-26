"""The loop, under the failures a factory actually produces.

A pilot does not fail because a protocol was decoded wrongly. It fails because
somebody power-cycled the PLC at 6am, or the site's internet dropped for forty
minutes, or the plant PC was restarted mid-shift by Windows Update. What the
gateway does in those three moments IS the product.

  1. THE PLC GOES AWAY. Reconnect, with bounded jittered backoff — twelve
     gateways retrying on the same 1s timer is a denial of service against a
     controller with an 8-session limit.
  2. AMP GOES AWAY. KEEP READING. The queue grows; nothing is lost; the poll
     loop never blocks on the publisher. This is why the two loops are separate.
  3. THE GATEWAY STOPS. Flush the in-flight window to disk FIRST. The parts
     counted since the last publish are in memory, and a restart mid-shift must
     not lose them.
  4. PUBLISHING FAILS PART-WAY. Stop at the first failure and keep the rest
     queued IN ORDER — AMP's machine state is last-write-wins, so delivering a
     later reading before an earlier one writes the wrong state.

Run: python edge/test_edge_runner.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from broker_stub import TinyBroker                # noqa: E402
from ampedge import buffer as buffer_mod          # noqa: E402
from ampedge import mapping as mapping_mod        # noqa: E402
from ampedge import health as health_mod          # noqa: E402
from ampedge import runner as runner_mod          # noqa: E402
from ampedge.adapters import base                 # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


SPECS = [
    {"tag": "run", "address": "run", "signal": "running", "datatype": "bool"},
    {"tag": "parts", "address": "parts", "signal": "part_count", "datatype": "int",
     "counter_mode": "cumulative"},
    {"tag": "rejects", "address": "rejects", "signal": "reject_count", "datatype": "int",
     "counter_mode": "cumulative"},
]


class ScriptedAdapter(base.Adapter):
    """Returns queued readings, or raises when told the PLC has gone."""

    protocol = "fake"
    instances = []

    def __init__(self, settings):
        super().__init__(settings)
        self.reads = 0
        self.disconnected = 0
        ScriptedAdapter.instances.append(self)

    async def connect(self):
        self.state = base.CONNECTED
        return self

    async def disconnect(self):
        self.disconnected += 1
        self.state = base.DISCONNECTED

    async def read(self, addresses):
        self.reads += 1
        # Real adapters stamp this in read(); the health report keys off it, so
        # a fake that skips it makes a working gateway look like one that has
        # never read anything.
        self.last_read_at = time.time()
        if PLC["down"]:
            self.state = base.DISCONNECTED
            raise base.AdapterError("connection lost")
        parts = PLC["parts"]
        return [base.Reading(tag="run", value=True),
                base.Reading(tag="parts", value=parts),
                base.Reading(tag="rejects", value=PLC["rejects"])]

    def endpoint(self):
        return "fake://plc"


PLC = {"down": False, "parts": 1000, "rejects": 0}


def worker(publish_window=0.0):
    spec = {"name": "CNC-01", "protocol": "fake", "poll_interval": 0.01,
            "connection": {}, "tags": SPECS, "mappings": mapping_mod.validate(SPECS)}
    buf = buffer_mod.Buffer(os.path.join(tempfile.mkdtemp(), "q.db"))
    return runner_mod.MachineWorker(spec, buf, publish_window=publish_window), buf


# ── 1. backoff ──────────────────────────────────────────────────────
section("1. BACKOFF IS BOUNDED, AND JITTERED SO A CELL DOES NOT SYNCHRONISE")
delays = [runner_mod.backoff(n) for n in range(1, 12)]
check("it grows", delays[3] > delays[0], str(delays[:4]))
check("...and is capped", max(delays) <= runner_mod.RECONNECT_MAX_S, str(max(delays)))
check("...never zero, so a failing adapter cannot spin",
      min(delays) >= runner_mod.RECONNECT_MIN_S * 0.5, str(min(delays)))
same = {runner_mod.backoff(5) for _ in range(20)}
check("twelve gateways do NOT all retry at the same instant", len(same) > 15, str(len(same)))


async def scenarios():
    runner_mod.build_adapter = lambda machine: ScriptedAdapter(machine["connection"])

    # ── 2. the PLC goes away and comes back ─────────────────────────
    section("2. THE PLC GOES AWAY, AND THE GATEWAY COMES BACK")
    PLC.update(down=False, parts=1000, rejects=0)
    ScriptedAdapter.instances.clear()
    w, buf = worker(publish_window=0.0)
    task = asyncio.create_task(w.run())
    await asyncio.sleep(0.12)
    check("it is reading", ScriptedAdapter.instances[0].reads > 0,
          str(ScriptedAdapter.instances[0].reads))

    PLC["down"] = True
    await asyncio.sleep(0.15)
    check("a lost PLC drops the session rather than reusing a dead one",
          ScriptedAdapter.instances[0].disconnected >= 1,
          str(ScriptedAdapter.instances[0].disconnected))
    check("...and the worker did not die", not task.done(), "the poll loop exited")

    PLC.update(down=False, parts=1010)
    # backoff is at least RECONNECT_MIN_S/2, so give it room to come back
    await asyncio.sleep(runner_mod.RECONNECT_MIN_S + 0.4)
    check("it reconnected with a NEW session", len(ScriptedAdapter.instances) >= 2,
          str(len(ScriptedAdapter.instances)))
    check("...and is reading again", ScriptedAdapter.instances[-1].reads > 0,
          str(ScriptedAdapter.instances[-1].reads))

    # A counter re-baselines across the gap rather than counting 10 parts it
    # cannot vouch for -- under-report, never invent.
    await w.stop()
    task.cancel()
    check("the queue holds readings taken across the outage", buf.depth() > 0, str(buf.depth()))
    buf.close()

    # ── 3. AMP goes away; reading does not stop ─────────────────────
    section("3. AMP GOES AWAY AND THE GATEWAY KEEPS READING")
    PLC.update(down=False, parts=2000, rejects=0)
    ScriptedAdapter.instances.clear()
    w, buf = worker(publish_window=0.0)
    task = asyncio.create_task(w.run())
    await asyncio.sleep(0.05)
    for n in range(2001, 2006):
        PLC["parts"] = n
        await asyncio.sleep(0.03)
    depth = buf.depth()
    check("with no publisher at all, the queue GROWS rather than the loop stalling",
          depth >= 3, str(depth))
    check("...and the poll loop is still alive", not task.done(), "it stopped when AMP was down")
    await w.stop()
    task.cancel()

    # ── 4. shutdown flushes ─────────────────────────────────────────
    section("4. STOPPING FLUSHES THE IN-FLIGHT WINDOW TO DISK FIRST")
    PLC.update(down=False, parts=3000, rejects=0)
    ScriptedAdapter.instances.clear()
    # A publish window longer than the test: nothing would be written unless
    # stop() forces it, which is exactly the restart-mid-shift case.
    w2, buf2 = worker(publish_window=9999.0)
    task2 = asyncio.create_task(w2.run())
    await asyncio.sleep(0.05)
    PLC["parts"] = 3007
    await asyncio.sleep(0.05)
    check("nothing has been queued yet, because the window has not closed",
          buf2.depth() == 0, str(buf2.depth()))
    await w2.stop()
    task2.cancel()
    check("stopping writes the in-flight window to disk", buf2.depth() >= 1, str(buf2.depth()))
    queued = buf2.peek(5)
    body = queued[0][3] if queued else {}
    check("...and it carries the parts made since the last publish",
          body.get("total_count") == 7, str(body))
    check("...on the right machine", body.get("machine") == "CNC-01", str(body.get("machine")))
    buf2.close()

    # ── 5. a part-way publish failure keeps order ───────────────────
    section("5. A FAILED PUBLISH STOPS THE DRAIN, IN ORDER")
    buf3 = buffer_mod.Buffer(os.path.join(tempfile.mkdtemp(), "q.db"))
    for i in range(5):
        buf3.put({"machine": "CNC-01", "n": i})

    class HalfPublisher:
        state = base.CONNECTED

        def __init__(self):
            self.sent = []

        def publish(self, body, now=None):
            if len(self.sent) >= 2:
                return False
            self.sent.append(body["n"])
            return True

    pub = HalfPublisher()
    batch = buf3.peek(10)
    sent = []
    for row_id, record_id, queued_at, body in batch:
        if not pub.publish(buffer_mod.stamp_for_publish(body, queued_at)):
            break
        sent.append(row_id)
    buf3.ack(sent)
    check("it stopped at the first refusal", pub.sent == [0, 1], str(pub.sent))
    check("...the delivered ones are gone", buf3.depth() == 3, str(buf3.depth()))
    remaining = [b[3]["n"] for b in buf3.peek(10)]
    check("...and the rest are still in ORDER, oldest first", remaining == [2, 3, 4],
          str(remaining))
    buf3.close()


async def assembled_gateway():
    """THE WHOLE THING, as `python -m ampedge run` builds it.

    Everything above drives ONE piece with the others faked: MachineWorker with
    a scripted adapter, Publisher with a stub broker. Each was green while the
    assembled Gateway -- the class the `run` command actually instantiates, and
    the only code path a commissioning engineer ever executes -- had no test at
    all. A wiring mistake between the poll loop, the queue and the drain would
    have passed every suite in this file and failed on site.

    So: a real Gateway, a real Buffer, a real Publisher, a real (small) MQTT
    broker, and a scripted PLC. Nothing faked but the controller.
    """
    section("6. THE ASSEMBLED GATEWAY, AS THE `run` COMMAND BUILDS IT")
    runner_mod.build_adapter = lambda machine: ScriptedAdapter(machine["connection"])
    PLC.update(down=False, parts=5000, rejects=0)
    ScriptedAdapter.instances.clear()

    broker = TinyBroker(48861)
    broker.start()
    await asyncio.sleep(0.2)

    resolved = {
        "amp": {"host": "127.0.0.1", "port": 48861, "tls": False, "tenant": "ACME",
                "site": "plant-1", "topic_prefix": "flowmes"},
        "gateway": {},
        "buffer": {"path": os.path.join(tempfile.mkdtemp(), "queue.db")},
        "machines": [{"name": "CNC-01", "protocol": "fake", "poll_interval": 0.01,
                      "connection": {}, "tags": SPECS,
                      "mappings": mapping_mod.validate(SPECS)}],
    }
    gateway = runner_mod.Gateway(resolved)
    # A window short enough that the test does not wait 30s for the first flush.
    for w in gateway.workers:
        w.publish_window = 0.0

    task = asyncio.create_task(gateway.run())
    await asyncio.sleep(0.3)
    PLC["parts"] = 5009
    await asyncio.sleep(0.6)

    check("the gateway is reading its machine",
          ScriptedAdapter.instances and ScriptedAdapter.instances[0].reads > 0,
          str(len(ScriptedAdapter.instances)))
    check("...and it connected to the broker on its own",
          gateway.publisher.state == base.CONNECTED, gateway.publisher.state)
    check("...and delivered something", gateway.publisher.published > 0,
          str(gateway.publisher.published))
    check("the broker received it on the workspace's own topic",
          broker.published and broker.published[0][0] == "flowmes/ACME/plant-1/machines",
          str(broker.published[:1])[:120])

    delivered = [json.loads(raw) for _, raw in broker.published]
    check("...carrying the machine the config named",
          any(b.get("machine") == "CNC-01" for b in delivered),
          str([b.get("machine") for b in delivered])[:120])
    check("...and the 9 parts made during the run, not the counter's 5009",
          any(b.get("total_count") == 9 for b in delivered),
          str([b.get("total_count") for b in delivered]))

    # THE DRAIN LOOP DID ITS JOB: delivered records are removed from disk, and
    # only on delivery. A gateway that publishes and never acks its own queue
    # fills the disk and re-sends everything forever.
    #
    # Asserted by letting it CATCH UP rather than by demanding an empty queue
    # mid-flight: a 10ms poll with a zero publish window produces ~100 messages
    # a second, and a backlog while that is running is arithmetic, not a fault.
    # What has to be true is that the queue empties once production stops.
    backlog = gateway.buffer.depth()
    for w in gateway.workers:
        w.stopping = True
    for _ in range(40):
        await asyncio.sleep(0.1)
        if gateway.buffer.depth() == 0:
            break
    check("the queue empties once the machines stop producing",
          gateway.buffer.depth() == 0, f"{backlog} -> {gateway.buffer.depth()}")
    check("...having actually had a backlog to clear, so that proves something",
          backlog > 0, str(backlog))

    verdict = gateway.health()
    check("the assembled gateway reports itself STREAMING",
          verdict["verdict"] == health_mod.GOOD, f"{verdict['verdict']}: {verdict['say']}")
    check("...and its health names the real machine and the real broker",
          "CNC-01" in verdict["machines"] and "48861" in verdict["cloud"]["broker"],
          str(verdict["cloud"]["broker"]))

    # AND IT STOPS CLEANLY. A gateway that cannot be stopped is a gateway that
    # gets killed, and a killed gateway loses the window it had not flushed.
    await gateway.stop()
    task.cancel()
    check("stopping leaves the publisher disconnected",
          gateway.publisher.state == base.DISCONNECTED, gateway.publisher.state)
    broker.stop()


def check_amp_command():
    """`check-amp` must name WHICH layer failed, or it is one more "failed".

    Before it existed, `validate` connected to nothing and `preview` reached the
    PLC, so the first test of the broker hostname, TLS setting or credentials
    was `run` -- with the PLC side working perfectly and no way to tell a wrong
    hostname from a firewall from a bad password.
    """
    import argparse
    import json as _json
    import tempfile as _tempfile
    from io import StringIO

    from ampedge import __main__ as cli
    from ampedge import config as config_mod

    section("7. `check-amp` SAYS WHICH LAYER IS BROKEN, NOT JUST 'FAILED'")

    def run_check(host, port, tls=False, no_publish=False):
        cfg = {
            "amp": {"host": host, "port": port, "tls": tls, "tenant": "ACME",
                    "site": "plant-1"},
            # A REAL protocol: config.validate refuses "fake" and exits, and
            # check-amp never touches the PLC anyway -- the whole point is that
            # it tests the cloud half in isolation.
            "machines": [{"name": "CNC-01", "protocol": "opcua", "poll_interval": 1.0,
                          "connection": {"url": "opc.tcp://127.0.0.1:4840"},
                          "tags": SPECS}],
        }
        path = os.path.join(_tempfile.mkdtemp(), "gateway.json")
        with open(path, "w", encoding="utf-8") as fh:
            _json.dump(cfg, fh)
        captured, sys.stdout = sys.stdout, StringIO()
        try:
            code = cli.cmd_check_amp(argparse.Namespace(config=path, no_publish=no_publish))
            return code, sys.stdout.getvalue()
        finally:
            sys.stdout = captured

    # A hostname that does not resolve. Not a firewall, not a password.
    code, out = run_check("no-such-broker.invalid", 8883)
    check("an unresolvable hostname is reported as DNS", code == 1 and "does not resolve" in out,
          out[-200:])
    check("...and says plant networks often have no DNS at all", "DNS" in out, out[-200:])

    # Resolves, nothing listening. A different fix entirely.
    code, out = run_check("127.0.0.1", 9)
    check("a closed port is reported as a refused connection",
          code == 1 and "refused the connection" in out, out[-200:])
    check("...and says the route is fine, so nobody goes hunting for a firewall",
          "route is fine" in out, out[-200:])

    # The happy path, against the real stub broker.
    broker = TinyBroker(48862)
    broker.start()
    time.sleep(0.2)
    code, out = run_check("127.0.0.1", 48862)
    check("a reachable broker passes", code == 0, out[-300:])
    check("...naming the exact topic it would publish to",
          "flowmes/ACME/plant-1/machines" in out, out[:300])
    check("...and proving the broker accepted a publish, which an ACL can refuse",
          "accepted a publish" in out, out[-500:])
    check("the probe carries NO machine, so AMP writes nothing for it",
          broker.published and "machine" not in _json.loads(broker.published[-1][1]),
          str(broker.published[-1][1])[:160] if broker.published else "nothing published")
    check("...and is marked as a self-test so nobody mistakes it for telemetry",
          broker.published and _json.loads(broker.published[-1][1]).get("amp_edge_selftest") is True,
          str(broker.published[-1][1])[:160] if broker.published else "nothing published")

    # IT DOES NOT OVERCLAIM. The broker cannot check a signature; AMP does.
    check("it says plainly what it has NOT proved", "DOES NOT PROVE" in out, out[-600:])
    check("...naming where a refused gateway actually shows up",
          "notification" in out, out[-600:])
    check("...and warns that the probe appears in AMP's log as a rejection",
          "REJECTED" in out, out[-400:])

    # --no-publish, for a broker whose ACL permits only live telemetry.
    before = len(broker.published)
    code, out = run_check("127.0.0.1", 48862, no_publish=True)
    check("--no-publish still connects and passes", code == 0, out[-200:])
    check("...and publishes nothing at all", len(broker.published) == before,
          f"{before} -> {len(broker.published)}")
    broker.stop()


asyncio.run(scenarios())
asyncio.run(assembled_gateway())
check_amp_command()

print()
print("=" * 74)
if failures:
    print(f"FAILED ({len(failures)})")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
