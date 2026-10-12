"""systemd readiness and watchdog notifications. The last line of the "it stays
up" story, and inert anywhere that story does not apply.

WHY A WATCHDOG AT ALL. `Restart=always` in the unit brings the gateway back
after a CRASH -- the process exits, systemd starts it again. It does NOTHING for
a process that is alive and HUNG: an event loop wedged on a blocking call, a
deadlock in a library, a kernel that has stopped scheduling. The process never
exits, so systemd never restarts it, and the plant goes dark with `systemctl
status` showing a cheerful green `active (running)`. On an unattended box in a
panel that is the worst failure there is, because nothing and nobody is watching
the thing that was supposed to be watching the machine.

A watchdog closes exactly that gap. The unit declares `WatchdogSec=`, systemd
puts the deadline in `WATCHDOG_USEC`, and the gateway must send `WATCHDOG=1`
before it expires or systemd kills and restarts it. The check-in is sent from
the asyncio loop, so the signal it really carries is "the loop is still turning":
a hung loop cannot send it, which is precisely when we want the restart.

NO DEPENDENCY, AND A NO-OP OFF systemd. The protocol is one datagram to the unix
socket named in `$NOTIFY_SOCKET` -- a dozen lines, so the `sdnotify` package is
not worth a new wheel on an ARM box. More importantly every function here does
NOTHING when there is nothing to talk to: on a laptop, on Windows, in a test,
`$NOTIFY_SOCKET` is unset, `sd_notify` returns False without touching a socket,
and `watchdog_interval` returns None so the caller never starts a watchdog loop.
The gateway behaves identically whether or not systemd is underneath it; the
watchdog is simply absent when nobody is holding the other end.
"""
import os
import socket


def sd_notify(state: str, env=None) -> bool:
    """Send one notification datagram to systemd. False if not under systemd.

    `state` is a systemd notify assignment: `READY=1`, `WATCHDOG=1`,
    `STOPPING=1`. Returns True only if the datagram was actually sent, so a
    caller can tell "told systemd" from "nothing was listening" -- though no
    caller should DEPEND on it being True, because running without systemd is a
    supported, ordinary mode (every developer laptop), not a failure.
    """
    env = os.environ if env is None else env
    addr = env.get("NOTIFY_SOCKET")
    if not addr or not hasattr(socket, "AF_UNIX"):
        return False
    # systemd uses the abstract namespace when the path begins with '@': the
    # leading '@' stands for a leading NUL, which is how Linux names an abstract
    # socket. A path beginning with '/' is an ordinary filesystem socket.
    path = "\0" + addr[1:] if addr.startswith("@") else addr
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(path)
            sock.sendall(state.encode("utf-8"))
        return True
    except OSError:
        # A missing or unwritable socket is not an error to crash on -- it means
        # systemd is not there, which is the same as any other non-systemd host.
        return False


def watchdog_interval(env=None):
    """Seconds between `WATCHDOG=1` check-ins, or None if no watchdog is set.

    systemd puts the deadline it will enforce in `WATCHDOG_USEC`. The convention
    -- and the reason this halves it -- is to check in at HALF the deadline, so a
    single scheduling hiccup or a slightly late wake-up is a delay, not a kill.

    Returns None, meaning "do not run a watchdog loop", when:
      * `WATCHDOG_USEC` is absent or unparseable (not under a watchdog), or
      * `WATCHDOG_PID` is set and names a DIFFERENT process. systemd sets that
        when the watchdog is meant for one pid; a child that inherited the
        environment must not feed a watchdog that is not its own, or a hung
        parent would be kept alive by a healthy child.
    """
    env = os.environ if env is None else env
    try:
        usec = int(env.get("WATCHDOG_USEC", ""))
    except (TypeError, ValueError):
        return None
    if usec <= 0:
        return None
    pid = env.get("WATCHDOG_PID")
    if pid is not None and pid != str(os.getpid()):
        return None
    return usec / 2_000_000.0
