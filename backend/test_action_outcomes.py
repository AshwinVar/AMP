"""Did it help? — and what AMP is not allowed to claim about the answer (ADR-0029).

This is the first thing AMP has ever built that looks BACK at its own
recommendations, and it is also the easiest place in the product to overclaim.
An uncontrolled before-and-after cannot show causation, so what is pinned here
is as much about the sentences as the arithmetic.

  1. THE BASELINE IS FROZEN AT APPROVAL, from the window before the decision,
     and a rejection freezes nothing: a rejected action changed nothing, so
     there is nothing to follow up.
  2. NOTHING IS JUDGED EARLY. Inside the window there is no verdict at all —
     not a provisional one — and the card says how many days are left.
  3. A READING IS FROZEN ONCE. After the window it is measured and never
     recomputed: the source rows are changed underneath it and the recorded
     value does not move.
  4. NULL IS NOT ZERO. A missing reading on either side gives NOT MEASURABLE,
     never an improvement out of nothing.
  5. THE NOISE FLOOR IS REAL AND TESTED AT ITS EDGE, both the 10% and the
     per-metric absolute floor.
  6. AN APPROVAL IS NEVER BLOCKED BY THE FOLLOW-UP. With the outcome recorder
     raising, the decision still stands and the item still advances.
  7. NO CAUSAL CLAIM ANYWHERE. Every change is CORRELATION, the caveat travels
     with it, and the payload may not contain "because", "caused", "proves",
     "thanks to" or a confidence figure.
  8. ONE ROW PER ACTION, enforced by the database, not only by the code.
  9. TENANT ISOLATION on every read.
 10. THE COPILOT says the same thing, grounded in its own facts.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_action_outcomes.py
"""
import re
import sys
from datetime import date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
from ai import agents, grounding
from ai import outcomes as oc
from ai.tools import registry as treg
from database import Base

failures = []
A, B = "OUTCOME_A", "OUTCOME_B"
# WALL-CLOCK, on purpose. record_baseline reads its own window from
# datetime.utcnow(), so a fixed timestamp here would have to be patched in —
# and the test would then be measuring the patch. Anchoring the fixture to the
# real clock lets record_baseline pick its own window, which is the behaviour a
# mutation reading the wrong side of the decision has to break.
NOW = datetime.utcnow()


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    return sessionmaker(bind=engine), engine


def seed(Session, tenant):
    """One machine with stoppages, one item, in one tenant."""
    db = Session()
    tok = tenancy.set_current_tenant(tenant)
    try:
        m = models.Machine(tenant_code=tenant, name="CNC-01", status="Running", utilization=70,
                           line="L1", downtime="0 min")
        item = models.InventoryItem(tenant_code=tenant, item_code="RM-1", item_name="Steel bar",
                                    category="Raw", unit="kg", current_stock=40, reorder_level=100)
        db.add_all([m, item])
        db.commit()
        return m.id, item.id
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def downtime(db, tenant, machine_id, minutes, at):
    db.add(models.DowntimeLog(tenant_code=tenant, machine_id=machine_id,
                              reason="Breakdown", duration=f"{minutes} min", created_at=at))


def approve(db, tenant, ref_kind, ref_id, machine_id=None, at=NOW, summary="do the thing"):
    """Propose and approve an action the way the product does, and return it."""
    # created_at is left to default. approvals.authorise refuses a proposal whose
    # item was created AFTER it ("a maintenance task created after the proposal
    # now has its id"), and the fixture's item is written microseconds before
    # this action — so pinning created_at to NOW would fail that freshness check
    # rather than test anything about outcomes.
    action = models.AgentAction(tenant_code=tenant, agent="maintenance", action_type="open_task",
                                summary=summary, ref_kind=ref_kind, ref_id=ref_id,
                                related_machine_id=machine_id, status="Proposed")
    db.add(action)
    db.flush()
    agents.apply_decision(db, action, "approve", decided_by="alice", require_actor=False)
    action.decided_at = at
    # baseline_value is DELIBERATELY left as record_baseline computed it — the
    # helper must not do the work under test. Only baseline_at is pinned, so the
    # "after" window below is a known length from a known instant.
    row = db.query(models.ActionOutcome).filter(models.ActionOutcome.action_id == action.id).first()
    if row is not None:
        row.baseline_at = at
    db.commit()
    # The id, not the instance: `within` closes the session, and a detached ORM
    # object refreshes on the next attribute read.
    return action.id


