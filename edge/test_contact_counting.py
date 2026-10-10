"""Counting cycles off a dry contact, where every mistake inflates the count.

THE DEFECTS THIS PINS, and note which way each one errs:

  BOUNCE COUNTED AS CYCLES. A mechanical contact chatters for a few
  milliseconds on close and every chatter is an edge. Counted, one cycle
  becomes three or five -- a random small multiple of the truth, in the
  direction that flatters the plant, on a number nobody re-derives.

  THE FIRST SAMPLE COUNTED AS A CYCLE. A line already high when the gateway
  starts is not a shot that just happened. Counting it adds a phantom part per
  restart, and a gateway that reconnects often would mint them steadily.

  A DEAD PORT READ AS A STOPPED MACHINE. A yanked USB adapter makes every line
  read false forever. A line that never rises is indistinguishable from a press
  that stopped, so absence has to be reported as absence and never as a count
  that happens not to be climbing.

  ONE MACHINE CLOSING THE PORT FOR FOUR. The four lines are four machines on
  one adapter, sharing one open handle. If the first to disconnect closed it,
  the other three would silently stop counting -- and would report a flat line
  rather than an error, which is the exact lie this package exists to prevent.

Run: python edge/test_contact_counting.py
"""
import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ampedge import config as config_mod          # noqa: E402
from ampedge.adapters import base, contact        # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def section(t):
    print("\n" + "=" * 74 + f"\n{t}\n" + "=" * 74)


class FakePort:
    """A serial port whose four lines we drive by hand. Never opens anything."""

    def __init__(self):
        self.cts = False
        self.dsr = False
        self.cd = False
        self.ri = False
        self.closed = False
        self.raise_on_read = None

    def _maybe_raise(self):
        if self.raise_on_read:
            raise OSError(self.raise_on_read)

    def __getattribute__(self, name):
        if name in ("cts", "dsr", "cd", "ri"):
            object.__getattribute__(self, "_maybe_raise")()
        return object.__getattribute__(self, name)

    def close(self):
        self.closed = True


def watcher_on(fake, debounce_ms=25.0):
    w = contact._PortWatcher("FAKE1", 50.0, debounce_ms)
    w._con = fake
    return w


def pulse(w, fake, line="cts", at=0.0, width=0.4, settle=0.01):
    """One clean close-then-open, sampled either side. Returns the next time."""
    setattr(fake, line, True)
    w.sample_once(now=at)
    setattr(fake, line, False)
    w.sample_once(now=at + width)
    return at + width + settle


# ── 1. A cycle is one count ──────────────────────────────────────────

def test_a_pulse_is_one_count():
    fake = FakePort()
    w = watcher_on(fake)
    w.sample_once(now=0.0)                      # baseline
    check("the first sample counts nothing", w.counts["cts"] == 0,
          f"counted {w.counts['cts']}")

    t = 1.0
    for _ in range(5):
        t = pulse(w, fake, at=t, width=0.4)
        t += 15.0                               # a real moulding cycle
    check("five cycles count as five", w.counts["cts"] == 5,
          f"counted {w.counts['cts']}")
    check("and no other line moved",
          all(w.counts[k] == 0 for k in ("dsr", "cd", "ri")), str(w.counts))


def test_a_line_already_high_at_startup_is_not_a_cycle():
    """The phantom part per restart."""
    fake = FakePort()
    fake.cts = True                             # already closed when we arrive
    w = watcher_on(fake)
    w.sample_once(now=0.0)
    w.sample_once(now=0.1)
    check("a line found high is a baseline, not a shot", w.counts["cts"] == 0,
          f"counted {w.counts['cts']}")

    # ...and the NEXT genuine close still counts.
    fake.cts = False
    w.sample_once(now=1.0)
    pulse(w, fake, at=2.0)
    check("the next real cycle still counts", w.counts["cts"] == 1,
          f"counted {w.counts['cts']}")


def test_only_the_closing_edge_counts():
    fake = FakePort()
    w = watcher_on(fake)
    w.sample_once(now=0.0)
    fake.cts = True
    w.sample_once(now=1.0)
    fake.cts = False
    w.sample_once(now=2.0)
    check("a close and its release are one cycle, not two",
          w.counts["cts"] == 1, f"counted {w.counts['cts']}")


# ── 2. Bounce is not production ──────────────────────────────────────

def test_contact_bounce_is_not_counted():
    fake = FakePort()
    w = watcher_on(fake, debounce_ms=25.0)
    w.sample_once(now=0.0)

    # A real close, then chatter at 5 ms intervals -- well inside the window.
    t = 1.0
    fake.cts = True
    w.sample_once(now=t)
    for i in range(1, 7):
        fake.cts = (i % 2 == 1)
        w.sample_once(now=t + i * 0.005)
    fake.cts = False
    w.sample_once(now=t + 0.4)

    check("a bouncing contact is one cycle, not several",
          w.counts["cts"] == 1, f"counted {w.counts['cts']}")


