"""An Offline machine is reported by nothing. It is the one you most need told.

THE DEFECT
----------
`machine_status.py` exists for exactly one reason, and says so in its own
docstring:

    "an unrecognised string ... written straight onto Machine.status silently
     removes the machine from all of those reports."

`Offline` is not an unrecognised string. It is the fifth entry in
VALID_MACHINE_STATUSES, `normalize_machine_status("offline")` returns it
happily, and a device whose gateway drops is the single most natural thing for
an ingest path to report. And then the machine disappears anyway, because every
status rollup in the product buckets FOUR statuses:

  * `/analytics/summary`, `/analytics/factory-command-center` and
    `/analytics/executive-oee` count Running / Idle / Breakdown / Maintenance,
    so the counts add up to less than the fleet with nothing saying so.
  * `build_management_summary` reports `breakdown_count` and `machine_count` and
    nothing between them.
  * `/analytics/machine-state-summary` buckets into a hardcoded four-tuple. That
    one was KNOWN — the code says "an Offline event bumped total_events but no
    bucket" — and was preserved verbatim through a rewrite because the rewrite's
    job was to keep the numbers identical. It was never a decision that Offline
    should vanish; it was a decision not to change behaviour that day.
  * the PER-ZONE rollup in the same command centre, three hundred lines below
    the fleet counts, through its own four-branch if/elif. The first version of
    this fix corrected the fleet counts and MISSED this one, so an offline
    machine kept vanishing from the zone view after the fleet view was fixed.
    Two implementations of one rule, and only one of them got corrected.

Measured on a six-machine fleet, one per status plus a spare, with utilization
healthy everywhere so nothing else can fire:

    machines in the plant        6
    the four buckets account for 5
    alerts naming the offline machine: none
    the copilot, asked "which machines are down?": names it

So two AMP surfaces disagree about the same plant, and the one that is silent is
the dashboard.

THE FIX, AND WHAT IT DELIBERATELY IS NOT
-----------------------------------------
Buckets are DERIVED from VALID_MACHINE_STATUSES rather than hardcoded, so the
next status added cannot silently vanish — the same "one source, not a second
list" shape as the copilot's drill-in labels (#546). Every count is ADDITIVE:
section 4 is a reference oracle proving the four existing numbers do not move.

It does NOT add an alert for an offline machine. That is the obviously useful
follow-up, and it is a product decision about severity — "we have lost sight of
this asset" is not self-evidently Critical or Warning, and inventing a severity
for a customer's alert list is not mine to invent. Flagged, not decided.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_machine_status_buckets.py
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import analytics_engine
import machine_status
import models
import tenancy
from database import Base

T = "BUCKETS"
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def seed(extra_status=None):
    """One machine per valid status, plus a second Running so no count is 1 by
    coincidence. Utilization is HEALTHY on every machine, including the offline
    one: an earlier probe of this defect seeded utilization=0 for everything not
    Running, which made a low-utilisation alert fire and look like the offline
    machine was being reported. It was not. The fixture has to make status the
    only thing that differs."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    Session = sessionmaker(bind=engine)
    db = Session()
    tok = tenancy.set_current_tenant(None)
    db.add(models.TenantConfig(tenant_code=T))
    fleet = machine_status.VALID_MACHINE_STATUSES + ("Running",)
    if extra_status is not None:
        fleet = fleet + (extra_status,)
    for i, status in enumerate(fleet):
        db.add(models.Machine(tenant_code=T, name=f"CELL-{i:02d}", site="P1",
                              status=status, utilization=85, downtime="0 min"))
        # Two transitions per machine INTO its status, so the state-summary
        # buckets have something to count and a dropped bucket shows as a
        # shortfall rather than as a zero row.
        for _ in range(2):
            db.add(models.MachineEvent(tenant_code=T, machine_name=f"CELL-{i:02d}",
                                       old_status="Running", new_status=status))
    db.flush()
    # A floor layout for the per-zone rollup (section 5). Two zones so a single
    # zone cannot hide a mis-bucketed machine in the other's total, plus ONE node
    # with no machine at all — an "area" — because the zone rollup counts every
    # node in `nodes` but only machine-bearing ones in the status buckets, and a
    # test that assumed those were the same number would assert the wrong thing.
    for i in range(len(fleet)):
        db.add(models.FactoryLayoutNode(
            tenant_code=T, machine_id=i + 1, node_name=f"CELL-{i:02d}",
            zone="SMT" if i % 2 else "IC"))
    db.add(models.FactoryLayoutNode(tenant_code=T, machine_id=None,
                                    node_name="Goods In", zone="SMT"))
    db.commit()
    tenancy.reset_current_tenant(tok)
    return db


