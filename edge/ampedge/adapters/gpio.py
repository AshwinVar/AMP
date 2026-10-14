"""Count cycles on a Raspberry Pi's GPIO pins. The permanent installation.

WHY THIS IS THE ONE THAT SHIPS. The other routes into a machine with no data
port are field expedients and the module docstrings say so: a USB-serial
adapter borrowing its CTS line counts four machines and needs a PC present; a
camera watching an indicator LED needs nothing but can be nudged and stops when
a phone sleeps. Both answer "I am on a shop floor right now with what is in my
bag". Neither answers "what do we install".

This does. A Pi in the cabinet, powered from the 24 V already there, counting
twenty machines on interrupt-driven inputs, starting itself after a power cut
and needing nobody present. That is an installation an electrician recognises
and a plant manager can be shown.

    machine output (24 V) -> opto isolation board -> GPIO pin -> this adapter

USE A READY-MADE OPTO BOARD, not discrete parts. An eight-channel optocoupler
isolation module has screw terminals, mounts on DIN rail and costs about the
same as eight loose PC817s. The isolation is the point: without it a fault on
a 24 V machine circuit reaches the Pi, and a wiring slip puts 24 V somewhere it
should not be in a live control cabinet. With it there is no electrical
connection at all, only light.

INTERRUPTS, NOT POLLING, and this is the real gain over every other route. The
kernel timestamps the edge. A cycle cannot be missed because the gateway was
busy, and the count does not depend on a sampling rate anyone has to choose.
The contact adapter samples at 50 Hz and the camera at 10; this samples at
never, and is more accurate than both.

THE COUNTING RULES ARE THE SAME, deliberately. Debounce, a first reading that
is a baseline rather than an edge, a stale count reported as absence, a run
state derived from the last edge. A plant moving from the serial route to a Pi
changes `protocol` and the pin numbers, and nothing downstream notices.

PIN NUMBERS ARE BCM, not board positions. BCM 17 is physical pin 11, and the
two numbering schemes have caused more wrong wiring than any other detail of
this hardware. `gpio_aim.py --pins` prints both columns side by side.
"""
import asyncio
import time

from . import base
from .base import AdapterError, Reading, no_data

#: Debounce for a relay or opto output. Shorter than the contact adapter's
#: because an opto board's output is clean -- there are no mechanical contacts
#: on the Pi side -- but not zero: the machine's own relay still bounces.
DEFAULT_DEBOUNCE_MS = 20.0

#: Refuse a debounce long enough to swallow real cycles. Generous, because a
#: slow press (an injection moulder cycles every 15-30s) driving a chattering
#: power relay legitimately needs a multi-second lockout to collapse contact
#: bounce into one count. The limit only catches a debounce so long it would
#: start eating a second genuine cycle.
MAX_DEBOUNCE_MS = 30000.0

#: A machine that has not completed a cycle in this long has stopped. Belongs
#: to the machine; two to three times its cycle time is the rule.
DEFAULT_IDLE_AFTER_S = 60.0

#: BCM numbers that are NOT general-purpose on any Pi: 0 and 1 are the ID
#: EEPROM pins, which are read at boot by the HAT detection and must be left
#: alone. Refusing them here turns a confusing boot failure into a config error.
RESERVED_BCM = {0, 1}

#: The highest BCM number on a 40-pin header.
MAX_BCM = 27


def _setting(settings, key, default):
    """A setting's value, where an explicit 0 means 0.

    `settings.get(key) or default` is the obvious spelling and it is wrong: 0
    is falsy, so a config that explicitly disables debouncing gets the default
    instead -- silently, with both numbers looking plausible.
    """
    value = settings.get(key)
    return default if value is None else value


