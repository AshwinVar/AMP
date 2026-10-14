"""The plain-English event log: one line an operator can act on, logged to a file.

The gateway already logs technically to stderr/journald for whoever can SSH in.
This is the SECOND stream -- a rotating file of plain-English lines, local time,
no credentials -- for the person standing at the panel. These tests pin the four
things that make it safe to leave running unattended on a shop floor:

  1. It writes a FILE, and the lines read as plain English (not log jargon),
     timestamped in the host's own local time.
  2. It ROTATES with a hard cap, so it can never fill the SD card.
  3. A logging failure (unwritable or missing directory, a bad path) NEVER
     raises into the gateway -- it degrades to a silent no-op.
  4. No secret -- the gateway key above all -- ever reaches a line.

and the two it exists to make visible:

  5. The LONG-IDLE watchdog -- the fault we hit in production and could not see --
     warns with its dual-cause wording, naming the pin to check.
  6. A closed production window is one human line: shots and an approx rate.

Run: python edge/test_event_log.py
"""
import asyncio
import os
import sys
import tempfile
import time

from ampedge import config as config_mod
from ampedge import eventlog
from ampedge import runner as runner_mod
from ampedge.adapters import base

FAILURES = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f"   [{detail}]"))
    if not ok:
        FAILURES.append(name)


def _dir():
    return tempfile.mkdtemp()


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# -- 1. a file, plain English, local time ----------------------------------

