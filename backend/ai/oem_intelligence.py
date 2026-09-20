"""What a manufacturer may learn from its installed base — and only that (ADR-0033).

An OEM can already see its fleet machine by machine, each field gated on what
that customer granted (ADR-0017). What it has never had is the view a
manufacturer actually wants: *across my installed base, which model is running
hardest, which is coming up for service, where is my exposure?*

Building that is easy. Building it without quietly undoing the consent is the
work, because **an aggregate can disclose what a field could not**:

    "average operating hours across the ACX-75 fleet: 4,120 h"

If exactly one customer shares operating hours, that average IS that customer's
reading, relabelled. The consent said no and the arithmetic said yes.

SO EVERY CROSS-CUSTOMER FIGURE HAS A FLOOR. A number is published only when at
least MIN_CUSTOMERS *distinct customers* contributed to it. Below the floor the
figure is withheld with the reason — never rounded, never noised, never silently
dropped, because a missing row reads as a fleet with nothing in it.

AND A FLOOR ON EACH CELL IS NOT A FLOOR ON THE TABLE. With the fleet figure
published beside the slices, one withheld slice is one equation with one
unknown, and it solves: a withheld 900.0 came back as 900.1 the first time this
was tried. `complementary_suppression` leaves either no slice withheld or at
least two, so the arithmetic never closes.

WHAT IS ALWAYS VISIBLE. Counts of the OEM's OWN records — how many machines it
shipped, of which model, to how many customers — come from the manufacturer's
own paperwork, not from any factory's operations. Those need no grant, exactly
as ADR-0017 already says for the per-machine view.

WHAT IS NEVER VISIBLE. A customer's identity attached to a shared figure in an
aggregate. The per-customer breakdown names only counts the OEM already knows;
operational figures are pooled across customers or not shown at all.

COVERAGE IS STATED, NOT IMPLIED. Every section says how many machines and how
many customers it was computed from, against how many exist. "62% utilisation
from 3 of 40 machines" is a different claim from "62% utilisation", and only one
of them is true.
"""
from collections import defaultdict
from datetime import datetime

import models
import oem_sharing
from ai import evidence as ev

name = "oem_intelligence"

# A cross-customer figure needs this many distinct contributing customers.
# Two is the minimum that is not a single customer's reading with a new label;
# it is a floor, not a privacy guarantee, and ADR-0033 says so.
MIN_CUSTOMERS = 2
MAX_MODELS = 12

WITHHELD = ("Fewer than {n} customers share this, so a figure here would be one customer's reading "
            "with a new label. AMP does not publish it.")
OWN_RECORDS = "from the manufacturer's own records, not from any customer's operations"
COMPLEMENT = ("Withheld so that another model's withheld figure cannot be recovered by "
              "subtracting the published rows from the fleet figure. This row could have been "
              "published on its own; the arithmetic around it is what AMP is withholding.")
MARGIN_IS_THE_CELL = ("Every model below the floor and none above it, so a fleet figure here "
                      "would be those same customers' reading with a wider label.")


def _fact(key, label, value, prov, unit="", source="", window="now", detail=""):
    return ev.Fact(key=key, label=label, value=value, provenance=prov, unit=unit,
                   source=source, window=window, detail=detail).to_dict(key)


def _pooled(values_by_customer, label):
    """A cross-customer figure, or the reason it is withheld.

    `values_by_customer` is {customer: [values]}. The floor counts CUSTOMERS,
    not machines: ten machines at one site are still one customer, and pooling
    them would still publish that customer's operation.
    """
    contributing = {c: v for c, v in values_by_customer.items() if v}
    customers = len(contributing)
    machines = sum(len(v) for v in contributing.values())
    if customers < MIN_CUSTOMERS:
        return {"label": label, "value": None, "customers": customers, "machines": machines,
                "state": ev.NOT_CONFIGURED if customers == 0 else ev.PARTIAL_DATA,
                "withheld": True,
                "reason": ("no customer shares this" if customers == 0
                           else WITHHELD.format(n=MIN_CUSTOMERS))}
    flat = [x for v in contributing.values() for x in v]
    return {"label": label, "value": round(sum(flat) / len(flat), 1), "customers": customers,
            "machines": machines, "state": ev.OK, "withheld": False, "reason": None}


