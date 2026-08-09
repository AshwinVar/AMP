# ADR-0011: A machine is identified by (tenant, site, name), and telemetry is routed by topic

**Status:** Accepted
**Date:** 2026-08-09
**Supersedes nothing. Depends on:** ADR-0002 (tenant scoping)

## Context

`models.Machine.name` was the whole identity for MQTT ingest, and it is not
unique. Three customers all run a machine somebody painted "CNC-01" on. One
customer with two plants runs two.

`mqtt_service.get_or_create_machine(db, name)` resolved a machine by
`Machine.name == name` with no tenant predicate. The MQTT listener runs on its
own thread, where ADR-0002's `do_orm_execute` hook has no bound tenant and so
does not filter — the hook protects HTTP requests, and nothing was protecting
this. `.first()` therefore returned whichever row the database ordered first.

Reproduced on master, with `FACTORY_A/CNC-01` and `FACTORY_B/CNC-01` both
present and one packet published for "CNC-01":

```
which machine did it hit?
   FACTORY_A  CNC-01   status=Breakdown  util=3      <- wrong customer
   FACTORY_B  CNC-01   status=Running    util=50
child rows and their tenant:
   ProductionRecord   tenant_code=DEFAULT
   DowntimeLog        tenant_code=DEFAULT
   MachineEvent       tenant_code=DEFAULT
```

Two separate defects. Resolution picked an arbitrary customer. And the
`MachineEvent`, `ProductionRecord` and `DowntimeLog` rows were constructed
without a `tenant_code`, so each fell back to the column default `"DEFAULT"` —
a tenant that is neither of the two involved, and one that in most deployments
is a real workspace with real users who would see the data.

This is the failure mode that makes a multi-tenant platform unsellable. It is
silent (the data looks normal), it corrupts derived figures (OEE, downtime,
MTBF), and it is not recoverable after the fact, because nothing in the written
row records that it was a guess.

## Decision

### 1. Identity is a triple

A machine is `(tenant_code, site, name)`, enforced by a database UNIQUE
constraint `uq_machine_identity` (alembic `0002_machine_site_identity`).

`site` is `NOT NULL DEFAULT ''`. This is load-bearing rather than stylistic: in
PostgreSQL `NULL != NULL`, so a UNIQUE constraint over a nullable column does
not prevent duplicates. A nullable `site` would present a constraint that
enforces nothing.

`site` exists as the middle term because one customer legitimately runs the same
machine name at two plants — that is a normal manufacturing fact, not an edge
case, and a scheme without it forces customers to rename physical assets to suit
our schema.

### 2. The tenant comes from the MQTT topic, never the payload

Topics are `{prefix}/{tenant}/{site}/machines`; ingest subscribes to
`{prefix}/+/+/machines`. `-` is the wire spelling of "no site".

The topic is the only part of an MQTT message a broker can enforce. Mosquitto,
EMQX, HiveMQ and AWS IoT all authenticate a client and then restrict which topic
filters it may publish to, so the tenant segment is a claim the broker has
already checked. A `"tenant"` field inside the JSON body is a claim the gateway
makes about itself, and one compromised or misconfigured gateway could then
write into any customer's factory by editing a string.

A payload MAY restate its tenant/site — gateways commonly echo their identity
and it is useful in logs. It is checked, not trusted: a disagreement between
body and topic means one of them is wrong, nothing available can say which, and
the message is dropped.

### 3. Fail closed, loudly

A message is rejected — writing nothing, logging the reason — when it is not
addressed to exactly one provisioned tenant: a malformed or wildcard-bearing
identifier, a topic outside the prefix, a tenant with no `TenantConfig`, `User`
or `Machine` in this deployment, or a payload that contradicts its topic.

Dropping telemetry is a visible, recoverable failure: the gap appears in the
data and the log says why. Guessing an owner is invisible and permanent. The
operational consequence — a new customer must be provisioned before their
gateway is pointed at the broker — is the correct order of operations anyway.