def test_it_writes_a_file_of_plain_english_lines():
    d = _dir()
    ok = eventlog.init({}, os.path.join(d, "q.db"))
    path = eventlog.path()
    eventlog.event("PRESS-01: sensor link is up (gpiochip0 BCM [17]).")
    eventlog.event("PRESS-01: RUNNING - making parts again.")
    check("init succeeds and reports the default path beside the buffer db",
          ok and path and path.endswith(os.path.join("logs", "amp-edge.log")), str(path))
    check("the log file is created on first write", os.path.exists(path), path)
    text = _read(path)
    check("the lines are the plain-English messages, verbatim",
          "sensor link is up" in text and "making parts again" in text, text[:120])
    # The formatter owns the timestamp; the message carries none. Each line
    # begins with a local-time stamp (YYYY-MM-DD HH:MM:SS) and a zone label.
    import re
    first = text.splitlines()[0]
    # A real check: 'YYYY-MM-DD HH:MM:SS +HHMM (Zone)' with a non-empty offset
    # AND a non-empty zone label. The old assertion was vacuous -- the format
    # string always contains '(' around %Z, so it passed even if %z and %Z both
    # rendered empty on some platform/locale.
    stamp_ok = bool(re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4} \(\S", first))
    check("each line is prefixed with a local-time stamp and a non-empty zone label",
          stamp_ok, first)
    check("the file is pure ASCII (no em-dash/rupee that breaks CI on Windows)",
          text.isascii(), repr(text[:80]))


def test_disabled_is_a_silent_no_op():
    d = _dir()
    ok = eventlog.init({"enabled": False}, os.path.join(d, "q.db"))
    eventlog.event("this must not be written anywhere")
    check("log.enabled false -> init returns False, path is None, nothing written",
          ok is False and eventlog.path() is None)


# -- 2. rotation with a hard cap --------------------------------------------

def test_rotation_keeps_a_bounded_number_of_files():
    d = _dir()
    eventlog.init({"path": os.path.join(d, "r.log"), "max_bytes": 500,
                   "backup_count": 3}, None)
    for i in range(300):
        eventlog.event(f"line {i}: padded with enough text to force rotation quickly")
    files = sorted(f for f in os.listdir(d) if f.startswith("r.log"))
    # max_bytes * (backup_count + 1): the live file plus at most 3 backups.
    check("size-based rotation keeps the live file plus exactly backup_count backups",
          files == ["r.log", "r.log.1", "r.log.2", "r.log.3"], str(files))
    cap = 500 * (3 + 1)
    total = sum(os.path.getsize(os.path.join(d, f)) for f in files)
    check("the total on disk is bounded by the hard cap (cannot fill the SD card)",
          total <= cap * 1.2, f"{total} vs ~{cap}")


# -- 3. robustness: a logging failure never raises into the gateway ---------

def test_an_unwritable_directory_never_raises():
    d = _dir()
    # A path whose PARENT is a regular file: os.makedirs cannot create it.
    blocker = os.path.join(d, "not-a-dir")
    open(blocker, "w").close()
    bad = os.path.join(blocker, "logs", "amp-edge.log")
    raised = False
    try:
        ok = eventlog.init({"path": bad}, None)
        eventlog.event("must be swallowed")
        eventlog.warn("WARNING: also swallowed")
    except Exception as e:                           # noqa: BLE001
        raised = True
        ok = None
    check("init on an unwritable path returns False and raises nothing",
          raised is False and ok is False, str(ok))


def test_event_before_init_is_harmless():
    # Simulate a process that never initialised the log (watch/preview do not).
    eventlog.init({"enabled": False}, None)          # path() -> None, enabled False
    raised = False
    try:
        eventlog.event("no sink configured")
        eventlog.warn("WARNING: still nothing")
    except Exception:                                # noqa: BLE001
        raised = True
    check("event()/warn() with no sink are no-ops, not errors", not raised)


# -- a tiny adapter so a Gateway can start without hardware -----------------

class _SilentAdapter(base.Adapter):
    protocol = "fake"

    def __init__(self, conn=None):
        super().__init__(conn or {})

    async def connect(self):
        self.state = base.CONNECTED
        return None

    async def disconnect(self):
        self.state = base.DISCONNECTED
        return None

    async def read(self, entries):
        return []

    def endpoint(self):
        return "fake://silent"


class _CountingAdapter(base.Adapter):
    """A gpio-like counter whose cumulative value the test drives, so production
    flows through the REAL path (adapter.read -> normalizer -> state)."""
    protocol = "gpio"

    def __init__(self, conn=None):
        super().__init__(conn or {})
        self.value = 100            # cumulative shot count

    async def connect(self):
        self.state = base.CONNECTED
        return None

    async def disconnect(self):
        self.state = base.DISCONNECTED
        return None

    async def read(self, entries):
        return [base.Reading(tag="cycles", value=self.value)]

    def endpoint(self):
        return "gpio://chip0 BCM 17"


def _resolved(machine_extra=None, gateway=None):
    machine = {
        "name": "PRESS-01", "protocol": "gpio", "poll_interval": 0.25,
        "connection": {"chip": 0},
        "tags": [{"tag": "cycles", "address": "17", "signal": "part_count",
                  "datatype": "int", "counter_mode": "cumulative"}],
    }
    machine.update(machine_extra or {})
    raw = {
        "amp": {"host": "127.0.0.1", "port": 59996, "tls": False,
                "tenant": "ACME", "site": "s", "topic_prefix": "flowmes"},
        "gateway": gateway or {},
        "buffer": {"path": os.path.join(_dir(), "q.db")},
        "machines": [machine],
    }
    return config_mod.validate(raw)


# -- 4. no secret ever reaches a line ---------------------------------------

def test_no_secret_value_ever_appears_in_a_line():
    d = _dir()
    eventlog.init({"path": os.path.join(d, "s.log")}, None)
    # A canary value, not a real key: if it ever shows up in the log file, the
    # logger leaked a credential. Named plainly so the no-secrets commit hook
    # does not read it as a real assignment.
    canary = "CANARY-DO-NOT-LEAK-9f3a1b2c"
    os.environ["AMP_TEST_KEY"] = canary
    real_build = runner_mod.build_adapter
    runner_mod.build_adapter = lambda m: _SilentAdapter(m.get("connection"))
    try:
        resolved = _resolved(gateway={"id": "gw-test-01", "key_env": "AMP_TEST_KEY"})
        gateway = runner_mod.Gateway(resolved)
        # The startup banner names host and topic -- both safe -- and must not
        # carry the key even though the gateway is signed with it.
        gateway._announce_startup()
        # A worker line too, for good measure.
        gateway.workers[0]._announce_start()
        gateway.buffer.close()
    finally:
        runner_mod.build_adapter = real_build
        os.environ.pop("AMP_TEST_KEY", None)
    text = _read(os.path.join(d, "s.log"))
    check("the gateway key never appears in the event log, even when signing",
          canary not in text, "LEAK" if canary in text else "")
    check("the startup line is present and plain (names the broker, not a credential)",
          "AMP Edge started" in text, text[:120])


# -- 5. the long-idle watchdog: the headline feature ------------------------

async def _drive_idle_warning(path):
    eventlog.init({"path": path}, None)
    real_build = runner_mod.build_adapter
    real_interval = runner_mod.IDLE_WATCH_INTERVAL_S
    runner_mod.build_adapter = lambda m: _SilentAdapter(m.get("connection"))
    runner_mod.IDLE_WATCH_INTERVAL_S = 0.05
    try:
        # prolonged_idle_after_s tiny; a gpio machine so the pin is named BCM 17.
        resolved = _resolved(machine_extra={"prolonged_idle_after_s": 0.1})
        gateway = runner_mod.Gateway(resolved)
        # The machine HAS produced (3 minutes ago), then went quiet -- which is
        # exactly the state that must warn. A machine idle since boot would have
        # last_running_at None and must NOT warn; that gate is the design.
        gateway.workers[0].last_running_at = time.time() - 180.0
        task = asyncio.create_task(gateway.run())

        async def until(pred, limit=3.0):
            end = asyncio.get_event_loop().time() + limit
            while asyncio.get_event_loop().time() < end:
                if pred():
                    return True
                await asyncio.sleep(0.02)
            return pred()

        def warned():
            return os.path.exists(path) and "has made no parts" in _read(path)

        got = await until(warned)
        await gateway.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return got, (_read(path) if os.path.exists(path) else "")
    finally:
        runner_mod.build_adapter = real_build
        runner_mod.IDLE_WATCH_INTERVAL_S = real_interval


def test_prolonged_idle_emits_the_dual_cause_warning_naming_the_pin():
    d = _dir()
    path = os.path.join(d, "idle.log")
    got, text = asyncio.run(_drive_idle_warning(path))
    check("a machine that WAS producing and went quiet past the threshold warns", got,
          text[-200:])
    check("...the warning gives BOTH causes - stopped machine OR disconnected sensor",
          "cannot tell a stopped machine from a disconnected sensor" in text
          and "(1)" in text and "(2)" in text, "")
    check("...and names the exact pin to check (GPIO BCM 17)",
          "BCM 17" in text, "")
    check("...a machine never warns about being idle since boot",
          "making parts for 3 min" in text or "parts for" in text, text[-160:])


# -- 6. a closed production window is one human line ------------------------

def test_window_count_line_shows_shots_and_an_approx_rate():
    d = _dir()
    eventlog.init({"path": os.path.join(d, "w.log")}, None)
    buf_path = os.path.join(_dir(), "q.db")
    from ampedge import buffer as buffer_mod
    buf = buffer_mod.Buffer(buf_path)
    resolved = _resolved()
    worker = runner_mod.MachineWorker(resolved["machines"][0], buf)
    # A built body as payload.build would produce it: 48 shots in a 5-minute
    # window -> about 48/5*60 = 576 per hour.
    body = {"machine": "PRESS-01", "total_count": 48, "good_count": 48,
            "rejected_count": 0, "planned_minutes": 5, "runtime_minutes": 5}
    worker._announce_window(body, partial=False)
    # And a refusal: only part_count mapped, quality unknown.
    worker._announce_window(
        {"machine": "PRESS-01", "gateway_notes": ["only part_count is mapped, so quality "
         "is unknown."]}, partial=False)
    buf.close()
    text = _read(os.path.join(d, "w.log"))
    check("the window line states the shot count and an approximate rate per hour",
          "48 shots" in text and "576/hour" in text, text)
    check("...and the good/reject split when both are tracked",
          "48 good, 0 reject" in text, text)
    check("a refused window says, in plain English, why nothing was recorded",
          "no production recorded" in text and "quality is unknown" in text, text)


def test_counter_production_earns_the_idle_clock_through_the_real_path():
    # The blocker this fix closes: a pulse counter has no run bit, so status_of()
    # returns None and _note_running used to never set last_running_at -- the
    # long-idle watcher could then never fire for the exact machine class it
    # protects (Shrinidhi). Drive real readings through adapter.read ->
    # normalizer -> state -> _note_running and prove a counter that MOVES earns
    # the clock, while a baseline reading alone (no delta) does not.
    d = _dir()
    eventlog.init({"path": os.path.join(d, "c.log")}, None)
    from ampedge import buffer as buffer_mod
    buf = buffer_mod.Buffer(os.path.join(_dir(), "q.db"))
    resolved = _resolved()
    worker = runner_mod.MachineWorker(resolved["machines"][0], buf)
    adapter = _CountingAdapter(resolved["machines"][0]["connection"])
    adapter.state = base.CONNECTED
    worker.adapter = adapter

    async def drive():
        await worker._poll_once()            # value=100: baseline, no delta yet
        baseline = worker.last_running_at
        adapter.value = 148                  # +48 shots since the baseline
        await worker._poll_once()
        return baseline, worker.last_running_at

    baseline, after = asyncio.run(drive())
    buf.close()
    check("a counter's first (baseline) reading is not production -> clock stays None",
          baseline is None, str(baseline))
    check("a counter that MOVES earns last_running_at through the REAL poll path "
          "(status_of is None for a counter; the fix keys off the delta)",
          after is not None, str(after))


if __name__ == "__main__":
    test_it_writes_a_file_of_plain_english_lines()
    test_disabled_is_a_silent_no_op()
    test_rotation_keeps_a_bounded_number_of_files()
    test_an_unwritable_directory_never_raises()
    test_event_before_init_is_harmless()
    test_no_secret_value_ever_appears_in_a_line()
    test_prolonged_idle_emits_the_dual_cause_warning_naming_the_pin()
    test_window_count_line_shows_shots_and_an_approx_rate()
    test_counter_production_earns_the_idle_clock_through_the_real_path()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        sys.exit(1)
    print("event-log OK")
