# ADR-0041: A gateway proves which workspace it speaks for — and a record is written once

**Status:** accepted · **Date:** 2026-09-25 · **Closes the gap named in** [ADR-0040](0040-the-edge-gateway-is-not-amp.md) (the edge gateway is not AMP) · **Extends** [ADR-0002](0002-tenant-scope-core-domain.md) (tenant scoping) · **Implements** the edge-to-cloud wire contract (`docs/integration/EDGE-CLOUD-WIRE-CONTRACT.md`) · **Schema:** migration 0013

---

## Context

`mqtt_identity` has always been right about one thing and silent about another.

Right: the topic is the only part of an MQTT message a broker can enforce.
Mosquitto, EMQX, HiveMQ and AWS IoT all authenticate the client and then
restrict which topic filters it may publish to, so
`flowmes/FACTORY_B/PLANT1/machines` is a claim the broker has *already checked*.
Reading the tenant out of the body instead would let any device write into any
customer's factory.

Silent: **that enforcement is the broker's, and it only exists if the broker's
per-gateway ACLs are configured correctly.** Every pilot gateway holds valid
broker credentials by definition — that is what makes it a gateway. So on a
broker whose ACL is wrong, absent, or simply `#`, one customer publishes into
another customer's plant by editing one string in a config file they own. No
exploit, no vulnerability, no unusual skill: the protocol working as designed,
plus one administrative mistake on a component AMP does not control.

AMP's tenancy rules (ADR-0002) stop a *user* reading across the boundary. They
had nothing to say about a *publisher*, because the publisher's identity was
never established — it was asserted, by the same message whose contents were in
question.

There is a second problem in the same place, about arithmetic rather than
access. A gateway deletes a record from its local queue only once the broker has
acknowledged it, so delivery is **at-least-once**: a publish that timed out may
or may not have arrived, and re-sending is the only safe response to not
knowing. The existing handler writes a production record for every valid
message, so a retry doubled a shift's output — silently, and in the direction
that flatters the customer, which is the worst direction for a number they will
act on. The wire contract calls this release-blocking and recommends disabling
production counts until it is fixed.

## Decision

**1. A gateway signs what it sends; AMP verifies it and then looks it up.**
`gateway_credentials` binds a `gateway_id` to exactly one `tenant_code` and one
`site`. Three things must agree before a packet is attributed:

| | |
|---|---|
| the SIGNATURE verifies under that gateway's key | it is that gateway |
| the credential's WORKSPACE matches the topic's | it is theirs |
| the credential's SITE matches the topic's | it is that plant |

Re-signing a stolen packet with your own key passes the first and fails the
second. **The signature proves identity; the lookup proves what that identity
is allowed to say.** Neither alone is sufficient, and the second is the one that
actually stops the attack.

**2. Identity is checked before authority.** A packet signed with a key that is
not this gateway's never reaches the workspace comparison, so an
unauthenticated caller cannot learn which workspace a gateway id belongs to by
reading the error.

**3. A workspace with no credential keeps the behaviour it has.** Every existing
deployment is in that state. Registering one credential closes the workspace:
from then on every packet for it must be signed by a registered, active gateway
whose workspace and site match the topic. There is no per-message opt-in,
because an attacker would not opt in.

**4. Revocation is a column, and it takes effect on the next packet.**
`is_active` is checked on every message rather than cached.

**5. A refusal is recorded where a human will see it**, deduplicated on title,
and worded for both readings: a gateway you just installed is misconfigured, or
somebody is publishing to your topic and failing to authenticate.

**6. A production record carries the gateway's own id for the message**, and
`UNIQUE(tenant_code, source_record_id)` makes a retry a no-op. The handler also
queries first, so the common case is quiet; the constraint is what holds when
two copies race, and the insert sits in a savepoint so a racing duplicate loses
only the duplicate and not the machine's status update.

## Consequences

**Positive.** A misconfigured broker ACL stops being a cross-tenant data breach
and becomes a rejected packet with a notification. A customer can be given a
gateway without being trusted not to edit its config. Production counts can be
enabled for a pilot, which the wire contract had advised against. Revoking a
gateway is one field.

**Negative — and this is the real cost.** `secret` is a shared secret **at rest
in AMP's database**. HMAC requires the same key on both sides, so a hash cannot
be stored in its place. Anyone with a database dump can publish as any gateway
in it. That is the same exposure a webhook signing secret has anywhere, and it
is accepted for a pilot, but it is not nothing and it is written down here
rather than left to be discovered.

Also negative: the algorithm exists **twice**, in `backend/gateway_auth.py` and
`edge/ampedge/signing.py`, because ADR-0040 forbids either side importing the
other. Duplicated code drifts, and the failure mode is horrible — every pilot
gateway in the field failing authentication at once, with "signature does not
match" as the only clue, which is indistinguishable from an attack.
`test_gateway_signature_parity.py` pins the two to the byte across realistic
payloads; that test is the reason the duplication is acceptable, and it must
never be deleted as redundant.

And: a gateway whose clock is more than five minutes out cannot authenticate at
all. That is deliberate — a replay window has to be short to be worth having —
but it makes NTP on the gateway host a real prerequisite rather than a nicety,
and the intake form asks for the site's time source for this reason.

## Alternatives

**Asymmetric signing (Ed25519), AMP holding only a public key.** Strictly better
on the one axis that matters most: a database dump would reveal nothing usable.
Rejected for the pilot because it puts a cryptography dependency on every plant
PC, and "pip install a package with native code" on a locked-down Windows box
behind a corporate proxy is a support ticket, not a step. Worth revisiting the
moment a customer's security review asks about secrets at rest — the wire
`scheme` field exists so a second scheme can be introduced without breaking
gateways already in the field.

**Deriving each gateway's key from one server-side master secret (HKDF).** No
per-gateway secret at rest, and rotation by a version counter. Rejected as the
same exposure wearing a hat: compromise of the master is compromise of every
gateway, and it adds a key-derivation step to debug at 3am for no change in the
threat it actually stops.

**Trusting the broker's ACLs alone.** This was the status quo. It is a
reasonable control and should still be configured; it is simply not one AMP can
verify, and the failure is silent and cross-tenant.

**Deduplicating production by (machine, window, counts) instead of a record id.**
No schema change on the gateway side. Rejected because two genuinely different
windows can carry identical counts — a machine making 40 parts an hour produces
them legitimately — and a dedup rule that discards real production is worse than
the double-count it replaces.

## Rollout

Migration 0013, additive and reversible, verified on PostgreSQL from the frozen
baseline with a populated table. No backfill: every production record written by
CSV import, the HTTP ingest or a person carries NULL, and NULL is distinct from
NULL in both engines, which is the only reason the unique constraint can be
added to a full table at all.

No tenant loses ingest on deploy. The rule tightens only when an operator
registers a credential, which is a deliberate act.

**What this does not do.** It does not encrypt the secret at rest, and it does
not stop a gateway whose own PC is compromised from publishing rubbish for the
machines it is legitimately allowed to publish for. It stops it speaking for
anybody else.