### 4. The legacy topic requires an explicit owner

`{prefix}/machines` carries no tenant. It is subscribed **only** when
`MQTT_LEGACY_TENANT` names its owner; otherwise ingest does not subscribe to it
and any message on it is refused. This is a breaking change for existing
single-tenant deployments, and deliberately a loud one — the alternative is
defaulting it to `DEFAULT`, which is exactly the bug this ADR exists to remove.

## Consequences

**Breaking:** publishers must move to the tenant-addressed topic, or the
deployment must set `MQTT_LEGACY_TENANT`. `.env.example`, `docker-compose.yml`,
`docs/DOCKER.md` and both in-repo publishers were updated together so no
supported path is left silently deaf.

**Operational:** the broker's ACLs are now a security control, not just routing
configuration. A broker that lets any gateway publish to any topic gives back
exactly the property this removes. That is the correct place for the control —
it is the only component that authenticates the device.

**Existing data:** a database that already contains two machines with one name
in one tenant cannot take the constraint. The migration renames the later rows
to `name#id` and logs each one, rather than failing the deploy (which leaves a
half-applied schema) or deleting rows (which destroys a customer's machine and
all its production history). Verified against PostgreSQL 18 on both a fresh
database and an existing one seeded with collisions.

**API:** `POST /machines` returns 409 with the offending name where it used to
create a duplicate; an unhandled `IntegrityError` would otherwise be a 500 and a
poisoned session for the rest of the request.

**A new race, and its recovery.** The constraint creates a conflict that did not
previously exist: AMP can run more than one instance, each with its own MQTT
listener, so two processes can both miss the lookup for a newly-seen machine and
both attempt the insert. The loser catches the `IntegrityError`, rolls back and
re-selects the winner's row rather than dropping the packet — a dropped first
packet is a permanent hole in that machine's history, and it would happen
exactly once per machine, during commissioning, while someone is watching the
screen to check the integration works. If the row is still absent after the
rollback the exception propagates: inventing a machine after a constraint
violation would be guessing about identity, which is what this ADR exists to
stop.

**Not addressed here:** CSV machine import still matches on name within a tenant
and creates at the empty site. That is correct for single-plant customers and is
a known limitation for multi-plant ones; it is not made worse by this change.

## Verification

`backend/test_mqtt_tenant_identity.py` — three factories publishing different
telemetry for the same machine name; per-site machines within one customer;
eight fail-closed rejection cases with a control proving the valid form is
accepted; the constraint at the database level; the broadcast payload; and the
**OEE each factory reads**, since the derived number is what a customer
actually sees.

`backend/test_mqtt_ingest_without_orm_hooks.py` — the same ingest with
`tenancy.install_scoping()` deliberately NOT called, which is the environment
the standalone `python mqtt_listener.py` runner creates. With the hooks
installed they substitute for ingest's own work — `do_orm_execute` scopes the
machine lookup and `before_flush` stamps the child rows — so four mutations that
reintroduce the original cross-tenant bug on that path survived the whole suite
until this file existed. The explicit filters and stamps in `mqtt_service` are
the primary boundary; the ADR-0002 hooks are the backstop, not the reverse.

`backend/mutate_mqtt_identity.py` — 15 mutations across three environments,
each removing exactly one control, all caught. Several initially survived and
each produced a real improvement rather than a justification:

* the tests never exercised a NULL `site`, so loosening the column was invisible
* the fixture lacked the `DEFAULT` tenant every real deployment has, so a
  legacy-topic fail-open was absorbed by the provisioning gate instead of the
  guard under test
* the legacy-topic mutation needed two coordinated edits: one alone is caught by
  a different guard, the other alone is unreachable behind the raise above it
* the four hook-substitution survivors above, which is why the third environment
  exists at all

`backend/verify_pg_migration.py` — the migration against PostgreSQL 18.3: fresh
database, idempotent re-run, and upgrade of an existing database containing
duplicate identities.
