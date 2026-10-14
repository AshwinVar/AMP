"""The loop. Reads PLCs, queues to disk, drains to AMP, and never stops for either.

THE DESIGN IS ONE SENTENCE: reading and sending are independent, and neither is
allowed to block the other.

    poll   PLC -> adapter -> mapper -> normalizer -> payload -> DISK
    drain  DISK -> publisher -> AMP -> delete from disk

A factory's internet and a factory's PLC network fail at different times and for
different reasons. Coupling them — publishing inline from the poll loop — means
an AMP outage stops the gateway reading, so the outage becomes a hole in the
plant's history rather than a delay in its upload. Splitting them is what makes
`PLC OFFLINE`, `AMP OFFLINE` and `BOTH FINE` three separate, visible states
instead of one silence.

BACKOFF IS BOUNDED AND JITTERED. A PLC that has been power-cycled comes back all
at once, and twelve gateways reconnecting on the same 1-second timer is a
self-inflicted denial of service against a controller with an 8-session limit.

SHUTDOWN IS CLEAN OR IT IS NOT SHUTDOWN. On stop, the in-flight window is
flushed to disk before the process exits — a gateway restarted during a shift
must not lose the parts made since its last publish.
"""
import asyncio
import logging
import random
import time

from . import buffer as buffer_mod
from . import config as config_mod
from . import eventlog
from . import health
from . import normalizer as normalizer_mod
from . import payload as payload_mod
from . import publisher as publisher_mod
from . import sdnotify
from . import signals as signals_mod
from .adapters import base

log = logging.getLogger("ampedge")

# How often the long-idle watcher wakes. Cheap (a timestamp compare per
# machine); far longer than any poll, so it never competes with PLC reads.
IDLE_WATCH_INTERVAL_S = 60.0
# A worker hitting an UNEXPECTED (non-AdapterError) failure in a tight crash
# loop must not flood the event log. Write the first, then at most one line per
# this interval while it keeps failing. The technical logger keeps every one.
WORKER_ERROR_QUIET_S = 300.0
DRAIN_ERROR_QUIET_S = 300.0
# The local queue crossing this fraction of its bound is the first warning that
# an outage is lasting -- said once, before anything is actually dropped.
BUFFER_WARN_FRACTION = 0.9


def _local(ts):
    """A wall-clock stamp in the host's own local time, ASCII only.

    The event line already carries a timestamp from the formatter; this is for
    naming a DIFFERENT time inside a message -- when a gap began, say -- so it is
    spelled out in full rather than relative.
    """
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "an unknown time"


def _bcm_of(address):
    """The BCM pin number behind a gpio address like '17', 'bcm17', '17.running'."""
    text = str(address).strip().lower()
    if text.endswith(".running"):
        text = text[: -len(".running")]
    if text.startswith("bcm"):
        text = text[3:].lstrip(" :")
    try:
        return int(text)
    except ValueError:
        return None


def _counter_addresses(spec):
    return [m.address for m in (spec.get("mappings") or [])
            if getattr(m, "counter_mode", None)]


def _sensor_location(spec):
    """Where to physically look for this machine's cycle sensor, in plain words.

    The whole value of the long-idle warning is that it names the pin or line an
    electrician can put a meter on. Derived from the counter mapping's address
    and the connection, not from the adapter (which reports '(none)' until it has
    claimed a pin).
    """
    protocol = spec.get("protocol")
    conn = spec.get("connection") or {}
    addresses = _counter_addresses(spec)
    if protocol == "gpio":
        pins = [b for b in (_bcm_of(a) for a in addresses) if b is not None]
        if pins:
            label = ", ".join(f"BCM {p}" for p in pins)
            return f"GPIO pin {label} (gpiochip{conn.get('chip', 0)})"
        return f"the GPIO pin on gpiochip{conn.get('chip', 0)}"
    if protocol == "contact":
        port = conn.get("serial_port") or "the serial port"
        lines = ", ".join(str(a).upper() for a in addresses) if addresses else "the serial line"
        return f"serial line {lines} on {port}"
    if addresses:
        return f"{protocol} address(es) {', '.join(str(a) for a in addresses)}"
    return f"the {protocol} input"