class _Lines:
    """The Pi's GPIO, behind an interface a test can replace.

    lgpio rather than RPi.GPIO: RPi.GPIO does not work on a Pi 5 at all, and a
    gateway that has to be rewritten when the customer buys current hardware is
    not an installation.
    """

    def __init__(self, chip=0):
        self.chip = chip
        self._h = None
        self._claimed = []

    def open(self):
        try:
            import lgpio
        except ImportError:
            raise AdapterError(
                "lgpio is not installed, so no GPIO pin can be read. On a "
                "Raspberry Pi: sudo apt install python3-lgpio, or pip install "
                "lgpio. This adapter only runs on a Pi.")
        self._lgpio = lgpio
        try:
            self._h = lgpio.gpiochip_open(self.chip)
        except Exception as exc:         # noqa: BLE001
            raise AdapterError(
                f"could not open gpiochip{self.chip}: {exc}. Is this a "
                f"Raspberry Pi, and is the user in the 'gpio' group?")

    def claim(self, bcm, pull_up):
        flags = self._pull_flag(pull_up)
        try:
            self._lgpio.gpio_claim_input(self._h, bcm, flags)
        except Exception as exc:         # noqa: BLE001
            raise AdapterError(f"could not claim BCM {bcm}: {exc}")
        self._claimed.append(bcm)

    def _pull_flag(self, pull_up):
        """The internal pull-resistor flag, whatever this lgpio build calls it.

        lgpio renamed these constants between releases: older builds (e.g. the
        one on Raspberry Pi OS Bullseye) expose SET_PULL_UP / SET_PULL_DOWN,
        newer ones SET_BIAS_PULL_UP / SET_BIAS_PULL_DOWN. The same gateway wheel
        has to run on both, so take whichever the installed lgpio actually has
        rather than hard-coding one name and crashing at claim time on the other.
        """
        lg = self._lgpio
        names = (("SET_BIAS_PULL_UP", "SET_PULL_UP") if pull_up
                 else ("SET_BIAS_PULL_DOWN", "SET_PULL_DOWN"))
        for name in names:
            flag = getattr(lg, name, None)
            if flag is not None:
                return flag
        raise AdapterError(
            "this lgpio build exposes neither SET_BIAS_PULL_* nor SET_PULL_* "
            "bias flags, so the input pull resistor cannot be set. Upgrade or "
            "reinstall lgpio.")

    def read(self, bcm):
        return bool(self._lgpio.gpio_read(self._h, bcm))

    def close(self):
        if self._h is None:
            return
        for bcm in self._claimed:
            try:
                self._lgpio.gpio_free(self._h, bcm)
            except Exception:            # noqa: BLE001
                pass
        try:
            self._lgpio.gpiochip_close(self._h)
        finally:
            self._h = None
            self._claimed = []


