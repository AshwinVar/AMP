"""A machine broken on day one is still broken, whatever the OEE says.

THE DEFECT
----------
`build_briefing` opened with:

    if not plant["has_data"]:
        return {..., "headline": "No production data yet.", "alerts": [], ...}

`has_data` means one specific thing — there is production to compute OEE from
(ADR-0014: an empty window is UNMEASURED, never 0%). That is a rule about the
OEE NUMBER. The early return quietly promoted it to "there is nothing worth
reporting", and threw away every alert on the way out — including the one the
code immediately below it calls "the most time-sensitive signal":

    # 1. Machines hard-down right now — the most time-sensitive signal.

So a plant with two machines hard-down and no production logged yet was told
"No production data yet." Three surfaces inherited it:

    build_briefing         alerts: []            -> the /briefing read-model
    assistant._briefing    "nothing to report"   -> the copilot
    agents.escalate_...    reason: "no_data"     -> no escalation raised at all

WHY THIS IS NOT A COSMETIC CASE
--------------------------------
Every tenant starts here. A new AMP install has machines registered and no
production history, which is exactly the commissioning window when equipment
gets knocked about and when a customer is forming their first impression of
whether the product notices anything. The escalation agent refusing to raise a
hard-down machine because the plant has not produced anything yet is the
product's worst moment to be silent.

WHAT DOES NOT CHANGE
---------------------
`has_data` stays False and OEE stays unreported. That rule is correct and is
not what was wrong: reporting 0% OEE for a plant that never ran is a fabricated
loss. The headline still leads with "No production data yet". The only change is
that the alerts which do not depend on production survive alongside it.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_briefing_down_before_production.py
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
from ai import agents, assistant, briefing
from database import Base

T = "COMMISSION"
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def seed(with_production=False, down=("Breakdown", "Offline")):
    """A plant on its first day: machines registered, nothing produced yet."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    db = sessionmaker(bind=engine)()
    tok = tenancy.set_current_tenant(None)
    db.add(models.TenantConfig(tenant_code=T))
    statuses = ("Running", "Running", "Idle") + tuple(down)
    for i, status in enumerate(statuses):
        m = models.Machine(tenant_code=T, name=f"SMT-{i:02d}", site="P1",
                           status=status, utilization=70, downtime="0 min")
        db.add(m)
        db.flush()
        if with_production:
            db.add(models.ProductionRecord(
                tenant_code=T, machine_id=m.id, planned_minutes=480,
                runtime_minutes=400, ideal_cycle_time_seconds=30,
                total_count=600, good_count=560, rejected_count=40))
    db.commit()
    tenancy.reset_current_tenant(tok)
    return db


