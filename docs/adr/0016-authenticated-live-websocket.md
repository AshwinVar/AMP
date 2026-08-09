# ADR-0016: The live WebSocket authenticates before it accepts

**Status:** Accepted
**Date:** 2026-08-09
**Depends on:** ADR-0002 (tenant scoping), ADR-0015 (revocation is a database fact)

## Context

`/ws/live` streams machine telemetry — status, utilisation, downtime, counts —
to whoever is connected. ADR-0002 made the *broadcast* tenant-aware: a payload
only reaches connections bound to its `tenant_code`. Nothing made the
*connection* authenticated.

One real connection per row, driven at the ASGI layer against master:

```
no token at all            ACCEPTED  bound_tenant=None
empty token                ACCEPTED  bound_tenant=None
garbage token              ACCEPTED  bound_tenant=None
wrong-secret token         ACCEPTED  bound_tenant=None
EXPIRED token              ACCEPTED  bound_tenant=None
DELETED user's token       ACCEPTED  bound_tenant='FACTORY_A'
no tenant claim            ACCEPTED  bound_tenant=None
CONTROL: a valid token     ACCEPTED  bound_tenant='FACTORY_A'
```

Authentication failed **open**. `tenant_from_token` returns `None` for anything
it cannot decode, and the handler passed that straight into
`manager.connect(websocket, None)`. The token decided which broadcasts you
received; it never decided whether you could connect.

Three consequences, in descending order of how much they cost:

**A revoked account kept a live feed.** The deleted user's token still decoded,
so the socket bound to `FACTORY_A` and streamed that factory's telemetry until
the token expired on its own. ADR-0015 had just made that same account unable to
approve a purchase order — so the system would refuse their decisions while
still showing them the plant.

**Expired credentials were not a boundary.** A session that ended hours ago kept
its socket. Whatever the token lifetime is set to, it did not apply here.

**A tenant-less payload reached every anonymous socket.** `broadcast` matches on
equality, so `tenant_code: None` matched every connection bound to `None`. No
machine row can produce such a payload today (`machines.tenant_code` is NOT NULL
with a default `'DEFAULT'`), which makes this a latent hazard rather than a leak
that happened — recorded because the reachability, not the code, is what is
holding it shut.

Cross-tenant filtering itself **held**: a `FACTORY_A` payload reached neither
the `FACTORY_B` socket nor the anonymous one. The door was the problem, not the
filter.

## Decision

**`backend/ws_auth.py` resolves the connection before the socket is accepted.**
`resolve(db, token)` returns the tenant this connection may receive, or raises
`WsDenied`. It never returns `None`: a connection with no tenant is one the
broadcast filter cannot reason about, which is how an unauthenticated socket
came to match a tenant-less payload in the first place.

The checks: a token exists; it decodes with a valid signature; it has not
expired; it names a user; it names a workspace; that user still exists; that
user is active; and the workspace the token claims **matches the account's own**.

**The tenant comes from the database row, not the token's claim.** The claim
must still match or the connection is refused — a token asserting a workspace
its account does not belong to is not a request to serve, it is a request to
refuse.

**Refusals close before accept**, so the handshake never completes and there is
no socket to account for or leak.

**Close codes are split by what the client should do**, in the application range
(4000–4999) so a refusal cannot be mistaken for a network drop:

| Code | Meaning | Client |
|---|---|---|
| `4401` | no token, malformed, bad signature, **expired** | a *new* credential would work |
| `4403` | account gone, disabled, or workspace mismatch | retrying will never work |
| `4400` | the client sent a frame | the feed has no inbound protocol |

The split is load-bearing. The browser client retries on every close with a
schedule capped at 30 s, so without a code meaning *stop*, every revoked session
would reconnect twice a minute forever. `lib/live.ts` now stops on any of these
and reports `refused` rather than `disconnected` — a revoked session must not
look like flaky wifi.

**The server accepts no client frames.** Previously it simply never read them,
which is fail-closed by accident; an accident is not a contract. The handler now
races a receive against the heartbeat and closes on any unexpected frame.

**`ConnectionManager.connect` refuses a connection with no tenant** and returns
`False`, and `broadcast` drops a payload that names no tenant. Both are defence
in depth behind `ws_auth` — the ingest defect in ADR-0011 was precisely a guard
that lived in one of two paths.

## Consequences

**Positive.** Revocation now applies to the live feed within one reconnect
rather than one token lifetime. Verified by 51 assertions on **PostgreSQL 18.3**
and a 17-mutation matrix (16 caught, 1 documented shadow), including removing
each check in turn, taking the tenant from the token instead of the account,
reporting expiry as permanent, and accepting the socket before refusing it.

**Negative.** One `SELECT` per connect — once per session, then the socket
streams for hours, so this is the cheapest place in the product to ask the
question. A client that used to get an open socket with no credential now gets a
close; nothing in AMP did that, but an external integration might have.

**Behaviour change for the founder workspace.** A `DEFAULT` admin previewing
another tenant via `X-Tenant` still receives `DEFAULT`'s live events, because a
WebSocket carries no headers. That is unchanged — the claim was already
`DEFAULT` — but it is now explicit rather than incidental.

**Not addressed.** Connection-rate limiting. Refusing unauthenticated sockets
removes the free-resource case; a valid-token holder can still open many
connections. Worth revisiting with the load-test results.

## Alternatives considered

**Authenticate but keep accepting, and just bind no tenant.** What master did.
It leaves an unauthenticated socket alive and makes the tenant-less-payload
hazard reachable.

**Use `1008 Policy Violation` for every refusal.** Standard, and useless to the
client: it cannot tell "refresh your token" from "stop, your account is gone",
so it must either retry forever or give up on a recoverable error.

**Take the tenant from the token claim.** One fewer query, and it makes the
token the authority on something only the database knows. A stale claim would
silently bind the wrong workspace.

**Re-verify on every heartbeat.** Revocation inside 30 s instead of one
reconnect, at the cost of a query per connection per 30 s forever. The reconnect
boundary is the honest trade; note it if a customer requires faster revocation.

## Rollout

No migration. `users.is_active` already exists from ADR-0015 — this reads it.
