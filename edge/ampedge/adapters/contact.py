"""Count cycles from a dry contact, using the USB-serial adapter you already own.

WHY THIS EXISTS. Most machines on an SME shop floor cannot be asked anything.
Measured on a real moulding floor: thirteen presses, three controller brands,
and not one with a data port a gateway could poll -- no Ethernet, serial ports
that load firmware rather than report production, no register map because there
is no protocol.

Those machines still COUNT. Every cycle energises a solenoid, closes a relay,
drives a counter. That is a dry contact, and a dry contact needs no protocol.

THE PART THAT SURPRISES PEOPLE: no I/O module is required. An ordinary
USB-to-serial adapter has four INPUT lines that software can read directly --
CTS, DSR, CD and RI -- and nothing says they must carry handshaking. Feed one
from the machine's cycle output through an opto-isolator and the adapter is a
four-channel counter. The bill of materials per machine is an opto-isolator and
two wires; per four machines, one adapter that a commissioning engineer is
already carrying.

    machine output -> opto-isolator -> CTS / DSR / CD / RI -> this adapter

THE OPTO IS NOT OPTIONAL and this file will not pretend otherwise. Wiring a
machine's 24 V directly to a laptop's serial port puts the press and the PC on
the same electrical reference: a fault on either travels to the other, and a
wiring mistake energises something in a live control cabinet. An opto-isolator
costs about ten rupees and there is no electrical connection at all through it,
only light. Anyone reading this for the quick version: fit the opto.

SAMPLING IS NOT POLLING, and conflating them loses parts. A cycle output is
brief -- 0.40 s on the press this was written for -- while a gateway's
poll_interval is a second or more. Reading the line when the runner happens to
ask would miss most pulses and, worse, would miss them irregularly, so the
count would look plausible and be wrong. So the port is sampled continuously in
the background at `sample_hz`, edges are counted there, and `read()` returns the
running total. A slow poll then costs nothing: the total is still complete.

ONE PORT, FOUR MACHINES. A serial port can only be opened once, so the open
handle and its sampler are shared between every machine configured on the same
port (see `_PortWatcher`). Four presses on one adapter is the normal case, not
an exotic one.

WHAT A PULSE IS NOT. It is a CYCLE, not a good part. A reject, a short shot and
a part the operator throws away all produce exactly the same pulse. So this
reports `part_count` and nothing else; good/reject needs a second source, and
until there is one AMP must keep reporting them as unmeasured rather than
quietly reporting zero rejects.
"""
import asyncio
import time

from . import base
from .base import AdapterError, Reading, no_data

try:
    import serial
except ImportError:                      # pragma: no cover - import guard
    serial = None

#: The input lines a serial port exposes, and the pyserial attribute for each.
#: These are the ONLY four; there is no fifth, and a typo must be refused rather
#: than silently read as never-asserted.
LINES = {"cts": "cts", "dsr": "dsr", "cd": "cd", "dcd": "cd", "ri": "ri"}

#: Suffix that asks for the DERIVED run state instead of the count: `cts.running`.
RUNNING_SUFFIX = ".running"

#: A machine that has not completed a cycle within this many seconds is not
#: running. There is no universally right value -- it belongs to the machine --
#: so it is a setting, and this default suits an injection press whose cycle is
#: 15-20 s. Too short and a slow machine flickers to Idle between shots; too
#: long and a stopped press reads as running for a minute after it stopped.
#: Two to three times the real cycle time is the rule.
DEFAULT_IDLE_AFTER_S = 60.0

#: Below this, the derivation is noise: no press cycles faster than a few
#: seconds, so an idle window under it would report Idle mid-cycle, constantly.
MIN_IDLE_AFTER_S = 3.0

#: How often the background sampler looks at the lines. 50 Hz catches a 0.4 s
#: pulse about twenty times over, and costs four attribute reads per tick.
DEFAULT_SAMPLE_HZ = 50.0