def main():
    db = seed()
    tok = tenancy.set_current_tenant(T)
    machines = db.query(models.Machine).all()
    VALID = machine_status.VALID_MACHINE_STATUSES

    print("=" * 74)
    print("1. THE PREMISE — Offline is a status the product ACCEPTS")
    print("=" * 74)
    check("a device reporting 'offline' normalises to a valid status",
          machine_status.normalize_machine_status("offline") == "Offline",
          repr(machine_status.normalize_machine_status("offline")))
    check("...and it is in the canonical vocabulary", "Offline" in VALID, str(VALID))
    check("the fleet holds one machine in every valid status",
          {m.status for m in machines} == set(VALID),
          str(sorted({m.status for m in machines})))

    print()
    print("=" * 74)
    print("2. THE STATUS COUNTS ACCOUNT FOR EVERY MACHINE")
    print("=" * 74)
    # The defect stated as arithmetic. `/analytics/summary` and
    # `/analytics/factory-command-center` are a CENSUS — they publish `machines`
    # alongside a count per status — so their counts must partition the fleet.
    # A bucket set that does not cover the vocabulary makes them sum short, with
    # nothing on the response or the screen saying which machine went missing.
    import analytics_routes
    for name, fn in (("/analytics/summary", analytics_routes.analytics_summary),
                     ("/analytics/factory-command-center",
                      analytics_routes.get_factory_command_center)):
        payload = fn(db, {"tenant": T})
        counted = sum(payload.get(k, 0) for k in
                      ("running", "idle", "breakdown", "maintenance", "offline"))
        check(f"{name}: status counts total {counted} of {payload['machines']} machines",
              counted == payload["machines"],
              f"{payload['machines'] - counted} machine(s) in NO bucket")
        check(f"{name}: the offline machine is counted by name",
              payload.get("offline") == 1, str(payload.get("offline")))

    # build_management_summary is NOT a census — it reports exceptions, which is
    # why it publishes breakdown_count and not a count per status. The property
    # that belongs here is narrower: a machine that is unexpectedly not producing
    # must be reported, and "the gateway dropped" is exactly that.
    summary = analytics_engine.build_management_summary(machines, [], [], [])
    check("management summary reports the offline machine alongside breakdowns",
          summary.get("offline_count") == 1, str(summary.get("offline_count")))

    print()
    print("=" * 74)
    print("3. THE STATE-SUMMARY BARS RECONCILE WITH total_events")
    print("=" * 74)
    # The chart stacks one bar per status against a total. If a status has no
    # bar, the stack is shorter than the total and nothing on screen says why.
    rows = analytics_routes.get_machine_state_summary(db, {"tenant": T})
    check("every machine appears", len(rows) == len(machines), str(len(rows)))
    short = [(r["machine_name"], sum(r.get(s, 0) for s in VALID), r["total_events"])
             for r in rows
             if sum(r.get(s, 0) for s in VALID) != r["total_events"]]
    check("the per-status buckets sum to total_events for every machine",
          not short, f"bars < total for: {short}")
    offline_row = next((r for r in rows if r.get("Offline")), None)
    check("...and the offline machine's transitions are in a bucket, not just the total",
          offline_row is not None and offline_row["Offline"] == 2,
          str(offline_row))

    print()
    print("=" * 74)
    print("4. REFERENCE ORACLE — the four existing numbers DID NOT MOVE")
    print("=" * 74)
    # Everything above is additive. These are the values the shipped code
    # produced, computed HERE from the rows rather than read from the code under
    # test, so a change that fixed the shortfall by moving an existing count
    # would fail here.
    for status in ("Running", "Idle", "Breakdown", "Maintenance"):
        expected = len([m for m in machines if m.status == status])
        got = sum(r.get(status, 0) for r in rows)
        check(f"{status}: {expected} machine(s) -> {expected * 2} transitions, unchanged",
              got == expected * 2, f"expected {expected * 2}, got {got}")
    check("breakdown_count is still Breakdown ONLY, not widened to 'not running'",
          summary["breakdown_count"] == 1, str(summary["breakdown_count"]))
    check("machine_count is unchanged", summary["machine_count"] == 6,
          str(summary["machine_count"]))

    print()
    print("=" * 74)
    print("5. THE PER-ZONE ROLLUP BUCKETS THE SAME VOCABULARY")
    print("=" * 74)
    # This section exists because the first version of this fix MISSED it.
    # /analytics/factory-command-center also rolls machine status up per zone,
    # three hundred lines below the fleet counts, through its own four-branch
    # if/elif -- so an offline machine kept vanishing from the zone view after
    # the fleet view had been fixed. Two implementations of one rule, and only
    # one of them got corrected.
    #
    # The property is NOT "the counts sum to nodes": a layout node with no
    # machine is an area, not a machine, and is legitimately in no status
    # bucket. It is "the counts sum to the nodes that HAVE a machine".
    fcc = analytics_routes.get_factory_command_center(db, {"tenant": T})
    zones = fcc["zone_summary"]
    check("the zone rollup returned at least one zone", len(zones) >= 1, str(len(zones)))
    by_id = {m.id: m for m in machines}
    nodes = db.query(models.FactoryLayoutNode).all()
    for zone in zones:
        with_machine = len([n for n in nodes
                            if (n.zone or "Production") == zone["zone"]
                            and n.machine_id in by_id])
        counted = sum(zone.get(s.lower(), 0) for s in VALID)
        check(f"zone {zone['zone']!r}: status counts total {counted} for "
              f"{with_machine} machine-bearing node(s)",
              counted == with_machine,
              f"{with_machine - counted} machine(s) in NO bucket")
    check("the offline machine's zone counts it",
          sum(z.get("offline", 0) for z in zones) == 1,
          str([z.get("offline") for z in zones]))
    zone_keys = set(zones[0]) - {"zone", "nodes"}
    check("...and the zone buckets are the canonical vocabulary too",
          zone_keys == {s.lower() for s in VALID},
          f"buckets={sorted(zone_keys)}")

    print()
    print("=" * 74)
    print("6. THE BUCKETS ARE DERIVED, SO A SIXTH STATUS CANNOT VANISH")
    print("=" * 74)
    # The durable half. A hardcoded tuple is how Offline was lost; this asserts
    # the tuple is gone rather than merely lengthened by one.
    row_keys = set(rows[0]) - {"machine_name", "total_events"}
    check("the state summary buckets exactly the canonical vocabulary",
          row_keys == set(VALID), f"buckets={sorted(row_keys)} valid={sorted(VALID)}")
    check("no status in the vocabulary is unbucketed",
          not (set(VALID) - row_keys), str(sorted(set(VALID) - row_keys)))
    check("no bucket exists for a status the vocabulary does not have",
          not (row_keys - set(VALID)), str(sorted(row_keys - set(VALID))))

    tenancy.reset_current_tenant(tok)
    db.close()

    print()
    print("=" * 74)
    print("7. A STATUS OUTSIDE THE VOCABULARY MUST NOT 500 THE COMMAND CENTRE")
    print("=" * 74)
    # Deriving the buckets turns "unknown status" from a silently-skipped branch
    # into a dict lookup, and Machine.status has no database constraint — a
    # legacy row, a migration or a raw-SQL write can hold anything.
    # normalize_machine_status guards INGEST; it does not guard the table. So the
    # membership check is load-bearing, and without this section a mutation
    # deleting it survived: the shared fixture holds only valid statuses, so the
    # guard was never once executed.
    #
    # Its own database on purpose. Dropping an unknown-status machine into the
    # fixture above would break section 2's census legitimately — with a status
    # outside the vocabulary the counts genuinely CANNOT sum to the fleet, which
    # is the whole reason normalize_machine_status exists at the door.
    odd = seed(extra_status="Faulted")
    tok = tenancy.set_current_tenant(T)
    try:
        payload = analytics_routes.get_factory_command_center(odd, {"tenant": T})
        raised = None
    except Exception as exc:
        payload, raised = None, exc
    check("the command centre still answers", raised is None,
          f"raised {type(raised).__name__}: {raised}")
    if payload is not None:
        zones = payload["zone_summary"]
        check("...the unknown-status machine is in NO status bucket",
              sum(z.get(s.lower(), 0) for z in zones for s in VALID) == len(machines),
              str([{k: v for k, v in z.items() if k != "zone"} for z in zones]))
        check("...and the census is honest about not adding up",
              sum(payload.get(s.lower(), 0) for s in VALID) == payload["machines"] - 1,
              f"counted={sum(payload.get(s.lower(), 0) for s in VALID)} "
              f"machines={payload['machines']}")
    tenancy.reset_current_tenant(tok)
    odd.close()

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