def test_debounce_does_not_swallow_real_cycles():
    """The opposite error: a window long enough to lose production."""
    fake = FakePort()
    w = watcher_on(fake, debounce_ms=25.0)
    w.sample_once(now=0.0)
    t = 1.0
    for _ in range(3):
        t = pulse(w, fake, at=t, width=0.4)
        t += 0.5                                # a fast press, far above 25 ms
    check("cycles 0.9s apart all count", w.counts["cts"] == 3,
          f"counted {w.counts['cts']}")


def test_an_absurd_debounce_is_refused():
    try:
        contact.ContactAdapter({"serial_port": "COM9", "debounce_ms": 5000})
        check("a debounce that would swallow cycles is refused", False,
              "it was accepted")
    except base.AdapterError as exc:
        check("a debounce that would swallow cycles is refused",
              "swallow real cycles" in str(exc), str(exc))

    try:
        contact.ContactAdapter({"serial_port": "COM9", "debounce_ms": -1})
        check("a negative debounce is refused", False, "it was accepted")
    except base.AdapterError as exc:
        check("a negative debounce is refused", "negative" in str(exc), str(exc))


# ── 3. Absence is absence ────────────────────────────────────────────

def test_a_dead_port_is_not_a_stopped_machine():
    async def run():
        fake = FakePort()
        a = contact.ContactAdapter({"serial_port": "FAKE1"})
        w = watcher_on(fake)
        contact._WATCHERS["FAKE1"] = w
        w._refs = 1
        a._watcher = w
        a.state = base.CONNECTED

        w.sample_once(now=0.0)
        pulse(w, fake, at=1.0)
        got = (await a.read(["cts"]))[0]
        check("a live port reports the count",
              got.is_usable and got.value == 1, repr(got))

        # The adapter is yanked out mid-shift.
        fake.raise_on_read = "device disconnected"
        ok = w.sample_once(now=5.0)
        check("the sampler reports it could not look", ok is False)
        got = (await a.read(["cts"]))[0]
        check("and the reading is NO_DATA, not the last count",
              not got.is_usable and got.quality == base.NO_DATA, repr(got))
        check("...naming the port rather than just failing",
              "FAKE1" in got.detail, got.detail)
        contact.reset_watchers()
    asyncio.get_event_loop().run_until_complete(run())


def test_a_port_never_sampled_reports_so():
    async def run():
        fake = FakePort()
        a = contact.ContactAdapter({"serial_port": "FAKE1"})
        w = watcher_on(fake)
        contact._WATCHERS["FAKE1"] = w
        a._watcher = w
        got = (await a.read(["cts"]))[0]
        check("before the first sample it is NO_DATA, not zero",
              not got.is_usable and got.value is None, repr(got))
        contact.reset_watchers()
    asyncio.get_event_loop().run_until_complete(run())


def test_an_unknown_line_is_refused_not_read_as_never_asserted():
    async def run():
        fake = FakePort()
        a = contact.ContactAdapter({"serial_port": "FAKE1"})
        w = watcher_on(fake)
        contact._WATCHERS["FAKE1"] = w
        a._watcher = w
        w.sample_once(now=0.0)
        got = (await a.read(["rts"]))[0]      # an OUTPUT line, not an input
        check("a line that cannot be read is NO_DATA",
              not got.is_usable and got.quality == base.NO_DATA, repr(got))
        check("...and it says which lines exist",
              "cts" in got.detail and "ri" in got.detail, got.detail)
        contact.reset_watchers()
    asyncio.get_event_loop().run_until_complete(run())


# ── 3b. Run state, derived from the same pulses ──────────────────────

