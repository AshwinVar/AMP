"""The plain-English event log. One line a non-technical person can act on.

WHY THIS EXISTS, SEPARATE FROM THE TECHNICAL LOG. The gateway already logs to
`logging.getLogger("ampedge")`, which goes to stderr and, under systemd, to the
journal. That stream is for whoever can SSH into the Pi and read
`journalctl`: it is interleaved, technical, and full of per-poll debug. A plant
operator standing in front of the panel cannot read it and should not have to.

So this is a SECOND stream, on its own logger `ampedge.events` with
`propagate=False`, writing to a rotating FILE on the Pi. One plain-English line
per real event, timestamped in LOCAL time with a zone label, so the question
"what is the gateway doing, and if it broke, WHERE do I look" has an answer that
does not need a terminal. The technical logger is left exactly as it was; this
is additional, never a replacement.

ROBUST ABOVE ALL. This box runs unattended in a factory panel. A logging
failure -- a missing or unwritable directory, a full SD card, a rotation error
-- must NEVER crash or stall the gateway. Every write is wrapped; on any trouble
the log silently becomes a no-op and the gateway keeps reading and sending. A
plant loses visibility into the log before it ever loses a part to it.

NO SECRETS. The gateway key lives in memory and must never reach this file. The
callers pass pre-formatted plain scalars (machine names, endpoints, counts, a
broker host, a topic) -- never the amp dict, never a credential, never a raw
exception that could echo a payload.

ASCII ONLY. The Pi ships from a Windows dev host where a non-ASCII byte in a
BOM-less source file breaks CI parsing. Plain hyphens, no em-dash, no rupee, no
unicode arrows -- here and in every message string the callers build.
"""
import logging
import logging.handlers
import os
import sys

# A rotation, disk-full or permission error inside a handler must be swallowed
# by logging's own handleError path rather than raised into the gateway. The
# handler below also overrides handleError, but this is the belt to that's
# suspenders: nothing logging does here is ever allowed to surface.
logging.raiseExceptions = False

LOGGER_NAME = "ampedge.events"

# Size-based rotation with a small, deterministic hard cap. On a plant PC or an
# SD card the ceiling matters more than keeping history: maxBytes * (backups +
# 1) is the worst case on disk. ~1 MB x 6 files ~= 6 MB.
DEFAULT_MAX_BYTES = 1_000_000
DEFAULT_BACKUP_COUNT = 5

# Local time with a zone label, derived from the system clock (the Pi is set to
# Asia/Kolkata). %z gives the offset (+0530), %Z the abbreviation (IST). No
# zoneinfo or pytz needed: logging formats asctime in localtime by default.
_DATEFMT = "%Y-%m-%d %H:%M:%S %z (%Z)"
_FORMAT = "%(asctime)s  %(message)s"

# Module singleton. event()/warn() route here; init() sets it. A gateway has one
# event log, and the callers (runner, __main__) reach it by module function so
# nothing has to thread a handle through every constructor.
_logger = None
_enabled = False
_path = None

# A caller may hand us text that carries a unicode punctuation glyph -- an
# em-dash from a health verdict or a payload note, a rupee sign, a smart quote.
# The file is UTF-8 and would take it, but the operator's `log` viewer prints to
# a Windows console whose codepage (cp437/cp850) raises on those bytes, so the
# viewer would crash on the very lines it exists to show. Fold the common glyphs
# to ASCII and replace anything else, at the one sink, so the log is ASCII end to
# end -- the same ASCII-only rule this box already lives by for its source.
_UNICODE_MAP = {
    0x2013: "-", 0x2014: "-", 0x2015: "-",       # en/em/horizontal dash
    0x2018: "'", 0x2019: "'", 0x201a: "'",        # single quotes
    0x201c: '"', 0x201d: '"', 0x201e: '"',        # double quotes
    0x2026: "...", 0x00a0: " ", 0x2022: "*",       # ellipsis, nbsp, bullet
    0x2192: "->", 0x2190: "<-",                     # arrows
    0x20b9: "Rs ", 0x00b0: " deg",                  # rupee, degree
}


def _ascii(msg):
    """Fold a message to pure ASCII so the file and the console reader stay safe."""
    return str(msg).translate(_UNICODE_MAP).encode("ascii", "replace").decode("ascii")


def _safe_print(text):
    """print() that cannot die on a console codepage that rejects a byte."""
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(enc, "replace").decode(enc, "replace"))


