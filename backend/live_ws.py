import json
import asyncio
from typing import List
from fastapi import WebSocket

import logging_config

log = logging_config.get_logger(__name__)


class ConnectionManager:
    """Tracks live WebSocket clients with the tenant each is authenticated as, so a
    broadcast only reaches connections belonging to the payload's tenant (ADR-0002).
    A payload whose ``tenant_code`` doesn't match a connection's tenant is not sent
    to it — a tenant's telemetry never leaks to another tenant (or to anonymous
    clients)."""

    def __init__(self):
        self.active_connections = []  # list of (websocket, tenant)

    async def connect(self, websocket: WebSocket, tenant=None):
        """Accept and register a connection. Returns False, having accepted
        nothing, if the connection has no tenant.

        A connection bound to None is one this filter cannot reason about: it
        matches every payload whose tenant_code is also absent. Refusing here as
        well as in ws_auth is deliberate -- the endpoint is not the only caller
        this class can ever have, and the ingest defect in ADR-0011 was exactly
        a guard that lived in only one of two paths.
        """
        if not tenant:
            log.info("WebSocket refused: no tenant bound")
            return False
        await websocket.accept()
        self.active_connections.append((websocket, tenant))
        log.info(f"WebSocket connected (tenant={tenant}). Active clients: {len(self.active_connections)}")
        return True

    def disconnect(self, websocket: WebSocket):
        self.active_connections = [(ws, t) for (ws, t) in self.active_connections if ws is not websocket]
        log.info(f"WebSocket disconnected. Active clients: {len(self.active_connections)}")

    async def broadcast(self, payload: dict):
        target = payload.get("tenant_code")
        if not target:
            # A payload that cannot say whose it is goes to nobody. This used to
            # be delivered to every connection bound to None, i.e. to every
            # unauthenticated client. No machine row can produce one today
            # (machines.tenant_code is NOT NULL with a default), so this is a
            # latent hazard rather than a leak that happened -- but "reaches
            # everyone" is the wrong default for the case we cannot classify.
            log.info("WebSocket broadcast dropped: payload names no tenant")
            return
        text = json.dumps(payload)
        disconnected = []
        for websocket, tenant in self.active_connections:
            if tenant != target:
                continue  # only same-tenant connections receive this payload
            try:
                await websocket.send_text(text)
            except Exception:
                disconnected.append(websocket)
        for websocket in disconnected:
            self.disconnect(websocket)


manager = ConnectionManager()


# The event loop the web server runs on, captured once at startup.
#
# WHY THIS EXISTS. Telemetry arrives on the MQTT client's WORKER THREAD, and the
# WebSocket objects belong to uvicorn's loop on the main thread. Handing work
# across that boundary has exactly one correct primitive —
# `asyncio.run_coroutine_threadsafe(coro, loop)` — and it needs a reference to
# the destination loop, which a worker thread cannot discover for itself:
# `asyncio.get_event_loop()` there raises RuntimeError, because that thread has
# no loop of its own.
#
# What the code did before was fall through that RuntimeError to
# `asyncio.run(manager.broadcast(payload))`, which spins up a BRAND-NEW loop and
# writes to sockets owned by a different one. That is undefined behaviour: it
# can appear to work, interleave frames, or fail under load, and nothing in the
# suite noticed because a delivery still happened.
_server_loop = None


def bind_loop(loop):
    """Record the loop the server runs on. Called once from startup.

    Passing None unbinds — the tests, the CLI simulator and any process with no
    running server take the direct path below.
    """
    global _server_loop
    _server_loop = loop


def broadcast_live_event(payload: dict):
    """Push one payload to every same-tenant client. SAFE TO CALL FROM ANY THREAD.

    Deliberately a plain function, not a coroutine: every caller is synchronous
    (the MQTT worker), and returning an un-awaited coroutine to a thread with no
    loop is how the original defect arose — `mqtt_service.safe_broadcast` wrapped
    the call in `asyncio.run(...)`, which was handed this function's None and
    raised `ValueError: a coroutine was expected` on every single message.

    NEVER RAISES. The contract the ingest path depends on is that a broken or
    absent WebSocket layer costs the live view and never the database write
    (test_mqtt_resilience). An exception escaping here would land inside
    `on_message`'s broad except and roll back a committed stoppage.
    """
    loop = _server_loop

    # The normal production path: a worker thread scheduling onto the server's
    # loop. `run_coroutine_threadsafe` is the only thread-safe way in.
    if loop is not None and not loop.is_closed():
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        try:
            if running is loop:
                # Already on the server's loop (an HTTP handler broadcasting).
                loop.create_task(manager.broadcast(payload))
            else:
                asyncio.run_coroutine_threadsafe(manager.broadcast(payload), loop)
            return
        except Exception as e:                       # noqa: BLE001 - see docstring
            log.info("WebSocket broadcast skipped (loop unavailable): %r", e)
            return

    # No server loop bound — tests, the CLI simulator, a worker with no server.
    # Run it to completion here; there are no foreign sockets to corrupt.
    try:
        asyncio.run(manager.broadcast(payload))
    except RuntimeError:
        # Already inside a loop on this thread: schedule instead of nesting.
        try:
            asyncio.get_running_loop().create_task(manager.broadcast(payload))
        except Exception as e:                       # noqa: BLE001
            log.info("WebSocket broadcast skipped: %r", e)
    except Exception as e:                           # noqa: BLE001
        log.info("WebSocket broadcast skipped: %r", e)
