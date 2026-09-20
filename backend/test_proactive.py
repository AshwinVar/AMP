"""AMP speaks first, and mostly decides not to (ADR-0031).

The moment a product notifies people, its real problem stops being detection and
becomes restraint: an alert nobody reads is worse than no alert, because it
trains a plant to ignore the next one. So what is pinned here is the SUPPRESSION,
not the sending.

  1. THE BAR. Only three things interrupt: a machine stopped right now, a risk
     the radar calls LIKELY, and an approved action whose metric came back
     WORSE. A POSSIBLE risk, a WATCH, a ranked-but-not-live problem and an
     outcome that improved are all held back — and each says why.
  2. NOTHING IS SILENTLY DROPPED. Everything considered appears in either
     `qualified` or `suppressed`, with its reason, and the counts reconcile.
  3. THE COOLDOWN IS REAL. Sending twice in a row sends nothing the second
     time, because a signature is stable across runs.
  4. THE CAP IS REPORTED. More qualified findings than the cap leaves the rest
     in `suppressed` as OVER THE CAP, never missing.
  5. THE READ WRITES NOTHING. GET /proactive is computed; only send writes.
  6. CRITICAL GOES FIRST when the cap has to choose.
  7. TENANT ISOLATION: a signature is never shared between factories.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_proactive.py
"""
import sys
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
from ai import proactive as pa
from copilot_eval import fixtures as F
from database import Base

failures = []
NOW = datetime(2026, 9, 20, 9, 0, 0)


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    Session = sessionmaker(bind=engine)
    F.seed(Session)
    return Session


def within(Session, tenant, fn):
    db = Session()
    tok = tenancy.set_current_tenant(tenant)
    try:
        return fn(db)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def seed_outcomes(Session, tenant):
    """One outcome per verdict AMP can reach, so every branch of the outcome
    rule is exercised: WORSE must interrupt, BETTER must not, and one still
    inside its window must not be judged at all."""
    def build(db):
        machine = db.query(models.Machine).filter(
            models.Machine.tenant_code == tenant).order_by(models.Machine.id).first()
        rows = [("WORSE", 40.0, 120.0, NOW - timedelta(days=8)),
                ("BETTER", 120.0, 20.0, NOW - timedelta(days=8)),
                (None, 90.0, None, NOW - timedelta(days=1))]
        for verdict, before, after, at in rows:
            action = models.AgentAction(
                tenant_code=tenant, agent="maintenance", action_type="open_task",
                summary=f"Service after {verdict or 'a pending'} outcome",
                ref_kind="maintenance_task", ref_id=1,
                related_machine_id=machine.id if machine else None,
                status="Approved", decided_at=at)
            db.add(action)
            db.flush()
            db.add(models.ActionOutcome(
                tenant_code=tenant, action_id=action.id, metric="downtime_minutes",
                scope_kind="machine", scope_id=machine.id if machine else None,
                scope_label=machine.name if machine else None, window_days=7,
                baseline_value=before, baseline_at=at,
                measured_value=after,
                measured_at=(at + timedelta(days=7)) if verdict else None,
                verdict=verdict))
        db.commit()
    within(Session, tenant, build)


def notifications(Session, tenant):
    return within(Session, tenant, lambda db: [
        (n.notification_type, n.title, n.severity)
        for n in db.query(models.Notification)
        .filter(models.Notification.tenant_code == tenant).all()])


