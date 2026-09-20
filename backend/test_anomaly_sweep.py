"""Every machine gets a reason, and an experimental model never gets an alarm (ADR-0032).

The anomaly check has existed since ADR-0020 behind a dropdown. Running it over
the whole fleet is the easy half. The half that matters is what the sweep says
about the machines it could NOT score, and what it refuses to call the ones it
could — because the model was evaluated and **not adopted**.

  1. EVERY MACHINE APPEARS, scored or not. A machine missing from the list
     reads as a machine that was fine, which is a claim AMP has not made.
  2. AN UNSCORED MACHINE SAYS WHY, in the honest-data vocabulary:
     INSUFFICIENT HISTORY, NOT MEASURED or NOT CONFIGURED — never a zero.
  3. NO CONSENT IS THE WHOLE FLEET'S ANSWER. Not an empty list, which would
     read as "nothing unusual"; and the check is asked once, not per machine.
  4. A PREVIEW NEVER RUNS IT, and says so.
  5. NOTHING IS AN ALARM. Every scored row is MODEL NOT VALIDATED with MODEL
     ESTIMATE provenance, and the payload may not contain "alarm", "alert" or
     "fault".
  6. IT RUNS THE SAME CHECK as the single-machine route — the scorer is passed
     in, so this module cannot drift into a second implementation.
  7. TENANT ISOLATION: the sweep reads only this tenant's machines.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_anomaly_sweep.py
"""
import sys
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
from ai import anomaly_sweep as sw
from amp_ai import consent
from amp_ai.core.contracts import (CAPABILITY_TELEMETRY_BASELINE, ConsentDecision,
                                   ConsentGate, ConsentRequired)
from amp_ai.telemetry_anomaly import service
from copilot_eval import fixtures as F
from database import Base

failures = []
NOW = datetime(2026, 9, 20, 9, 0, 0)


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


class Granted(ConsentGate):
    def check(self, db, tenant, capability):
        return ConsentDecision(granted=True, capability=capability, reason="granted in this test",
                               granted_by="alice", granted_at=NOW)


class Refused(ConsentGate):
    """Counts how many times it is asked: the sweep must ask ONCE per fleet."""

    def __init__(self):
        self.asked = 0

    def check(self, db, tenant, capability):
        self.asked += 1
        return ConsentDecision(granted=False, capability=capability,
                               reason="this company has not granted it", granted_by=None,
                               granted_at=None)


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


def scripted(results, seen=None):
    """A scorer that returns a canned result per machine id, honouring the gate."""
    def scorer(db, tenant, machine_id, *, gate, now=None):
        decision = gate.check(db, tenant, CAPABILITY_TELEMETRY_BASELINE)
        if not decision.granted:
            raise ConsentRequired(decision)
        if seen is not None:
            seen.append(machine_id)
        return results.get(machine_id, {"status": "ok", "score": 1.0})
    return scorer


