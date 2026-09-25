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
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ampedge import buffer as buffer_mod          # noqa: E402
from ampedge import mapping as mapping_mod        # noqa: E402
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


asyncio.run(scenarios())

print()
print("=" * 74)
if failures:
    print(f"FAILED ({len(failures)})")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
