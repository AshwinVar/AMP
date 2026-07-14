"""WebSocket tenant-scoping test (ADR-0002 / PR #5).

The live feed must deliver a machine's update only to connections authenticated
as that machine's tenant. Uses a fake WebSocket that records what it is sent.

Run:  python backend/test_live_ws.py     (exit 0 = pass)
"""
import asyncio
import json

from live_ws import ConnectionManager


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def accept(self):
        pass

    async def send_text(self, text):
        self.sent.append(json.loads(text))


def test_broadcast_reaches_only_matching_tenant():
    async def run():
        m = ConnectionManager()
        default_ws, gmats_ws, anon_ws = _FakeWS(), _FakeWS(), _FakeWS()
        await m.connect(default_ws, "DEFAULT")
        await m.connect(gmats_ws, "GMATS")
        await m.connect(anon_ws, None)
        await m.broadcast({
            "event": "machine_update", "tenant_code": "DEFAULT",
            "machine": {"id": 1, "status": "Running"},
        })
        return default_ws.sent, gmats_ws.sent, anon_ws.sent

    default_sent, gmats_sent, anon_sent = asyncio.run(run())
    assert len(default_sent) == 1 and default_sent[0]["machine"]["id"] == 1  # its tenant receives it
    assert gmats_sent == []   # another tenant does NOT
    assert anon_sent == []    # anonymous does NOT


if __name__ == "__main__":
    test_broadcast_reaches_only_matching_tenant()
    print("WS OK: a machine update reaches only its own tenant's live connections")
