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
from . import health
from . import normalizer as normalizer_mod
from . import payload as payload_mod
from . import publisher as publisher_mod
from .adapters import base

log = logging.getLogger("ampedge")

RECONNECT_MIN_S = 2.0
RECONNECT_MAX_S = 60.0
DRAIN_BATCH = 50
DRAIN_IDLE_S = 1.0
PUBLISH_WINDOW_S = 30.0


def build_adapter(machine: dict):
    """The one place a protocol name becomes a class. Unknown never guesses."""
    protocol = machine["protocol"]
    if protocol == "opcua":
        from .adapters.opcua import OpcUaAdapter
        return OpcUaAdapter(machine["connection"])
    if protocol == "modbus":
        from .adapters.modbus import ModbusAdapter
        return ModbusAdapter(machine["connection"])
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
        self.attempt = 0
        self.stopping = False
        self.last_publish = time.time()

    async def run(self):
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
                await self._flush(force=True)
                await self._drop()
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            except Exception:                        # noqa: BLE001 - keep the gateway alive
                self.attempt += 1
                delay = backoff(self.attempt)
                log.exception("%s: unexpected failure — retrying in %.0fs", self.name, delay)
                await asyncio.sleep(delay)

    async def _connect(self):
        self.adapter = build_adapter(self.spec)
        await self.adapter.connect()
        log.info("%s: connected to %s (%s)", self.name, self.adapter.endpoint(),
                 self.adapter.protocol)

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
        self.state.absorb(samples)
        await self._flush()

    async def _flush(self, force=False):
        """Close the window and queue a message, if there is one worth sending."""
        due = (time.time() - self.last_publish) >= self.publish_window
        if not (due or force):
            return
        body = payload_mod.build(self.state, machine_name=self.name)
        self.last_publish = time.time()
        self.state.reset()
        if body is None:
            return
        self.buffer.put(body)

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

    # ── the two loops ───────────────────────────────────────────────
    async def run(self):
        self._tasks = [asyncio.create_task(w.run()) for w in self.workers]
        self._tasks.append(asyncio.create_task(self._drain()))
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass

    async def _drain(self):
        attempt = 0
        while not self.stopping:
            try:
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
                    except base.AdapterError as e:
                        delay = backoff(attempt)
                        log.warning("AMP unreachable (%s) — retrying in %.0fs; %d record(s) "
                                    "held on disk", e, delay, self.buffer.depth())
                        await asyncio.sleep(delay)
                        continue
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
                if len(sent) < len(batch):
                    await asyncio.sleep(DRAIN_IDLE_S)
            except asyncio.CancelledError:
                raise
            except Exception:                        # noqa: BLE001 - the drain must not die
                log.exception("drain loop failure — continuing")
                await asyncio.sleep(DRAIN_IDLE_S)

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
        for worker in self.workers:
            await worker.stop()
        for task in self._tasks:
            task.cancel()
        self.publisher.disconnect()
        self.buffer.close()