#: A mechanical contact bounces for a few milliseconds on close, and each bounce
#: is an edge. Counting them would multiply a shift's output by a random small
#: integer -- the kind of error that looks like good news. Ignore any change
#: within this window of the last accepted one.
DEFAULT_DEBOUNCE_MS = 25.0

#: Refuse a debounce long enough to swallow real cycles. A press at 4 s/cycle is
#: fast for injection moulding; anything approaching that is a configuration
#: mistake, not a tuning choice.
MAX_DEBOUNCE_MS = 2000.0



def _setting(settings, key, default):
    """A setting's value, where an explicit 0 means 0.

    `settings.get(key) or default` is the obvious spelling and it is wrong:
    0 is falsy, so a config that explicitly disables debouncing gets the
    default instead -- silently, and with both numbers looking plausible. The
    same shape of bug as an ORM column where None takes the DEFAULT.
    """
    value = settings.get(key)
    return default if value is None else value


class _PortWatcher:
    """One open serial port, sampled continuously, shared by its machines.

    Holds the edge counts. Nothing here knows what a machine or a signal is --
    it counts transitions on four lines and says when it last managed to look.
    """

    def __init__(self, port, sample_hz, debounce_ms):
        self.port = port
        self.sample_hz = sample_hz
        self.debounce_s = debounce_ms / 1000.0
        self._con = None
        self._task = None
        self._refs = 0
        #: line -> count of accepted LOW->HIGH transitions since this opened.
        self.counts = {name: 0 for name in ("cts", "dsr", "cd", "ri")}
        self._last = {name: None for name in self.counts}
        self._changed_at = {name: 0.0 for name in self.counts}
        #: When each line last COMPLETED a cycle. The run state is derived
        #: from this and nothing else: a press that finished a shot ten
        #: seconds ago, on a fifteen-second cycle, is running.
        self.last_edge_at = {name: None for name in self.counts}
        self.last_sample_at = None
        self.error = ""

    def open(self):
        if self._con is not None:
            return
        if serial is None:
            raise AdapterError("pyserial is not installed. pip install pyserial")
        try:
            # Baud is irrelevant -- no bytes are sent or received. The port is
            # opened only so the control lines can be read.
            self._con = serial.Serial(port=self.port, baudrate=9600, timeout=0)
            # RTS HIGH, EXPLICITLY, because on some machines it is the only
            # power supply in the circuit.
            #
            # The usual wiring feeds CTS from an opto driven by the machine's
            # own 24 V. But a plant that will not allow anything wired into a
            # running control cabinet -- which is a reasonable thing for a
            # plant to say -- can instead tape an LDR over the output's
            # indicator LED and wire it RTS -> LDR -> CTS. Nothing touches the
            # machine; the serial port supplies the ~12 V itself and the LDR's
            # resistance falls when the LED lights.
            #
            # pyserial asserts RTS on open by default, but a default is not a
            # guarantee: it is a constructor argument on some versions and a
            # property on others, and a driver that opens with RTS low turns
            # this into a port that reads a running machine as permanently
            # stopped. Setting it here costs nothing and removes the question.
            try:
                self._con.rts = True
            except Exception:                # noqa: BLE001 - not every port has it
                pass
        except Exception as exc:         # noqa: BLE001 - reported, never raised blind
            self.error = f"could not open {self.port}: {exc}"
            raise AdapterError(self.error)
        self.error = ""

    def close(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._con is not None:
            try:
                self._con.close()
            finally:
                self._con = None

    def sample_once(self, now=None):
        """Read the four lines and count accepted rising edges. Returns ok."""
        if self._con is None:
            return False
        now = time.time() if now is None else now
        try:
            states = {"cts": bool(self._con.cts), "dsr": bool(self._con.dsr),
                      "cd": bool(self._con.cd), "ri": bool(self._con.ri)}
        except Exception as exc:         # noqa: BLE001
            # A yanked USB adapter. Say so; do not invent a state, because a
            # line that reads False forever is indistinguishable from a machine
            # that stopped.
            self.error = f"{self.port} stopped answering: {exc}"
            return False
        for name, now_high in states.items():
            was = self._last[name]
            if was is None:
                # FIRST SAMPLE IS A BASELINE, NEVER AN EDGE. A line already
                # high when the gateway starts is not a cycle that just
                # happened; counting it would add one phantom part per restart.
                self._last[name] = now_high
                self._changed_at[name] = now
                continue
            if now_high == was:
                continue
            if now - self._changed_at[name] < self.debounce_s:
                continue                 # contact bounce, not a second cycle
            self._last[name] = now_high
            self._changed_at[name] = now
            if now_high:
                self.counts[name] += 1
                self.last_edge_at[name] = now
        self.last_sample_at = now
        self.error = ""
        return True

    async def run(self):
        interval = 1.0 / self.sample_hz
        while True:
            self.sample_once()
            await asyncio.sleep(interval)

    def start(self):
        if self._task is None:
            self._task = asyncio.ensure_future(self.run())


#: port -> _PortWatcher. Module level because the sharing is a property of the
#: HOST, not of any one machine's configuration.
_WATCHERS = {}


def watcher_for(port, sample_hz, debounce_ms):
    """The watcher for this port, created once and shared after that."""
    w = _WATCHERS.get(port)
    if w is None:
        w = _PortWatcher(port, sample_hz, debounce_ms)
        _WATCHERS[port] = w
    return w


def reset_watchers():
    """Drop every watcher. For tests, and for a clean gateway restart."""
    for w in list(_WATCHERS.values()):
        w.close()
    _WATCHERS.clear()


class ContactAdapter(base.Adapter):
    """Cycles counted off a serial control line.

    Addresses are line names: cts, dsr, cd (or dcd), ri. One machine normally
    has exactly one, and the value returned is a cumulative count -- so the
    mapping must declare `counter_mode: cumulative`, which validate() already
    enforces for any counter signal.
    """

    protocol = "contact"

    def __init__(self, settings):
        super().__init__(settings)
        self.port = str(self.settings.get("serial_port") or "").strip()
        if not self.port:
            raise AdapterError(
                "a contact connection needs `serial_port` -- the USB-serial "
                "adapter the machine's cycle output is wired to, e.g. COM4 or "
                "/dev/ttyUSB0.")
        self.sample_hz = float(
            _setting(self.settings, "sample_hz", DEFAULT_SAMPLE_HZ))
        if self.sample_hz <= 0:
            raise AdapterError("sample_hz must be greater than zero.")
        self.debounce_ms = float(
            _setting(self.settings, "debounce_ms", DEFAULT_DEBOUNCE_MS))
        if self.debounce_ms < 0:
            raise AdapterError("debounce_ms cannot be negative.")
        if self.debounce_ms > MAX_DEBOUNCE_MS:
            raise AdapterError(
                f"debounce_ms is {self.debounce_ms:g}, which is long enough to "
                f"swallow real cycles. The limit is {MAX_DEBOUNCE_MS:g} ms.")
        self.idle_after_s = float(
            _setting(self.settings, "idle_after_s", DEFAULT_IDLE_AFTER_S))
        if self.idle_after_s < MIN_IDLE_AFTER_S:
            raise AdapterError(
                f"idle_after_s is {self.idle_after_s:g}s, shorter than any real "
                f"moulding cycle, so the machine would read Idle between every "
                f"shot. The minimum is {MIN_IDLE_AFTER_S:g}s; two to three times "
                f"the machine's cycle time is the rule.")
        self._watcher = None

    def endpoint(self):
        return f"{self.port} @ {self.sample_hz:g}Hz"

    async def connect(self):
        self.state = base.CONNECTING
        w = watcher_for(self.port, self.sample_hz, self.debounce_ms)
        try:
            w.open()
        except AdapterError as exc:
            self.state = base.ERROR
            self.last_error = str(exc)
            raise
        w.start()
        w._refs += 1
        self._watcher = w
        self.state = base.CONNECTED
        self.connected_at = time.time()
        self.last_error = ""

    async def disconnect(self):
        w, self._watcher = self._watcher, None
        if w is not None:
            w._refs -= 1
            # The LAST machine on a port closes it. Closing on the first
            # disconnect would stop counting for every other machine sharing
            # the adapter, and they would report a flat line rather than an
            # error -- which is the failure this whole package exists to avoid.
            if w._refs <= 0:
                w.close()
                _WATCHERS.pop(w.port, None)
        self.state = base.DISCONNECTED

    async def read(self, addresses):
        out = []
        w = self._watcher
        for address in addresses:
            tag = str(address)
            want = tag.strip().lower()
            # `cts.running` asks for the DERIVED run state; `cts` for the count.
            running = want.endswith(RUNNING_SUFFIX)
            if running:
                want = want[: -len(RUNNING_SUFFIX)]
            key = LINES.get(want)
            if key is None:
                out.append(no_data(
                    tag, f"{tag!r} is not a serial input line. Use one of "
                         f"cts, dsr, cd, ri -- optionally with '{RUNNING_SUFFIX}' "
                         f"for the run state derived from the same pulses."))
                continue
            if w is None or w._con is None:
                out.append(no_data(tag, w.error if w else "not connected"))
                continue
            if w.last_sample_at is None:
                out.append(no_data(
                    tag, "the port has not been sampled yet; the next read "
                         "carries the count"))
                continue
            if w.error:
                # The sampler is failing. The count we hold is STALE, not
                # current, and a stale count republished looks like a machine
                # that stopped making parts.
                out.append(no_data(tag, w.error))
                continue
            if running:
                # DERIVED, AND SAYING SO. A press that completed a cycle within
                # idle_after_s is running; one that has not, has stopped. That
                # is a measurement of the machine, not a guess about it -- the
                # pulse either arrived or it did not.
                #
                # Before the FIRST pulse there is nothing to derive from. Not
                # "False": a gateway started during a tea break has not
                # observed a stopped machine, it has observed nothing, and
                # reporting Idle would put a machine on the board as stopped on
                # the strength of never having looked.
                last = w.last_edge_at[key]
                if last is None:
                    out.append(no_data(
                        tag, "no cycle seen yet, so the run state is not known. "
                             "It becomes known at the first pulse."))
                    continue
                out.append(Reading(
                    tag=tag,
                    value=bool(time.time() - last <= self.idle_after_s),
                    quality=base.GOOD, source_time=False))
                continue
            out.append(Reading(tag=tag, value=int(w.counts[key]),
                               quality=base.GOOD, source_time=False))
        self.last_read_at = time.time()
        return out

    async def browse(self, root=None):
        """The four lines, with their live counts -- the whole address space.

        Unlike Modbus, this protocol CAN say what is readable, because there are
        exactly four things. A commissioning engineer running `preview` sees all
        four counts at once, which is how you find which terminal you have
        actually wired without a meter: the one that climbs once per cycle.
        """
        w = self._watcher
        out = []
        for name in ("cts", "dsr", "cd", "ri"):
            out.append({"address": name,
                        "count": (w.counts[name] if w else None),
                        "signal": "part_count"})
            out.append({"address": name + RUNNING_SUFFIX,
                        "count": None,
                        "signal": "running"})
        return out

    def describe(self):
        d = super().describe()
        w = self._watcher
        d["counts"] = dict(w.counts) if w else {}
        d["last_sample_at"] = w.last_sample_at if w else None
        d["sample_hz"] = self.sample_hz
        d["debounce_ms"] = self.debounce_ms
        d["idle_after_s"] = self.idle_after_s
        d["last_edge_at"] = dict(w.last_edge_at) if w else {}
        return d
