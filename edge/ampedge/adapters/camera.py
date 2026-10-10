"""Count cycles by WATCHING the output's indicator LED with a camera.

WHY THIS EXISTS. The dry-contact route needs an opto-isolator and two wires to
a terminal. The LED route needs an LDR taped to the panel. Both are cheap --
and both are useless to an engineer standing in a plant twenty kilometres from
the nearest shop that sells either, which is where this was written.

A camera needs nothing. Almost every laptop has one, almost every phone can be
one, and an LED that blinks once per cycle is about the easiest thing a camera
can be asked to see. There is no electrical connection to the machine at all --
not a wire, not a terminal, not a sensor stuck to the panel. Nothing to ask
permission for beyond standing a phone on a shelf.

WHAT IT IS NOT. It is not better than an opto. A camera can be nudged, the
light in a factory changes through the day, and a phone that goes to sleep
stops counting. This is the route that works when the others cannot be bought,
and the honest ranking is opto > LDR > camera. It earns its place by being the
one that is always available.

HOW IT WORKS. ffmpeg reads the camera, this samples the mean brightness of ONE
small region -- the LED -- and counts dark->bright transitions, with the same
debounce, baseline and rollover rules as the dry-contact adapter. Downstream,
nothing knows the difference: it is a cumulative part_count either way.

AIMING IS THE WHOLE JOB, so `camera_aim.py` exists to do it: it prints the
region's brightness live, so the camera can be positioned and the threshold set
by watching numbers move rather than by guessing.

THE REGION MUST BE SMALL AND ON ONE LED. A region covering two LEDs counts
both, and a region covering half the panel counts the factory lights going off
at lunch. The aim tool reports how much of the region actually changes, which
is the check that catches both.
"""
import asyncio
import subprocess
import time

from . import base
from .base import AdapterError, Reading, no_data

try:
    import imageio_ffmpeg
except ImportError:                      # pragma: no cover - import guard
    imageio_ffmpeg = None

#: Frames per second to pull. An LED lit for a good fraction of a second needs
#: nothing like video rate, and a lower rate costs less CPU on a plant PC that
#: is also running the gateway.
DEFAULT_FPS = 10.0

#: Mean brightness (0-255) above which the region counts as LIT. Deliberately
#: no clever auto-threshold: a factory's light changes through the day and an
#: adaptive threshold would quietly re-learn a stuck-on LED as the new dark.
#: The aim tool shows the real numbers and a human picks one between them.
DEFAULT_THRESHOLD = 110.0

#: Same reason as the contact adapter: a contact bounces, and so does a
#: brightness sitting exactly on the threshold.
DEFAULT_DEBOUNCE_MS = 150.0

#: Frame size requested from the camera. Small on purpose -- the region is a
#: few pixels of LED and a 1080p frame is 6x the pixels for no more signal.
WIDTH, HEIGHT = 640, 480



def _setting(settings, key, default):
    """A setting's value, where an explicit 0 means 0.

    `settings.get(key) or default` is the obvious spelling and it is wrong:
    0 is falsy, so a config that explicitly disables debouncing gets the
    default instead -- silently, and with both numbers looking plausible. The
    same shape of bug as an ORM column where None takes the DEFAULT.
    """
    value = settings.get(key)
    return default if value is None else value


class _Frames:
    """ffmpeg reading one camera, as raw grayscale frames.

    Separate from the adapter so the counting can be tested with synthetic
    frames and no camera: `CameraAdapter(settings, frames=FakeFrames())`.
    """

    def __init__(self, device, fps, width=WIDTH, height=HEIGHT):
        self.device = device
        self.fps = fps
        self.width = width
        self.height = height
        self._proc = None

    def open(self):
        if imageio_ffmpeg is None:
            raise AdapterError(
                "imageio-ffmpeg is not installed, so no camera can be read. "
                "pip install imageio-ffmpeg")
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        # dshow on Windows; the adapter documents the Linux/macOS forms.
        args = [exe, "-hide_banner", "-loglevel", "error",
                "-f", "dshow", "-framerate", str(int(self.fps)),
                "-video_size", f"{self.width}x{self.height}",
                "-i", f"video={self.device}",
                "-pix_fmt", "gray", "-f", "rawvideo", "-"]
        try:
            self._proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=self.width * self.height * 4)
        except Exception as exc:         # noqa: BLE001
            raise AdapterError(f"could not start ffmpeg: {exc}")

    def read(self):
        """One frame as bytes, or None. Never blocks forever."""
        if self._proc is None or self._proc.poll() is not None:
            return None
        need = self.width * self.height
        buf = self._proc.stdout.read(need)
        if not buf or len(buf) < need:
            return None
        return buf

    def close(self):
        if self._proc is not None:
            try:
                self._proc.kill()
            finally:
                self._proc = None


def region_mean(frame, width, height, box):
    """Mean brightness of `box` = (x, y, w, h) in pixels. Pure.

    Returns None when the box falls outside the frame, which is a
    configuration error and must not be averaged into a plausible number.
    """
    x, y, w, h = box
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > width or y + h > height:
        return None
    total = 0
    for row in range(y, y + h):
        start = row * width + x
        total += sum(frame[start:start + w])
    return total / float(w * h)