def test_running_is_derived_from_the_last_cycle():
    """Status without a second wire, and without inventing anything.

    A press that completed a cycle ten seconds ago, on a fifteen-second cycle,
    IS running. One that has not completed a cycle in two minutes has stopped.
    That is a measurement -- the pulse arrived or it did not -- and it is the
    difference between AMP showing a live machine status and showing nothing.
    """
    async def run():
        fake = FakePort()
        a = contact.ContactAdapter({"serial_port": "FAKE1", "idle_after_s": 60})
        w = watcher_on(fake)
        contact._WATCHERS["FAKE1"] = w
        a._watcher = w
        w.sample_once(now=0.0)

        got = (await a.read(["cts.running"]))[0]
        check("before the first cycle the run state is UNKNOWN, not Idle",
              not got.is_usable and got.quality == base.NO_DATA, repr(got))
        check("...and it says when it becomes known",
              "first pulse" in got.detail, got.detail)

        # A cycle just completed.
        fake.cts = True
        w.sample_once(now=time.time())
        fake.cts = False
        w.sample_once(now=time.time() + 0.4)
        got = (await a.read(["cts.running"]))[0]
        check("a press that just cycled reads as RUNNING",
              got.is_usable and got.value is True, repr(got))

        # Wind the last edge back past the idle window.
        w.last_edge_at["cts"] = time.time() - 120.0
        got = (await a.read(["cts.running"]))[0]
        check("a press that has not cycled for two minutes reads as STOPPED",
              got.is_usable and got.value is False, repr(got))

        # And just inside the window is still running.
        w.last_edge_at["cts"] = time.time() - 30.0
        got = (await a.read(["cts.running"]))[0]
        check("one still inside the idle window is running",
              got.is_usable and got.value is True, repr(got))

        # The count and the run state come off the SAME line, independently.
        got = (await a.read(["cts"]))[0]
        check("the count is unaffected by asking for the run state",
              got.is_usable and got.value == 1, repr(got))
        contact.reset_watchers()
    asyncio.get_event_loop().run_until_complete(run())


def test_an_idle_window_shorter_than_a_cycle_is_refused():
    """The misconfiguration that would flicker every machine to Idle mid-shot."""
    try:
        contact.ContactAdapter({"serial_port": "COM9", "idle_after_s": 1})
        check("an idle window under a real cycle is refused", False,
              "it was accepted")
    except base.AdapterError as exc:
        check("an idle window under a real cycle is refused",
              "shorter than any real" in str(exc), str(exc))
        check("...and the message gives the rule",
              "two to three times" in str(exc), str(exc))


def test_browse_lists_both_the_count_and_the_run_state():
    async def run():
        fake = FakePort()
        a = contact.ContactAdapter({"serial_port": "FAKE1"})
        w = watcher_on(fake)
        contact._WATCHERS["FAKE1"] = w
        a._watcher = w
        rows = await a.browse()
        addrs = [r["address"] for r in rows]
        check("browse offers all four counts", 
              all(n in addrs for n in ("cts", "dsr", "cd", "ri")), str(addrs))
        check("and the derived run state for each",
              all(n + ".running" in addrs for n in ("cts", "dsr", "cd", "ri")),
              str(addrs))
        check("the run state is offered as the `running` signal",
              {r["signal"] for r in rows} == {"part_count", "running"},
              str({r["signal"] for r in rows}))
        contact.reset_watchers()
    asyncio.get_event_loop().run_until_complete(run())


# ── 4. Four machines, one adapter ────────────────────────────────────

def test_four_lines_count_independently():
    fake = FakePort()
    w = watcher_on(fake)
    w.sample_once(now=0.0)
    t = 1.0
    for line, n in (("cts", 3), ("dsr", 1), ("cd", 2), ("ri", 5)):
        for _ in range(n):
            t = pulse(w, fake, line=line, at=t, width=0.3)
            t += 1.0
    check("each line counts only its own machine",
          [w.counts[k] for k in ("cts", "dsr", "cd", "ri")] == [3, 1, 2, 5],
          str(w.counts))


def test_one_port_means_one_watcher():
    """The sharing itself, un-stubbed.

    A mutation making watcher_for() build a fresh watcher every call survived
    the test below, because that test replaces watcher_for to avoid opening a
    real port -- so it could not see a defect in the function it had stubbed
    out. Four machines on one adapter would each have got their own sampler,
    three of which would never run, and those three would report a count that
    simply never moved. The contract is pinned here, directly.
    """
    contact.reset_watchers()
    try:
        a = contact.watcher_for("FAKE1", 50.0, 25.0)
        b = contact.watcher_for("FAKE1", 50.0, 25.0)
        c = contact.watcher_for("FAKE2", 50.0, 25.0)
        check("the same port always returns the SAME watcher", a is b,
              f"{a!r} vs {b!r}")
        check("a different port gets its own", c is not a)
        check("and the registry holds one entry per port",
              set(contact._WATCHERS) == {"FAKE1", "FAKE2"},
              str(sorted(contact._WATCHERS)))
    finally:
        contact.reset_watchers()