def main_():
    Session = session()
    # Outcomes exist in FACTORY_A only, so the outcome rule is exercised in one
    # place and its ABSENCE elsewhere stays a real case too.
    seed_outcomes(Session, F.A)
    plans = {t: within(Session, t, lambda db, t=t: pa.build_proactive(db, t, now=NOW))
             for t in F.TENANTS}

    print("\n1. Everything considered is accounted for")
    for t, p in plans.items():
        check(f"{t}: qualified + suppressed == considered",
              len(p["qualified"]) + len(p["suppressed"]) == p["considered"],
              f"{len(p['qualified'])} + {len(p['suppressed'])} != {p['considered']}")
        check(f"{t}: every suppression carries a reason from the published list",
              all(s["suppressed_by"] in pa.SUPPRESSIONS for s in p["suppressed"]),
              str({s.get("suppressed_by") for s in p["suppressed"]}))
        check(f"{t}: every suppression says why in words",
              all(len(s.get("why") or "") > 10 for s in p["suppressed"]))
        check(f"{t}: the bar is on the payload", "interrupts for three things only" in p["bar"])

    print("\n2. The bar: only three things interrupt")
    b = plans[F.B]
    check("FACTORY_B has something worth saying", bool(b["qualified"]), b["headline"])
    for q in b["qualified"]:
        live = q["why"] == "a machine is stopped right now"
        likely = q["why"] == "the radar calls this LIKELY"
        worse = "worse number" in q["why"]
        check(f"qualified: {q['title'][:44]}", live or likely or worse, q["why"])
    for s in b["suppressed"]:
        if s["suppressed_by"] != pa.BELOW_THE_BAR:
            continue
        check(f"held back: {s['title'][:44]}",
              "not happening this second" in s["why"] or "does not interrupt" in s["why"], s["why"])
    # A POSSIBLE or WATCH risk must never qualify.
    for p in plans.values():
        for q in p["qualified"]:
            check(f"no POSSIBLE/WATCH risk qualified ({q['signature']})",
                  "POSSIBLE" not in q["why"] and "WATCH" not in q["why"], q["why"])

    print("\n2b. The outcome rule: only a WORSE one interrupts")
    a = plans[F.A]
    outcome_items = [c for c in a["qualified"] + a["suppressed"]
                     if c["signature"].startswith("outcome:")]
    check("the outcome branch produced candidates at all", len(outcome_items) == 2,
          f"{len(outcome_items)} (the one still inside its window must not be a candidate)")
    raised = [c for c in a["qualified"] if c["signature"].startswith("outcome:")]
    check("exactly one outcome is raised", len(raised) == 1, str([c["title"] for c in raised]))
    check("...and it is the one that got WORSE", "got worse" in raised[0]["title"], raised[0]["title"])
    check("...for the stated reason",
          raised[0]["why"] == "an action AMP recommended was followed by a worse number",
          raised[0]["why"])
    improved = [c for c in a["suppressed"] if c["signature"].startswith("outcome:")]
    check("the one that improved is held back", len(improved) == 1, str(len(improved)))
    check("...saying BETTER does not interrupt", "BETTER" in improved[0]["why"], improved[0]["why"])
    check("nothing still inside its window is a candidate at all",
          all("pending" not in c["title"] for c in outcome_items),
          str([c["title"] for c in outcome_items]))

    print("\n3. A quiet factory says it is quiet, and what it held back")
    for t in (F.C,):
        p = plans[t]
        check(f"{t}: nothing qualified", not p["qualified"],
              str([q["title"] for q in p["qualified"]]))
        check(f"{t}: and it says how many it considered", "considered and held back" in p["headline"],
              p["headline"])
        check(f"{t}: it did not claim the plant is fine", "all good" not in p["headline"].lower()
              and "no issues" not in p["headline"].lower(), p["headline"])

    print("\n4. The read writes nothing")
    before = {t: len(notifications(Session, t)) for t in F.TENANTS}
    for t in F.TENANTS:
        within(Session, t, lambda db, t=t: pa.build_proactive(db, t, now=NOW))
    after = {t: len(notifications(Session, t)) for t in F.TENANTS}
    check("building the plan created no notification anywhere", before == after,
          f"{before} -> {after}")

    print("\n5. Sending, and the cooldown")
    rows_before = len(notifications(Session, F.B))
    first = within(Session, F.B, lambda db: pa.send_proactive(db, F.B, now=NOW))
    check("the first send writes what qualified", first["sent"] == len(b["qualified"]),
          f"{first['sent']} != {len(b['qualified'])}")
    # The COUNT it reports must be the count it wrote. A send that also wrote
    # everything it said it was holding back would still return the right
    # number, and only this check would notice.
    rows_after = len(notifications(Session, F.B))
    check("...and writes NOTHING it said it was holding back",
          rows_after - rows_before == first["sent"],
          f"{rows_after - rows_before} rows written, {first['sent']} reported")
    written = notifications(Session, F.B)
    check("every notification carries its signature in the type",
          all(k.startswith(f"{pa.TYPE_PREFIX}:") for k, _t, _s in written), str(written[:2]))
    second = within(Session, F.B, lambda db: pa.send_proactive(db, F.B, now=NOW + timedelta(hours=1)))
    check("an hour later it sends NOTHING: same signatures, inside the cooldown",
          second["sent"] == 0, str(second))
    plan2 = within(Session, F.B, lambda db: pa.build_proactive(db, F.B, now=NOW + timedelta(hours=1)))
    check("...and the plan says SAID RECENTLY for each of them",
          sum(1 for s in plan2["suppressed"] if s["suppressed_by"] == pa.SAID_RECENTLY)
          == first["sent"],
          str([s["suppressed_by"] for s in plan2["suppressed"]]))
    check("...naming the cooldown in the reason",
          all(f"{pa.COOLDOWN_HOURS} hours" in s["why"]
              for s in plan2["suppressed"] if s["suppressed_by"] == pa.SAID_RECENTLY))

    after_cooldown = NOW + timedelta(hours=pa.COOLDOWN_HOURS + 1)
    plan3 = within(Session, F.B, lambda db: pa.build_proactive(db, F.B, now=after_cooldown))
    check("once the cooldown has passed the same thing may be raised again",
          bool(plan3["qualified"]),
          str([s["suppressed_by"] for s in plan3["suppressed"]]))

    print("\n6. The cap, and Critical first")
    saved = pa.MAX_PER_RUN
    pa.MAX_PER_RUN = 1
    try:
        capped = within(Session, F.B, lambda db: pa.build_proactive(db, F.B, now=after_cooldown))
        check("only one thing is sent when the cap is one", len(capped["qualified"]) == 1,
              str(len(capped["qualified"])))
        check("and it is the Critical one", capped["qualified"][0]["severity"] == "Critical",
              capped["qualified"][0]["severity"])
        over = [s for s in capped["suppressed"] if s["suppressed_by"] == pa.OVER_THE_CAP]
        check("the rest are reported as OVER THE CAP, not dropped", bool(over), str(len(over)))
        check("nothing vanished: the counts still reconcile",
              len(capped["qualified"]) + len(capped["suppressed"]) == capped["considered"])
        check("the cap is named in the reason", all("more than 1" in s["why"] for s in over))
    finally:
        pa.MAX_PER_RUN = saved

    print("\n7. Tenant isolation")
    within(Session, F.A, lambda db: pa.send_proactive(db, F.A, now=NOW))
    b_types = {k for k, _t, _s in notifications(Session, F.B)}
    a_types = {k for k, _t, _s in notifications(Session, F.A)}
    # A now has one thing worth raising — the outcome that got worse — so it
    # writes exactly that, and nothing of B's.
    check("A wrote exactly its own one qualified thing",
          a_types == {f"{pa.TYPE_PREFIX}:{c['signature']}" for c in plans[F.A]["qualified"]},
          str(a_types))
    check("A's signatures and B's do not overlap", not (a_types & b_types),
          str(a_types & b_types))
    for t in F.TENANTS:
        p = within(Session, t, lambda db, t=t: pa.build_proactive(db, t, now=after_cooldown))
        for other in F.TENANTS:
            if other != t:
                check(f"{t}: its plan never names {other}", other not in repr(p))
    check("B's own signatures are still only B's",
          all(k.startswith(f"{pa.TYPE_PREFIX}:") for k in b_types))

    print("\n8. Nothing here claims to have acted")
    for t, p in plans.items():
        blob = repr(p).lower()
        for word in ("fixed", "resolved it", "amp will handle", "automatically ordered"):
            check(f"{t}: the plan does not say \"{word}\"", word not in blob)

    print("\n9. The Copilot reports the restraint too")
    from ai.tools import registry as treg
    r = within(Session, F.B, lambda db: treg.run_tool(
        db, treg.Principal(tenant=F.B, role="Admin"), "get_what_to_raise"))
    check("the tool answers", r.state in ("OK", "NO DATA"), f"{r.state}: {r.summary}")
    check("its sentence carries the bar", "interrupts for three things only" in r.summary,
          r.summary)
    plan_now = within(Session, F.B, lambda db: pa.build_proactive(db, F.B, now=NOW))
    held = [f for f in r.facts if f.key == "raise.held_back"]
    check("how much it held back is a fact", len(held) == 1)
    check("...and it is the real number", held and held[0].value == len(plan_now["suppressed"]),
          f"{held and held[0].value} != {len(plan_now['suppressed'])}")
    considered = [f for f in r.facts if f.key == "raise.considered"]
    check("what it considered is a fact too",
          considered and considered[0].value == plan_now["considered"])
    qualified_fact = [f for f in r.facts if f.key == "raise.qualified"]
    check("and the two sides still reconcile in the facts",
          (qualified_fact and held
           and qualified_fact[0].value + held[0].value == considered[0].value),
          f"{qualified_fact and qualified_fact[0].value} + {held and held[0].value}")

    print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'All checks passed'}")
    for f in failures:
        print("  -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main_())