def within(Session, tenant, fn):
    db = Session()
    tok = tenancy.set_current_tenant(tenant)
    try:
        return fn(db)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def main_():
    Session, engine = session()
    machine_a, item_a = seed(Session, A)
    machine_b, _item_b = seed(Session, B)

    print("\n1. The baseline is frozen at approval, from the window before it")
    def build_a(db):
        # 90 minutes of stoppage in the week before the decision, and one older
        # log outside the window that must NOT be counted.
        downtime(db, A, machine_a, 60, NOW - timedelta(days=2))
        downtime(db, A, machine_a, 30, NOW - timedelta(days=6))
        downtime(db, A, machine_a, 999, NOW - timedelta(days=30))
        task = models.MaintenanceTask(tenant_code=A, task_no="MT-1", machine_id=machine_a,
                                      task_type="Preventive", assigned_to="bob",
                                      planned_date=date(2026, 9, 21), status="Proposed")
        db.add(task)
        db.flush()
        db.commit()
        return approve(db, A, "maintenance_task", task.id, machine_a, summary="Service CNC-01")
    action_a_id = within(Session, A, build_a)

    row = within(Session, A, lambda db: db.query(models.ActionOutcome)
                 .filter(models.ActionOutcome.action_id == action_a_id).first())
    check("an approved maintenance action is followed up", row is not None)
    check("the metric is the one the action was meant to move",
          row.metric == "downtime_minutes", row.metric if row else "")
    check("the baseline is the window BEFORE the decision (90, not 1089)",
          row.baseline_value == 90.0, str(row.baseline_value if row else None))
    check("the scope is the machine, by id and by the name it had then",
          row.scope_kind == "machine" and row.scope_id == machine_a and row.scope_label == "CNC-01")
    check("nothing is measured yet", row.measured_at is None and row.verdict is None)

    print("\n2. A rejection follows up nothing")
    def reject(db):
        task = models.MaintenanceTask(tenant_code=A, task_no="MT-2", machine_id=machine_a,
                                      task_type="Preventive", assigned_to="bob",
                                      planned_date=date(2026, 9, 21), status="Proposed")
        db.add(task)
        db.flush()
        action = models.AgentAction(tenant_code=A, agent="maintenance", action_type="open_task",
                                    summary="no", ref_kind="maintenance_task", ref_id=task.id,
                                    related_machine_id=machine_a, status="Proposed")
        db.add(action)
        db.flush()
        agents.apply_decision(db, action, "reject", decided_by="alice", require_actor=False)
        db.commit()
        return action.id
    rejected_id = within(Session, A, reject)
    check("a rejected action has no outcome row",
          within(Session, A, lambda db: db.query(models.ActionOutcome)
                 .filter(models.ActionOutcome.action_id == rejected_id).count()) == 0)

    print("\n3. Every approvable kind of action is followed up, and nothing else is")
    # The approval gate itself refuses a ref_kind it does not know
    # (approvals.PENDING), so an unknown kind can never reach record_baseline
    # through the product. What CAN happen is someone adding a fourth kind to
    # that registry and not to METRICS — after which AMP would approve work and
    # then silently never look at it again. That is what this asserts.
    import approvals
    missing = sorted(set(approvals.PENDING) - set(oc.METRICS))
    check("every kind a human can approve has a metric to follow up", not missing,
          f"{missing} can be approved but would never be measured")
    check("and no metric is declared for a kind nobody can approve",
          not sorted(set(oc.METRICS) - set(approvals.PENDING)))
    check("every metric has a full specification",
          all(m in oc.METRIC_SPEC for m in oc.METRICS.values()))
    check("and every specification says which way is good",
          all(s["better"] in ("lower", "higher") for s in oc.METRIC_SPEC.values()))

    def unknown_kind(db):
        action = models.AgentAction(tenant_code=A, agent="other", action_type="notify",
                                    summary="fyi", ref_kind="notification", ref_id=1,
                                    status="Proposed")
        db.add(action)
        db.flush()
        return oc.record_baseline(db, A, action)
    check("record_baseline declines a kind it has no metric for",
          within(Session, A, unknown_kind) is None,
          "AMP would rather record nothing than watch a number the action has no claim on")

    print("\n4. Nothing is judged before the window has elapsed")
    early = within(Session, A, lambda db: oc.build_outcome_summary(
        db, A, now=NOW + timedelta(days=oc.WINDOW_DAYS - 1)))
    check("the summary state is INSUFFICIENT HISTORY", early["state"] == "INSUFFICIENT HISTORY",
          early["state"])
    check("the row is marked as waiting", early["outcomes"][0]["waiting"] is True)
    check("with no verdict at all, not a provisional one", early["outcomes"][0]["verdict"] is None)
    check("and it says how many days are left", early["outcomes"][0]["days_left"] == 1,
          str(early["outcomes"][0]["days_left"]))
    check("the headline does not claim anything about the action",
          "helped" not in early["headline"] and "worked" not in early["headline"], early["headline"])
    check("nothing was frozen in the database",
          within(Session, A, lambda db: db.query(models.ActionOutcome)
                 .filter(models.ActionOutcome.measured_at.isnot(None)).count()) == 0)

    print("\n5. After the window: measured once, and never recomputed")
    def add_after(db):
        # 20 minutes in the window after the decision: a large improvement.
        downtime(db, A, machine_a, 20, NOW + timedelta(days=1))
        db.commit()
    within(Session, A, add_after)
    after = NOW + timedelta(days=oc.WINDOW_DAYS, seconds=1)
    summary = within(Session, A, lambda db: oc.build_outcome_summary(db, A, now=after))
    view = summary["outcomes"][0]
    check("the reading after the decision is the window after it (20 min)",
          view["measured_value"] == 20.0, str(view["measured_value"]))
    check("the change is stated as a figure", view["change"] == -70.0, str(view["change"]))
    check("the verdict is BETTER for a metric where lower is better", view["verdict"] == oc.BETTER,
          str(view["verdict"]))
    check("the summary is now OK", summary["state"] == "OK", summary["state"])
    check("and it counts what it measured", summary["measured"] == 1 and summary["waiting"] == 0)

    def rewrite_history(db):
        downtime(db, A, machine_a, 500, NOW + timedelta(days=2))
        db.commit()
    within(Session, A, rewrite_history)
    again = within(Session, A, lambda db: oc.build_outcome_summary(db, A, now=after + timedelta(days=3)))
    check("a frozen reading is not recomputed when the source data changes",
          again["outcomes"][0]["measured_value"] == 20.0, str(again["outcomes"][0]["measured_value"]))
    check("...and its verdict does not move either", again["outcomes"][0]["verdict"] == oc.BETTER)

    print("\n5b. A machine that stopped breaking entirely reads as zero, not as no reading")
    def clean_machine(db):
        m = models.Machine(tenant_code=A, name="CNC-09", status="Running", utilization=70,
                           line="L1", downtime="0 min")
        db.add(m)
        db.flush()
        downtime(db, A, m.id, 120, NOW - timedelta(days=3))       # before the decision
        task = models.MaintenanceTask(tenant_code=A, task_no="MT-9", machine_id=m.id,
                                      task_type="Preventive", assigned_to="bob",
                                      planned_date=date(2026, 9, 21), status="Proposed")
        db.add(task)
        db.flush()
        db.commit()
        # ...and NOTHING logged afterwards.
        return approve(db, A, "maintenance_task", task.id, m.id, summary="Service CNC-09")
    clean_id = within(Session, A, clean_machine)
    clean = within(Session, A, lambda db: oc.build_outcome_summary(db, A, now=after))
    row9 = next(o for o in clean["outcomes"] if o["action_id"] == clean_id)
    check("no stoppage after the decision is a measured 0, not a missing reading",
          row9["measured_value"] == 0.0, str(row9["measured_value"]))
    check("...so the verdict is BETTER, not NOT MEASURABLE", row9["verdict"] == oc.BETTER,
          str(row9["verdict"]))
    check("...and the change is the whole baseline", row9["change"] == -120.0, str(row9["change"]))

    print("\n6. A broken follow-up never blocks a decision a person has made")
    real = oc.record_baseline
    def boom(*a, **k):
        raise RuntimeError("the follow-up is broken")
    agents.outcomes.record_baseline = boom
    try:
        def approve_with_broken_followup(db):
            task = models.MaintenanceTask(tenant_code=A, task_no="MT-3", machine_id=machine_a,
                                          task_type="Preventive", assigned_to="bob",
                                          planned_date=date(2026, 9, 21), status="Proposed")
            db.add(task)
            db.flush()
            action = models.AgentAction(tenant_code=A, agent="maintenance", action_type="open_task",
                                        summary="still approve me", ref_kind="maintenance_task",
                                        ref_id=task.id, related_machine_id=machine_a, status="Proposed")
            db.add(action)
            db.flush()
            agents.apply_decision(db, action, "approve", decided_by="alice", require_actor=False)
            db.commit()
            return action.status, task.status, action.id
        status, task_status, broken_id = within(Session, A, approve_with_broken_followup)
        check("the action is still Approved", status == "Approved", status)
        check("and its task still advanced to Open", task_status == "Open", task_status)
        check("and simply has no outcome row",
              within(Session, A, lambda db: db.query(models.ActionOutcome)
                     .filter(models.ActionOutcome.action_id == broken_id).count()) == 0)
    finally:
        agents.outcomes.record_baseline = real

    print("\n7. The verdicts, and the noise floor at its edge")
    # downtime: lower is better, floor 5 minutes, 10% of the baseline.
    cases = [
        ("downtime 100 -> 80 is BETTER", "downtime_minutes", 100.0, 80.0, oc.BETTER),
        ("downtime 100 -> 120 is WORSE", "downtime_minutes", 100.0, 120.0, oc.WORSE),
        ("downtime 100 -> 91 is inside 10%, so NO CHANGE", "downtime_minutes", 100.0, 91.0, oc.NO_CHANGE),
        ("downtime 100 -> 89 is outside 10%, so BETTER", "downtime_minutes", 100.0, 89.0, oc.BETTER),
        ("downtime 10 -> 6 is inside the 5-minute floor", "downtime_minutes", 10.0, 6.0, oc.NO_CHANGE),
        ("downtime 10 -> 4 is outside it", "downtime_minutes", 10.0, 4.0, oc.BETTER),
        ("downtime 0 -> 0 is NO CHANGE, not an improvement", "downtime_minutes", 0.0, 0.0, oc.NO_CHANGE),
        ("downtime 0 -> 30 is WORSE", "downtime_minutes", 0.0, 30.0, oc.WORSE),
        ("stock 40 -> 200 is BETTER (higher is better)", "stock_on_hand", 40.0, 200.0, oc.BETTER),
        ("stock 200 -> 40 is WORSE", "stock_on_hand", 200.0, 40.0, oc.WORSE),
        ("no reading before is NOT MEASURABLE", "downtime_minutes", None, 10.0, oc.NOT_MEASURABLE),
        ("no reading after is NOT MEASURABLE", "downtime_minutes", 10.0, None, oc.NOT_MEASURABLE),
        ("no reading at all is NOT MEASURABLE", "stock_on_hand", None, None, oc.NOT_MEASURABLE),
    ]
    for label, metric, before, value, want in cases:
        got = oc.judge(metric, before, value)
        check(label, got == want, f"{got} != {want}")
    check("a missing reading is never read as an improvement",
          oc.judge("downtime_minutes", None, 0.0) == oc.NOT_MEASURABLE)
    check("every verdict is in the published list", set(
        oc.judge(m, b, v) for _l, m, b, v, _w in cases) <= set(oc.VERDICTS))

    print("\n8. One outcome row per action, enforced by the database")
    def duplicate(db):
        first = db.query(models.ActionOutcome).order_by(models.ActionOutcome.id).first()
        db.add(models.ActionOutcome(tenant_code=A, action_id=first.action_id, metric="downtime_minutes",
                                    scope_kind="machine", scope_id=machine_a, window_days=7,
                                    baseline_value=1.0, baseline_at=NOW))
        try:
            db.commit()
            return False
        except IntegrityError:
            db.rollback()
            return True
    check("a second outcome for the same action is refused by the database",
          within(Session, A, duplicate))
    def rerecord(db):
        action = db.query(models.AgentAction).filter(models.AgentAction.id == action_a_id).first()
        return oc.record_baseline(db, A, action).action_id
    check("and record_baseline returns the existing row rather than making one",
          within(Session, A, rerecord) == action_a_id)

    print("\n9. No causal claim anywhere")
    full = within(Session, A, lambda db: oc.build_outcome_summary(db, A, now=after + timedelta(days=3)))
    blob = repr(full).lower()
    for word in ("because", "proves", "thanks to", "due to the action", "confidence"):
        check(f'the payload does not say "{word}"', word not in blob)
    # "caused" DOES appear — inside "cannot show the action caused the change".
    # The rule is not that the word is absent; it is that every occurrence is a
    # denial. Same shape as the "machine learning" check in ADR-0027's suite.
    unnegated = [m.start() for m in re.finditer("caused", blob)
                 if "cannot show the action caused" not in blob[max(0, m.start() - 34):m.start() + 6]]
    check("every use of \"caused\" is a denial", not unnegated,
          str([blob[max(0, i - 60):i + 20] for i in unnegated[:2]]))
    check("the caveat is on the card", "cannot show the action caused" in full["note"], full["note"])
    change_facts = [f for row in full["outcomes"] for f in row["facts"] if f["key"].endswith(".change")]
    check("every change figure is labelled CORRELATION", change_facts and all(
        f["provenance"] == "CORRELATION" for f in change_facts),
        str([f["provenance"] for f in change_facts]))
    check("and carries the caveat in its own detail",
          all("cannot show the action caused" in (f["detail"] or "") for f in change_facts))
    before_facts = [f for row in full["outcomes"] for f in row["facts"] if f["key"].endswith(".baseline")]
    check("the readings themselves are MEASURED FACTs",
          all(f["provenance"] in ("MEASURED FACT", "UNKNOWN") for f in before_facts))
    check("no fact claims to be a model estimate",
          all(f["provenance"] != "MODEL ESTIMATE"
              for row in full["outcomes"] for f in row["facts"]))

    print("\n10. Tenant isolation")
    def seed_b(db):
        downtime(db, B, machine_b, 45, NOW - timedelta(days=1))
        task = models.MaintenanceTask(tenant_code=B, task_no="MT-B", machine_id=machine_b,
                                      task_type="Preventive", assigned_to="carol",
                                      planned_date=date(2026, 9, 21), status="Proposed")
        db.add(task)
        db.flush()
        db.commit()
        return approve(db, B, "maintenance_task", task.id, machine_b, summary="Service B's CNC-01")
    within(Session, B, seed_b)
    a_view = within(Session, A, lambda db: oc.build_outcome_summary(db, A, now=after))
    b_view = within(Session, B, lambda db: oc.build_outcome_summary(db, B, now=after))
    a_actions = {o["action"] for o in a_view["outcomes"]}
    b_actions = {o["action"] for o in b_view["outcomes"]}
    check("A does not see B's action", "Service B's CNC-01" not in repr(a_view), str(a_actions))
    check("B does not see A's action", "Service CNC-01" not in repr(b_view), str(b_actions))
    check("B's baseline is measured from B's own stoppages",
          b_view["outcomes"][0]["baseline_value"] == 45.0,
          str(b_view["outcomes"][0]["baseline_value"]))

    print("\n11. The Copilot says the same thing, grounded")
    r = within(Session, A, lambda db: treg.run_tool(
        db, treg.Principal(tenant=A, role="Admin"), "get_action_outcomes"))
    check("the tool answers", r.state in ("OK", "NO DATA", "INSUFFICIENT HISTORY"), f"{r.state}: {r.summary}")
    check("its sentence carries the caveat", "cannot show the action caused" in r.summary, r.summary)
    g = grounding.check(r.summary, r.to_dict()["facts"], question="did it help?")
    check("every figure in the sentence is in its evidence", g.passed,
          f"{r.summary} :: {g.ungrounded_numbers} {g.unknown_identifiers}")
    change = [f for f in r.facts if f.key.startswith("outcomes.change.")]
    check("the change facts are CORRELATION here too",
          all(f.provenance == "CORRELATION" for f in change), str([f.provenance for f in change]))
    for word in ("because", "proves", "guarantee"):
        check(f'the tool does not say "{word}"', word not in r.summary.lower())
    # A workspace with nothing followed up must say NO DATA, not OK: "of 0
    # followed up, 0 got better" reads as a clean bill rather than an absence.
    empty = within(Session, "OUTCOME_EMPTY", lambda db: treg.run_tool(
        db, treg.Principal(tenant="OUTCOME_EMPTY", role="Admin"), "get_action_outcomes"))
    check("a workspace with no followed-up action says NO DATA", empty.state == "NO DATA",
          f"{empty.state}: {empty.summary}")
    check("...and says so in words rather than reporting zeros",
          "has been followed up yet" in empty.summary, empty.summary)
    said = r.summary.lower()
    check("the tool's only use of \"caused\" is a denial",
          "caused" not in said or "cannot show the action caused" in said, r.summary)

    print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'All checks passed'}")
    for f in failures:
        print("  -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main_())
