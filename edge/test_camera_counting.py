"""Counting cycles from an LED seen by a camera.

Driven entirely by SYNTHETIC frames. No camera is opened, which is not just
convenience: a test that needs a webcam cannot run in CI, and a counter whose
rules are only ever checked by pointing a phone at a wall is a counter nobody
has actually tested.

THE DEFECTS THIS PINS. The counting rules are the contact adapter's, so they
fail the same way -- upward, where nobody questions them. The camera adds two
of its own:

  A REGION OFF THE FRAME, AVERAGED ANYWAY. A region outside the frame must be
  an error, not a number. Clamping it to the edge would produce a perfectly
  plausible brightness from a patch of wall and count the factory's lights.

  A CAMERA THAT STOPPED, READ AS A STOPPED MACHINE. A phone that sleeps or a
  USB camera that is unplugged stops delivering frames. The last count is then
  STALE, and republishing it says the machine made nothing -- which is exactly
  what a stopped machine looks like.

Run: python edge/test_camera_counting.py
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ampedge import config as config_mod          # noqa: E402
from ampedge.adapters import base, camera         # noqa: E402

failures = []
W, H = 64, 48
BOX = (10, 10, 4, 4)


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def section(t):
    print("\n" + "=" * 74 + f"\n{t}\n" + "=" * 74)


def frame(bg=20, lit=False, value=200, box=BOX):
    """A W*H grayscale frame with `box` bright when lit."""
    buf = bytearray([bg]) * (W * H)
    if lit:
        x, y, w, h = box
        for row in range(y, y + h):
            start = row * W + x
            buf[start:start + w] = bytes([value]) * w
    return bytes(buf)


class FakeFrames:
    """Hands out whatever frames the test queues. Opens nothing."""

    def __init__(self):
        self.queue = []
        self.opened = False
        self.closed = False
        self.fail = False

    def open(self):
        self.opened = True

    def read(self):
        if self.fail:
            return None
        return self.queue[-1] if self.queue else None

    def close(self):
        self.closed = True


def adapter(frames, **over):
    settings = dict(device="fake", region=list(BOX), width=W, height=H,
                    threshold=110, debounce_ms=0, fps=10)
    settings.update(over)
    return camera.CameraAdapter(settings, frames=frames)


# ── 1. region_mean is the measurement, so it is pinned alone ─────────

def test_region_mean():
    f = frame(bg=20, lit=True, value=200)
    m = camera.region_mean(f, W, H, BOX)
    check("a lit region reads its brightness", abs(m - 200) < 0.001, str(m))
    m = camera.region_mean(frame(bg=20, lit=False), W, H, BOX)
    check("a dark region reads the background", abs(m - 20) < 0.001, str(m))

    # THE ONE THAT MATTERS: off-frame is None, not a number.
    for bad in ((W - 2, 10, 4, 4), (10, H - 2, 4, 4), (-1, 10, 4, 4),
                (10, 10, 0, 4), (10, 10, 4, -1)):
        check(f"region {bad} outside the frame is refused",
              camera.region_mean(f, W, H, bad) is None,
              str(camera.region_mean(f, W, H, bad)))


# ── 2. One blink is one count ────────────────────────────────────────

def test_a_blink_is_one_count():
    ff = FakeFrames()
    a = adapter(ff)
    ff.queue.append(frame(lit=False))
    a.sample_once(now=0.0)
    check("the first frame counts nothing", a.count == 0, str(a.count))

    t = 1.0
    for _ in range(5):
        ff.queue.append(frame(lit=True));  a.sample_once(now=t)
        ff.queue.append(frame(lit=False)); a.sample_once(now=t + 2.0)
        t += 16.0
    check("five blinks count as five", a.count == 5, str(a.count))


def test_an_led_already_lit_at_startup_is_not_a_cycle():
    ff = FakeFrames()
    a = adapter(ff)
    ff.queue.append(frame(lit=True))
    a.sample_once(now=0.0)
    a.sample_once(now=0.1)
    check("an LED found lit is a baseline, not a shot", a.count == 0, str(a.count))
    ff.queue.append(frame(lit=False)); a.sample_once(now=1.0)
    ff.queue.append(frame(lit=True));  a.sample_once(now=2.0)
    check("the next real blink still counts", a.count == 1, str(a.count))


def test_only_the_rising_edge_counts():
    ff = FakeFrames()
    a = adapter(ff)
    ff.queue.append(frame(lit=False)); a.sample_once(now=0.0)
    ff.queue.append(frame(lit=True));  a.sample_once(now=1.0)
    ff.queue.append(frame(lit=False)); a.sample_once(now=3.0)
    check("a blink and its end are one cycle, not two", a.count == 1, str(a.count))


def test_flicker_on_the_threshold_is_debounced():
    ff = FakeFrames()
    a = adapter(ff, debounce_ms=200)
    ff.queue.append(frame(lit=False)); a.sample_once(now=0.0)
    t = 1.0
    ff.queue.append(frame(lit=True)); a.sample_once(now=t)
    for i in range(1, 7):               # 20 ms flicker, inside the window
        ff.queue.append(frame(lit=(i % 2 == 1)))
        a.sample_once(now=t + i * 0.02)
    ff.queue.append(frame(lit=False)); a.sample_once(now=t + 2.0)
    check("brightness sitting on the threshold is one cycle, not several",
          a.count == 1, str(a.count))


def test_a_dim_led_below_the_threshold_is_not_lit():
    ff = FakeFrames()
    a = adapter(ff, threshold=110)
    ff.queue.append(frame(lit=False)); a.sample_once(now=0.0)
    ff.queue.append(frame(lit=True, value=90)); a.sample_once(now=1.0)
    check("a region brighter but below the threshold counts nothing",
          a.count == 0, str(a.count))
    ff.queue.append(frame(lit=True, value=200)); a.sample_once(now=2.0)
    check("and above it counts", a.count == 1, str(a.count))


# ── 3. Absence is absence ────────────────────────────────────────────

def test_a_camera_that_stopped_is_not_a_stopped_machine():
    async def run():
        ff = FakeFrames()
        a = adapter(ff)
        ff.queue.append(frame(lit=False)); a.sample_once(now=0.0)
        ff.queue.append(frame(lit=True));  a.sample_once(now=1.0)
        got = (await a.read(["led"]))[0]
        check("a live camera reports the count",
              got.is_usable and got.value == 1, repr(got))

        ff.fail = True                   # the phone slept / USB pulled
        ok = a.sample_once(now=5.0)
        check("the sampler reports it got no frame", ok is False)
        got = (await a.read(["led"]))[0]
        check("and the reading is NO_DATA, not the last count",
              not got.is_usable and got.quality == base.NO_DATA, repr(got))
    asyncio.get_event_loop().run_until_complete(run())


def test_a_region_off_the_frame_is_an_error_not_a_number():
    async def run():
        ff = FakeFrames()
        a = adapter(ff, region=[W - 2, 10, 8, 8])
        ff.queue.append(frame(lit=False))
        ok = a.sample_once(now=0.0)
        check("a region past the frame edge fails the sample", ok is False)
        got = (await a.read(["led"]))[0]
        check("...and reads NO_DATA rather than a patch of wall",
              not got.is_usable, repr(got))
        check("...naming the region and the frame size",
              "region" in got.detail and "64x48" in got.detail, got.detail)
    asyncio.get_event_loop().run_until_complete(run())


def test_before_the_first_frame_nothing_is_claimed():
    async def run():
        a = adapter(FakeFrames())
        got = (await a.read(["led"]))[0]
        check("before any frame it is NO_DATA, not zero",
              not got.is_usable and got.value is None, repr(got))
    asyncio.get_event_loop().run_until_complete(run())


def test_an_unknown_address_is_refused():
    async def run():
        ff = FakeFrames()
        a = adapter(ff)
        ff.queue.append(frame(lit=False)); a.sample_once(now=0.0)
        got = (await a.read(["pixel"]))[0]
        check("an address that is not an LED is NO_DATA",
              not got.is_usable and got.quality == base.NO_DATA, repr(got))
    asyncio.get_event_loop().run_until_complete(run())


# ── 4. The run state, from the same blinks ───────────────────────────

def test_running_is_derived_from_the_last_blink():
    async def run():
        import time as _t
        ff = FakeFrames()
        a = adapter(ff, idle_after_s=48)
        ff.queue.append(frame(lit=False)); a.sample_once(now=_t.time())
        got = (await a.read(["led.running"]))[0]
        check("before the first blink the run state is UNKNOWN",
              not got.is_usable, repr(got))

        ff.queue.append(frame(lit=True)); a.sample_once(now=_t.time())
        got = (await a.read(["led.running"]))[0]
        check("a machine that just blinked is RUNNING",
              got.is_usable and got.value is True, repr(got))

        a.last_edge_at = _t.time() - 300
        got = (await a.read(["led.running"]))[0]
        check("one that has not blinked for five minutes is STOPPED",
              got.is_usable and got.value is False, repr(got))
    asyncio.get_event_loop().run_until_complete(run())


# ── 5. The config refuses what cannot work ───────────────────────────

def test_the_config_refuses_a_camera_without_aim():
    os.environ.setdefault("AMP_TEST_GW_KEY", "not-a-real-key")
    base_cfg = {
        "amp": {"host": "b", "port": 8883, "tenant": "T", "site": "s",
                "ca_cert": __file__},
        "gateway": {"id": "gw", "key_env": "AMP_TEST_GW_KEY"},
    }

    def refusal(connection):
        cfg = dict(base_cfg, machines=[{
            "name": "PRESS-01", "protocol": "camera", "connection": connection,
            "tags": [{"tag": "c", "address": "led", "signal": "part_count",
                      "datatype": "int", "counter_mode": "cumulative"}]}])
        try:
            config_mod.validate(cfg)
            return ""
        except config_mod.ConfigError as exc:
            return str(exc)

    text = refusal({"region": [1, 2, 3, 4]})
    check("a camera with no device is refused", "device" in text, text[:160])
    text = refusal({"device": "cam"})
    check("a camera with no region is refused", "region" in text, text[:160])
    check("...and the message says how to find it",
          "camera_aim" in text, text[:200])
    check("a camera with both is accepted",
          refusal({"device": "cam", "region": [1, 2, 3, 4]}) == "",
          refusal({"device": "cam", "region": [1, 2, 3, 4]})[:160])


def test_camera_is_a_known_protocol():
    check("'camera' is in the supported list",
          "camera" in config_mod.PROTOCOLS, str(config_mod.PROTOCOLS))
    from ampedge import runner
    a = runner.build_adapter({"name": "P", "protocol": "camera",
                              "connection": {"device": "cam",
                                             "region": [1, 2, 3, 4]}})
    check("the runner builds a CameraAdapter",
          isinstance(a, camera.CameraAdapter), type(a).__name__)
    check("and it never claims to be another protocol",
          a.protocol == "camera", a.protocol)


def main():
    section("1. THE MEASUREMENT ITSELF")
    test_region_mean()
    section("2. ONE BLINK IS ONE COUNT")
    test_a_blink_is_one_count()
    test_an_led_already_lit_at_startup_is_not_a_cycle()
    test_only_the_rising_edge_counts()
    test_flicker_on_the_threshold_is_debounced()
    test_a_dim_led_below_the_threshold_is_not_lit()
    section("3. ABSENCE IS ABSENCE, NEVER A COUNT THAT STOPPED CLIMBING")
    test_a_camera_that_stopped_is_not_a_stopped_machine()
    test_a_region_off_the_frame_is_an_error_not_a_number()
    test_before_the_first_frame_nothing_is_claimed()
    test_an_unknown_address_is_refused()
    section("4. THE RUN STATE, FROM THE SAME BLINKS")
    test_running_is_derived_from_the_last_blink()
    section("5. THE CONFIG REFUSES WHAT CANNOT WORK")
    test_the_config_refuses_a_camera_without_aim()
    test_camera_is_a_known_protocol()

    print()
    print("=" * 74)
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL CHECKS PASSED - a camera counts blinks, and absence stays absent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