RECONNECT_MIN_S = 2.0
RECONNECT_MAX_S = 60.0
DRAIN_BATCH = 50
DRAIN_IDLE_S = 1.0
# A gateway that was DOWN longer than this across a restart missed production it
# cannot recover: the poll adapters count in RAM and reset to a baseline on
# start. Below it, a restart is an ordinary bounce (a deploy, a service restart)
# and flagging every one as a data gap would be noise; above it -- a power cut, a
# crash loop, a host left off -- the next record is marked so the hole is visible
# instead of drawn across. buffer.note_restart() owns the detection.
RESTART_GAP_MIN_S = 120.0
# THE PRODUCTION WINDOW MUST BE LONG ENOUGH TO CARRY A DUTY CYCLE.
#
# AMP's ProductionRecord stores planned_minutes and runtime_minutes as whole
# MINUTES, and availability is their ratio. A 30-second window cannot express a
# duty cycle in that unit: a machine cutting 21% of the time (100 minutes of a
# 480-minute shift, which is an ordinary tool room) yields planned=1 and
# round(1 x 0.21) = 0 — availability 0%, every window, forever, and therefore
# OEE 0% for a plant working normally.
#
#   window  30s -> planned=1  runtime=0  ->   0%   (true 21%)
#   window 120s -> planned=2  runtime=0  ->   0%
#   window 300s -> planned=5  runtime=1  ->  20%   usable
#
# Five minutes is the shortest window at which the ratio survives the rounding.
# It costs nothing in responsiveness: since state changes publish immediately
# and carry no counts, this window governs ONLY the production accounting, and
# a count arriving up to five minutes late is an accounting figure, not an
# alarm.
PUBLISH_WINDOW_S = 300.0


def build_adapter(machine: dict):
    """The one place a protocol name becomes a class. Unknown never guesses."""
    protocol = machine["protocol"]
    if protocol == "opcua":
        from .adapters.opcua import OpcUaAdapter
        return OpcUaAdapter(machine["connection"])
    if protocol == "modbus":
        from .adapters.modbus import ModbusAdapter
        return ModbusAdapter(machine["connection"])
    if protocol == "focas":
        from .adapters.focas import FocasAdapter
        return FocasAdapter(machine["connection"])
    if protocol == "contact":
        from .adapters.contact import ContactAdapter
        return ContactAdapter(machine["connection"])
    if protocol == "camera":
        from .adapters.camera import CameraAdapter
        return CameraAdapter(machine["connection"])
    if protocol == "gpio":
        from .adapters.gpio import GpioAdapter
        return GpioAdapter(machine["connection"])
    raise config_mod.ConfigError(
        f"{machine.get('name')}: AMP Edge does not speak {protocol!r}. "
        f"Supported: {', '.join(config_mod.PROTOCOLS)}.")


def backoff(attempt: int) -> float:
    """Exponential with jitter, capped. Jitter matters more than the curve."""
    base_delay = min(RECONNECT_MAX_S, RECONNECT_MIN_S * (2 ** max(0, attempt - 1)))
    return base_delay * (0.5 + random.random() * 0.5)


