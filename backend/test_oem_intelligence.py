"""An aggregate can disclose what a field could not (ADR-0033).

ADR-0017 stops a manufacturer reading a customer's operating hours without a
grant. Nothing stopped it reading an AVERAGE of those hours — and where exactly
one customer shares, that average IS that customer's reading with a new label.
The consent said no and the arithmetic said yes.

So what is pinned here is the floor, and the places it must not be walked
around:

  1. ONE SHARING CUSTOMER PUBLISHES NOTHING, however many machines it has. Ten
     machines at one site is still one customer.
  2. TWO SHARING CUSTOMERS PUBLISH A POOLED FIGURE, and it is the pooled value,
     not one of them.
  3. A WITHHELD FIGURE SAYS WHY and is UNKNOWN, never absent and never zero: a
     missing figure reads as a fleet with nothing running in it.
  4. THE FLOOR COUNTS CUSTOMERS, NOT MACHINES — the whole point.
  5. PER MODEL TOO. A model installed at one customer is the same disclosure in
     a narrower slice, and the per-model row obeys the same floor.
  6. COUNTS ARE ALWAYS VISIBLE, because they come from the manufacturer's own
     shipment records — but the operational figures beside them still do not.
  7. NO CUSTOMER IS NAMED beside an operational figure.
  8. A RIVAL MANUFACTURER'S FLEET IS INVISIBLE, as ever.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_oem_intelligence.py
"""
import sys
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import oem_sharing
import tenancy
from ai import oem_intelligence as oi
from database import Base

failures = []
NOW = datetime(2026, 9, 20, 9, 0, 0)
OEM, RIVAL = "AERON", "RIVALCO"
A, B, C = "FACTORY_A", "FACTORY_B", "FACTORY_C"


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    return sessionmaker(bind=engine)


def seed(Session, *, sharing, health=()):
    """Two models, three customers, real factory machines behind the
    installations, and two independent grant lists: `sharing` grants operating
    hours, `health` grants machine health. They are separate on purpose — a
    customer granting one has not granted the other (ADR-0017), and a fixture
    without factory Machine rows cannot test the health gate at all."""
    db = Session()
    try:
        for code, name in ((OEM, "Aeron"), (RIVAL, "Rivalco")):
            db.add(models.OemOrganization(oem_code=code, name=name, is_active=True))
        db.flush()
        acx = models.MachineModel(oem_code=OEM, family="Compressor", model_code="ACX-75",
                                  name="ACX-75", status="Active")
        bdx = models.MachineModel(oem_code=OEM, family="Dryer", model_code="BDX-10",
                                  name="BDX-10", status="Active")
        other = models.MachineModel(oem_code=RIVAL, family="Compressor", model_code="RV-1",
                                    name="RV-1", status="Active")
        db.add_all([acx, bdx, other])
        db.flush()
        # ACX-75 at A (two machines) and B (one). BDX-10 at C only — one customer,
        # so its operational figures must stay below the floor even if C shares.
        plan = [(acx, A, 2, 4000.0, 70), (acx, B, 1, 2000.0, 50), (bdx, C, 3, 900.0, 30)]
        n = 0
        for model, tenant, count, hours, util in plan:
            for _ in range(count):
                n += 1
                # A REAL factory machine behind the installation. Without one,
                # oem_sharing.visible_machine returns None whatever the grants
                # say, and the health gate is never the thing being tested.
                machine = models.Machine(tenant_code=tenant, name=f"M-{n:03d}", status="Running",
                                         utilization=util, line="L1", downtime="0 min")
                db.add(machine)
                db.flush()
                db.add(models.MachineInstallation(
                    oem_code=OEM, serial_number=f"SN-{n:03d}", model_id=model.id,
                    factory_tenant_code=tenant, site="P1", status="Active",
                    machine_id=machine.id, operating_hours=hours))
        db.add(models.MachineInstallation(
            oem_code=RIVAL, serial_number="SN-RIVAL", model_id=other.id,
            factory_tenant_code=A, site="P1", status="Active", operating_hours=9999.0))
        for tenant in sorted(set(sharing) | set(health)):
            # `grants` is a CSV of grant keys, and NO ROW means nothing shared —
            # the default-deny this whole area rests on (models.OemDataSharingPolicy).
            keys = []
            if tenant in sharing:
                keys.append(oem_sharing.SHARE_OPERATING_HOURS)
            if tenant in health:
                keys.append(oem_sharing.SHARE_MACHINE_HEALTH)
            db.add(models.OemDataSharingPolicy(
                oem_code=OEM, tenant_code=tenant, grants=",".join(keys), updated_by="admin"))
        db.commit()
    finally:
        db.close()
    return Session