def complementary_suppression(model_rows, fleet):
    """A floor on each cell is not a floor on the table (ADR-0033 §7).

    The per-model slice obeying the floor is not enough, because the MARGIN is
    published beside it. With the fleet figure shown and every slice but one:

        withheld_total = fleet.value x fleet.machines - sum(published totals)
        withheld_mean  = withheld_total / that slice's own machine count

    and for a slice with a single customer, that IS that customer's reading.
    Suppressing the cell while publishing the margin withholds nothing; the
    arithmetic is the disclosure, not the row. Measured against the fixture in
    test_oem_intelligence: a withheld 900.0 came back as 900.1.

    So the table must leave either NO slice withheld or at least TWO: one
    withheld slice is the only case that solves. When exactly one is withheld,
    the smallest publishable slice is suppressed with it -- smallest because it
    is the least the manufacturer loses. When there is nothing to suppress
    alongside it, the FLEET figure goes instead, because a margin over one
    unknown is that unknown.

    This is ordinary complementary cell suppression, and like the floor itself
    it is a bound, not a guarantee: two withheld cells still publish their
    combined total, and an OEM that knows one of them by other means recovers
    the other. ADR-0033 says so rather than implying the table is safe.
    """
    if fleet.get("withheld"):
        return                                  # no margin to subtract from
    # Only a slice that CONTRIBUTES to the margin can be subtracted out of it.
    # A model whose customers share nothing is not in the fleet total, so its
    # withheld row is protected by the consent, not by this arithmetic -- and
    # suppressing a good row to shield it would cost the manufacturer a figure
    # for no gain.
    withheld = [r for r in model_rows if r["average_operating_hours"]["withheld"]
                and r["average_operating_hours"]["machines"] > 0]
    if len(withheld) != 1:
        return                                  # none to protect, or already unsolvable
    publishable = [r for r in model_rows if not r["average_operating_hours"]["withheld"]]
    if not publishable:
        # The one withheld slice IS the whole margin. Withhold the margin.
        fleet.update({"value": None, "withheld": True, "state": ev.PARTIAL_DATA,
                      "reason": MARGIN_IS_THE_CELL})
        return
    victim = min(publishable, key=lambda r: (r["average_operating_hours"]["machines"],
                                             str(r["model_code"])))
    victim["average_operating_hours"].update({
        "value": None, "withheld": True, "state": ev.PARTIAL_DATA, "reason": COMPLEMENT})


