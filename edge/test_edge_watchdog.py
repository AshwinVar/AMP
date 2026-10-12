"""The watchdog, and the clean shutdown that must survive a restart.

Restart=always in the unit is only half of "it stays up". It brings back a
gateway that CRASHED; it does nothing for one that is alive and HUNG, because a
hung process never exits. These tests pin the three things that close that gap:

  1. The notify protocol is a NO-OP off systemd. The same binary runs on a
     laptop, in CI and under Docker, and none of them have a NOTIFY_SOCKET. If
     any call raised, or blocked, or guessed, the gateway would behave
     differently on the one platform where it actually ships.
  2. The check-in interval is HALF the deadline systemd set, and a watchdog
     meant for another pid is left alone.
  3. The runner actually announces READY when its loops are up, feeds the
     watchdog while they turn, and says STOPPING on the way down -- so a clean
     stop is never mistaken for a hang, and a hung loop stops the check-ins.

Run: python edge/test_edge_watchdog.py
"""
import asyncio
import os
import socket
import sys
import tempfile

from ampedge import runner as runner_mod
from ampedge import sdnotify
from ampedge.adapters import base

FAILURES = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f"   [{detail}]"))
    if not ok:
        FAILURES.append(name)


# ── 1. sd_notify is inert off systemd, and speaks the protocol on it ──────

def test_sd_notify_is_a_noop_without_a_socket():
    check("no NOTIFY_SOCKET -> sd_notify returns False and touches nothing",
          sdnotify.sd_notify("READY=1", env={}) is False)


