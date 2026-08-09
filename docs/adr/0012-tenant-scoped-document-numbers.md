# ADR-0012: Business document numbers are unique per tenant, and issued from a sequence

**Status:** Accepted
**Date:** 2026-08-09
**Depends on:** ADR-0002 (tenant scoping). Same class of defect as ADR-0011.

## Context

Nineteen business codes carried a plain `unique=True`, which in SQLAlchemy is a
UNIQUE constraint over the whole table — and therefore over every customer at
once. Measured on master, seeding two tenants and asking each to create the same
code, with every NOT NULL column filled so a rejection could only be the
constraint:

```
20 of 20 business codes cannot be reused by a second tenant
```

`WO-001`, `INV-001`, `PO-001`, `SUP-001` — the numbers every manufacturer
actually uses — were first-come-first-served across the entire platform.

Two consequences, and the second is worse:

**Onboarding.** Customer #2 cannot use their own numbering. Where the route
anticipated a clash it returns a clean 409 ("Work order number is already in
use"), which is a good error message for a situation that should not exist: the
number is not in use *by them*.

**Ordinary work.** The generated numbers are
`f"MIS-{5000 + db.query(Model).count() + 1}"`. The ADR-0002 hook scopes that
count to the caller's tenant, so two customers independently generate the same
number and the platform-wide constraint rejects the second. Reproduced:

```
Each customer issues their FIRST material issue slip.
  FACTORY_A   OK   -> MIS-5001
  FACTORY_B   IntegrityError escapes the route -> 500 to the client
              UNIQUE constraint failed: material_issue_slips.slip_no
```

That is customer #2's first material issue slip, on their first day, and unlike
the work-order path it is an unhandled 500.

Part of the schema was already right: the GMATS tables use
`nullable=False` with no global unique, and scope their counters by tenant
explicitly. This ADR makes the rest of the schema agree with that part.

## Decision

### 1. Nineteen codes become `UNIQUE (tenant_code, <code>)`

Enforced in the database, not by every future query remembering to filter.

### 2. Four codes stay platform-wide, deliberately

| Column | Why it stays global |
|---|---|
| `User.username` | The login identity. It is resolved **before** any tenant is known, so scoping it by tenant makes authentication ambiguous. |
| `TenantConfig.tenant_code` | This *is* the tenant registry — one row per tenant is the point. |
| `AgentPolicy.tenant_code` | Same. |
| `CompanyTenant.company_code` | Same. |

### 3. Per-tenant, not per-site

Considered and rejected for all nineteen. A document number is a commercial and
legal artefact of the **company**: an invoice number must be unique to the entity
that issued it, not to the plant that printed it. Machine identity is the one
thing that is genuinely per-site, and ADR-0011 already handles it. If a customer
later needs per-site sequences, the `DocumentSequence` key is the place to add
the term, and this section is where the decision should be revisited.

### 4. Numbers come from a sequence, not a row count

`count() + 1` is a population, not a sequence. Three defects, worsening:

1. It collided across tenants (fixed by the constraint above).
2. **It reuses numbers after a deletion.** Delete a slip and the count drops, so
   the next create regenerates a number that still exists. Measured as an
   `IntegrityError` escaping the route.
3. **It races.** Two requests reading the same count produce the same number.

Reuse is wrong on its own terms even when nothing crashes: a slip, GRN or
invoice number is how a piece of paper and an audit trail refer to one event.

`DocumentSequence` holds one row per `(tenant_code, doc_type)`. Allocation is
`SELECT ... FOR UPDATE` then increment, inside the caller's transaction:

* On PostgreSQL the row lock serialises concurrent allocators.
* On SQLite `FOR UPDATE` is accepted and ignored, which is harmless because
  SQLite serialises writers anyway.
* Numbers only move forward, so a deletion leaves a **gap**. A gap is auditable;
  a reuse is not.
* Nothing is committed by the allocator, so a request that fails afterwards
  rolls the reservation back rather than burning a number on a write that never
  happened.

The tenant is taken from `tenancy.current_tenant()`, **not** from the JWT claim.
The row's `tenant_code` is stamped by tenancy's `before_flush` hook from exactly
that value, and the two differ for an Admin acting on another workspace through
`X-Tenant` (the claim says `DEFAULT`, the write lands in the header tenant).
Keying the sequence on the claim would draw the number from a different tenant's
series than the document is filed under.

### 5. Deploying over live data needs no data migration

The first allocation for a `(tenant, doc_type)` seeds itself from the highest
number already present **for that tenant**. An existing customer holding
`MIS-5001..5040` continues at `MIS-5041` rather than restarting and colliding.

## Consequences

**The migration cannot fail on existing data.** Unlike ADR-0011's, this
constraint is strictly *weaker* than the one it replaces — every row that was
unique across the platform is unique within its tenant — so there is nothing to
de-duplicate and no row to rename.

**`downgrade()` can legitimately fail, and should.** Once two tenants each hold a
`WO-001`, restoring a global constraint means renaming or deleting one of them.
The migration refuses and names the collision rather than silently picking a
customer to damage.

**Numbers now have gaps.** Deleting a document no longer makes its number
available. That is the intended behaviour and is worth saying out loud, because
"why does our GRN numbering skip 3007" is a support question that deserves the
answer "because GRN-3007 was deleted, and reissuing it would make two different
receipts share a number".

## Verification

`backend/test_tenant_document_numbers.py` — all nineteen codes reusable by a
second tenant *and* still unique within one; the four deliberately-global codes
pinned with their reasons; three tenants each issuing their own first slip;
a deletion leaving a gap rather than reusing a number; seeding from existing
history without inheriting another tenant's; two allocators racing; a second
document type having its own counter; and an allocation with **no tenant bound**,
which is how a script or background job calls it and the only place the
allocator's own tenant filter is load-bearing rather than shadowed by the
ADR-0002 hook.

`backend/mutate_doc_numbers.py` — 11 mutations, all caught. Three initially
did not: one was a no-op dressed as a mutation (`... if True else None`), one
matched two columns and correctly reported SKIP rather than a false pass
(`GmatsItem` also has an `item_code`), and one was shadowed by the ADR-0002 hook
until a test existed that ran with nothing bound.

`backend/verify_pg_docnumbers.py` — against **PostgreSQL 18.3**, converting a
database built in the old shape: all nineteen constraints dropped by reflection
(PostgreSQL names them itself) and replaced, no row lost, a second tenant then
able to own `WO-001` while the same tenant still cannot duplicate it,
idempotent on re-run, and a fresh database reaching the same shape.

**A defect this verification found, which SQLite could never show.** The first
revision id was `0003_tenant_scoped_document_numbers` — 35 characters, and
`alembic_version.version_num` is `VARCHAR(32)`. The migration applied its DDL and
then failed on the stamp:

```
psycopg2.errors.StringDataRightTruncation: value too long for type character varying(32)
[SQL: UPDATE alembic_version SET version_num='0003_tenant_scoped_document_numbers']
```

Alembic's transaction rolled that back, but every subsequent deploy fails the
same way and the release command stays broken — the same dead-migrations outcome
as #499, reached by a different route. SQLite ignores VARCHAR lengths entirely,
so the whole suite passed locally. `test_migrate.py` now asserts every revision
id fits, and that guard is itself mutation-verified.