class GpioAdapter(base.Adapter):
    """Cycles counted on GPIO pins.

    Addresses are BCM pin numbers: `17` for the cumulative count, `17.running`
    for the run state derived from the same edges -- the same shape as the
    contact and camera adapters, so a config changes `protocol` and the
    addresses and nothing else.
    """

    protocol = "gpio"

    def __init__(self, settings, lines=None):
        super().__init__(settings)
        self.chip = int(_setting(self.settings, "chip", 0))
        self.pull_up = bool(_setting(self.settings, "pull_up", True))
        self.debounce_ms = float(
            _setting(self.settings, "debounce_ms", DEFAULT_DEBOUNCE_MS))
        if self.debounce_ms < 0:
            raise AdapterError("debounce_ms cannot be negative.")
        if self.debounce_ms > MAX_DEBOUNCE_MS:
            raise AdapterError(
                f"debounce_ms is {self.debounce_ms:g}, long enough to swallow "
                f"real cycles. The limit is {MAX_DEBOUNCE_MS:g} ms.")
        self.idle_after_s = float(
            _setting(self.settings, "idle_after_s", DEFAULT_IDLE_AFTER_S))
        #: ACTIVE LOW BY DEFAULT, because that is what an opto board does. Its
        #: output transistor pulls the pin DOWN when the machine's output is
        #: energised, so a cycle is a falling edge on a pin held up. Getting
        #: this backwards counts the gaps between cycles instead of the cycles
        #: -- the same number of edges, so the rate looks right and the timing
        #: is inverted.
        self.active_low = bool(_setting(self.settings, "active_low", True))
        self.sample_hz = float(_setting(self.settings, "sample_hz", 200.0))
        if self.sample_hz <= 0:
            raise AdapterError("sample_hz must be greater than zero.")
        self._lines = lines or _Lines(self.chip)
        self.counts = {}
        self.last_edge_at = {}
        self._state = {}
        self._changed_at = {}
        self.last_sample_at = None
        self._task = None

    def endpoint(self):
        return f"gpiochip{self.chip} BCM {sorted(self.counts) or '(none)'}"

    @staticmethod
    def parse(address):
        """(bcm, wants_run_state) or (None, _) when the address is not a pin."""
        text = str(address).strip().lower()
        running = text.endswith(".running")
        if running:
            text = text[: -len(".running")]
        if text.startswith("bcm"):
            text = text[3:].lstrip(" :")
        try:
            bcm = int(text)
        except ValueError:
            return None, running
        return bcm, running

    def _ensure(self, bcm):
        if bcm in self.counts:
            return
        self.counts[bcm] = 0
        self.last_edge_at[bcm] = None
        self._state[bcm] = None
        self._changed_at[bcm] = 0.0
        self._lines.claim(bcm, self.pull_up)

    def sample_once(self, now=None):
        """Read every claimed pin and count accepted edges. Returns ok."""
        if not self.counts:
            return True
        now = time.time() if now is None else now
        for bcm in list(self.counts):
            try:
                level = self._lines.read(bcm)
            except Exception as exc:     # noqa: BLE001
                self.last_error = f"BCM {bcm} stopped reading: {exc}"
                return False
            # An opto board pulls the pin LOW when the machine energises, so
            # "active" is low unless the config says otherwise.
            active = (not level) if self.active_low else level
            was = self._state[bcm]
            if was is None:
                # Baseline, never an edge: a pin already active when the
                # gateway starts is not a cycle that just happened.
                self._state[bcm] = active
                self._changed_at[bcm] = now
                continue
            if active == was:
                continue
            if now - self._changed_at[bcm] < self.debounce_ms / 1000.0:
                continue
            self._state[bcm] = active
            self._changed_at[bcm] = now
            if active:
                self.counts[bcm] += 1
                self.last_edge_at[bcm] = now
        self.last_sample_at = now
        self.last_error = ""
        return True

    async def _run(self):
        interval = 1.0 / self.sample_hz
        while True:
            self.sample_once()
            await asyncio.sleep(interval)

    async def connect(self):
        self.state = base.CONNECTING
        try:
            self._lines.open()
        except AdapterError as exc:
            self.state = base.ERROR
            self.last_error = str(exc)
            raise
        self._task = asyncio.ensure_future(self._run())
        self.state = base.CONNECTED
        self.connected_at = time.time()
        self.last_error = ""

    async def disconnect(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._lines.close()
        self.state = base.DISCONNECTED

    async def read(self, addresses):
        out = []
        for entry in addresses:
            address = base.address_of(entry)
            tag = str(address)
            bcm, running = self.parse(tag)
            if bcm is None:
                out.append(no_data(
                    tag, f"{tag!r} is not a GPIO pin. Use a BCM number such as "
                         f"17, or 17.running for the run state."))
                continue
            if bcm in RESERVED_BCM:
                out.append(no_data(
                    tag, f"BCM {bcm} is an ID EEPROM pin, read at boot by HAT "
                         f"detection. Use another pin."))
                continue
            if not 0 <= bcm <= MAX_BCM:
                out.append(no_data(
                    tag, f"BCM {bcm} is not on a 40-pin header (0-{MAX_BCM}). "
                         f"Note these are BCM numbers, not board positions."))
                continue
            try:
                self._ensure(bcm)
            except AdapterError as exc:
                out.append(no_data(tag, str(exc)))
                continue
            if self.last_sample_at is None:
                out.append(no_data(
                    tag, "the pins have not been sampled yet; the next read "
                         "carries the count"))
                continue
            if self.last_error:
                # A count we can no longer refresh is STALE, and a stale count
                # republished reads as a machine that stopped making parts.
                out.append(no_data(tag, self.last_error))
                continue
            if running:
                last = self.last_edge_at[bcm]
                if last is None:
                    out.append(no_data(
                        tag, "no cycle seen yet, so the run state is not known"))
                    continue
                out.append(Reading(
                    tag=tag,
                    value=bool(time.time() - last <= self.idle_after_s),
                    quality=base.GOOD, source_time=False))
                continue
            out.append(Reading(tag=tag, value=int(self.counts[bcm]),
                               quality=base.GOOD, source_time=False))
        self.last_read_at = time.time()
        return out

    def describe(self):
        d = super().describe()
        d.update({"counts": dict(self.counts), "chip": self.chip,
                  "active_low": self.active_low, "pull_up": self.pull_up,
                  "debounce_ms": self.debounce_ms,
                  "idle_after_s": self.idle_after_s,
                  "last_sample_at": self.last_sample_at})
        return d