def build(Session, oem=OEM):
    db = Session()
    try:
        return oi.build_oem_intelligence(db, oem, now=NOW)
    finally:
        db.close()


def main_():
    print("\n1. One sharing customer publishes NOTHING, however many machines")
    one = build(seed(session(), sharing=[A]))          # A has TWO machines
    hours = one["operating_hours"]
    check("the figure is withheld", hours["withheld"] is True, str(hours))
    check("...and carries no value at all", hours["value"] is None, str(hours["value"]))
    check("...naming the floor in the reason", f"{oi.MIN_CUSTOMERS} customers" in (hours["reason"] or ""),
          str(hours["reason"]))
    check("...even though two MACHINES contributed", hours["machines"] == 2, str(hours["machines"]))
    check("THE FLOOR COUNTS CUSTOMERS, NOT MACHINES", hours["customers"] == 1, str(hours["customers"]))
    fact = next(f for f in one["facts"] if f["key"] == "oem.hours")
    check("the fact is UNKNOWN, not absent and not zero", fact["provenance"] == "UNKNOWN"
          and fact["value"] is None, str(fact))
    check("...and says why", "one customer's reading" in (fact["detail"] or ""), str(fact["detail"]))

    print("\n2. Two sharing customers publish a POOLED figure")
    two = build(seed(session(), sharing=[A, B]))
    hours2 = two["operating_hours"]
    check("the figure is published", hours2["withheld"] is False, str(hours2))
    # A: 4000, 4000. B: 2000. Pooled mean over the three machines.
    check("it is the pooled value across all contributing machines",
          hours2["value"] == round((4000 + 4000 + 2000) / 3, 1), str(hours2["value"]))
    check("...and is NOT either customer's own figure",
          hours2["value"] not in (4000.0, 2000.0), str(hours2["value"]))
    check("it says how many customers it pooled", hours2["customers"] == 2, str(hours2["customers"]))
    fact2 = next(f for f in two["facts"] if f["key"] == "oem.hours")
    check("the fact is DERIVED with its coverage in the detail",
          fact2["provenance"] == "DERIVED METRIC" and "2 customers" in (fact2["detail"] or ""),
          str(fact2))

    print("\n3. No sharing customer at all")
    none = build(seed(session(), sharing=[]))
    check("the state is NOT CONFIGURED", none["state"] == "NOT CONFIGURED", none["state"])
    check("the headline says nothing is shared",
          "None of them shares operating data" in none["headline"], none["headline"])
    check("the reason is 'no customer shares this', not the floor",
          none["operating_hours"]["reason"] == "no customer shares this",
          str(none["operating_hours"]["reason"]))
    check("...but the shipment counts are still there",
          none["fleet"]["machines"] == 6 and none["fleet"]["customers"] == 3, str(none["fleet"]))

    print("\n4. Per model, the same floor applies")
    per_model = {m["model_code"]: m for m in two["models"]}
    acx = per_model["ACX-75"]
    check("ACX-75 spans two sharing customers, so its average is published",
          acx["average_operating_hours"]["withheld"] is False,
          str(acx["average_operating_hours"]))
    bdx = per_model["BDX-10"]
    check("BDX-10 is at ONE customer, so its average is withheld",
          bdx["average_operating_hours"]["withheld"] is True, str(bdx["average_operating_hours"]))
    check("...and its machine COUNT is still shown", bdx["machines"] == 3, str(bdx["machines"]))
    check("...because counts come from the manufacturer's own records",
          "own records" in two["fleet"]["detail"], two["fleet"]["detail"])
    # The slice must not become a way round the floor: C shares in this scenario
    # too, and BDX-10 is still withheld because C is its only customer.
    three = build(seed(session(), sharing=[A, B, C]))
    bdx3 = {m["model_code"]: m for m in three["models"]}["BDX-10"]
    check("even with every customer sharing, a single-customer MODEL is withheld",
          bdx3["average_operating_hours"]["withheld"] is True,
          str(bdx3["average_operating_hours"]))
    check("...while the fleet-wide figure, which spans three, is published",
          three["operating_hours"]["withheld"] is False, str(three["operating_hours"]))

    print("\n4c. THE MARGIN GIVES BACK WHAT THE CELL WITHHELD (ADR-0033 §7)")
    # A floor on each cell is not a floor on the table. In `three`, every
    # customer shares: the fleet figure is published, ACX-75 spans two so it is
    # published, and BDX-10 spans one so it is withheld. That is one equation
    # with one unknown:
    #
    #     fleet.value x fleet.machines - ACX.value x ACX.machines
    #     ------------------------------------------------------  =  BDX
    #                        BDX.machines
    #
    # Measured before the fix: BDX's withheld 900.0 came back as 900.1, which
    # is C's own reading. The cell said no and the arithmetic said yes -- the
    # same failure as ADR-0033's opening paragraph, one level up.
    rows3 = {m["model_code"]: m for m in three["models"]}
    hidden = [m for m in three["models"]
              if m["average_operating_hours"]["withheld"]
              and m["average_operating_hours"]["machines"] > 0]
    check("a solvable table leaves at least TWO slices withheld, never one",
          len(hidden) >= 2, str([m["model_code"] for m in hidden]))
    check("...so the model that was ALONE at one customer is still withheld",
          rows3["BDX-10"]["average_operating_hours"]["withheld"] is True,
          str(rows3["BDX-10"]["average_operating_hours"]))
    check("...and the publishable one is suppressed WITH it, not instead of it",
          rows3["ACX-75"]["average_operating_hours"]["withheld"] is True,
          str(rows3["ACX-75"]["average_operating_hours"]))
    check("...saying it is the arithmetic being withheld, not the row",
          "subtracting" in (rows3["ACX-75"]["average_operating_hours"]["reason"] or ""),
          str(rows3["ACX-75"]["average_operating_hours"]["reason"]))
    # A row flagged withheld that still CARRIES its number is not withheld: the
    # screen hides it and the API hands it over. Both withheld rows must be
    # empty of a value, and both must keep their counts, which are the OEM's
    # own records and were never the secret.
    for code in ("ACX-75", "BDX-10"):
        fig = rows3[code]["average_operating_hours"]
        check(f"{code} carries NO value, not merely a withheld flag",
              fig["value"] is None, str(fig))
        check(f"...while {code}'s machine count survives, being the OEM's own record",
              fig["machines"] == 3 and rows3[code]["machines"] == 3, str(fig["machines"]))
    # The attack itself, run: the subtraction must no longer produce EITHER
    # true value. A is 4000 x2 and B 2000 x1 (ACX = 3333.3); C is 900 x3 (BDX).
    published = [m["average_operating_hours"] for m in three["models"]
                 if not m["average_operating_hours"]["withheld"]]
    fleet_total = three["operating_hours"]["value"] * three["operating_hours"]["machines"]
    left = fleet_total - sum(p["value"] * p["machines"] for p in published)
    recovered = {round(left / m["average_operating_hours"]["machines"], 1) for m in hidden}
    check("the subtraction recovers NEITHER withheld figure",
          900.0 not in recovered and 3333.3 not in recovered, str(sorted(recovered)))
    check("...because one equation now has two unknowns", len(hidden) == 2,
          str([m["model_code"] for m in hidden]))

    # CONTROL 1: nothing is suppressed when nothing needs to be. `two` has A and
    # B sharing and C not, so BDX-10 contributes NOTHING to the fleet total --
    # its row is protected by the consent, and ACX-75 must keep its figure.
    check("CONTROL: a withheld slice that is not IN the margin costs nobody a figure",
          per_model["ACX-75"]["average_operating_hours"]["withheld"] is False,
          str(per_model["ACX-75"]["average_operating_hours"]))
    # CONTROL 2: the smallest publishable slice is the one suppressed, because
    # it is the least the manufacturer loses.
    check("CONTROL: the suppressed row is one that COULD have been published",
          rows3["ACX-75"]["average_operating_hours"]["customers"] >= oi.MIN_CUSTOMERS,
          str(rows3["ACX-75"]["average_operating_hours"]["customers"]))
    # CONTROL 3: with nothing publishable to suppress alongside it, the MARGIN
    # goes instead -- a fleet figure over one unknown is that unknown.
    only_c = build(seed(session(), sharing=[C]))
    check("CONTROL: when every slice is below the floor, the FLEET figure goes",
          only_c["operating_hours"]["withheld"] is True,
          str(only_c["operating_hours"]))

    print("\n4d. The suppression rule itself, at its edges")
    # The `no publishable slice` branch needs a fleet that spans two customers
    # while every SHOWN slice is below the floor, which happens when
    # contributing machines sit outside the shown rows -- past MAX_MODELS, or
    # on installations with no model. Thirteen models is a heavy fixture for
    # one branch, and the rule is a pure function over rows, so it is exercised
    # here directly. A mutation that deleted this branch survived the fixture
    # above; that is why this section exists.
    def row(code, value, customers, machines):
        withheld = value is None
        return {"model_code": code, "machines": machines, "customers": customers,
                "average_operating_hours": {
                    "label": "Average operating hours", "value": value,
                    "customers": customers, "machines": machines,
                    "state": "PARTIAL DATA" if withheld else "OK",
                    "withheld": withheld, "reason": "floor" if withheld else None}}

    margin = {"value": 2000.0, "machines": 4, "withheld": False, "state": "OK", "reason": None}
    rows = [row("ONLY", None, 1, 2)]           # one withheld slice, nothing publishable
    oi.complementary_suppression(rows, margin)
    check("with no publishable slice, the MARGIN is withheld instead",
          margin["withheld"] is True and margin["value"] is None, str(margin))
    check("...and says a margin over one unknown IS that unknown",
          margin["reason"] == oi.MARGIN_IS_THE_CELL, str(margin["reason"]))
    check("...and the slice itself is left as the floor left it",
          rows[0]["average_operating_hours"]["reason"] == "floor",
          str(rows[0]["average_operating_hours"]))

    # The victim is the SMALLEST publishable slice, because it is the least the
    # manufacturer loses. With 5 and 2 machines publishable, the 2 goes.
    margin2 = {"value": 1000.0, "machines": 9, "withheld": False, "state": "OK", "reason": None}
    rows2 = [row("HIDDEN", None, 1, 2), row("BIG", 900.0, 3, 5), row("SMALL", 1200.0, 2, 2)]
    oi.complementary_suppression(rows2, margin2)
    by_code = {r["model_code"]: r["average_operating_hours"] for r in rows2}
    check("the SMALLEST publishable slice is the one suppressed",
          by_code["SMALL"]["withheld"] is True and by_code["BIG"]["withheld"] is False,
          f"SMALL={by_code['SMALL']['withheld']} BIG={by_code['BIG']['withheld']}")
    check("...and the margin is left alone, since a slice could carry the cost",
          margin2["withheld"] is False, str(margin2))

    # Two already withheld: one equation, two unknowns. Nothing more is taken.
    margin3 = {"value": 1000.0, "machines": 9, "withheld": False, "state": "OK", "reason": None}
    rows3u = [row("H1", None, 1, 2), row("H2", None, 1, 2), row("OK1", 900.0, 3, 5)]
    oi.complementary_suppression(rows3u, margin3)
    check("two withheld slices are already unsolvable, so nothing else is taken",
          not [r for r in rows3u if r["model_code"] == "OK1"][0]["average_operating_hours"]["withheld"]
          and margin3["withheld"] is False, str(margin3))

    print("\n4b. The health grant is independent of the hours grant")
    # A and B share HOURS. Only A shares HEALTH. Utilisation therefore has ONE
    # contributing customer and must be withheld even though the fleet-wide
    # hours figure, which spans two, is published. Without this, a mutation
    # reading utilisation with no grant at all changed nothing.
    split = build(seed(session(), sharing=[A, B], health=[A]))
    check("hours span two customers and are published",
          split["operating_hours"]["withheld"] is False, str(split["operating_hours"]))
    check("utilisation spans ONE customer and is withheld",
          split["utilisation"]["withheld"] is True, str(split["utilisation"]))
    check("...counted as one customer, not as two machines",
          split["utilisation"]["customers"] == 1 and split["utilisation"]["machines"] == 2,
          str(split["utilisation"]))
    # With a second customer granting health, the figure appears — which proves
    # the grant, not the floor, was what was missing above.
    both = build(seed(session(), sharing=[A, B], health=[A, B]))
    check("a second health grant publishes utilisation",
          both["utilisation"]["withheld"] is False, str(both["utilisation"]))
    check("...pooled across both, not either one's own number",
          both["utilisation"]["value"] == round((70 + 70 + 50) / 3, 1),
          str(both["utilisation"]["value"]))
    # And the gate itself: C shares hours but NOT health, so its utilisation
    # must never reach the figure.
    hours_only = build(seed(session(), sharing=[A, B, C], health=[A, B]))
    check("a customer that shares hours but not health contributes no utilisation",
          hours_only["utilisation"]["customers"] == 2, str(hours_only["utilisation"]))
    check("...so C's 30% is not in the pooled value",
          hours_only["utilisation"]["value"] == round((70 + 70 + 50) / 3, 1),
          str(hours_only["utilisation"]["value"]))

    print("\n5. No customer is named beside an operational figure")
    for label, payload in (("two", two), ("three", three), ("one", one)):
        for tenant in (A, B, C):
            # A customer code may appear nowhere in the aggregate payload: the
            # per-model rows carry counts, never a named customer's reading.
            check(f"{label}: {tenant} is not named anywhere in the aggregate",
                  tenant not in repr(payload), tenant)

    print("\n6. Coverage is stated, not implied")
    cov = three["coverage"]
    check("it says how many customers share anything",
          cov["customers_sharing_anything"] == 3 and cov["customers_total"] == 3, str(cov))
    check("...and how many machines the hours came from",
          cov["machines_with_hours"] == 6 and cov["machines_total"] == 6, str(cov))
    check("...in a sentence", "of 3 customers share anything" in cov["phrase"], cov["phrase"])
    check("the floor is published", cov["floor"] == oi.MIN_CUSTOMERS)
    partial = build(seed(session(), sharing=[A, B]))
    check("with one customer not sharing, the state is PARTIAL DATA or OK, never a bare claim",
          partial["state"] in ("OK", "PARTIAL DATA"), partial["state"])
    check("...and the coverage says 2 of 3",
          partial["coverage"]["customers_sharing_anything"] == 2, str(partial["coverage"]))

    print("\n7. A rival manufacturer's fleet is invisible")
    Session = seed(session(), sharing=[A, B])
    mine = build(Session, OEM)
    check("my fleet holds only my machines", mine["fleet"]["machines"] == 6,
          str(mine["fleet"]["machines"]))
    check("the rival's serial appears nowhere", "SN-RIVAL" not in repr(mine))
    check("the rival's model appears nowhere", "RV-1" not in repr(mine))
    check("and the rival's 9999 hours are not in any figure", "9999" not in repr(mine))
    theirs = build(Session, RIVAL)
    check("the rival sees only its own one machine", theirs["fleet"]["machines"] == 1,
          str(theirs["fleet"]["machines"]))
    check("...and with one customer, its hours are withheld too",
          theirs["operating_hours"]["withheld"] is True, str(theirs["operating_hours"]))
    check("...and none of my serials appear", "SN-001" not in repr(theirs))

    print("\n8. Nothing here claims more than a count and a pooled average")
    blob = repr(three).lower()
    for word in ("predict", "forecast", "failure risk", "recommend"):
        check(f'the payload does not say "{word}"', word not in blob)
    check("the note explains the floor in words",
          "that customer's reading with a new label" in three["note"], three["note"])

    print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'All checks passed'}")
    for f in failures:
        print("  -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main_())