def test_sd_notify_sends_the_datagram_under_systemd():
    # The real transport is an AF_UNIX datagram, which Windows does not do. On a
    # platform without it the no-op path above is the whole behaviour, so there
    # is nothing here to exercise.
    if not hasattr(socket, "AF_UNIX"):
        check("sd_notify datagram (skipped: no AF_UNIX on this platform)", True)
        return
    path = os.path.join(tempfile.mkdtemp(), "notify.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(path)
    srv.settimeout(2.0)
    try:
        sent = sdnotify.sd_notify("WATCHDOG=1", env={"NOTIFY_SOCKET": path})
        got = srv.recv(64)
        check("a configured NOTIFY_SOCKET receives exactly the assignment sent",
              sent is True and got == b"WATCHDOG=1", f"{sent!r} {got!r}")
    finally:
        srv.close()


# ── 2. the check-in interval ──────────────────────────────────────────────

def test_no_watchdog_interval_without_systemd():
    check("no WATCHDOG_USEC -> watchdog_interval is None (the loop never starts)",
          sdnotify.watchdog_interval(env={}) is None)


def test_interval_is_half_the_deadline():
    # systemd sets the DEADLINE; the gateway checks in at half of it so a single
    # late wake-up is a delay, not a kill. 60s deadline -> 30s check-in.
    val = sdnotify.watchdog_interval(env={"WATCHDOG_USEC": "60000000"})
    check("a 60s deadline becomes a 30s check-in", val == 30.0, repr(val))


def test_garbage_and_nonpositive_deadlines_are_refused():
    cases = ["", "abc", "0", "-5", None]
    bad = [c for c in cases
           if sdnotify.watchdog_interval(env={} if c is None else {"WATCHDOG_USEC": c}) is not None]
    check("an absent, unparseable or non-positive deadline yields None", not bad, str(bad))


def test_a_watchdog_for_another_pid_is_left_alone():
    # systemd sets WATCHDOG_PID when the watchdog is meant for one process. A
    # child that inherited the environment must NOT feed a watchdog that names
    # the parent, or a hung parent would be held up by a healthy child.
    mine = str(os.getpid())
    other = str(os.getpid() + 1)
    good = sdnotify.watchdog_interval(env={"WATCHDOG_USEC": "60000000", "WATCHDOG_PID": mine})
    skip = sdnotify.watchdog_interval(env={"WATCHDOG_USEC": "60000000", "WATCHDOG_PID": other})
    check("our own pid is fed; another pid's watchdog is ignored",
          good == 30.0 and skip is None, f"{good!r} {skip!r}")


# ── 3. the runner announces its lifecycle ─────────────────────────────────

class _SilentAdapter(base.Adapter):
    """Enough adapter to let a Gateway start: it connects and reads nothing."""
    protocol = "fake"

    def __init__(self, conn=None):
        super().__init__(conn or {})

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def read(self, entries):
        return []

    def endpoint(self):
        return "fake://silent"


async def _run_lifecycle():
    sent = []
    real_notify = sdnotify.sd_notify
    real_build = runner_mod.build_adapter
    # Record the assignments the runner sends, decoupled from the unix socket so
    # this runs on every platform. The interval is read from the environment by
    # the real watchdog_interval, so the loop is genuinely exercised.
    sdnotify.sd_notify = lambda state, env=None: sent.append(state) or True
    runner_mod.build_adapter = lambda machine: _SilentAdapter(machine.get("connection"))
    os.environ["WATCHDOG_USEC"] = "200000"          # 0.2s deadline -> 0.1s check-in
    os.environ["WATCHDOG_PID"] = str(os.getpid())
    try:
        resolved = {
            "amp": {"host": "127.0.0.1", "port": 59997, "tls": False,
                    "tenant": "ACME", "site": "s", "topic_prefix": "flowmes"},
            "gateway": {},
            "buffer": {"path": os.path.join(tempfile.mkdtemp(), "q.db")},
            "machines": [{"name": "M1", "protocol": "fake", "poll_interval": 0.01,
                          "connection": {}, "tags": [], "mappings": []}],
        }
        gateway = runner_mod.Gateway(resolved)
        task = asyncio.create_task(gateway.run())

        # READY the moment the loops are scheduled, then at least one WATCHDOG
        # as the loop turns.
        async def until(pred, limit=3.0):
            end = asyncio.get_event_loop().time() + limit
            while asyncio.get_event_loop().time() < end:
                if pred():
                    return True
                await asyncio.sleep(0.02)
            return pred()

        ready = await until(lambda: "READY=1" in sent)
        fed = await until(lambda: sent.count("WATCHDOG=1") >= 1)
        await gateway.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return sent, ready, fed
    finally:
        sdnotify.sd_notify = real_notify
        runner_mod.build_adapter = real_build
        os.environ.pop("WATCHDOG_USEC", None)
        os.environ.pop("WATCHDOG_PID", None)


def test_runner_announces_ready_feeds_watchdog_and_says_stopping():
    sent, ready, fed = asyncio.run(_run_lifecycle())
    check("the gateway announces READY once its loops are scheduled", ready, str(sent[:3]))
    check("...feeds the watchdog while the loop turns", fed,
          str(sent.count("WATCHDOG=1")))
    check("...and says STOPPING on the way down, so a clean stop is not a hang",
          "STOPPING=1" in sent, str(sent[-3:]))
    # READY must come before the first WATCHDOG: feeding a watchdog systemd has
    # not armed yet is harmless, but claiming readiness after already running is
    # the kind of ordering bug that makes Type=notify units flap.
    if "READY=1" in sent and "WATCHDOG=1" in sent:
        check("READY precedes the first WATCHDOG",
              sent.index("READY=1") < sent.index("WATCHDOG=1"), str(sent[:4]))


if __name__ == "__main__":
    test_sd_notify_is_a_noop_without_a_socket()
    test_sd_notify_sends_the_datagram_under_systemd()
    test_no_watchdog_interval_without_systemd()
    test_interval_is_half_the_deadline()
    test_garbage_and_nonpositive_deadlines_are_refused()
    test_a_watchdog_for_another_pid_is_left_alone()
    test_runner_announces_ready_feeds_watchdog_and_says_stopping()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        sys.exit(1)
    print("watchdog + clean-shutdown OK")
