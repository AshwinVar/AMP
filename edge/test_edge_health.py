"""The report that tells a commissioning engineer WHICH SIDE is broken.

This is the gateway's most-used surface and it had no test at all. That matters
more than it sounds: every other thing in this package fails in a way somebody
eventually notices, but a health report that is merely PLAUSIBLE sends an
engineer to the wrong end of the building and costs the afternoon.

What is pinned:

  * the verdict is the FIRST broken thing, in the order they must be fixed —
    a tag list cannot be debugged through an unreachable PLC
  * "connected but silent" is not "healthy"
  * nothing read yet is STARTING, never STREAMING
  * an AMP outage says NOTHING IS BEING LOST, because that is true and it is the
    only thing the person actually wants to know
  * the queue is counted ONCE, not once per machine

Run: python edge/test_edge_health.py
"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ampedge import buffer as buffer_mod   # noqa: E402
from ampedge import health                 # noqa: E402
from ampedge.adapters import base          # noqa: E402

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


NOW = 1_800_000_000.0


class FakeAdapter:
    def __init__(self, **kw):
        self.d = {"protocol": "opcua", "state": base.CONNECTED, "connected_at": NOW - 600,
                  "last_read_at": NOW - 1, "endpoint": "opc.tcp://10.0.0.5:4840",
                  "last_error": "", "bad_tags": []}
        self.d.update(kw)

    def describe(self):
        return dict(self.d)


class FakePublisher:
    def __init__(self, **kw):
        self.d = {"state": base.CONNECTED, "broker": "broker.amp:8883", "tls": True,
                  "topic": "flowmes/ACME/plant-1/machines", "signed": True,
                  "gateway_id": "gw-1", "published": 42, "refused": 0,
                  "last_publish_at": NOW - 2, "last_error": ""}
        self.d.update(kw)

    def describe(self):
        return dict(self.d)


def make_buffer(records=0, age=None):
    buf = buffer_mod.Buffer(os.path.join(tempfile.mkdtemp(), "q.db"))
    for i in range(records):
        buf.put({"machine": "M", "n": i}, now=(NOW - age) if age else NOW)
    return buf


def report(adapters, publisher=None, buffer=None, started=NOW - 3600):
    return health.report(started_at=started, adapters=adapters,
                         publisher=publisher or FakePublisher(), buffer=buffer, now=NOW)


# ── 1. the order of the diagnosis ───────────────────────────────────
section("1. THE VERDICT IS THE FIRST BROKEN THING, IN FIXING ORDER")
r = report({"CNC-01": FakeAdapter(state=base.DISCONNECTED,
                                  last_error="connection refused", last_read_at=None)})
check("an unreachable PLC outranks everything", r["verdict"] == health.NO_PLC, r["verdict"])
check("...and names the machine", "CNC-01" in r["say"], r["say"])
check("...and the endpoint, so the engineer knows what to ping",
      "10.0.0.5" in r["say"], r["say"])
check("...and tells them what to check", "firewall" in r["say"] or "subnet" in r["say"], r["say"])

# A PLC that is unreachable AND has bad tags is a PLC problem. Fixing the tag
# list first is wasted time, so the verdict must not name tags.
r = report({"CNC-01": FakeAdapter(state=base.ERROR, bad_tags=["ns=2;i=9"], last_read_at=None)})
check("a broken PLC is not reported as a tag problem", r["verdict"] == health.NO_PLC,
      r["verdict"])

r = report({"CNC-01": FakeAdapter(state=base.DEGRADED, bad_tags=["ns=2;i=9", "ns=2;i=10"])})
check("connected with bad tags IS a tag problem", r["verdict"] == health.BAD_TAGS, r["verdict"])
check("...and says the network is fine, which is the actionable half",
      "network is fine" in r["say"], r["say"])
check("...naming the tags", "ns=2;i=9" in r["say"], r["say"])

# ── 2. connected but silent ─────────────────────────────────────────
section("2. A SESSION THAT IS UP AND DELIVERING NOTHING IS NOT HEALTHY")
r = report({"CNC-01": FakeAdapter(last_read_at=NOW - 3600)})
check("an hour of silence on a connected session is not STREAMING",
      r["verdict"] != health.GOOD, r["verdict"])
check("...it is reported as a PLC problem", r["verdict"] == health.NO_PLC, r["verdict"])
check("...saying the session is up but the values are not coming",
      "values are not coming" in r["say"], r["say"])

# ── 3. nothing read yet ─────────────────────────────────────────────
section("3. NOTHING READ YET IS 'STARTING', NEVER 'STREAMING'")
r = report({"CNC-01": FakeAdapter(last_read_at=None)}, started=NOW - 5)
check("a gateway five seconds old has not 'succeeded'", r["verdict"] == health.STARTING,
      r["verdict"])
check("...and says exactly that", "has not read anything yet" in r["say"], r["say"])
r = report({}, started=NOW - 5)
check("no machines at all is also STARTING, not STREAMING",
      r["verdict"] == health.STARTING, r["verdict"])

# ── 4. an AMP outage ────────────────────────────────────────────────
section("4. AN AMP OUTAGE SAYS THE ONE THING THE PERSON WANTS TO HEAR")
buf = make_buffer(records=37)
r = report({"CNC-01": FakeAdapter()},
           publisher=FakePublisher(state=base.DISCONNECTED, last_error="connection timed out"),
           buffer=buf)
check("a cloud outage is reported as a cloud outage", r["verdict"] == health.NO_CLOUD,
      r["verdict"])
check("...it says the controllers are fine, so nobody goes to the plant floor",
      "controllers are fine" in r["say"], r["say"])
check("...and says NOTHING IS BEING LOST", "nothing is being lost" in r["say"], r["say"])
check("...with the number of readings waiting", "37" in r["say"], r["say"])

# ── 5. THE QUEUE IS COUNTED ONCE ────────────────────────────────────
section("5. THE QUEUE IS COUNTED ONCE, NOT ONCE PER MACHINE")
# The bug this pins: `buffers` used to be a dict keyed by machine and summed.
# One gateway has ONE queue, so a cell of three machines multiplied it by three
# and the number an engineer uses to judge a backlog was simply wrong.
r = report({"CNC-01": FakeAdapter(), "CNC-02": FakeAdapter(), "CNC-03": FakeAdapter()},
           buffer=make_buffer(records=10))
check("three machines sharing one queue of 10 report 10, not 30",
      r["buffer"]["queued"] == 10, str(r["buffer"]["queued"]))

# ── 6. a backlog that is not a blip ─────────────────────────────────
section("6. A BACKLOG WITH AGE IS DIFFERENT FROM A QUEUE WITH DEPTH")
r = report({"CNC-01": FakeAdapter()}, buffer=make_buffer(records=5, age=30))
check("a few seconds of queue while connected is still STREAMING",
      r["verdict"] == health.GOOD, f"{r['verdict']}: {r['say']}")
r = report({"CNC-01": FakeAdapter()}, buffer=make_buffer(records=500, age=3600))
check("an hour-old backlog is called out", r["verdict"] == health.BACKLOG, r["verdict"])
check("...naming how far behind it is", "minutes" in r["say"], r["say"])

# ── 7. the good case, and what it does not claim ────────────────────
section("7. STREAMING SAYS WHAT WAS DELIVERED, NOT THAT ALL IS WELL")
r = report({"CNC-01": FakeAdapter()}, buffer=make_buffer())
check("everything working reports STREAMING", r["verdict"] == health.GOOD, r["verdict"])
check("...with the count actually delivered to AMP", "42" in r["say"], r["say"])
check("the empty queue reports None age, not 0 (which would read as 1970)",
      r["buffer"]["oldest_age_s"] is None, str(r["buffer"]["oldest_age_s"]))
check("a machine never read reports None, not 0 seconds ago",
      report({"X": FakeAdapter(last_read_at=None)})["machines"]["X"]["since_last_read_s"] is None)

# ── 8. it prints, and prints nothing secret ─────────────────────────
section("8. THE PRINTED FORM CARRIES NO CREDENTIAL")
lines = health.lines(report({"CNC-01": FakeAdapter()}, buffer=make_buffer()))
text = chr(10).join(lines)
check("the console form renders", len(lines) > 5, str(len(lines)))
check("...naming the verdict first", "STREAMING" in lines[0], lines[0])
check("...and whether messages are signed at all, which is security-relevant",
      "signed: yes" in text, text)
for secret in ("password", "gateway_key", "hunter2", "secret"):
    check(f"no {secret!r} in the printed report", secret not in text.lower(), text[:200])

# A gateway that is NOT signing must say so loudly — it is the difference
# between a pilot that is authenticated and one that is not.
unsigned = health.lines(report({"CNC-01": FakeAdapter()},
                               publisher=FakePublisher(signed=False), buffer=make_buffer()))
check("an UNSIGNED gateway is visible in the report",
      "signed: NO" in chr(10).join(unsigned), chr(10).join(unsigned))

print()
print("=" * 74)
if failures:
    print(f"FAILED ({len(failures)})")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