class MachineWorker:
    """One machine: its adapter, its mapper state, its window, its queue."""

    def __init__(self, spec: dict, buf, publish_window=PUBLISH_WINDOW_S):
        self.spec = spec
        self.name = spec["name"]
        self.buffer = buf
        self.adapter = None
        self.normalizer = normalizer_mod.Normalizer(spec["mappings"])
        self.state = payload_mod.MachineState(
            self.name, quality_unknown_is_good=bool(spec.get("quality_unknown_is_good")))
        # The whole spec, not just the address: a Modbus register number carries
        # none of the information needed to read it (type, width, word order).
        self.addresses = [m.raw for m in spec["mappings"]]
        self.poll_interval = spec["poll_interval"]
        self.publish_window = publish_window
        # The last state AMP has been told. None means 'nothing yet', which
        # is not the same as Idle and must not publish as one.
        self.published_status = None
        self.attempt = 0
        self.stopping = False
        self.last_publish = time.time()
        # -- event-log transition state (never touches the technical logger) --
        # Announce the "watching ..." line exactly once, at the first run.
        self._announced_start = False
        # The reason last written for a lost connection, so a long outage is one
        # line, not one per backoff retry; written again only when it changes.
        self._plc_down_reason = None
        # The last "no production this window" note written, so the standing
        # config advice is said once per occurrence, not once every 5-min window.
        self._window_note_said = None
        # Rate-limit the unexpected-failure line in a crash loop.
        self._worker_error_at = 0.0
        # Edge-trigger for a live sensor going silent (gpio/contact read fault),
        # which never raises and so never reaches the AdapterError path.
        self._sensor_silent = False
        # The long-idle watcher's view of this machine. last_running_at stays
        # None until the machine has actually produced, so a machine idle since
        # boot (shift not started) never warns.
        self.last_running_at = None
        self.idle_warned = False
        self.prolonged_idle_after_s = float(spec.get("prolonged_idle_after_s") or 1800.0)

    async def run(self):
        self._announce_start()
        while not self.stopping:
            try:
                if self.adapter is None or self.adapter.state not in (base.CONNECTED, base.DEGRADED):
                    await self._connect()
                await self._poll_once()
                self.attempt = 0
                await asyncio.sleep(self.poll_interval)
            except base.AdapterError as e:
                # Named protocol failure: drop the session and come back. The
                # window is flushed first so nothing already read is lost to a
                # reconnect.
                self.attempt += 1
                delay = backoff(self.attempt)
                log.warning("%s: %s — reconnecting in %.0fs", self.name, e, delay)
                self._announce_lost(str(e), delay)
                await self._flush(force=True)
                await self._drop()
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            except Exception:                        # noqa: BLE001 - keep the gateway alive
                self.attempt += 1
                delay = backoff(self.attempt)
                log.exception("%s: unexpected failure — retrying in %.0fs", self.name, delay)
                now = time.time()
                if now - self._worker_error_at >= WORKER_ERROR_QUIET_S:
                    self._worker_error_at = now
                    where = eventlog.path() or "the gateway log"
                    eventlog.warn(
                        f"WARNING: {self.name} hit an unexpected problem and is retrying in "
                        f"{delay:.0f}s. If this keeps repeating, note the time and send the "
                        f"technical log ({where}, or run 'journalctl -u amp-edge' on the Pi) "
                        f"to AMP.")
                await asyncio.sleep(delay)

    def _announce_start(self):
        """Say, once, what this machine is being watched for and how often."""
        if self._announced_start:
            return
        self._announced_start = True
        protocol = self.spec.get("protocol")
        conn = self.spec.get("connection") or {}
        poll = self.poll_interval
        if protocol == "gpio":
            pins = [b for b in (_bcm_of(a) for a in _counter_addresses(self.spec))
                    if b is not None]
            where = ", ".join(str(p) for p in pins) or "(no counter pin mapped)"
            eventlog.event(
                f"{self.name}: watching for cycle pulses on GPIO pin BCM {where} "
                f"(gpiochip{conn.get('chip', 0)}), reading every {poll:g}s.")
        elif protocol == "contact":
            lines = ", ".join(str(a).upper() for a in _counter_addresses(self.spec)) \
                or "(no line mapped)"
            eventlog.event(
                f"{self.name}: watching for cycle pulses on serial line {lines} of "
                f"{conn.get('serial_port')}, reading every {poll:g}s.")
        else:
            addrs = ", ".join(str(m.address) for m in (self.spec.get("mappings") or []))
            eventlog.event(
                f"{self.name}: starting {protocol} link, watching {addrs or '(no tags)'}, "
                f"reading every {poll:g}s.")

    def _announce_lost(self, reason, delay):
        """Lost the connection -- once per outage, not once per backoff retry."""
        if reason == self._plc_down_reason:
            return
        self._plc_down_reason = reason
        protocol = self.spec.get("protocol")
        conn = self.spec.get("connection") or {}
        low = reason.lower()
        if protocol == "gpio" and "gpiochip" in low:
            eventlog.warn(
                f"WARNING: {self.name}: cannot reach the Pi's GPIO - is this a Raspberry Pi, "
                f"and is the service in the 'gpio' group? Retrying in {delay:.0f}s.")
        elif protocol == "contact" and "could not open" in low:
            eventlog.warn(
                f"WARNING: {self.name}: cannot open the USB-serial adapter "
                f"{conn.get('serial_port')} - check it is plugged in. Retrying in {delay:.0f}s.")
        else:
            eventlog.warn(
                f"WARNING: {self.name}: lost the connection to the machine - {reason}. "
                f"Retrying in {delay:.0f}s.")

    async def _connect(self):
        reconnect = self.attempt > 0
        self.adapter = build_adapter(self.spec)
        await self.adapter.connect()
        log.info("%s: connected to %s (%s)", self.name, self.adapter.endpoint(),
                 self.adapter.protocol)
        endpoint = self.adapter.endpoint()
        if reconnect:
            eventlog.event(f"{self.name}: reconnected to the machine ({endpoint}).")
        else:
            eventlog.event(f"{self.name}: sensor link is up ({endpoint}).")
        # The connection is back; the next loss is news again.
        self._plc_down_reason = None

    async def _drop(self):
        if self.adapter is not None:
            try:
                await self.adapter.disconnect()
            except Exception:                        # noqa: BLE001 - already failing
                pass
        self.adapter = None

    async def _poll_once(self):
        readings = await self.adapter.read(self.addresses)
        samples = self.normalizer.absorb(readings)
        for rejection in self.normalizer.rejections:
            # Debug, not warning: on a first commissioning pass half the tag
            # list is wrong and a warning per tag per poll makes the log
            # useless. The health report counts them, which is where a person
            # should be looking.
            log.debug("%s: %s -> %s", self.name, rejection.tag, rejection.reason)
        self._note_sensor_health(readings)
        self.state.absorb(samples)
        # A pulse counter has no run bit; the ONLY evidence it is alive is a
        # counter that moved this poll. Pass that through so the long-idle
        # watcher has a clock on a counter, not just on a PLC with a RUNNING tag.
        produced = any(signals_mod.is_counter(s.signal) and (s.value or 0) > 0
                       for s in samples)
        self._note_running(time.time(), produced)
        await self._announce_state_change()
        await self._flush()

    def _note_sensor_health(self, readings):
        """A live sensor going silent -- a gpio/contact read fault -- never raises
        (base.py's contract: read() returns no_data for a failed tag). So it does
        not reach the AdapterError path, and the machine just goes quiet. Catch
        the transition here, edge-triggered, and name the pin to check.

        A wire that simply falls OFF throws nothing and sets no detail -- it just
        stops pulsing -- which is why this is only HALF the story; the long-idle
        watcher is the other half.
        """
        if not readings:
            return
        unusable = [r for r in readings if not r.is_usable]
        all_silent = len(unusable) == len(readings)
        # Only a reading that carries a 'stopped answering' / 'stopped reading'
        # detail is a FAULT; a counter's first-poll baseline is also unusable and
        # must not trip this.
        faulting = [r for r in unusable
                    if "stopped" in (r.detail or "").lower()]
        if all_silent and faulting and not self._sensor_silent:
            self._sensor_silent = True
            reason = faulting[0].detail
            protocol = self.spec.get("protocol")
            conn = self.spec.get("connection") or {}
            if protocol == "contact":
                eventlog.warn(
                    f"WARNING: {self.name}: the USB-serial adapter {conn.get('serial_port')} "
                    f"stopped answering ({reason}). Check the adapter is still plugged in "
                    f"(this can affect every machine on {conn.get('serial_port')}).")
            else:
                pin = _sensor_location(self.spec)
                eventlog.warn(
                    f"WARNING: {self.name}: the cycle sensor on {pin} stopped responding "
                    f"({reason}). Check the sensor wire and the opto board.")
        elif not all_silent and self._sensor_silent:
            # Values are flowing again; the next window/state line shows it.
            self._sensor_silent = False

    def _note_running(self, now, produced=False):
        """Keep the long-idle watcher's clock, and announce a return to producing.

        A PLC with a run bit announces RUNNING through status_of(). A pulse
        counter -- the Shrinidhi case -- has no run bit at all: its only evidence
        of life is a counter that moved, so `produced` (a positive delta this
        poll) is what marks it running. Without this, last_running_at stays None
        forever on a counter and the long-idle watcher can never fire -- the one
        machine class it exists to protect.
        """
        status = payload_mod.status_of(self.state.latest)
        if status == payload_mod.RUNNING or produced:
            was_quiet_since = self.last_running_at
            self.last_running_at = now
            if self.idle_warned:
                self.idle_warned = False
                mins = round((now - was_quiet_since) / 60.0) if was_quiet_since else 0
                tail = f" (was quiet for {mins} min)" if mins else ""
                eventlog.event(f"{self.name}: making parts again{tail}.")

    async def _announce_state_change(self):
        """Send a stop or a start the moment it is seen, not up to 30s later.

        A dashboard that shows a machine running half a minute after it stopped
        is not reporting the floor, it is reporting the gateway's batching
        interval — and an operator who can see the spindle has stopped stops
        believing the screen. Production counts can wait for their window; a
        state change cannot.

        State ONLY. Sending the counts here as well would publish them twice:
        once now and once when the window closes.
        """
        status = payload_mod.status_of(self.state.latest)
        if status is None or status == self.published_status:
            return
        body = payload_mod.build(self.state, machine_name=self.name, include_counts=False)
        if body is None:
            return
        self.published_status = status
        self.buffer.put(body)
        log.info("%s: %s", self.name, status)
        if status == payload_mod.RUNNING:
            eventlog.event(f"{self.name}: RUNNING - making parts again.")
        elif status == payload_mod.IDLE:
            eventlog.event(f"{self.name}: IDLE - no cycle seen for a while.")
        elif status == payload_mod.DOWN:
            code = self.state.latest.get(signals_mod.FAULT_CODE)
            tail = f" (code {code})" if code not in (None, "") else ""
            eventlog.warn(f"WARNING: {self.name}: DOWN - the machine is reporting a fault{tail}.")

    async def _flush(self, force=False):
        """Close the window and queue a message, if there is one worth sending."""
        due = (time.time() - self.last_publish) >= self.publish_window
        if not (due or force):
            return
        body = payload_mod.build(
            self.state, machine_name=self.name,
            ideal_cycle_time_seconds=self.spec.get('ideal_cycle_time_seconds'))
        self.last_publish = time.time()
        self.state.reset()
        if body is None:
            return
        status = body.get("status")
        if status is not None:
            self.published_status = status
        self.buffer.put(body)
        # A forced flush that closed early (a reconnect or a shutdown, not the
        # window coming due) covered less than a full window -- say so rather
        # than imply a full five minutes.
        self._announce_window(body, partial=force and not due)

    def _announce_window(self, body, partial):
        """One human line when a window of production closes."""
        if "total_count" in body:
            total = body["total_count"]
            planned = body.get("planned_minutes", 0)
            rate = round(total / planned * 60.0) if planned else 0
            window = (f"last {planned} min (partial window)" if partial
                      else f"last {planned} min")
            line = f"{self.name}: {total} shots in the {window} (about {rate}/hour)."
            good = body.get("good_count")
            rejected = body.get("rejected_count")
            if good is not None and rejected is not None:
                line += f" ({good} good, {rejected} reject)."
            eventlog.event(line)
            # A good window clears the standing note, so a later genuine gap
            # (after real production) is announced again rather than suppressed.
            self._window_note_said = None
        elif body.get("gateway_notes"):
            note = str(body["gateway_notes"][0])
            # The "only part_count is mapped" advice is a standing config note,
            # identical every window. Say it once per occurrence, not once per
            # 5-min window -- otherwise it floods the log (~12/hour/machine) and
            # evicts the rare real warnings from the rotation. Re-stated only if
            # the note text changes.
            if note != self._window_note_said:
                self._window_note_said = note
                eventlog.warn(
                    f"WARNING: {self.name}: no production recorded for the last window - {note}")

    async def stop(self):
        self.stopping = True
        # Flush BEFORE disconnecting: the parts counted since the last publish
        # are in memory, and a gateway restarted mid-shift must not lose them.
        await self._flush(force=True)
        await self._drop()