class CameraAdapter(base.Adapter):
    """Cycles counted by watching one LED.

    Addresses are the same shape as the contact adapter's: `led` for the
    cumulative count and `led.running` for the derived run state, so a config
    moving from one to the other changes the protocol and nothing else.
    """

    protocol = "camera"

    def __init__(self, settings, frames=None):
        super().__init__(settings)
        self.device = str(self.settings.get("device") or "").strip()
        if not self.device and frames is None:
            raise AdapterError(
                "a camera connection needs `device` -- the camera's name as "
                "the operating system reports it, e.g. 'HP True Vision FHD "
                "Camera'. `camera_aim.py --list` prints them.")
        self.fps = float(_setting(self.settings, "fps", DEFAULT_FPS))
        if self.fps <= 0:
            raise AdapterError("fps must be greater than zero.")
        self.threshold = float(
            _setting(self.settings, "threshold", DEFAULT_THRESHOLD))
        self.debounce_s = float(
            _setting(self.settings, "debounce_ms", DEFAULT_DEBOUNCE_MS)) / 1000.0
        self.idle_after_s = float(_setting(self.settings, "idle_after_s", 60.0))
        box = self.settings.get("region")
        if not (isinstance(box, (list, tuple)) and len(box) == 4):
            raise AdapterError(
                "a camera connection needs `region: [x, y, w, h]` in pixels -- "
                "the patch of frame the LED occupies. `camera_aim.py` finds it.")
        self.box = tuple(int(v) for v in box)
        if self.box[2] <= 0 or self.box[3] <= 0:
            raise AdapterError("region width and height must be positive.")
        self.width = int(_setting(self.settings, "width", WIDTH))
        self.height = int(_setting(self.settings, "height", HEIGHT))
        self._frames = frames or _Frames(self.device, self.fps,
                                         self.width, self.height)
        self.count = 0
        self.last_edge_at = None
        self._lit = None
        self._changed_at = 0.0
        self.last_sample_at = None
        self.last_mean = None
        self._task = None

    def endpoint(self):
        return f"{self.device or 'injected'} {self.box}"

    def sample_once(self, now=None):
        """Read one frame and count an edge if there is one. Returns ok."""
        frame = self._frames.read()
        if frame is None:
            self.last_error = f"no frame from {self.device or 'the camera'}"
            return False
        mean = region_mean(frame, self.width, self.height, self.box)
        if mean is None:
            self.last_error = (
                f"region {self.box} falls outside a {self.width}x{self.height} "
                f"frame")
            return False
        now = time.time() if now is None else now
        self.last_mean = mean
        self.last_sample_at = now
        self.last_error = ""
        lit = mean >= self.threshold
        if self._lit is None:
            # Baseline, never an edge -- an LED already lit when the gateway
            # starts is not a cycle that just happened.
            self._lit = lit
            self._changed_at = now
            return True
        if lit == self._lit:
            return True
        if now - self._changed_at < self.debounce_s:
            return True
        self._lit = lit
        self._changed_at = now
        if lit:
            self.count += 1
            self.last_edge_at = now
        return True

    async def _run(self):
        interval = 1.0 / self.fps
        while True:
            self.sample_once()
            await asyncio.sleep(interval)

    async def connect(self):
        self.state = base.CONNECTING
        try:
            self._frames.open()
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
        self._frames.close()
        self.state = base.DISCONNECTED

    async def read(self, addresses):
        out = []
        for address in addresses:
            tag = str(address)
            want = tag.strip().lower()
            running = want.endswith(".running")
            name = want[:-len(".running")] if running else want
            if name not in ("led", "lamp"):
                out.append(no_data(
                    tag, f"{tag!r} is not a camera address. Use 'led' for the "
                         f"count or 'led.running' for the run state."))
                continue
            if self.last_sample_at is None:
                out.append(no_data(
                    tag, self.last_error or "no frame read yet; the next poll "
                                            "carries the count"))
                continue
            if self.last_error:
                # A camera that stopped answering holds a count that is STALE.
                # Republished it would read as a machine that stopped working.
                out.append(no_data(tag, self.last_error))
                continue
            if running:
                if self.last_edge_at is None:
                    out.append(no_data(
                        tag, "no cycle seen yet, so the run state is not known"))
                    continue
                out.append(Reading(
                    tag=tag,
                    value=bool(time.time() - self.last_edge_at <= self.idle_after_s),
                    quality=base.GOOD, source_time=False))
                continue
            out.append(Reading(tag=tag, value=int(self.count),
                               quality=base.GOOD, source_time=False))
        self.last_read_at = time.time()
        return out

    def describe(self):
        d = super().describe()
        d.update({"count": self.count, "region": list(self.box),
                  "threshold": self.threshold, "fps": self.fps,
                  "last_mean": self.last_mean,
                  "last_sample_at": self.last_sample_at})
        return d
