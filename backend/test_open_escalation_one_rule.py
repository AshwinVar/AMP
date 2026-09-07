"""Cancelling an escalation silenced that alert forever; working one raised a duplicate.

THE DEFECT
----------
"An open escalation" had FIVE spellings across eight sites, disagreeing in both
directions:

  A. analytics_routes.py:1031 (command centre)   status IS NULL OR
     analytics_routes.py:1528 (system health)      status != "Resolved"
     core_routes.py:262       (smart-alert dedup)
     factory_ops_routes.py:696 (notifications)

  B. ai/escalations.py:119    (the read-model)    lower(trim(coalesce(status,"")))
     ai/handover.py:47        (shift handover)      NOT IN CLOSED_STATUSES

  C. ai/agents.py:279         (escalation dedup)  status IN ("Proposed", "Open")
     ai/agents.py:341         (briefing dedup)

  D. ai/agents.py:171         (task dedup)        status IN ("Proposed", "Open")
     vs ai/maintenance.py:27  OPEN_STATUSES = ("Proposed", "Open", "In Progress")

  E. saas_routes.py:304       (tenant adoption)  status IS NULL OR status NOT IN
                                                 ("Resolved","Cancelled","Closed")

A counts a WITHDRAWN escalation as open; B and E do not.
C misses an IN PROGRESS one; A, B and E all count it.
E is case-sensitive and lacks the "canceled" spelling B carries.

Two of those are user-visible defects, not just a count that disagrees:

1. `POST /escalations/from-smart-alerts` dedups on A. A Cancelled escalation is
   `!= "Resolved"`, so it is found, and the alert is skipped — **permanently**.
   A supervisor withdraws one duplicate breakdown escalation and that machine can
   never raise that alert again, however many times it actually breaks down.
   Cancelled is written by the Approvals Inbox on reject (ai/agents.py:124) and
   by the escalation UI, so this is the ordinary path, not a corner case.

2. The agents dedup on C/D, which whitelist only ("Proposed", "Open") — but
   `EscalationSection.tsx:280` offers "In Progress" and that is what a technician
   sets when they pick the work up. The moment they do, the escalation and the
   task become invisible to the guard, and the agent proposes the SAME work
   again — a second approval action, and a second task, while someone is holding
   a spanner over the first one.

3. `factory_ops_routes.py` sends operators a notification saying "N escalation(s)
   still require action" off A — so it counts escalations somebody already
   withdrew. An alert about work nobody is ever going to do is how a
   notification channel earns being ignored.

And the counts are on screen simultaneously: `DigitalTwinSection.tsx:169` renders
the command centre's figure (A) and `HandoverSnapshot.tsx:77` the handover's (B)
for the same plant at the same moment, so a withdrawn escalation makes them
differ indefinitely. `TenantAdoptionCard.tsx:106` renders E to the platform
operator — the number used to judge whether a customer is struggling, disagreeing
with the number that customer sees on their own dashboard.

THE FIX
-------
One predicate, `ai.escalations.open_clause()` — an escalation is open when its
status is not terminal, matched NULL-, case- and whitespace-safe against
CLOSED_STATUSES, which is what B already did. A and C both adopt it. D adopts
`ai.maintenance.OPEN_STATUSES`, which already carries "In Progress".

Deliberately NOT changed: a withdrawn item stays outside the agents' dedup, so
an agent may re-propose work a human rejected if the condition recurs. That is
the existing behaviour of C and D for "Cancelled" and it is defensible — the
plant changed. This fix is about the state where a human said YES and is doing
the work.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_open_escalation_one_rule.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import analytics_routes
import core_routes
import factory_ops_routes
import models
import saas_routes
import tenancy
from ai import agents, escalations, handover
from database import Base
from events import QualityInspectionFailed

T = "OPENESC"
USER = {"tenant": T, "username": "tester", "role": "Admin"}
# The adoption panel is founder-only and iterates the tenant registry, so it
# needs a DEFAULT-workspace Admin and a registry row for T (added in seed()).
FOUNDER = {"tenant": tenancy.DEFAULT_TENANT, "username": "founder", "role": "Admin"}
BREAKDOWN_TITLE = "Breakdown - PRESS-01"
BRIEFING_KEY = "downtime_spike"
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def seed():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    db = sessionmaker(bind=engine)()
    tok = tenancy.set_current_tenant(None)
    db.add(models.TenantConfig(tenant_code=T))
    db.add(models.CompanyTenant(company_code=T, company_name="Open Esc Ltd",
                                plan_name="Starter", subscription_status="Active"))
    # utilization 80 keeps generate_alerts to exactly one alert (Breakdown); a
    # value under 50 would add "Low Utilization" and blur the dedup assertions.
    m = models.Machine(tenant_code=T, name="PRESS-01", site="P1", status="Breakdown",
                       utilization=80, downtime="0 min")
    db.add(m)
    db.flush()
    now = datetime.utcnow()

    # 1. A human withdrew this one. It must not gag the alert forever.
    db.add(models.Escalation(tenant_code=T, title=BREAKDOWN_TITLE, machine_id=m.id,
                             severity="High", status="Cancelled", source="Smart alert",
                             owner="Ops", department="Maintenance",
                             created_at=now - timedelta(days=2)))
    # 2. A technician picked this one up. It is open work, and the agent that
    #    raised it must not raise it again.
    db.add(models.Escalation(tenant_code=T, title="Repeated downtime - PRESS-01",
                             machine_id=m.id, severity="High", status="In Progress",
                             source="Escalation agent", owner="Ops", department="Maintenance",
                             created_at=now - timedelta(days=1),
                             notes=f"Raised from the daily briefing. [briefing:{BRIEFING_KEY}]"))
    # 3. Control: genuinely finished, and must stay out of every open count.
    db.add(models.Escalation(tenant_code=T, title="Old fault - PRESS-01", machine_id=m.id,
                             severity="Low", status="Resolved", source="Smart alert",
                             owner="Ops", department="Maintenance",
                             created_at=now - timedelta(days=5), resolved_at=now))
    # 4. Vocabulary drift, which is the whole reason CLOSED_STATUSES is matched
    #    lowercased and trimmed rather than compared literally. Both of these are
    #    CLOSED, and a surface that keeps its own literal list counts them open:
    #    saas_routes' list was ("Resolved", "Cancelled", "Closed"), so "canceled"
    #    (one l) and "  Resolved  " both fell through it. Without these rows the
    #    consolidation is untested — mutation 12 of mutate_open_escalation.py
    #    survived on a fixture that had only exact-cased statuses.
    db.add(models.Escalation(tenant_code=T, title="US-spelt withdrawal - PRESS-01",
                             machine_id=m.id, severity="Low", status="canceled",
                             source="Smart alert", owner="Ops", department="Maintenance",
                             created_at=now - timedelta(days=4)))
    db.add(models.Escalation(tenant_code=T, title="Padded status - PRESS-01",
                             machine_id=m.id, severity="Low", status="  Resolved  ",
                             source="Smart alert", owner="Ops", department="Maintenance",
                             created_at=now - timedelta(days=4)))
    # 5. A quality task the agent proposed and a technician is now working.
    db.add(models.MaintenanceTask(tenant_code=T, task_no="AUTO-QUAL-EXISTING",
                                  machine_id=m.id, task_type=agents.QUALITY_TASK_TYPE,
                                  assigned_to="Maintenance team", planned_date=now.date(),
                                  status="In Progress"))
    db.commit()
    tenancy.reset_current_tenant(tok)
    return db, m


def main():
    db, m = seed()
    tok = tenancy.set_current_tenant(T)

    print("=" * 74)
    print("1. ONE PLANT, ONE OPEN-ESCALATION COUNT")
    print("=" * 74)
    # Cancelled and Resolved are both off the queue; only the In Progress one is
    # open. Every surface has to say 1.
    cc = analytics_routes.get_factory_command_center(db, USER)
    sh = analytics_routes.get_system_health(db, USER)
    ho = handover.build_handover(db, T)
    summary = escalations.build_escalation_summary(db, T)
    check(f"command centre open_escalations = {cc['open_escalations']}",
          cc["open_escalations"] == 1, str(cc["open_escalations"]))
    check(f"system health open_escalations = {sh['open_escalations']}",
          sh["open_escalations"] == 1, str(sh["open_escalations"]))
    check(f"shift handover open_escalations = {ho['open_work']['open_escalations']}",
          ho["open_work"]["open_escalations"] == 1,
          str(ho["open_work"]["open_escalations"]))
    check(f"the escalation read-model open = {summary['open']}",
          summary["open"] == 1, str(summary["open"]))
    # The operator-facing notification: it must not tell anyone that work
    # somebody already withdrew "still requires action".
    factory_ops_routes.generate_system_notifications(db, USER)
    notes = [n for n in db.query(models.Notification).all()
             if n.title == "Open escalations pending"]
    check("the system notification says 1 escalation requires action, not 2",
          len(notes) == 1 and "1 escalation(s)" in (notes[0].message or ""),
          str([n.message for n in notes]))
    # The platform operator's adoption panel, which had its own terminal list.
    row = next(r for r in saas_routes.get_tenant_activity(db, FOUNDER)["tenants"]
               if r["tenant_code"] == T)
    check(f"the tenant adoption panel open_escalations = {row['open_escalations']}",
          row["open_escalations"] == 1, str(row["open_escalations"]))
    check("...and all six are the same number",
          len({cc["open_escalations"], sh["open_escalations"],
               ho["open_work"]["open_escalations"], summary["open"],
               row["open_escalations"]}) == 1,
          f"cc={cc['open_escalations']} health={sh['open_escalations']} "
          f"handover={ho['open_work']['open_escalations']} model={summary['open']} "
          f"saas={row['open_escalations']}")

    print()
    print("=" * 74)
    print("2. WORK A TECHNICIAN PICKED UP IS STILL OPEN TO THE AGENT")
    print("=" * 74)
    check("the Escalation agent sees its own In Progress escalation",
          agents._open_agent_escalation_exists(db, m.id) is True)
    ids = agents.open_briefing_escalation_ids(db, T)
    check(f"the briefing sees it too, so it will not re-raise [{BRIEFING_KEY}]",
          BRIEFING_KEY in ids, str(list(ids)))

    print()
    print("=" * 74)
    print("3. AN AGENT DOES NOT RE-PROPOSE A TASK SOMEONE IS WORKING")
    print("=" * 74)
    before = db.query(models.MaintenanceTask).count()
    agents.inspect_on_quality_failed(QualityInspectionFailed(
        tenant_code=T, inspection_no="QI-DUP", failed_quantity=8,
        inspected_quantity=10, machine_id=m.id, defect_category="Dimensional"), db)
    db.commit()
    after = db.query(models.MaintenanceTask).count()
    check(f"no duplicate '{agents.QUALITY_TASK_TYPE}' task ({before} -> {after})",
          after == before, f"{before} -> {after}")

    print()
    print("=" * 74)
    print("4. A WITHDRAWN ESCALATION DOES NOT GAG THE ALERT FOREVER")
    print("=" * 74)
    # The machine is still in Breakdown. Someone cancelled the earlier
    # escalation; the fault did not go away with it.
    r1 = core_routes.create_escalations_from_smart_alerts(db, USER)
    titles = [e.title for e in db.query(models.Escalation)
              .filter(models.Escalation.title == BREAKDOWN_TITLE).all()]
    check(f"the recurring breakdown is escalated again (created={r1.get('created')})",
          r1.get("created") == 1, str(r1))
    check("...so there are now two rows for that title: the withdrawn one and the live one",
          len(titles) == 2, str(titles))
    # And the dedup still works: the one just created IS open.
    r2 = core_routes.create_escalations_from_smart_alerts(db, USER)
    check(f"a second run raises nothing — the live one dedups (created={r2.get('created')})",
          r2.get("created") == 0, str(r2))

    print()
    print("=" * 74)
    print("5. THE RULE ITSELF")
    print("=" * 74)
    check("'resolved' is terminal", escalations._is_closed("Resolved"))
    check("'cancelled' is terminal — a withdrawn escalation is off the queue",
          escalations._is_closed("Cancelled"))
    check("'In Progress' is NOT terminal — work in flight is still open",
          not escalations._is_closed("In Progress"))
    check("a NULL status is NOT terminal (the #295/#298 convention)",
          not escalations._is_closed(None))
    check("case and whitespace do not decide it",
          escalations._is_closed("  RESOLVED  "))
    check("'canceled' (one l) is terminal too — vocabulary drift must not "
          "silently reopen a closed item",
          escalations._is_closed("canceled"))
    # The SQL clause must select exactly what _is_closed() rejects, or the
    # divergence just moves from four spellings to two.
    rows = db.query(models.Escalation).filter(escalations.open_clause()).all()
    py = [e for e in db.query(models.Escalation).all() if not escalations._is_closed(e.status)]
    check(f"open_clause() selects row-for-row what _is_closed() rejects "
          f"({len(rows)} vs {len(py)})",
          {e.id for e in rows} == {e.id for e in py},
          f"sql={sorted(e.id for e in rows)} python={sorted(e.id for e in py)}")
    check("'In Progress' is in the maintenance open set",
          "In Progress" in __import__("ai.maintenance", fromlist=["x"]).OPEN_STATUSES)

    print()
    print("=" * 74)
    print("6. A REAL NULL STATUS IS STILL OPEN EVERYWHERE")
    print("=" * 74)
    # Escalation.status is Column(String, default="Open"), so a NULL has to be
    # written in SQL — constructing the object with status=None stores "Open"
    # and the case is never exercised (the trap from #407/#562).
    db.execute(text("UPDATE escalations SET status = NULL WHERE title = :t"),
               {"t": "Old fault - PRESS-01"})
    db.commit()
    assert db.execute(text("SELECT status FROM escalations WHERE title = 'Old fault - PRESS-01'")
                      ).scalar() is None, "fixture failed to store a real NULL"
    cc2 = analytics_routes.get_factory_command_center(db, USER)
    ho2 = handover.build_handover(db, T)
    check(f"the NULL-status row counts as open in the command centre "
          f"({cc['open_escalations']} -> {cc2['open_escalations']})",
          cc2["open_escalations"] == cc["open_escalations"] + 2,   # +NULL +the new live one
          str(cc2["open_escalations"]))
    check("...and in the handover, still agreeing",
          ho2["open_work"]["open_escalations"] == cc2["open_escalations"],
          f"handover={ho2['open_work']['open_escalations']} cc={cc2['open_escalations']}")

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


def test_open_escalation_one_rule():
    """The pytest entry point.

    CI runs this file as `python test_X.py` (conftest.py explains why that mode
    is the contract), but the coverage job runs pytest, and pytest collects
    module-level ``test_*`` functions — nothing else. A suite that exposes only
    ``main()`` therefore runs in the `backend` job and contributes NOTHING to the
    coverage measurement: the code it exercises is counted as untested, so
    adding a well-tested module can push the floor DOWN. Three lines restore the
    measurement without changing how the suite reads or runs.
    """
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
