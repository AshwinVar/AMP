"""Bill of materials — each tenant's own recipe book, read from the database.

WHAT THIS REPLACED
------------------
A module-level dict:

    PART_BOM = {
        "SHAFT-001": {"raw": "RM-STEEL-001", "consume_per_unit": 2, "fg": "FG-SHAFT-001"},
        ...
    }

Six parts, one component each, compiled into the application and shared by every
customer. Two measured consequences, and the second is the worse one:

    FACTORY_B completes a work order for 10 of THEIR OWN SHAFT-001:
       FACTORY_B  RM-STEEL-001   stock=80      (was 100)

Their stock moved at 2 per unit — GMATS's rate, a number nobody at FACTORY_B
entered and could not change. Isolation was never breached (GMATS's own stock
was untouched); the recipe was simply somebody else's.

    FACTORY_B completes 50 of VALVE-77 — a product THEY make:
       B-ALLOY-9      stock=500     (unchanged)
       inventory transactions written: 0

A product the dict had never heard of moved nothing and logged nothing. So the
inventory half of the platform only worked for the one company whose recipes had
been written into the source.

RESOLUTION RULES
----------------
`resolve(db, tenant, part_number)` returns the ONE bill of materials that
applies, or None:

  * scoped to the tenant EXPLICITLY, not via the ADR-0002 hook. This is called
    from an event subscriber inside a work-order completion, and while that does
    run in a request today, a recipe resolved through an ambient binding rather
    than a stated one is exactly the shape of the MQTT defect in ADR-0011.
  * `active` only. A superseded revision stays in the table as history and must
    never be picked up by production.
  * `effective_from` in the past or null. A revision dated next month is being
    prepared, not used.
  * of the candidates, the LATEST effective_from wins; ties break on the highest
    id, so re-issuing a revision on the same day takes effect.

A part with no active BOM returns None and the caller moves no stock — the same
outcome as the old dict's miss, but now it is a state a customer can fix by
entering their recipe rather than a fact about our source code.
"""
from datetime import datetime

import models


def resolve(db, tenant, part_number):
    """The active bill of materials for ``part_number`` in ``tenant``, or None."""
    if not tenant or not part_number:
        return None

    today = datetime.utcnow().date()
    candidates = (
        db.query(models.BillOfMaterials)
        .filter(models.BillOfMaterials.tenant_code == tenant,
                models.BillOfMaterials.part_number == part_number,
                models.BillOfMaterials.active.is_(True))
        .all()
    )
    usable = [b for b in candidates
              if b.effective_from is None or b.effective_from <= today]
    if not usable:
        return None
    # A NULL effective_from sorts oldest: it means "always applied", so a dated
    # revision supersedes it rather than the other way round.
    usable.sort(key=lambda b: (b.effective_from or _EPOCH, b.id))
    return usable[-1]


_EPOCH = datetime(1970, 1, 1).date()


def components_of(bom):
    """[(component_code, quantity_per_unit, unit)] for a resolved BOM."""
    if bom is None:
        return []
    return [(c.component_code, c.quantity_per_unit or 0.0, c.unit or "")
            for c in bom.components]
