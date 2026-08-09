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
        # An anonymous connection is now refused outright (ADR-0016), so
        # "anon_sent == []" below would hold even if the filter were removed.
        # Assert the refusal itself, or the anonymous case proves nothing.
        anon_accepted = await m.connect(anon_ws, None)
        await m.broadcast({
            "event": "machine_update", "tenant_code": "DEFAULT",
            "machine": {"id": 1, "status": "Running"},
        })
        return default_ws.sent, gmats_ws.sent, anon_ws.sent, anon_accepted

    default_sent, gmats_sent, anon_sent, anon_accepted = asyncio.run(run())
    assert len(default_sent) == 1 and default_sent[0]["machine"]["id"] == 1  # its tenant receives it
    assert gmats_sent == []   # another tenant does NOT
    assert anon_accepted is False   # an unbound connection is never accepted
    assert anon_sent == []    # anonymous does NOT


def test_a_payload_with_no_tenant_reaches_nobody():
    """It used to reach every connection bound to None -- i.e. every
    unauthenticated client. No machine row can produce such a payload today
    (machines.tenant_code is NOT NULL with a default), so this is a latent
    hazard rather than a leak that happened; a broadcast that cannot say whose
    it is should still reach nobody."""
    async def run():
        m = ConnectionManager()
        gmats_ws = _FakeWS()
        await m.connect(gmats_ws, "GMATS")
        await m.broadcast({"event": "machine_update", "tenant_code": None,
                           "machine": {"id": 9, "status": "Running"}})
        # CONTROL: the same manager DOES deliver a payload that names a tenant,
        # so the empty list above is the filter's doing and not a broken fixture.
        await m.broadcast({"event": "machine_update", "tenant_code": "GMATS",
                           "machine": {"id": 9, "status": "Running"}})
        return gmats_ws.sent

    sent = asyncio.run(run())
    assert len(sent) == 1, sent
    assert sent[0]["machine"]["id"] == 9


if __name__ == "__main__":
    test_broadcast_reaches_only_matching_tenant()
    test_a_payload_with_no_tenant_reaches_nobody()
    print("WS OK: a machine update reaches only its own tenant's live connections; "
          "an unbound connection is refused; a tenant-less payload reaches nobody")