def build_oem_intelligence(db, oem_code: str, now=None) -> dict:
    """The installed base as its manufacturer may see it, in aggregate."""
    at = now or datetime.utcnow()
    installations = oem_sharing.installations_for(db, oem_code)
    models_by_id = {m.id: m for m in db.query(models.MachineModel)
                    .filter(models.MachineModel.oem_code == oem_code).all()}

    # Grants once per CUSTOMER, never once per machine (the per-machine view
    # already learned this lesson).
    grants_cache = {}

    def grants(tenant):
        if tenant not in grants_cache:
            grants_cache[tenant] = oem_sharing.grants_for(db, oem_code, tenant)
        return grants_cache[tenant]

    customers = sorted({i.factory_tenant_code for i in installations})
    by_model = defaultdict(list)
    hours_by_customer, util_by_customer = defaultdict(list), defaultdict(list)
    hours_by_model = defaultdict(lambda: defaultdict(list))
    sharing_customers = set()

    for inst in installations:
        tenant = inst.factory_tenant_code
        g = grants(tenant)
        by_model[inst.model_id].append(inst)
        if g:
            sharing_customers.add(tenant)
        if oem_sharing.SHARE_OPERATING_HOURS in g and inst.operating_hours is not None:
            hours_by_customer[tenant].append(float(inst.operating_hours))
            hours_by_model[inst.model_id][tenant].append(float(inst.operating_hours))
        if oem_sharing.SHARE_MACHINE_HEALTH in g:
            machine = oem_sharing.visible_machine(db, inst, g)
            if machine is not None and machine.utilization is not None:
                util_by_customer[tenant].append(float(machine.utilization))

    # ── the fleet, from the OEM's own paperwork ─────────────────────────
    fleet = {
        "machines": len(installations),
        "customers": len(customers),
        "models": len({i.model_id for i in installations if i.model_id}),
        "active": sum(1 for i in installations if (i.status or "") == "Active"),
        "detail": OWN_RECORDS,
    }

    # ── per model: counts always, operational figures only above the floor ──
    model_rows = []
    for model_id, rows in sorted(by_model.items(),
                                 key=lambda kv: (-len(kv[1]), str(kv[0])))[:MAX_MODELS]:
        model = models_by_id.get(model_id)
        pooled = _pooled(hours_by_model[model_id], "Average operating hours")
        model_rows.append({
            "model_id": model_id,
            "model_code": getattr(model, "model_code", None),
            "model_name": getattr(model, "name", None),
            # Counts: the OEM's own records.
            "machines": len(rows),
            "customers": len({r.factory_tenant_code for r in rows}),
            # Operational: the customers' to grant, and floored.
            "average_operating_hours": pooled,
        })

    hours = _pooled(hours_by_customer, "Average operating hours across the fleet")
    utilisation = _pooled(util_by_customer, "Average utilisation across the fleet")
    complementary_suppression(model_rows, hours)

    # ── coverage, stated ────────────────────────────────────────────────
    shared_machines = sum(len(v) for v in hours_by_customer.values())
    coverage = {
        "customers_sharing_anything": len(sharing_customers),
        "customers_total": len(customers),
        "machines_with_hours": shared_machines,
        "machines_total": len(installations),
        "floor": MIN_CUSTOMERS,
        "phrase": (f"{len(sharing_customers)} of {len(customers)} customers share anything at all; "
                   f"operating hours come from {shared_machines} of {len(installations)} machines"),
    }

    facts = [
        _fact("oem.machines", "Machines shipped", fleet["machines"], ev.MEASURED, "machines",
              "machine_installations", detail=OWN_RECORDS),
        _fact("oem.customers", "Customers", fleet["customers"], ev.MEASURED, "customers",
              "machine_installations", detail=OWN_RECORDS),
        _fact("oem.sharing_customers", "Customers sharing anything",
              coverage["customers_sharing_anything"], ev.MEASURED, "customers",
              "oem_sharing_policies",
              detail="a customer that shares nothing still counts as a customer, and contributes no figure"),
    ]
    for key, pooled in (("hours", hours), ("utilisation", utilisation)):
        if pooled["withheld"]:
            # UNKNOWN, with the reason — never absent, because an absent figure
            # reads as a fleet with nothing running in it.
            facts.append(_fact(f"oem.{key}", pooled["label"], None, ev.UNKNOWN, "",
                               "oem_sharing_policies", detail=pooled["reason"]))
        else:
            facts.append(_fact(f"oem.{key}", pooled["label"], pooled["value"], ev.DERIVED, "",
                               "machine_installations",
                               detail=f"pooled across {pooled['customers']} customers, "
                                      f"{pooled['machines']} machines"))

    if not installations:
        state, headline = ev.NO_DATA, "There are no machines registered to this manufacturer yet."
    elif not sharing_customers:
        state = ev.NOT_CONFIGURED
        headline = (f"{fleet['machines']} machines at {fleet['customers']} customers. None of them "
                    "shares operating data, so there is nothing to summarise beyond the shipment "
                    "records.")
    else:
        state = ev.OK if hours["state"] == ev.OK else ev.PARTIAL_DATA
        headline = (f"{fleet['machines']} machines at {fleet['customers']} customers, "
                    f"{fleet['models']} models. {coverage['phrase']}.")
    return {
        "generated_at": at.isoformat(),
        "state": state,
        "headline": headline,
        "fleet": fleet,
        "models": model_rows,
        "operating_hours": hours,
        "utilisation": utilisation,
        "coverage": coverage,
        "facts": facts,
        "note": ("Counts come from the manufacturer's own shipment records. Every operational figure "
                 f"needs at least {MIN_CUSTOMERS} customers sharing it, because a figure from one "
                 "customer is that customer's reading with a new label."),
    }