def test_one_machine_leaving_does_not_stop_the_others():
    async def run():
        contact.reset_watchers()
        fake = FakePort()
        made = {}

        def fake_watcher(port, hz, db):
            if port not in contact._WATCHERS:
                w = contact._PortWatcher(port, hz, db)
                w._con = fake
                contact._WATCHERS[port] = w
                made[port] = w
            return contact._WATCHERS[port]

        real_watcher_for, real_start = contact.watcher_for, contact._PortWatcher.start
        contact.watcher_for = fake_watcher
        contact._PortWatcher.start = lambda self: None
        try:
            a = contact.ContactAdapter({"serial_port": "FAKE1"})
            b = contact.ContactAdapter({"serial_port": "FAKE1"})
            await a.connect()
            await b.connect()
            check("two machines share one open port",
                  a._watcher is b._watcher and a._watcher._refs == 2,
                  f"refs={a._watcher._refs}")

            w = a._watcher
            await a.disconnect()
            check("the first to leave does NOT close the port",
                  w._con is not None and not fake.closed)

            w.sample_once(now=0.0)
            pulse(w, fake, line="dsr", at=1.0)
            got = (await b.read(["dsr"]))[0]
            check("so the machine still running keeps counting",
                  got.is_usable and got.value == 1, repr(got))

            await b.disconnect()
            check("the last to leave closes it", fake.closed)
        finally:
            contact.watcher_for = real_watcher_for
            contact._PortWatcher.start = real_start
            contact.reset_watchers()
    asyncio.get_event_loop().run_until_complete(run())


# ── 5. The config refuses what cannot work ───────────────────────────

def test_the_config_refuses_a_contact_without_a_wire():
    os.environ.setdefault("AMP_TEST_GW_KEY", "not-a-real-key")
    base_cfg = {
        "amp": {"host": "b", "port": 8883, "tenant": "T", "site": "s",
                "ca_cert": __file__},
        "gateway": {"id": "gw", "key_env": "AMP_TEST_GW_KEY"},
    }

    def refusal(connection):
        """validate() RAISES with every problem joined; we want the text."""
        cfg = dict(base_cfg, machines=[{
            "name": "PRESS-01", "protocol": "contact", "connection": connection,
            "tags": [{"tag": "c", "address": "cts", "signal": "part_count",
                      "datatype": "int", "counter_mode": "cumulative"}]}])
        try:
            config_mod.validate(cfg)
            return ""
        except config_mod.ConfigError as exc:
            return str(exc)

    text = refusal({})
    check("a contact with no serial_port is refused",
          "serial_port" in text, text[:200])
    check("...and the message says what to wire",
          "opto" in text.lower(), text[:200])

    text = refusal({"serial_port": "COM4", "host": "10.0.0.5"})
    check("a contact given a network address is refused",
          "not a network address" in text, text[:200])

    check("...while a plain serial_port is accepted",
          refusal({"serial_port": "COM4"}) == "",
          refusal({"serial_port": "COM4"})[:200])


def test_contact_is_a_known_protocol():
    check("'contact' is in the supported list",
          "contact" in config_mod.PROTOCOLS, str(config_mod.PROTOCOLS))
    from ampedge import runner
    a = runner.build_adapter({"name": "P", "protocol": "contact",
                              "connection": {"serial_port": "COM4"}})
    check("the runner builds a ContactAdapter for it",
          isinstance(a, contact.ContactAdapter), type(a).__name__)
    check("and it never claims to be another protocol",
          a.protocol == "contact", a.protocol)


def main():
    section("1. A CYCLE IS ONE COUNT")
    test_a_pulse_is_one_count()
    test_a_line_already_high_at_startup_is_not_a_cycle()
    test_only_the_closing_edge_counts()
    section("2. BOUNCE IS NOT PRODUCTION")
    test_contact_bounce_is_not_counted()
    test_debounce_does_not_swallow_real_cycles()
    test_an_absurd_debounce_is_refused()
    section("3. ABSENCE IS ABSENCE, NEVER A COUNT THAT STOPPED CLIMBING")
    test_a_dead_port_is_not_a_stopped_machine()
    test_a_port_never_sampled_reports_so()
    test_an_unknown_line_is_refused_not_read_as_never_asserted()
    section("3b. RUN STATE, DERIVED FROM THE SAME PULSES")
    test_running_is_derived_from_the_last_cycle()
    test_an_idle_window_shorter_than_a_cycle_is_refused()
    test_browse_lists_both_the_count_and_the_run_state()
    section("4. FOUR MACHINES, ONE ADAPTER")
    test_four_lines_count_independently()
    test_one_port_means_one_watcher()
    test_one_machine_leaving_does_not_stop_the_others()
    section("5. THE CONFIG REFUSES WHAT CANNOT WORK")
    test_the_config_refuses_a_contact_without_a_wire()
    test_contact_is_a_known_protocol()

    print()
    print("=" * 74)
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL CHECKS PASSED - a dry contact counts cycles, and absence stays absent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
