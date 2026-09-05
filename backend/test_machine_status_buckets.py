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


def seed():
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
    for i, status in enumerate(machine_status.VALID_MACHINE_STATUSES + ("Running",)):
        db.add(models.Machine(tenant_code=T, name=f"CELL-{i:02d}", site="P1",
                              status=status, utilization=85, downtime="0 min"))
        # Two transitions per machine INTO its status, so the state-summary
        # buckets have something to count and a dropped bucket shows as a
        # shortfall rather than as a zero row.
        for _ in range(2):
            db.add(models.MachineEvent(tenant_code=T, machine_name=f"CELL-{i:02d}",
                                       old_status="Running", new_status=status))
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
    print("5. THE BUCKETS ARE DERIVED, SO A SIXTH STATUS CANNOT VANISH")
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