def main_():
    Session = session()
    ids = within(Session, F.B, lambda db: [m.id for m in db.query(models.Machine)
                                           .filter(models.Machine.tenant_code == F.B)
                                           .order_by(models.Machine.id).all()])
    check("the fixture has machines to sweep", len(ids) >= 3, str(len(ids)))

    print("\n1. Every machine appears, scored or not")
    results = {
        ids[0]: {"status": "ok", "score": 4.5},
        ids[1]: {"status": "insufficient_history",
                 "needed": {"distinct_days": 7}, "have": {"distinct_days": 2}},
        ids[2]: {"status": "model_unavailable", "score": None, "reason": "the artifact did not verify"},
    }
    sweep = within(Session, F.B, lambda db: sw.build_anomaly_sweep(
        db, F.B, scorer=scripted(results), gate=Granted(), now=NOW))
    check("every machine is in the list", len(sweep["machines"]) == len(ids),
          f"{len(sweep['machines'])} of {len(ids)}")
    check("the counts reconcile", sweep["scored"] + sweep["not_scored"] == len(ids),
          f"{sweep['scored']} + {sweep['not_scored']}")
    by_id = {r["machine_id"]: r for r in sweep["machines"]}
    check("the scored machine carries its score", by_id[ids[0]]["score"] == 4.5,
          str(by_id[ids[0]]["score"]))

    print("\n2. An unscored machine says why, and is never a zero")
    thin = by_id[ids[1]]
    check("thin history is INSUFFICIENT HISTORY", thin["state"] == "INSUFFICIENT HISTORY", thin["state"])
    check("...with the shortfall in words", "have 2 of 7" in (thin["reason"] or ""), str(thin["reason"]))
    check("...and NO score, not a zero", thin["score"] is None, str(thin["score"]))
    gone = by_id[ids[2]]
    check("an unavailable model is NOT CONFIGURED", gone["state"] == "NOT CONFIGURED", gone["state"])
    check("...and says what happened", "did not verify" in (gone["reason"] or ""), str(gone["reason"]))
    check("...and has no score either", gone["score"] is None)
    check("no unscored machine carries a fact that implies a reading",
          all(not r["facts"] for r in sweep["machines"] if r["score"] is None))

    print("\n3. Scored first, and the order is stable")
    two_scores = {ids[0]: {"status": "ok", "score": 2.0}, ids[1]: {"status": "ok", "score": 9.0},
                  ids[2]: {"status": "ok", "score": 5.0}}
    ranked = within(Session, F.B, lambda db: sw.build_anomaly_sweep(
        db, F.B, scorer=scripted(two_scores), gate=Granted(), now=NOW))
    check("the highest score is first", [r["score"] for r in ranked["machines"][:3]] == [9.0, 5.0, 2.0],
          str([r["score"] for r in ranked["machines"]]))
    check("the headline names the highest", ranked["machines"][0]["name"] in ranked["headline"],
          ranked["headline"])
    mixed = within(Session, F.B, lambda db: sw.build_anomaly_sweep(
        db, F.B, scorer=scripted(results), gate=Granted(), now=NOW))
    scored_first = [r["score"] is not None for r in mixed["machines"]]
    check("every scored row comes before every unscored one",
          scored_first == sorted(scored_first, reverse=True), str(scored_first))

    print("\n4. No consent is the whole fleet's answer, asked once")
    gate = Refused()
    refused = within(Session, F.B, lambda db: sw.build_anomaly_sweep(
        db, F.B, scorer=scripted(results), gate=gate, now=NOW))
    check("the consent gate was asked exactly once, not once per machine", gate.asked == 1,
          f"asked {gate.asked} times for {len(ids)} machines")
    check("the state is NOT CONFIGURED", refused["state"] == "NOT CONFIGURED", refused["state"])
    check("it says learning is off and who can turn it on",
          "Learning from telemetry is off" in refused["headline"]
          and "An Admin can turn it on" in refused["headline"], refused["headline"])
    check("it does NOT return an empty list that reads as 'nothing unusual'",
          refused["consent"] is False and refused["not_scored"] == len(ids),
          f"consent={refused['consent']} not_scored={refused['not_scored']}")
    check("and it claims no scores at all", refused["scored"] == 0 and not refused["machines"])

    print("\n5. A preview never runs it")
    preview = within(Session, F.B, lambda db: sw.build_anomaly_sweep(
        db, F.B, scorer=scripted(results), gate=Granted(), previewing=True, now=NOW))
    check("nothing is scored from a preview", preview["scored"] == 0 and not preview["machines"])
    check("...and it says why", "does not run from a platform preview" in preview["headline"],
          preview["headline"])
    check("...without claiming the plant is fine",
          "normal" not in preview["headline"] and "fine" not in preview["headline"])

    print("\n6. An experimental model is never an alarm")
    for label, payload in (("scored", sweep), ("ranked", ranked), ("refused", refused),
                           ("preview", preview)):
        blob = repr(payload).lower()
        for word in ("alarm", "alert", "fault", "failure detected", "anomaly detected"):
            # "not an alarm" is the one allowed use, in the note and headline.
            allowed = blob.count("not an alarm") + blob.count("never as an alarm")
            check(f"{label}: the payload does not raise an \"{word}\"",
                  blob.count(word) <= (allowed if word == "alarm" else 0),
                  f"{word} x{blob.count(word)}")
    for r in sweep["machines"]:
        if r["score"] is None:
            continue
        check(f"{r['name']}: a scored row is MODEL NOT VALIDATED", r["state"] == "MODEL NOT VALIDATED",
              r["state"])
        check(f"{r['name']}: its score is a MODEL ESTIMATE",
              all(f["provenance"] == "MODEL ESTIMATE" for f in r["facts"]),
              str([f["provenance"] for f in r["facts"]]))
        check(f"{r['name']}: the caveat rides with the score",
              all("experimental" in (f["detail"] or "") for f in r["facts"]))
    check("the note says it did not beat the rules", "did not beat the rules" in sweep["note"],
          sweep["note"])
    check("the whole sweep is MODEL NOT VALIDATED", sweep["state"] == "MODEL NOT VALIDATED",
          sweep["state"])

    print("\n7. Nothing scorable is reported as normal")
    none_scored = within(Session, F.B, lambda db: sw.build_anomaly_sweep(
        db, F.B, scorer=scripted({i: {"status": "insufficient_history", "needed": {}, "have": {}}
                                  for i in ids}), gate=Granted(), now=NOW))
    check("with nothing scored the state is INSUFFICIENT HISTORY",
          none_scored["state"] == "INSUFFICIENT HISTORY", none_scored["state"])
    check("...and it says none is being reported as normal",
          "none of them is being reported as normal" in none_scored["headline"],
          none_scored["headline"])

    print("\n8. It sweeps only this tenant's machines")
    seen = []
    within(Session, F.B, lambda db: sw.build_anomaly_sweep(
        db, F.B, scorer=scripted({}, seen), gate=Granted(), now=NOW))
    check("only B's machine ids were scored", set(seen) == set(ids), f"{sorted(seen)} vs {sorted(ids)}")
    for other in (F.A, F.C):
        other_ids = within(Session, other, lambda db, t=other: {
            m.id for m in db.query(models.Machine).filter(models.Machine.tenant_code == t).all()})
        check(f"no machine of {other} was scored", not (set(seen) & other_ids),
              str(set(seen) & other_ids))
    a_sweep = within(Session, F.A, lambda db: sw.build_anomaly_sweep(
        db, F.A, scorer=scripted({}), gate=Granted(), now=NOW))
    check("A's sweep names no other factory", F.B not in repr(a_sweep) and F.C not in repr(a_sweep))

    print("\n9. The Copilot reports what it could NOT score too")
    from ai.tools import registry as treg
    for role in ("Operator", "Viewer"):
        refused = within(Session, F.B, lambda db, r=role: treg.run_tool(
            db, treg.Principal(tenant=F.B, role=r), "get_anomaly_sweep"))
        check(f"a {role} is refused the sweep", refused.state == "NOT PERMITTED",
              f"{refused.state}: {refused.summary}")
    r = within(Session, F.B, lambda db: treg.run_tool(
        db, treg.Principal(tenant=F.B, role="Admin"), "get_anomaly_sweep"))
    check("an Admin may ask", r.state not in ("NOT PERMITTED", "NOT LICENSED", "FAILED"),
          f"{r.state}: {r.summary}")
    # The tool runs the REAL scorer and the REAL consent gate, so whatever it
    # comes back with, the two counts must match what the sweep itself says.
    live = within(Session, F.B, lambda db: sw.build_anomaly_sweep(
        db, F.B, scorer=service.score_machine, gate=consent.DbConsentGate(), now=NOW))
    not_scored = [f for f in r.facts if f.key == "anomaly.not_scored"]
    scored_fact = [f for f in r.facts if f.key == "anomaly.scored"]
    check("how many it could not score is a fact", len(not_scored) == 1)
    check("...and it is the real number", not_scored and not_scored[0].value == live["not_scored"],
          f"{not_scored and not_scored[0].value} != {live['not_scored']}")
    check("how many it scored is a fact too",
          scored_fact and scored_fact[0].value == live["scored"],
          f"{scored_fact and scored_fact[0].value} != {live['scored']}")
    check("the caveat is on the result", any("experimental" in n for n in r.notes), str(r.notes))
    check("no fact claims a measurement",
          all(f.provenance != "MEASURED FACT" or not f.key.startswith("anomaly.") or
              f.key in ("anomaly.scored", "anomaly.not_scored", "anomaly.consent")
              for f in r.facts),
          str([(f.key, f.provenance) for f in r.facts]))
    for word in ("alarm", "alert", "fault"):
        allowed = r.summary.lower().count("not an alarm") + r.summary.lower().count("never as an alarm")
        check(f'the tool does not raise an "{word}"',
              r.summary.lower().count(word) <= (allowed if word == "alarm" else 0), r.summary)

    print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'All checks passed'}")
    for f in failures:
        print("  -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main_())
