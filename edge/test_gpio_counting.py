"""Counting cycles on a Pi's GPIO pins. Driven by a fake chip, so CI can run it.

A test that needs a Raspberry Pi is a test that never runs. The counting rules
are checked here against an injected line reader; what cannot be checked
without hardware -- that lgpio opens a real chip -- is a single call, and
isolating it is the point of `_Lines`.

THE DEFECTS THIS PINS, beyond the ones every counter shares:

  ACTIVE SENSE BACKWARDS. An opto board pulls the pin DOWN when the machine's
  output energises. Read it as active-high and you count the GAPS between
  cycles instead of the cycles -- the same number of edges, so the rate looks
  exactly right and every timestamp is wrong by most of a cycle. There is no
  way to notice this from the count alone, which is why active_low defaults to
  true and is asserted here.

  BCM VERSUS BOARD NUMBERING. BCM 17 is physical pin 11. Wiring to the wrong
  one is the commonest mistake on this hardware, so a number outside the BCM
  range is refused with the distinction spelled out rather than silently
  claimed and read as never firing.

  THE ID EEPROM PINS. BCM 0 and 1 are read at boot by HAT detection. Claiming
  them produces a confusing boot-time failure, not an error at the config.

Run: python edge/test_gpio_counting.py
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ampedge import config as config_mod      # noqa: E402
from ampedge.adapters import base, gpio       # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def section(t):
    print("\n" + "=" * 74 + f"\n{t}\n" + "=" * 74)


class FakeLines:
    """A GPIO chip whose pin levels the test sets by hand."""

    def __init__(self):
        self.level = {}
        self.opened = False
        self.closed = False
        self.claimed = []
        self.fail = None

    def open(self):
        self.opened = True

    def claim(self, bcm, pull_up):
        self.claimed.append((bcm, pull_up))
        self.level.setdefault(bcm, True)      # idle high: opto not conducting

    def read(self, bcm):
        if self.fail:
            raise OSError(self.fail)
        return self.level[bcm]

    def close(self):
        self.closed = True


def adapter(lines, **over):
    settings = dict(debounce_ms=0, idle_after_s=60)
    settings.update(over)
    return gpio.GpioAdapter(settings, lines=lines)


def energise(a, lines, bcm, now, width=0.4):
    """One machine output pulse: opto conducts, pin goes LOW, then releases."""
    lines.level[bcm] = False
    a.sample_once(now=now)
    lines.level[bcm] = True
    a.sample_once(now=now + width)
    return now + width


# ── 1. A cycle is one count ──────────────────────────────────────────

def test_a_pulse_is_one_count():
    async def run():
        lines = FakeLines()
        a = adapter(lines)
        await a.read(["17"])                  # claims the pin
        a.sample_once(now=0.0)
        check("the first sample counts nothing", a.counts[17] == 0,
              str(a.counts))
        t = 1.0
        for _ in range(5):
            t = energise(a, lines, 17, t)
            t += 15.0
        check("five cycles count as five", a.counts[17] == 5, str(a.counts))
    asyncio.get_event_loop().run_until_complete(run())


def test_active_low_is_the_default_because_an_opto_pulls_down():
    async def run():
        lines = FakeLines()
        a = adapter(lines)
        check("active_low defaults to true", a.active_low is True)
        await a.read(["17"])
        a.sample_once(now=0.0)
        # Pin going LOW is the machine energising.
        lines.level[17] = False
        a.sample_once(now=1.0)
        check("a pin pulled LOW counts a cycle", a.counts[17] == 1,
              str(a.counts))

        # And the opposite sense counts the gaps instead -- same edge count,
        # inverted timing, which is why the default matters.
        lines2 = FakeLines()
        b = adapter(lines2, active_low=False)
        await b.read(["17"])
        b.sample_once(now=0.0)
        lines2.level[17] = False
        b.sample_once(now=1.0)
        check("active_high ignores that same LOW", b.counts[17] == 0,
              str(b.counts))
    asyncio.get_event_loop().run_until_complete(run())


def test_a_pin_already_active_at_startup_is_not_a_cycle():
    async def run():
        lines = FakeLines()
        a = adapter(lines)
        await a.read(["17"])
        lines.level[17] = False               # already energised
        a.sample_once(now=0.0)
        a.sample_once(now=0.1)
        check("a pin found active is a baseline, not a shot",
              a.counts[17] == 0, str(a.counts))
        lines.level[17] = True;  a.sample_once(now=1.0)
        lines.level[17] = False; a.sample_once(now=2.0)
        check("the next real cycle still counts", a.counts[17] == 1,
              str(a.counts))
    asyncio.get_event_loop().run_until_complete(run())


def test_bounce_is_not_production():
    async def run():
        lines = FakeLines()
        a = adapter(lines, debounce_ms=20)
        await a.read(["17"])
        a.sample_once(now=0.0)
        t = 1.0
        lines.level[17] = False
        a.sample_once(now=t)
        for i in range(1, 7):                 # 4 ms chatter, inside the window
            lines.level[17] = (i % 2 == 1)
            a.sample_once(now=t + i * 0.004)
        lines.level[17] = True
        a.sample_once(now=t + 0.4)
        check("a bouncing relay is one cycle, not several",
              a.counts[17] == 1, str(a.counts))
    asyncio.get_event_loop().run_until_complete(run())


def test_pins_count_independently():
    async def run():
        lines = FakeLines()
        a = adapter(lines)
        await a.read(["17", "27", "22"])
        a.sample_once(now=0.0)
        t = 1.0
        for bcm, n in ((17, 3), (27, 1), (22, 5)):
            for _ in range(n):
                t = energise(a, lines, bcm, t)
                t += 1.0
        check("each pin counts only its own machine",
              [a.counts[17], a.counts[27], a.counts[22]] == [3, 1, 5],
              str(a.counts))
    asyncio.get_event_loop().run_until_complete(run())


# ── 2. The numbering that gets wired wrong ───────────────────────────

def test_bcm_numbering_is_enforced():
    async def run():
        a = adapter(FakeLines())
        got = (await a.read(["40"]))[0]
        check("a number off the BCM range is refused",
              not got.is_usable, repr(got))
        check("...and the message names BCM versus board numbering",
              "BCM" in got.detail and "board" in got.detail, got.detail)

        for pin in ("0", "1"):
            got = (await a.read([pin]))[0]
            check(f"BCM {pin} (ID EEPROM) is refused",
                  not got.is_usable and "EEPROM" in got.detail, got.detail)

        got = (await a.read(["GPIO17"]))[0]
        check("a name that is not a number is refused",
              not got.is_usable, repr(got))

        # The spellings that ARE accepted.
        for spelling in ("17", " 17 ", "bcm17", "BCM 17"):
            bcm, _ = gpio.GpioAdapter.parse(spelling)
            check(f"{spelling!r} parses as BCM 17", bcm == 17, str(bcm))
        bcm, running = gpio.GpioAdapter.parse("17.running")
        check("'17.running' parses as pin 17 wanting the run state",
              bcm == 17 and running is True, f"{bcm} {running}")
    asyncio.get_event_loop().run_until_complete(run())


# ── 3. Absence is absence ────────────────────────────────────────────

def test_a_chip_that_stopped_is_not_a_stopped_machine():
    async def run():
        lines = FakeLines()
        a = adapter(lines)
        await a.read(["17"])
        a.sample_once(now=0.0)
        energise(a, lines, 17, 1.0)
        got = (await a.read(["17"]))[0]
        check("a live pin reports the count",
              got.is_usable and got.value == 1, repr(got))

        lines.fail = "chip went away"
        ok = a.sample_once(now=5.0)
        check("the sampler reports it could not read", ok is False)
        got = (await a.read(["17"]))[0]
        check("and the reading is NO_DATA, not the last count",
              not got.is_usable and got.quality == base.NO_DATA, repr(got))
        check("...naming the pin", "17" in got.detail, got.detail)
    asyncio.get_event_loop().run_until_complete(run())


def test_before_the_first_sample_nothing_is_claimed():
    async def run():
        a = adapter(FakeLines())
        got = (await a.read(["17"]))[0]
        check("before any sample it is NO_DATA, not zero",
              not got.is_usable and got.value is None, repr(got))
    asyncio.get_event_loop().run_until_complete(run())


def test_running_is_derived_from_the_last_cycle():
    async def run():
        import time as _t
        lines = FakeLines()
        a = adapter(lines, idle_after_s=48)
        await a.read(["17"])
        a.sample_once(now=_t.time())
        got = (await a.read(["17.running"]))[0]
        check("before the first cycle the run state is UNKNOWN",
              not got.is_usable, repr(got))
        energise(a, lines, 17, _t.time())
        got = (await a.read(["17.running"]))[0]
        check("a machine that just cycled is RUNNING",
              got.is_usable and got.value is True, repr(got))
        a.last_edge_at[17] = _t.time() - 300
        got = (await a.read(["17.running"]))[0]
        check("one that has not cycled for five minutes is STOPPED",
              got.is_usable and got.value is False, repr(got))
    asyncio.get_event_loop().run_until_complete(run())


# ── 4. Settings, and the falsy-zero trap ─────────────────────────────

def test_an_explicit_zero_is_not_the_default():
    a = adapter(FakeLines(), debounce_ms=0)
    check("debounce_ms: 0 means 0, not the default",
          a.debounce_ms == 0, str(a.debounce_ms))
    # NOT `adapter(FakeLines())` -- that helper injects debounce_ms=0 so the
    # counting tests can work in synthetic time, so "omitting it" through the
    # helper omits nothing and the assertion was testing the helper's default
    # rather than the adapter's. Build it directly, with the key genuinely
    # absent, which is the only thing that exercises _setting()'s None branch.
    a = gpio.GpioAdapter({"idle_after_s": 60}, lines=FakeLines())
    check("and omitting it gives the default",
          a.debounce_ms == gpio.DEFAULT_DEBOUNCE_MS, str(a.debounce_ms))
    a = adapter(FakeLines(), active_low=False)
    check("active_low: false is honoured", a.active_low is False)


def test_an_absurd_debounce_is_refused():
    # 35000 ms is past the 30s ceiling; -1 is nonsense. Both refused, by name.
    for bad, want in ((35000, "swallow real cycles"), (-1, "negative")):
        try:
            adapter(FakeLines(), debounce_ms=bad)
            check(f"debounce_ms={bad} is refused", False, "accepted")
        except base.AdapterError as exc:
            check(f"debounce_ms={bad} is refused", want in str(exc), str(exc))
    # But a long-yet-legitimate lockout IS accepted: a chattering power-relay
    # dry contact on a slow press needs ~15s to collapse its bounce into one
    # count, and the old 2s ceiling wrongly refused exactly that.
    a = adapter(FakeLines(), debounce_ms=15000)
    check("a 15s debounce (chattering relay on a slow press) is accepted",
          a.debounce_ms == 15000, str(a.debounce_ms))


# ── 5. The config and the runner ─────────────────────────────────────

def test_the_config_refuses_a_gpio_with_a_wire_it_cannot_have():
    os.environ.setdefault("AMP_TEST_GW_KEY", "not-a-real-key")
    base_cfg = {
        "amp": {"host": "b", "port": 8883, "tenant": "T", "site": "s",
                "ca_cert": __file__},
        "gateway": {"id": "gw", "key_env": "AMP_TEST_GW_KEY"},
    }

    def refusal(connection):
        cfg = dict(base_cfg, machines=[{
            "name": "PRESS-01", "protocol": "gpio", "connection": connection,
            "tags": [{"tag": "c", "address": "17", "signal": "part_count",
                      "datatype": "int", "counter_mode": "cumulative"}]}])
        try:
            config_mod.validate(cfg)
            return ""
        except config_mod.ConfigError as exc:
            return str(exc)

    check("a bare gpio connection is accepted", refusal({}) == "",
          refusal({})[:160])
    check("one with chip is accepted", refusal({"chip": 0}) == "",
          refusal({"chip": 0})[:160])
    for key in ("host", "serial_port", "device"):
        text = refusal({key: "x"})
        check(f"a gpio given {key} is refused", key in text, text[:160])


def test_gpio_is_a_known_protocol():
    check("'gpio' is in the supported list",
          "gpio" in config_mod.PROTOCOLS, str(config_mod.PROTOCOLS))
    from ampedge import runner
    a = runner.build_adapter({"name": "P", "protocol": "gpio",
                              "connection": {}})
    check("the runner builds a GpioAdapter",
          isinstance(a, gpio.GpioAdapter), type(a).__name__)
    check("and it never claims to be another protocol",
          a.protocol == "gpio", a.protocol)


def main():
    section("1. A CYCLE IS ONE COUNT")
    test_a_pulse_is_one_count()
    test_active_low_is_the_default_because_an_opto_pulls_down()
    test_a_pin_already_active_at_startup_is_not_a_cycle()
    test_bounce_is_not_production()
    test_pins_count_independently()
    section("2. BCM VERSUS BOARD NUMBERING, THE COMMONEST MISWIRING")
    test_bcm_numbering_is_enforced()
    section("3. ABSENCE IS ABSENCE, NEVER A COUNT THAT STOPPED CLIMBING")
    test_a_chip_that_stopped_is_not_a_stopped_machine()
    test_before_the_first_sample_nothing_is_claimed()
    test_running_is_derived_from_the_last_cycle()
    section("4. SETTINGS, AND THE FALSY-ZERO TRAP")
    test_an_explicit_zero_is_not_the_default()
    test_an_absurd_debounce_is_refused()
    section("5. THE CONFIG AND THE RUNNER")
    test_the_config_refuses_a_gpio_with_a_wire_it_cannot_have()
    test_gpio_is_a_known_protocol()

    print()
    print("=" * 74)
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL CHECKS PASSED - GPIO counts cycles, and absence stays absent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
