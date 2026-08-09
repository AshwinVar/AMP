"""Per-tenant business document numbers.

WHAT WAS WRONG WITH `count() + 1`
---------------------------------
Every generated number in the enterprise inventory routes was
``f"MIS-{5000 + db.query(Model).count() + 1}"``. That has three defects, and
they get worse in the order listed:

1. **It collided across tenants.** The count is scoped to the caller's tenant by
   the ADR-0002 hook, so two customers independently produced MIS-5001 — and the
   old platform-wide UNIQUE constraint rejected the second. Alembic 0003 makes
   the constraint per-tenant, which fixes that half.

2. **It reuses numbers after a deletion.** Delete one slip and the count drops,
   so the next create regenerates a number that still exists on another row.
   Measured: an ``IntegrityError`` escaping the route as a 500. A count is a
   population, not a sequence, and the two only agree while nothing is ever
   removed.

3. **It races.** Two requests reading the same count both produce the same
   number. One wins; the other 500s. Under one process that is rare; across the
   several instances AMP runs on Railway it is not.

Reusing a document number is also wrong on its own terms, independent of any
crash: an issue slip, GRN or invoice number is how a physical piece of paper and
an audit trail refer to one event. Two events sharing a number is a
reconciliation problem long after the request has succeeded.

HOW THIS WORKS
--------------
One row per (tenant, document type) holding the next value. Allocation is
``SELECT ... FOR UPDATE`` then increment, inside the caller's transaction:

  * On PostgreSQL the row lock genuinely serialises concurrent allocators, so
    two simultaneous requests get consecutive numbers rather than the same one.
  * On SQLite ``FOR UPDATE`` is accepted and ignored, which is harmless because
    SQLite serialises writers anyway.
  * Numbers are never reused, because the counter only ever moves forward. A
    deleted slip leaves a gap, which is the correct behaviour for a document
    sequence — a gap is auditable, a reuse is not.

SEEDING AN EXISTING DATABASE
----------------------------
The first allocation for a (tenant, type) has no row yet, and starting from 1
would immediately collide with the numbers `count()+1` already issued. So the
first allocation seeds itself from the highest number already present in the
target table for that tenant. It is a single scan, once per tenant per document
type, and it means this can be deployed over live data without a data migration.
"""
import re

from sqlalchemy.exc import IntegrityError

import models


def _highest_existing(db, model, column, prefix, tenant):
    """The largest numeric suffix already used for this prefix by this tenant.

    Read explicitly filtered by tenant rather than relying on the ADR-0002 hook:
    this runs from request handlers (where the hook is bound) AND from scripts
    and tests (where it is not), and a seed that silently spanned tenants would
    start one customer's numbering above another's.
    """
    col = getattr(model, column)
    rows = db.query(col).filter(
        model.tenant_code == tenant, col.like(f"{prefix}-%")).all()
    pattern = re.compile(r"^%s-(\d+)$" % re.escape(prefix))
    highest = 0
    for (value,) in rows:
        m = pattern.match(value or "")
        if m:
            highest = max(highest, int(m.group(1)))
    return highest


def allocate(db, tenant, doc_type, model, column, prefix, start=0):
    """Reserve and return the next number for ``doc_type`` within ``tenant``.

    Does NOT commit: the number is reserved inside the caller's transaction, so
    a request that fails afterwards rolls the reservation back with everything
    else rather than burning a number on a write that never happened.
    """
    row = (db.query(models.DocumentSequence)
             .filter(models.DocumentSequence.tenant_code == tenant,
                     models.DocumentSequence.doc_type == doc_type)
             .with_for_update()
             .first())

    if row is None:
        seed = max(start, _highest_existing(db, model, column, prefix, tenant))
        row = models.DocumentSequence(tenant_code=tenant, doc_type=doc_type,
                                      next_value=seed + 1)
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            # Another allocator created the row between the SELECT and the
            # INSERT. Its value is authoritative — take it rather than
            # overwriting, which would hand out a number it has already issued.
            db.rollback()
            row = (db.query(models.DocumentSequence)
                     .filter(models.DocumentSequence.tenant_code == tenant,
                             models.DocumentSequence.doc_type == doc_type)
                     .with_for_update()
                     .one())

    value = row.next_value
    row.next_value = value + 1
    db.flush()
    return f"{prefix}-{value}"