class Gateway:
    """Everything, supervised. One per config file."""

    def __init__(self, resolved: dict):
        self.config = resolved
        self.started_at = time.time()
        buffer_settings = resolved.get("buffer") or {}
        self.buffer = buffer_mod.Buffer(
            buffer_settings.get("path") or "amp-edge-queue.db",
            max_records=buffer_settings.get("max_records") or buffer_mod.DEFAULT_MAX_RECORDS)
        # If this queue ran before and the gateway was then DOWN past the
        # threshold, arm a gap marker now -- before any reading is taken, so the
        # first production record after the restart carries it. Keep the window
        # it returns so run() can say, in plain English, that the box was off.
        self._restart_gap = self.buffer.note_restart(min_gap_s=RESTART_GAP_MIN_S)
        amp = dict(resolved.get("amp") or {})
        gateway = resolved.get("gateway") or {}
        if gateway.get("id"):
            amp["gateway_id"] = gateway["id"]
            # Read from the environment at construction, held in memory only.
            import os
            amp["gateway_key"] = os.environ.get(str(gateway.get("key_env") or ""), "")
        self.publisher = publisher_mod.Publisher(amp)
        self.workers = [MachineWorker(m, self.buffer) for m in resolved["machines"]]
        self.stopping = False
        self._tasks = []
        # -- event-log transition state for the send loop --
        # Emit the AMP outage line once on the way down and the recovery line
        # once on the way back, never one per backoff retry.
        self._amp_was_down = False
        self._amp_backlog_at_reconnect = 0
        # True once a backlog has built up, so "back in sync" fires exactly when
        # the queue returns to empty -- not on every ordinary single-record ack.
        self._amp_backlogged = False
        # Warn once when the local queue crosses the near-full line; re-arm when
        # it falls back under it.
        self._buffer_warned = False
        # Watch the cumulative dropped count so a fresh drop is announced once.
        self._dropped_seen = 0
        self._drain_error_at = 0.0

    # ── the two loops ───────────────────────────────────────────────
    async def run(self):
        self._announce_startup()
        self._tasks = [asyncio.create_task(w.run()) for w in self.workers]
        self._tasks.append(asyncio.create_task(self._drain()))
        self._tasks.append(asyncio.create_task(self._watchdog()))
        self._tasks.append(asyncio.create_task(self._idle_watch()))
        # READY=1 AFTER the loops are scheduled, not before: the unit is
        # Type=notify, so systemd treats the service as "starting" until this
        # arrives and only then arms the watchdog. Sending it earlier would tell
        # systemd the gateway is up while its poll and drain loops do not yet
        # exist. Inert off systemd (sd_notify is a no-op with no NOTIFY_SOCKET).
        sdnotify.sd_notify("READY=1")
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass

    def _announce_startup(self):
        """The 'we started' line, the restart-gap line, and the watchdog note.

        Written here rather than in __init__ so the event log (initialised by
        cmd_run just before the Gateway was built) is certainly ready, and so a
        restart gap the buffer detected at construction is reported alongside the
        start it belongs to.
        """
        n = len(self.workers)
        try:
            host = self.publisher.host
            topic = self.publisher.topic()
        except Exception:                            # noqa: BLE001 - never block start
            host, topic = "AMP", ""
        eventlog.event(
            f"AMP Edge started. Watching {n} machine(s). Saving readings to "
            f"{self.buffer.path}; sending to AMP at {host}"
            + (f", topic {topic}." if topic else "."))
        if self._restart_gap:
            gap_from, gap_to = self._restart_gap
            mins = round((gap_to - gap_from) / 60.0)
            eventlog.warn(
                f"WARNING: Gateway was offline from {_local(gap_from)} to {_local(gap_to)} "
                f"(about {mins} min). Any parts made during that time were not counted - "
                f"AMP will show a gap here, not a flat line.")
        interval = sdnotify.watchdog_interval()
        if interval is not None:
            eventlog.event(
                f"Safety watchdog on: if this gateway stops responding it will be "
                f"automatically restarted within about {int(interval * 2)}s.")

    async def _idle_watch(self):
        """A machine that WAS producing and has gone quiet for too long.

        THE FAULT THIS EXISTS FOR, and the one we could not see in production: a
        pulse counter infers "running" only from pulses, so a machine that is
        genuinely stopped and one whose sensor wire fell off look identical --
        both just stop pulsing, and an open circuit throws no error. After the
        machine has produced at least once (last_running_at set), this warns when
        the silence passes the per-machine threshold, naming BOTH causes and the
        pin to check. Gated on having produced, so a machine idle since boot (a
        shift not started) never warns.
        """
        while not self.stopping:
            await asyncio.sleep(IDLE_WATCH_INTERVAL_S)
            now = time.time()
            for w in self.workers:
                if w.last_running_at is None or w.idle_warned:
                    continue
                quiet = now - w.last_running_at
                if quiet < w.prolonged_idle_after_s:
                    continue
                w.idle_warned = True
                mins = round(quiet / 60.0)
                pin = _sensor_location(w.spec)
                eventlog.warn(
                    f"WARNING: {w.name} has made no parts for {mins} min. A pulse counter "
                    f"cannot tell a stopped machine from a disconnected sensor, so this is ONE "
                    f"of two things: (1) the machine is genuinely stopped, or (2) the cycle "
                    f"sensor on {pin} has lost its signal - a wire off, or the opto board "
                    f"unpowered. Check the machine first; if it is actually running, check the "
                    f"wiring on {pin}.")

    async def _watchdog(self):
        """Feed the systemd watchdog for as long as the event loop is turning.

        Absent off systemd: watchdog_interval() returns None when no
        WATCHDOG_USEC is set, and this returns at once, so the task costs
        nothing on a laptop or in a test.

        Under systemd the signal it sends is "this loop still runs". It is an
        ordinary coroutine sharing the one event loop with every poll and the
        drain, so a blocking call that wedges the loop stops this check-in too,
        and systemd restarts a gateway that `Restart=always` could never catch
        because a hung process does not exit. See sdnotify.py.
        """
        interval = sdnotify.watchdog_interval()
        if interval is None:
            return
        while not self.stopping:
            sdnotify.sd_notify("WATCHDOG=1")
            await asyncio.sleep(interval)

    async def _drain(self):
        attempt = 0
        while not self.stopping:
            try:
                # Heartbeat to disk: what note_restart() reads on the next start
                # to tell an ordinary bounce from a real absence. Cheap (one meta
                # UPSERT), and runs at least once per DRAIN_IDLE_S.
                self.buffer.mark_alive()
                if self.publisher.state != base.CONNECTED:
                    attempt += 1
                    try:
                        # IN A THREAD. Publisher.connect() blocks on a
                        # threading.Event for up to 15s, and publish() blocks on
                        # wait_for_publish for up to 10s. Called directly from
                        # this coroutine they block the EVENT LOOP -- which is
                        # every machine's poll loop too, because they share it.
                        #
                        # This file's own docstring says reading and sending are
                        # independent and neither may block the other. Until the
                        # assembled Gateway was finally driven end to end, that
                        # was simply untrue: a broker that stopped acknowledging
                        # stalled every PLC read on the gateway for ten seconds
                        # per message, and one that stopped answering stalled
                        # them for fifteen per reconnect. The separation that
                        # makes an AMP outage a delay rather than a hole in the
                        # plant's history did not exist.
                        await asyncio.to_thread(self.publisher.connect)
                        attempt = 0
                        log.info("connected to AMP at %s, publishing to %s",
                                 self.publisher.host, self.publisher.topic())
                        self._announce_amp_up()
                    except base.AdapterError as e:
                        delay = backoff(attempt)
                        log.warning("AMP unreachable (%s) — retrying in %.0fs; %d record(s) "
                                    "held on disk", e, delay, self.buffer.depth())
                        self._announce_amp_down(str(e))
                        await asyncio.sleep(delay)
                        continue
                self._note_buffer_pressure()
                batch = self.buffer.peek(DRAIN_BATCH)
                if not batch:
                    await asyncio.sleep(DRAIN_IDLE_S)
                    continue
                sent = []
                for row_id, record_id, queued_at, body in batch:
                    stamped = buffer_mod.stamp_for_publish(body, queued_at)
                    if not await asyncio.to_thread(self.publisher.publish, stamped):
                        # Stop at the first failure and keep the rest queued, in
                        # order. Skipping ahead would deliver a later reading
                        # before an earlier one, and AMP's machine state is
                        # last-write-wins.
                        break
                    sent.append(row_id)
                if sent:
                    self.buffer.ack(sent)
                self._note_caught_up()
                if len(sent) < len(batch):
                    await asyncio.sleep(DRAIN_IDLE_S)
            except asyncio.CancelledError:
                raise
            except Exception:                        # noqa: BLE001 - the drain must not die
                log.exception("drain loop failure — continuing")
                now = time.time()
                if now - self._drain_error_at >= DRAIN_ERROR_QUIET_S:
                    self._drain_error_at = now
                    where = eventlog.path() or "the gateway log"
                    eventlog.warn(
                        "WARNING: Hit an unexpected problem while sending to AMP; recovered and "
                        "still running. If readings stop appearing in AMP, note the time and "
                        f"check the technical log ({where}).")
                await asyncio.sleep(DRAIN_IDLE_S)

    # -- event-log transitions for the send loop -----------------------
    def _announce_amp_up(self):
        host = self.publisher.host
        if self._amp_was_down:
            self._amp_was_down = False
            depth = self.buffer.depth()
            self._amp_backlog_at_reconnect = depth
            self._amp_backlogged = depth > 0
            eventlog.event(
                f"Back online with AMP ({host}). Sending {depth} saved reading(s) now.")
        else:
            eventlog.event(f"Connected to AMP ({host}).")

    def _announce_amp_down(self, reason):
        if self._amp_was_down:
            return                                   # already said so; not once per retry
        self._amp_was_down = True
        depth = self.buffer.depth()
        eventlog.warn(
            f"WARNING: Cannot reach AMP right now ({reason}). Nothing is lost - {depth} "
            f"reading(s) are being held on disk and will go up when the connection returns.")

    def _note_caught_up(self):
        if self._amp_backlogged and self.buffer.depth() == 0:
            self._amp_backlogged = False
            eventlog.event(
                f"Back in sync with AMP - delivered the {self._amp_backlog_at_reconnect} "
                f"reading(s) that were saved during the outage.")

    def _note_buffer_pressure(self):
        """Warn once when the local queue nears its bound, and once per fresh drop.

        Read from the buffer's own stats (one cheap COUNT and a couple of meta
        reads) rather than from inside buffer.py, which stays decoupled from the
        event log. Runs on the drain loop at ~1 Hz, never on the poll path.
        """
        try:
            stats = self.buffer.stats()
        except Exception:                            # noqa: BLE001 - never block the drain
            return
        queued = stats.get("queued") or 0
        cap = stats.get("max_records") or self.buffer.max_records
        if cap:
            frac = queued / cap
            if frac >= BUFFER_WARN_FRACTION and not self._buffer_warned:
                self._buffer_warned = True
                eventlog.warn(
                    f"WARNING: local storage is {int(100 * frac)}% full ({queued} of {cap} "
                    f"saved readings). AMP has been unreachable for a while; if it stays down, "
                    f"the oldest readings will start to be dropped.")
            elif frac < BUFFER_WARN_FRACTION and self._buffer_warned:
                self._buffer_warned = False
        dropped = stats.get("dropped") or 0
        if dropped > self._dropped_seen:
            just = dropped - self._dropped_seen
            self._dropped_seen = dropped
            eventlog.warn(
                f"WARNING: local storage is full ({cap} readings). Discarded the oldest {just} "
                f"reading(s) to make room - those are lost and AMP will show a gap. AMP has "
                f"been down too long.")

    # ── health ──────────────────────────────────────────────────────
    def health(self, now=None):
        return health.report(
            started_at=self.started_at,
            adapters={w.name: w.adapter for w in self.workers},
            publisher=self.publisher,
            buffer=self.buffer,
            normalizers={w.name: w.normalizer for w in self.workers},
            now=now)

    async def stop(self):
        self.stopping = True
        # Tell systemd we are going down on purpose, so a clean stop is not read
        # as a missed watchdog check-in and does not count against the restart
        # limiter. Inert off systemd.
        sdnotify.sd_notify("STOPPING=1")
        for worker in self.workers:
            await worker.stop()
        # AFTER the workers' final flush, so the depth counts the in-flight
        # windows they just saved, and BEFORE the buffer closes, so it can still
        # be read. A clean stop and a crash look the same without this line.
        try:
            depth = self.buffer.depth()
        except Exception:                            # noqa: BLE001 - never block shutdown
            depth = 0
        eventlog.event(
            f"AMP Edge is shutting down cleanly. Saved the last parts from "
            f"{len(self.workers)} machine(s); {depth} reading(s) still waiting will be sent "
            f"after the next start.")
        for task in self._tasks:
            task.cancel()
        self.publisher.disconnect()
        self.buffer.close()
