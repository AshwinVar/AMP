# ADR-0019: factory-controlled machine claim and installation assignment

**Status:** accepted · **Date:** 2026-08-11 · **Supersedes nothing. Extends [ADR-0017](0017-oem-fleet-and-cross-tenant-equipment.md).**

---

## Problem

A manufacturer registers a machine it built. Somebody has to record that the
machine now sits on a particular customer's shop floor, because until that
record exists nothing else in the OEM layer can happen: no commissioning, no
service position, no consented telemetry.

Nothing in AMP could do this. `MachineInstallation.factory_tenant_code` was
nullable and no route would set it, deliberately — [#517](https://github.com/AshwinVar/AMP/pull/517)
refused to build an OEM-side assignment and wrote a test that posts
`factory_tenant_code` in the body to prove it is ignored. That refusal was
correct and it left the last blocker to a controlled pilot.

## Threat model

The thing that must not be possible:

> **An OEM attaches one of its machines to a factory that never bought it.**

The damage is not "the OEM reads their data" — the sharing policy (ADR-0017)
still stands, and a fresh installation shares nothing. The damage is that a row
appears on that factory's **Connected Equipment** screen, presented by AMP as
equipment they own, from a supplier they have never dealt with, with sharing
controls inviting them to grant it access. AMP would be lending its own
credibility to a stranger. A plausible administrator ticks a box.

Secondary threats, each addressed below:

| Threat | Addressed by |
|---|---|
| guessing a claim code | 75 bits of entropy, hashed at rest, uniform refusal |
| replaying a used code | one-time use enforced by a conditional UPDATE |
| two factories racing the same code | the same conditional UPDATE — the database decides |
| a code outliving its purpose | mandatory expiry, checked at use |
| a code that should not have been sent | revocation, with the machine still re-issuable |
| learning whether a code exists | one refusal message for every failure mode |
| enumerating serials | serials are never accepted as a claim credential |
| a claim widening OEM access | claiming and consent are separate decisions |
| a machine moving factories silently | release-then-reclaim; no direct reassignment |

## Ownership, restated

Two ownership dimensions, unchanged from ADR-0017: `tenant_code` (which factory)
and `oem_code` (which manufacturer). This ADR adds the *transition* by which the
first one becomes set, and makes the factory the only party who can perform it.

**The OEM proposes. The factory disposes.** An OEM can create an invitation for
its own machine and hand it over. It cannot make anyone accept.

## Responsibilities

**The OEM must** register the machine against its own catalogue with a durable
serial; create an invitation; deliver the code with the machine; revoke it if
the shipment changes.

**The factory must** authenticate as an Admin; present the code; read what AMP
says the machine is; choose the sharing categories; and confirm. Nothing happens
until the confirm.

## The claim lifecycle

```
OEM registers machine          MachineInstallation, factory_tenant_code = NULL
        ↓                      status: Manufactured
OEM creates invitation         MachineClaim  status: Pending, expires_at set
        ↓                      the RAW code is returned ONCE and never stored
machine ships with the code    (QR sticker / paperwork)
        ↓
factory previews the code      GET  — no side effect, no state change
        ↓
factory confirms + consents    POST — conditional UPDATE decides
        ↓
        ├─ claim  Pending → Claimed
        ├─ installation  factory_tenant_code := the ACCEPTING factory
        ├─ installation  status Manufactured/Sold → Assigned
        └─ sharing policy written from the factory's choice
        ↓
commissioning, telemetry, service   (ADR-0017, unchanged)
```

Terminal states other than `Claimed`: **`Revoked`** (the OEM withdrew it) and
**`Expired`** (time passed). Expiry is evaluated **at use**, not by a sweeper —
a claim whose `expires_at` has passed is refused whatever its stored status says,
so an expired claim cannot be resurrected by a clock change or a missed job.

Invalid transitions are impossible by construction: every terminal state is
reached by a conditional UPDATE from `Pending`, so a second attempt matches zero
rows.

## Claim-token design

- **The code is the credential.** 15 characters from a 32-symbol alphabet with
  `I`, `L`, `O`, `U` and digits `0`/`1` removed — ~75 bits, unambiguous when read
  off a sticker or dictated over a phone. Grouped `AMP-XXXXX-XXXXX-XXXXX` for
  transcription.
- **Only a hash is stored.** `sha256(normalised code)`, unique. A database dump
  does not yield a working claim. Lookup is by hash of what was presented, so no
  scan and no timing difference across codes.
- **Normalisation before hashing**: upper-cased, dashes and whitespace stripped.
  A person typing `amp xxxxx xxxxx xxxxx` succeeds; the stored hash is unchanged.
- **The database id is never the credential.** `/oem/claims/{id}/revoke` takes an
  id because the caller is the issuing OEM; the factory-facing routes take the
  code and never an id.
- **Expiry is mandatory**, defaulting to 30 days, capped at 365. An invitation
  that never expires is a permanent bearer credential printed on a box.

### What the QR carries

The claim URL and the code, and nothing else — no tenant code, no OEM code, no
database id, no machine detail. Scanning it opens the claim screen; it does not
claim. Possession of the sticker is possession of the code, which is the intended
security model: the machine and its code travel together.

## Authorization

| Action | Who | Enforced by |
|---|---|---|
| register a machine | OEM with `manage_installations` | `require_oem` |
| create an invitation | OEM with `manage_installations` | `require_oem` |
| revoke an invitation | OEM with `manage_installations` | `require_oem`, own claim only (404 otherwise) |
| preview a claim | factory **Admin** | `require_roles(["Admin"])` |
| accept a claim | factory **Admin** | `require_roles(["Admin"])` |
| release an installation | factory **Admin** | `require_roles(["Admin"])` |

Preview is Admin-gated too, even though it only reads. An unauthenticated or
Operator-level preview would turn the endpoint into an oracle for testing codes
at leisure.

**Every failure returns the same 404 and the same sentence.** Wrong code, expired
code, revoked code, already-claimed code, machine already installed elsewhere —
all indistinguishable. Distinguishing them would tell a prober which guesses were
close.

## Consent

Claiming and sharing are **separate decisions taken at the same moment**. The
accept request carries the grant list, written through the existing
`OemDataSharingPolicy` — the same seven grants, the same storage, the same
withdrawal path in Connected Equipment. No new vocabulary, no new table.

**The default is nothing.** An accept with no grants creates the installation and
shares nothing beyond what the OEM already knows from having sold the machine.
Claiming never widens access by itself, and a claim at one site grants nothing at
another: the policy is keyed `(oem_code, tenant_code)` exactly as before.

## Machine identity

Unchanged from ADR-0017 and ADR-0011. `serial_number` is the durable identity,
unique **per OEM**, and it is what the invitation points at. Display names,
factory-local machine names, IP addresses and MQTT topics remain unusable as
identity — a claim never names any of them.

Linking the installation to a factory `Machine` row stays a separate,
already-existing step (`machine_id`), because a factory may claim a machine
before it has been physically wired up.

## Transfer and reinstallation

A machine may move. It may **not** move silently.

```
Factory A releases   → installation: factory_tenant_code := NULL,
                                     machine_id := NULL, status := Sold
                     → the sharing policy is left in place but now matches
                       nothing, because there is no installation at that site
OEM issues a new invitation
Factory B accepts    → a new claim, a new explicit consent
```

There is no route that reassigns a factory. The only path from A to B passes
through A's own decision to let go.

**History survives the move.** The installation row carries *current* state; what
happened is in `event_log` and `audit_log`, filed under whichever tenant it
happened to (ADR-0002). Factory A keeps its record of the commissioning and the
services performed there, and Factory B's log starts at its own acceptance.
Nothing is deleted to make the row consistent.

## Events

One new event, because one new fact occurs.

| Event | tenant | When |
|---|---|---|
| `MachineClaimed` | **the accepting factory** | a factory accepts an installation |

It reuses the ADR-0017 rule exactly: an OEM event is filed under the *customer's*
tenant, because it records something that happened to the customer's asset; the
OEM code travels as a field.

**Claim created / revoked / expired are NOT bus events.** There is no factory yet
— the fact belongs to nobody's shop floor — and `EventBus` would stamp them
`DEFAULT`, the founder's workspace. They are audited instead (below), which is
where they belong.

`MachineInstalled` and `MachineCommissioned` are untouched and still mean what
they meant.

## Audit

Every security-relevant step writes an `AuditLog` row: `claim_created`,
`claim_revoked`, `claim_accepted`, `claim_rejected` (a failed attempt),
`installation_released`, and the existing `oem_sharing_changed` for the consent.

Each records actor, OEM, machine serial, installation id, and the state
transition. OEM-side rows are stamped with the OEM sentinel and are invisible to
every factory; factory-side rows land in that factory's tenant.

**No row ever contains the raw code.** Where identification is needed the last
four characters are recorded — enough to match a claim to a sticker, useless as a
credential.

Failed attempts are audited *in the tenant that attempted them*, so a factory can
see somebody typing bad codes at their workspace, and are deliberately not
reported to the OEM: an OEM learning that a specific factory tried a specific
code is a leak in the other direction.

## Notifications

The accepting factory gets one notification: their connected equipment was added.

The OEM gets one too — **and this corrects a claim in `oem_subscribers.py`** that
an OEM-side notification store did not exist. It does: `Notification` is in
`SCOPED_MODELS`, so a row stamped with the OEM sentinel `OEM:<code>` is visible
to that manufacturer's sessions and to no factory. The existing table, the
existing scoping mechanism, no new concept. `GET /oem/notifications` reads them.

Two notifications per acceptance, one per party, and none for the states nobody
is waiting on.

## Alternatives rejected

**An OEM sets `factory_tenant_code` directly.** The whole reason this ADR exists.
It puts a stranger's equipment on a customer's screen with AMP's endorsement.
Rejected in #517 and rejected again here.

**The invitation names the factory, and that is enough.** An OEM would then still
be choosing the customer; the factory would merely be informed. The invitation
*may* carry an intended tenant as a hint, and it is checked when present — but it
can never substitute for acceptance, and a claim with no hint is equally valid.

**Auto-claim on opening the QR link.** A URL is not consent, and a link forwarded
to the wrong person would silently attach equipment. The GET previews; only a
POST from an authenticated Admin commits.

**Store the raw code.** Convenient for support ("read it back to me") and a
standing liability in every backup. The last four characters serve the support
case.

**A sweeper job that expires claims.** A background job that has not run yet is a
window in which an expired credential still works. Expiry is evaluated at use;
the stored status is a convenience for listing, never the authority.

**Distinguishing "wrong code" from "already claimed".** Kinder, and an oracle.
One message.

**A new `Machine` entity for manufactured stock.** `MachineInstallation` already
models exactly this — nullable factory, `Manufactured` status. A second entity
would duplicate the serial namespace and the lifecycle.
