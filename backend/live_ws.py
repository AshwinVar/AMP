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


def broadcast_live_event(payload: dict):
    try:
        loop = asyncio.get_event_loop()

        if loop.is_running():
            asyncio.create_task(manager.broadcast(payload))
        else:
            loop.run_until_complete(manager.broadcast(payload))

    except RuntimeError:
        asyncio.run(manager.broadcast(payload))