class _SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A rotating file handler that can never raise into the caller.

    An SD card that fills AFTER startup turns an ordinary write or a rotation
    into an error. The default handler prints that to stderr via handleError;
    here it is swallowed, because a gateway that is otherwise fine must not even
    blink because its log could not be written.
    """

    def handleError(self, record):
        pass

    def emit(self, record):
        try:
            super().emit(record)
        except Exception:                            # noqa: BLE001 - never surface
            pass


def default_path(buffer_path):
    """logs/amp-edge.log next to the buffer db, so one move keeps them together."""
    base = os.path.dirname(os.path.abspath(buffer_path or "")) or "."
    return os.path.join(base, "logs", "amp-edge.log")


def init(log_cfg, buffer_path):
    """Build the rotating file log. Safe: returns False and no-ops on any trouble.

    Called once, from cmd_run, before the Gateway is constructed, so the very
    first lines (startup, a restart gap) have somewhere to go. `log_cfg` is the
    optional [log] config section; an absent section means defaults. Setting
    `enabled: false` silences ONLY this log, never the gateway.
    """
    global _logger, _enabled, _path
    _logger = None
    _enabled = False
    _path = None

    log_cfg = dict(log_cfg or {})
    if log_cfg.get("enabled", True) is False:
        return False

    path = log_cfg.get("path") or default_path(buffer_path)
    try:
        max_bytes = int(log_cfg.get("max_bytes") or DEFAULT_MAX_BYTES)
    except (TypeError, ValueError):
        max_bytes = DEFAULT_MAX_BYTES
    if max_bytes <= 0:
        max_bytes = DEFAULT_MAX_BYTES
    backup_count = log_cfg.get("backup_count")
    try:
        backup_count = int(DEFAULT_BACKUP_COUNT if backup_count is None else backup_count)
    except (TypeError, ValueError):
        backup_count = DEFAULT_BACKUP_COUNT
    if backup_count < 0:
        backup_count = DEFAULT_BACKUP_COUNT

    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        # delay=True opens the file lazily, so merely building the handler can
        # never fail the gateway on a bad path; the first real write, which is
        # itself swallowed, is where any trouble shows.
        handler = _SafeRotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backup_count,
            delay=True, encoding="utf-8")
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))

        logger = logging.getLogger(LOGGER_NAME)
        logger.setLevel(logging.INFO)
        # The whole point: these plain lines do NOT also hit the technical
        # stderr/journald stream, and that stream never leaks into this file.
        logger.propagate = False
        # Re-init (a second run in one process, or a test) must not stack a
        # second file handler on the same logger.
        for old in list(logger.handlers):
            logger.removeHandler(old)
            try:
                old.close()
            except Exception:                        # noqa: BLE001 - closing anyway
                pass

        # Writes are SYNCHRONOUS, and that is deliberate. The drain loop pushes
        # its publish into asyncio.to_thread because a broker blocks it for up to
        # 10-15 SECONDS per call; an event-log line is a local append of a few
        # dozen bytes, and -- unlike the per-poll read -- it is edge-triggered,
        # deduped and rate-limited, so steady state is at most one line every
        # five minutes per machine. A local write that infrequent, even if a
        # flash wear-levelling spike stretches it to a fraction of a second, is
        # nothing beside the 2s poll interval and loses no count (counters are
        # cumulative). A background writer thread would buy a negligible stall
        # back at the cost of a thread to supervise on a box that must never
        # surprise anyone -- not a trade worth making here.
        logger.addHandler(handler)

        _logger = logger
        _enabled = True
        _path = path
        return True
    except Exception:                                # noqa: BLE001 - degrade silently
        _logger = None
        _enabled = False
        _path = None
        return False


def event(msg):
    """Write one plain-English line. A no-op, never a raise, if logging is off."""
    if not _enabled or _logger is None:
        return
    try:
        _logger.info(_ascii(msg))
    except Exception:                                # noqa: BLE001 - never surface
        pass


def warn(msg):
    """A warning is the same stream; the message text leads with WARNING: itself.

    Kept as a named helper so a caller reads as warn(...) at a hazard and the
    intent is visible at the call site even though the sink is one file.
    """
    event(msg)


def shutdown():
    """Flush and close the log file cleanly at process exit.

    Writes are synchronous, so every line is already on disk; this just flushes
    and closes the handler so the file is released tidily. Safe to call when
    logging is off or already shut down.
    """
    global _enabled
    if _logger is not None:
        for h in list(_logger.handlers):
            try:
                h.flush()
            except Exception:                        # noqa: BLE001 - exiting anyway
                pass
            try:
                h.close()
            except Exception:                        # noqa: BLE001 - exiting anyway
                pass
            _logger.removeHandler(h)
    _enabled = False


def path():
    """Where the log is being written, or None when it is off. For the reader."""
    return _path


def is_enabled():
    return _enabled


def tail(log_path, lines=50, follow=False):
    """Print the last `lines` of the event log; with follow, keep printing new ones.

    Read-only and stdlib-only -- the plain-tone counterpart to `watch`. A reader
    on the Pi runs `python -m ampedge log gateway.yaml` and sees the same lines
    the operator would, without journalctl.
    """
    import time

    if not log_path or not os.path.exists(log_path):
        _safe_print(f"No event log yet at {log_path}. It is written once the gateway "
                    f"has run (python -m ampedge run ...).")
        return 0
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            tail_lines = fh.readlines()[-max(1, int(lines)):]
            for line in tail_lines:
                _safe_print(line.rstrip("\n"))
            if not follow:
                return 0
            fh.seek(0, os.SEEK_END)
            while True:
                chunk = fh.readline()
                if chunk:
                    _safe_print(chunk.rstrip("\n"))
                else:
                    time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    except OSError as e:
        _safe_print(f"Could not read {log_path}: {e}")
        return 1
    return 0
