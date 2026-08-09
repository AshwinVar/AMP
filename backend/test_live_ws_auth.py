"""The live WebSocket authenticates before it accepts (ADR-0016).

WHAT WAS MEASURED ON MASTER
---------------------------
An adversarial probe, one real connection per row, driven at the ASGI layer:

    no token at all            ACCEPTED  bound_tenant=None
    empty token                ACCEPTED  bound_tenant=None
    garbage token              ACCEPTED  bound_tenant=None
    wrong-secret token         ACCEPTED  bound_tenant=None
    EXPIRED token              ACCEPTED  bound_tenant=None
    DELETED user's token       ACCEPTED  bound_tenant='FACTORY_A'
    no tenant claim            ACCEPTED  bound_tenant=None
    CONTROL: a valid token     ACCEPTED  bound_tenant='FACTORY_A'

Authentication failed OPEN. `tenant_from_token` returns None for anything it
cannot decode, and the handler then called `manager.connect(websocket, None)`
regardless — so the token decided which broadcasts you received, never whether
you were allowed to connect.

The sharp one is the deleted user. Their token still decodes, so the socket
bound to FACTORY_A and kept streaming that factory's machine telemetry until the
token expired on its own. That is the same defect ADR-0015 closed for approvals:
a revoked account could be refused an approval while still watching the plant.

Cross-tenant filtering itself HELD — a FACTORY_A payload reached neither the
FACTORY_B socket nor the anonymous one. What did not hold was the front door.

WHY AN ASGI DRIVER AND NOT TestClient
-------------------------------------
Starlette's TestClient needs httpx, which CI does not install and which would
have to become a RUNTIME dependency to serve a test — the repo already made the
opposite call for pytest-cov. Driving the app directly exercises the same
endpoint, the same ConnectionManager and the same accept/close; the only thing
skipped is Starlette's HTTP-upgrade plumbing, which is not our code.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_live_ws_auth.py
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

from jose import jwt as pyjwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import auth
import database
import live_ws
import main
import models
import tenancy
import ws_auth
from database import Base

# A private engine: this suite seeds and deletes users, and must not depend on
# or disturb whatever another suite left in the shared one.
_URL = os.environ.get("DATABASE_URL", "")
engine = (create_engine(_URL) if _URL.startswith("postgresql") else
          create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool))
SessionLocal = sessionmaker(bind=engine)

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def seed():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    db.add_all([
        models.User(username="a-admin", password="x", role="Admin",
                    tenant_code="FACTORY_A", is_active=True),
        models.User(username="a-operator", password="x", role="Operator",
                    tenant_code="FACTORY_A", is_active=True),
        models.User(username="b-admin", password="x", role="Admin",
                    tenant_code="FACTORY_B", is_active=True),
        models.User(username="suspended", password="x", role="Admin",
                    tenant_code="FACTORY_A", is_active=False),
        models.User(username="leaver", password="x", role="Admin",
                    tenant_code="FACTORY_A", is_active=True),
    ])
    db.commit()
    db.query(models.User).filter(models.User.username == "leaver").delete(
        synchronize_session=False)
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()
    # The endpoint opens its own session; point it at this suite's engine.
    database.SessionLocal = SessionLocal
    main.SessionLocal = SessionLocal


ALGO = getattr(auth, "ALGORITHM", "HS256")


def token(sub, tenant="FACTORY_A", role="Admin", delta=timedelta(hours=1),
          secret=None):
    claims = {"sub": sub, "role": role,
              "exp": datetime.utcnow() + delta}
    if tenant is not None:
        claims["tenant"] = tenant
    return pyjwt.encode(claims, secret or auth.SECRET_KEY, algorithm=ALGO)


class WsClient:
    """One real WebSocket connection to the app, at the ASGI layer."""

    def __init__(self, query=""):
        self.scope = {
            "type": "websocket", "asgi": {"version": "3.0"},
            "http_version": "1.1", "scheme": "ws",
            "path": "/ws/live", "raw_path": b"/ws/live",
            "query_string": query.encode(), "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
            "subprotocols": [],
        }
        self.outbound = asyncio.Queue()
        self.inbound = []
        self.accepted = False
        self.close_code = None
        self.close_reason = None
        self.task = None

    async def _receive(self):
        return await self.outbound.get()

    async def _send(self, message):
        self.inbound.append(message)
        if message["type"] == "websocket.accept":
            self.accepted = True
        elif message["type"] == "websocket.close":
            self.close_code = message.get("code", 1000)
            self.close_reason = message.get("reason", "")

    async def open(self):
        await self.outbound.put({"type": "websocket.connect"})
        self.task = asyncio.ensure_future(
            main.app(self.scope, self._receive, self._send))
        for _ in range(50):
            await asyncio.sleep(0)
            if self.accepted or self.close_code is not None or self.task.done():
                break
        await asyncio.sleep(0.05)
        return self

    def events(self):
        return [json.loads(m["text"]) for m in self.inbound
                if m["type"] == "websocket.send"]

    async def send(self, text):
        await self.outbound.put({"type": "websocket.receive", "text": text})
        await asyncio.sleep(0.05)

    async def close(self):
        """Disconnect the way a real client does.

        Cancelling the task instead would leave the handler's pending
        websocket.receive() coroutine un-awaited, and the loop prints a
        traceback for each one at teardown. A suite that prints tracebacks on a
        pass teaches you to stop reading them.
        """
        if self.task and not self.task.done():
            await self.outbound.put({"type": "websocket.disconnect",
                                     "code": 1000})
            try:
                await asyncio.wait_for(self.task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.task.cancel()
            except BaseException:
                pass


async def refused(label, query, expect_code, expect_reason=None):
    """Refused, with the RIGHT code and the RIGHT reason.

    The reason is asserted because it is what a support engineer reads, and
    because without it several distinct guards become indistinguishable: a
    token naming no user and a token naming a deleted user both end in a
    refusal, and only the reason says which. Two mutations survived until this
    was checked.
    """
    c = await WsClient(query).open()
    check(label, not c.accepted and c.close_code == expect_code,
          f"accepted={c.accepted} close={c.close_code}")
    if expect_reason is not None:
        check(f"    ...saying {expect_reason!r}", c.close_reason == expect_reason,
              repr(c.close_reason))
    check("    ...and it is not registered as a live client",
          all(w is not c for (w, t) in live_ws.manager.active_connections),
          str(len(live_ws.manager.active_connections)))
    await c.close()
    return c


# =====================================================================
async def case_the_front_door_is_shut():
    print("=" * 74)
    print("1. WHO IS LET IN")
    print("=" * 74)
    live_ws.manager.active_connections = []

    await refused("no token at all is refused", "", ws_auth.NEEDS_AUTH,
                  "A token is required")
    await refused("an empty token is refused", "token=", ws_auth.NEEDS_AUTH,
                  "A token is required")
    await refused("a garbage token is refused", "token=not-a-jwt",
                  ws_auth.NEEDS_AUTH, "Invalid token")
    await refused("a token signed with the WRONG secret is refused",
                  "token=" + token("a-admin", secret="wrong-secret-wrong-secret"),
                  ws_auth.NEEDS_AUTH, "Invalid token")
    # Expiry is its own reason, and its own code: it is the ONE refusal a
    # client can fix by itself, by fetching a new token. Reporting it as
    # REFUSED would tell the browser to stop trying forever.
    await refused("an EXPIRED token is refused",
                  "token=" + token("a-admin", delta=timedelta(hours=-1)),
                  ws_auth.NEEDS_AUTH, "Token expired")
    await refused("a token naming no user is refused",
                  "token=" + pyjwt.encode(
                      {"tenant": "FACTORY_A",
                       "exp": datetime.utcnow() + timedelta(hours=1)},
                      auth.SECRET_KEY, algorithm=ALGO),
                  ws_auth.REFUSED, "Token names no user")
    await refused("a token with no tenant claim is refused",
                  "token=" + token("a-admin", tenant=None), ws_auth.REFUSED,
                  "Token names no workspace")
    await refused("a DELETED user's token is refused",
                  "token=" + token("leaver"), ws_auth.REFUSED,
                  "This account no longer exists")
    await refused("a DISABLED user's token is refused",
                  "token=" + token("suspended"), ws_auth.REFUSED,
                  "This account has been disabled")
    await refused("a token claiming another tenant than the account is refused",
                  "token=" + token("b-admin", tenant="FACTORY_A"),
                  ws_auth.REFUSED, "Token does not match this account")


async def case_a_real_user_still_gets_a_live_feed():
    """CONTROL. Every assertion above is satisfied by a door that is nailed
    shut, so these must pass or the suite proves nothing."""
    print()
    print("=" * 74)
    print("2. CONTROL - A REAL USER STILL GETS A LIVE FEED")
    print("=" * 74)
    live_ws.manager.active_connections = []

    a = await WsClient("token=" + token("a-admin")).open()
    check("a valid token is accepted", a.accepted, f"close={a.close_code}")
    check("...and greeted", [e.get("event") for e in a.events()] == ["connected"],
          str(a.events()))
    bound = [t for (w, t) in live_ws.manager.active_connections]
    check("...and bound to its own tenant", bound == ["FACTORY_A"], str(bound))

    await live_ws.manager.broadcast({"event": "machine_update",
                                     "tenant_code": "FACTORY_A",
                                     "machine": {"name": "CNC-01"}})
    check("...and receives its own tenant's telemetry",
          [e.get("event") for e in a.events()] == ["connected", "machine_update"],
          str(a.events()))

    # An Operator is not an approver, but they run the machines — the live
    # dashboard is for everyone in the factory. A role check here would be a
    # different product, not a safer one.
    op = await WsClient("token=" + token("a-operator", role="Operator")).open()
    check("an Operator may watch the live feed too", op.accepted,
          f"close={op.close_code}")
    await a.close()
    await op.close()


async def case_one_factory_never_sees_another():
    print()
    print("=" * 74)
    print("3. ONE FACTORY NEVER SEES ANOTHER")
    print("=" * 74)
    live_ws.manager.active_connections = []

    a = await WsClient("token=" + token("a-admin")).open()
    b = await WsClient("token=" + token("b-admin", tenant="FACTORY_B")).open()
    check("both factories are connected", a.accepted and b.accepted,
          f"a={a.accepted} b={b.accepted}")
    before_b = len(b.events())

    await live_ws.manager.broadcast({"event": "machine_update",
                                     "tenant_code": "FACTORY_A",
                                     "machine": {"name": "CNC-01"}})
    check("FACTORY_A's telemetry does NOT reach FACTORY_B",
          len(b.events()) == before_b, str(b.events()))
    check("...and FACTORY_A did receive it (control for the line above)",
          any(e.get("event") == "machine_update" for e in a.events()),
          str(a.events()))

    # A payload with no tenant used to match every connection bound to None.
    # No machine row can produce one today (machines.tenant_code is NOT NULL
    # with a default), so this is a latent hazard rather than a live leak — but
    # a broadcast that cannot say who it belongs to must reach nobody.
    before_a, before_b = len(a.events()), len(b.events())
    await live_ws.manager.broadcast({"event": "machine_update",
                                     "tenant_code": None,
                                     "machine": {"name": "MYSTERY-01"}})
    check("a payload with NO tenant reaches nobody at all",
          len(a.events()) == before_a and len(b.events()) == before_b,
          f"a={a.events()[before_a:]} b={b.events()[before_b:]}")
    await a.close()
    await b.close()


async def case_the_manager_refuses_an_unbound_connection():
    """Defence in depth: even if a caller reaches ConnectionManager directly,
    a connection with no tenant is not a connection we can filter for."""
    print()
    print("=" * 74)
    print("4. THE MANAGER ITSELF REFUSES AN UNBOUND CONNECTION")
    print("=" * 74)

    class _FakeWS:
        def __init__(self):
            self.sent, self.accepted, self.closed = [], False, None

        async def accept(self):
            self.accepted = True

        async def send_text(self, text):
            self.sent.append(text)

        async def close(self, code=1000, reason=""):
            self.closed = code

    async def run():
        m = live_ws.ConnectionManager()
        anon = _FakeWS()
        bound = _FakeWS()
        ok_anon = await m.connect(anon, None)
        ok_bound = await m.connect(bound, "FACTORY_A")
        return m, anon, bound, ok_anon, ok_bound

    m, anon, bound, ok_anon, ok_bound = await run()
    check("connect(ws, None) is refused", ok_anon is False, str(ok_anon))
    check("...and the socket was never accepted", anon.accepted is False,
          str(anon.accepted))
    check("...and it is not in the connection list",
          all(w is not anon for (w, t) in m.active_connections),
          str(m.active_connections))
    check("CONTROL: connect(ws, 'FACTORY_A') still works",
          ok_bound is True and bound.accepted, f"{ok_bound} {bound.accepted}")


async def case_client_frames_are_refused():
    print()
    print("=" * 74)
    print("5. THE SERVER ACCEPTS NO CLIENT FRAMES")
    print("=" * 74)
    live_ws.manager.active_connections = []

    c = await WsClient("token=" + token("a-admin")).open()
    check("connected", c.accepted, f"close={c.close_code}")
    await c.send('{"subscribe": {"tenant": "FACTORY_B"}}')
    check("a subscription frame closes the connection",
          c.close_code == ws_auth.NO_CLIENT_FRAMES, str(c.close_code))
    check("...and the connection is dropped from the live list",
          live_ws.manager.active_connections == [],
          str(live_ws.manager.active_connections))
    check("...and it never became a FACTORY_B subscriber",
          all(t != "FACTORY_B" for (w, t) in live_ws.manager.active_connections),
          str(live_ws.manager.active_connections))
    await c.close()


async def case_reconnect_works():
    print()
    print("=" * 74)
    print("6. RECONNECT")
    print("=" * 74)
    live_ws.manager.active_connections = []

    first = await WsClient("token=" + token("a-admin")).open()
    check("first connection accepted", first.accepted)
    await first.close()
    live_ws.manager.disconnect(first)

    second = await WsClient("token=" + token("a-admin")).open()
    check("a fresh connection with the same token is accepted", second.accepted,
          f"close={second.close_code}")
    await live_ws.manager.broadcast({"event": "machine_update",
                                     "tenant_code": "FACTORY_A",
                                     "machine": {"name": "CNC-01"}})
    check("...and receives telemetry",
          any(e.get("event") == "machine_update" for e in second.events()),
          str(second.events()))
    check("...and the refused/closed sockets did not leak into the list",
          len(live_ws.manager.active_connections) == 1,
          str(len(live_ws.manager.active_connections)))
    await second.close()


async def run_all():
    seed()
    await case_the_front_door_is_shut()
    await case_a_real_user_still_gets_a_live_feed()
    await case_one_factory_never_sees_another()
    await case_the_manager_refuses_an_unbound_connection()
    await case_client_frames_are_refused()
    await case_reconnect_works()


def test_live_ws_authentication():
    asyncio.new_event_loop().run_until_complete(run_all())
    assert not failures, failures


if __name__ == "__main__":
    asyncio.new_event_loop().run_until_complete(run_all())
    print()
    print("=" * 74)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        sys.exit(1)
    print("THE LIVE FEED AUTHENTICATES BEFORE IT ACCEPTS")