def main():
    print("=" * 74)
    print("1. THE PREMISE — a new plant genuinely has no OEE")
    print("=" * 74)
    db = seed(with_production=False)
    tok = tenancy.set_current_tenant(T)
    b = briefing.build_briefing(db, T)
    check("has_data is False, and stays that way", b["has_data"] is False, str(b["has_data"]))
    check("...so no OEE number is claimed", b["oee"] == 0, str(b["oee"]))
    check("...and the headline still says so",
          "No production data yet" in b["headline"], b["headline"])

    print()
    print("=" * 74)
    print("2. THE HARD-DOWN ALERT SURVIVES ANYWAY")
    print("=" * 74)
    keys = [a["key"] for a in b["alerts"]]
    check("a machines_down alert is raised", "machines_down" in keys, str(keys))
    down_alert = next((a for a in b["alerts"] if a["key"] == "machines_down"), None)
    if down_alert is not None:
        check("...at high severity, like the produced-plant path",
              down_alert["severity"] == "high", down_alert["severity"])
        check("...naming both hard-down machines",
              sorted(down_alert["detail"].split(", ")) == ["SMT-03", "SMT-04"],
              down_alert["detail"])
    check("...and the headline mentions it, not just 'no data'",
          "down" in b["headline"].lower(), b["headline"])

    print()
    print("=" * 74)
    print("3. THE COPILOT SAYS IT TOO")
    print("=" * 74)
    # The producer alone is not enough: the copilot branched on has_data itself
    # and would have kept saying "nothing to report" while holding the alert.
    text, _view = assistant._briefing(db, T)
    check("the briefing answer names the down machines", "SMT-03" in text and "SMT-04" in text, text)
    check("...and no longer claims there is nothing to report",
          "nothing to report" not in text.lower(), text)
    check("...while still being honest that OEE is unmeasured",
          "no production data" in text.lower(), text)
    d = assistant.digest(db, T)["digest"]
    check("the rundown says it as well", "down" in d.lower() and "SMT-03" in d, d)

    print()
    print("=" * 74)
    print("4. THE ESCALATION AGENT RAISES IT")
    print("=" * 74)
    # The one that writes. It returned reason="no_data" and created nothing.
    result = agents.escalate_from_briefing(db, T)
    check("an escalation is raised", result.get("escalated") is True, str(result))
    check("...for the machines_down signal", result.get("alert_key") == "machines_down",
          str(result))
    rows = db.query(models.Escalation).all()
    check("...and exactly one Escalation row exists", len(rows) == 1, str(len(rows)))
    if rows:
        check("...linked to a machine that is actually down",
              rows[0].machine_id in [m.id for m in db.query(models.Machine)
                                     .filter(models.Machine.status.in_(("Breakdown", "Offline")))],
              str(rows[0].machine_id))
    again = agents.escalate_from_briefing(db, T)
    check("running the agent twice does NOT raise a second one",
          again.get("escalated") is False and again.get("reason") == "already_open",
          str(again))
    check("...and the table still holds exactly one",
          db.query(models.Escalation).count() == 1,
          str(db.query(models.Escalation).count()))
    tenancy.reset_current_tenant(tok)
    db.close()

    print()
    print("=" * 74)
    print("5. A HEALTHY NEW PLANT IS STILL QUIET")
    print("=" * 74)
    # The other half of the behaviour: nothing to report must still report
    # nothing. Without this, "raise the alert" could degenerate into "always
    # raise something", which is how an alert list becomes wallpaper.
    quiet = seed(with_production=False, down=("Running", "Idle"))
    tok = tenancy.set_current_tenant(T)
    qb = briefing.build_briefing(quiet, T)
    check("no machines down -> no alerts", qb["alerts"] == [], str(qb["alerts"]))
    check("...and the plain headline comes back",
          qb["headline"] == "No production data yet.", qb["headline"])
    check("the copilot reports nothing to report",
          "nothing to report" in assistant._briefing(quiet, T)[0].lower(),
          assistant._briefing(quiet, T)[0])
    check("the escalation agent raises nothing",
          agents.escalate_from_briefing(quiet, T).get("escalated") is False,
          str(agents.escalate_from_briefing(quiet, T)))
    tenancy.reset_current_tenant(tok)
    quiet.close()

    print()
    print("=" * 74)
    print("6. THE PRODUCED-PLANT PATH IS UNCHANGED")
    print("=" * 74)
    # Reference oracle. Everything above is about the no-data branch; a plant
    # that HAS produced must behave exactly as it did.
    full = seed(with_production=True)
    tok = tenancy.set_current_tenant(T)
    fb = briefing.build_briefing(full, T)
    check("has_data is True", fb["has_data"] is True, str(fb["has_data"]))
    check("...OEE is a real number", fb["oee"] > 0, str(fb["oee"]))
    check("...the machines_down alert is present as before",
          any(a["key"] == "machines_down" for a in fb["alerts"]),
          str([a["key"] for a in fb["alerts"]]))
    check("...and the headline is the produced-plant one",
          "No production data yet" not in fb["headline"], fb["headline"])
    tenancy.reset_current_tenant(tok)
    full.close()

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
