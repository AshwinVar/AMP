import json
import asyncio
from typing import List
from fastapi import WebSocket


class ConnectionManager:
    """Tracks live WebSocket clients with the tenant each is authenticated as, so a
    broadcast only reaches connections belonging to the payload's tenant (ADR-0002).
    A payload whose ``tenant_code`` doesn't match a connection's tenant is not sent
    to it — a tenant's telemetry never leaks to another tenant (or to anonymous
    clients)."""

    def __init__(self):
        self.active_connections = []  # list of (websocket, tenant)

    async def connect(self, websocket: WebSocket, tenant=None):
        await websocket.accept()
        self.active_connections.append((websocket, tenant))
        print(f"WebSocket connected (tenant={tenant}). Active clients: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        self.active_connections = [(ws, t) for (ws, t) in self.active_connections if ws is not websocket]
        print(f"WebSocket disconnected. Active clients: {len(self.active_connections)}")

    async def broadcast(self, payload: dict):
        target = payload.get("tenant_code")
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
