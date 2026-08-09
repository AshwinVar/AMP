"""Telemetry profiles, commissioning, warranty and service intelligence.

The load-bearing property here is HONESTY, not arithmetic. Every figure this
module produces goes to somebody who will put an engineer in a van, so the
failure mode that matters is a confident number nobody can justify — the same
class this platform removed from OEE in ADR-0014.

So the tests below spend as much effort on what the code REFUSES to say
("not configured", "unknown", no projection from two points) as on what it does.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_oem_service.py
"""
import json
import sys
from datetime import date, datetime, timedelta

import oem_service
import oem_telemetry

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


class FakeInstall:
    def __init__(self, **kw):
        self.serial_number = kw.get("serial", "SN-1")
        self.status = kw.get("status", "Active")
        self.factory_tenant_code = kw.get("tenant", "FACTORY_A")
        self.machine_id = kw.get("machine_id", 1)
        self.model_id = kw.get("model_id", 1)
        self.operating_hours = kw.get("hours", 1000.0)
        self.last_service_hours = kw.get("last_service", None)
        self.last_service_at = None
        self.last_seen_at = kw.get("last_seen", datetime.utcnow())
        self.warranty_start = kw.get("w_start", date(2026, 1, 1))
        self.warranty_end = kw.get("w_end", date(2028, 1, 1))
        self.installed_at = kw.get("installed_at")
        self.commissioned_at = kw.get("commissioned_at")
        self.decommissioned_at = None


class FakeModel:
    def __init__(self, interval=2000, code="X200"):
        self.service_interval_hours = interval
        self.model_code = code


# =====================================================================
def test_a_profile_keeps_machine_vocabulary_out_of_core():
    print("=" * 74)
    print("1. TELEMETRY PROFILES ARE CONFIGURATION, NOT CODE")
    print("=" * 74)
    comp = oem_telemetry.parse(json.dumps(oem_telemetry.EXAMPLE_COMPRESSOR))
    cnc = oem_telemetry.parse(json.dumps(oem_telemetry.EXAMPLE_CNC))
    check("a compressor profile parses to its own signal names",
          {s["name"] for s in comp} >= {"pressure", "discharge_temperature"},
          str([s["name"] for s in comp]))
    check("a CNC profile parses to a COMPLETELY different vocabulary",
          {s["name"] for s in cnc} >= {"spindle_speed", "cycle_count"}
          and "pressure" not in {s["name"] for s in cnc},
          str([s["name"] for s in cnc]))
    check("core shares only the SHAPE: both expose the same keys",
          set(comp[0].keys()) == set(cnc[0].keys()))

    # No compressor word may appear in the module that interprets profiles.
    import inspect
    src = inspect.getsource(oem_telemetry.parse) + inspect.getsource(
        oem_telemetry.interpret)
    for word in ("pressure", "discharge", "spindle", "compressor", "cnc"):
        check(f"the interpreter never mentions {word!r}", word not in src.lower())

    # A broken profile is an ERROR, not silently "no signals".
    for bad, why in (("{not json", "malformed JSON"),
                     ('[{"source":"X","datatype":"number"}]', "no name"),
                     ('[{"name":"a","source":"X","datatype":"wat"}]', "bad datatype"),
                     ('[{"name":"a","source":"X","datatype":"number"},'
                      '{"name":"a","source":"Y","datatype":"number"}]', "duplicate name")):
        try:
            oem_telemetry.parse(bad)
            check(f"a profile with {why} is refused", False, "accepted")
        except oem_telemetry.ProfileError:
            check(f"a profile with {why} is refused", True)
    check("an EMPTY profile is legal and means 'reports nothing'",
          oem_telemetry.parse("") == [] and oem_telemetry.parse(None) == [])


def test_out_of_range_is_flagged_never_clamped():
    print()
    print("=" * 74)
    print("2. AN OUT-OF-RANGE READING IS FLAGGED, NEVER CLAMPED")
    print("=" * 74)
    profile = oem_telemetry.parse(json.dumps(oem_telemetry.EXAMPLE_COMPRESSOR))
    named, unknown, out = oem_telemetry.interpret(
        profile, {"T_dis": 400, "P1": 7.2, "Run": True, "MysteryTag": 1})

    check("the overheating value is kept AS MEASURED",
          named["discharge_temperature"]["value"] == 400.0,
          str(named["discharge_temperature"]))
    check("...and marked out of range",
          named["discharge_temperature"]["in_range"] is False)
    check("...and listed", "discharge_temperature" in out, str(out))
    check("a normal reading is in range",
          named["pressure"]["in_range"] is True and named["pressure"]["value"] == 7.2)
    check("a tag the profile does not describe is REPORTED, not dropped",
          unknown == ["MysteryTag"], str(unknown))
    check("units are carried through as configured",
          named["discharge_temperature"]["unit"] == "degC")
    # A non-numeric value where a number was configured is a fault, not a zero.
    named2, _, out2 = oem_telemetry.interpret(profile, {"P1": "n/a"})
    check("a non-numeric reading becomes None + out-of-range, never 0",
          named2["pressure"]["value"] is None and "pressure" in out2,
          str(named2))


def test_the_lifecycle_cannot_be_skipped():
    print()
    print("=" * 74)
    print("3. COMMISSIONING CANNOT BE SKIPPED")
    print("=" * 74)
    inst = FakeInstall(status="Manufactured")
    try:
        oem_service.transition(inst, "Active")
        check("Manufactured cannot jump straight to Active", False, "allowed")
    except oem_service.LifecycleError:
        check("Manufactured cannot jump straight to Active", True)

    for target in ("Sold", "Assigned", "Installed", "Commissioning", "Active"):
        oem_service.transition(inst, target)
    check("the full path Manufactured -> Active works", inst.status == "Active")
    check("...and stamps commissioned_at", inst.commissioned_at is not None)
    check("...and installed_at", inst.installed_at is not None)

    oem_service.transition(inst, "Decommissioned")
    check("a decommissioned machine stamps its date",
          inst.decommissioned_at is not None)
    try:
        oem_service.transition(inst, "Active")
        check("a decommissioned machine cannot be revived", False, "allowed")
    except oem_service.LifecycleError:
        check("a decommissioned machine cannot be revived", True)


def test_commissioning_names_what_is_missing():
    print()
    print("=" * 74)
    print("4. A FAILED COMMISSIONING SAYS WHICH FACT IS MISSING")
    print("=" * 74)
    profile = oem_telemetry.parse(json.dumps(oem_telemetry.EXAMPLE_COMPRESSOR))
    model = FakeModel()

    ready = oem_service.commissioning_report(FakeInstall(), model, profile)
    check("a fully-prepared installation is ready", ready["ready"] is True,
          str([c for c in ready["checks"] if not c["passed"]]))

    never = FakeInstall(last_seen=None)
    rep = oem_service.commissioning_report(never, model, profile)
    failed = [c["key"] for c in rep["checks"] if not c["passed"]]
    check("a machine that never reported is NOT ready", rep["ready"] is False)
    check("...and the failure names exactly that", failed == ["has_reported"],
          str(failed))

    unassigned = FakeInstall(tenant=None, machine_id=None)
    rep = oem_service.commissioning_report(unassigned, model, [])
    failed = [c["key"] for c in rep["checks"] if not c["passed"]]
    check("three missing facts are reported as three failures",
          set(failed) == {"assigned_to_customer", "linked_to_machine",
                          "telemetry_profile"}, str(failed))
    check("...each with a detail an engineer can act on",
          all(c["detail"] for c in rep["checks"]))


def test_it_refuses_to_invent_a_service_interval():
    print()
    print("=" * 74)
    print("5. NO INTERVAL, NO SCHEDULE — IT SAYS SO")
    print("=" * 74)
    inst = FakeInstall(hours=1900.0)
    check("a model with no interval reports not_configured",
          oem_service.service_state(inst, FakeModel(interval=None))["state"]
          == "not_configured")
    check("...and offers no hours_remaining",
          oem_service.service_state(inst, FakeModel(interval=None))
          ["hours_remaining"] is None)
    check("a machine that never reported hours is 'unknown', not 0",
          oem_service.service_state(FakeInstall(hours=None), FakeModel())["state"]
          == "unknown")

    # Hand-derived. NEVER serviced, 1900 h run, 2000 h interval -> 100 h left,
    # which is 5% of the interval -> "due".
    svc = oem_service.service_state(FakeInstall(hours=1900.0), FakeModel(2000))
    check("1900 h, never serviced, leaves 100 h",
          svc["hours_remaining"] == 100.0, str(svc))
    check("...which is 'due' (within 5%)", svc["state"] == "due", svc["state"])

    # THE CASE THE MODULO FORM COULD NOT EXPRESS. 2100 h run, never serviced, is
    # 100 h PAST a 2000 h interval. `hours % interval` would have called this
    # "1900 h remaining" and `overdue` would have been unreachable.
    svc = oem_service.service_state(FakeInstall(hours=2100.0), FakeModel(2000))
    check("2100 h never serviced is OVERDUE, not 1900 h remaining",
          svc["state"] == "overdue" and svc["hours_remaining"] == -100.0, str(svc))
    check("...and the reason says it was never serviced",
          "never serviced" in svc["reason"], svc["reason"])

    # Serviced on time: 2100 h run, serviced at 2000 h -> 100 h in, 1900 left.
    svc = oem_service.service_state(
        FakeInstall(hours=2100.0, last_service=2000.0), FakeModel(2000))
    check("2100 h serviced at 2000 h leaves 1900 h",
          svc["hours_remaining"] == 1900.0 and svc["state"] == "ok", str(svc))
    check("...and reports 100 h since the service",
          svc["hours_since_service"] == 100.0, str(svc))

    # A counter that reads BELOW the last service is a reset, not a fresh machine.
    svc = oem_service.service_state(
        FakeInstall(hours=50.0, last_service=2000.0), FakeModel(2000))
    check("a counter below the last service is 'unknown', not 'ok'",
          svc["state"] == "unknown", str(svc))
    check("...and says the counter was probably reset",
          "reset" in svc["reason"], svc["reason"])


def test_warranty_never_guesses():
    print()
    print("=" * 74)
    print("6. WARRANTY IS RECORDED OR UNKNOWN — NEVER ASSUMED")
    print("=" * 74)
    today = date(2027, 1, 1)
    check("no end date recorded -> unknown, not expired",
          oem_service.warranty_state(FakeInstall(w_end=None), today)["state"]
          == "unknown")
    check("...and it explains why",
          "no warranty end date" in
          oem_service.warranty_state(FakeInstall(w_end=None), today)["reason"])
    check("an end date in the future -> active",
          oem_service.warranty_state(FakeInstall(w_end=date(2028, 1, 1)), today)
          ["state"] == "active")
    check("an end date in the past -> expired",
          oem_service.warranty_state(FakeInstall(w_end=date(2026, 6, 1)), today)
          ["state"] == "expired")
    check("a future start -> not_started",
          oem_service.warranty_state(
              FakeInstall(w_start=date(2027, 6, 1), w_end=date(2029, 6, 1)),
              today)["state"] == "not_started")
    check("days remaining is arithmetic, not a guess",
          oem_service.warranty_state(FakeInstall(w_end=date(2027, 1, 31)), today)
          ["days_remaining"] == 30)


def test_recommendations_carry_their_evidence():
    print()
    print("=" * 74)
    print("7. EVERY RECOMMENDATION CARRIES ITS EVIDENCE")
    print("=" * 74)
    # 2100 h run, NEVER serviced, against a 2000 h interval -> overdue.
    recs = oem_service.recommendations(FakeInstall(hours=2100.0), FakeModel(2000))
    check("an overdue machine produces a recommendation", recs, str(recs))
    for r in recs:
        check(f"[{r['kind']}] names the machine by SERIAL", r["machine"] == "SN-1")
        check(f"[{r['kind']}] gives a reason in words", bool(r["reason"]))
        check(f"[{r['kind']}] gives numeric evidence", bool(r["evidence"]))
        check(f"[{r['kind']}] gives an action", bool(r["action"]))
        check(f"[{r['kind']}] is timestamped", bool(r["at"]))

    kinds = {r["kind"] for r in recs}
    check("the overdue service is one of them", "service_due" in kinds, str(kinds))

    silent = oem_service.recommendations(
        FakeInstall(last_seen=datetime.utcnow() - timedelta(days=7)), FakeModel())
    quiet = [r for r in silent if r["kind"] == "not_reporting"]
    check("a machine silent for a week is flagged", quiet, str([r['kind'] for r in silent]))
    check("...and the wording does NOT claim to know why",
          "cannot be told apart" in quiet[0]["reason"], quiet[0]["reason"])


def test_confidence_appears_only_where_it_means_something():
    print()
    print("=" * 74)
    print("8. NO INVENTED CONFIDENCE, NO CLAIMED PREDICTION")
    print("=" * 74)
    model = FakeModel(2000)

    recs = oem_service.recommendations(FakeInstall(hours=2100.0), model)
    arithmetic = [r for r in recs if r["kind"] in
                  ("service_due", "warranty_expiring", "not_reporting",
                   "warranty_unknown")]
    check("arithmetic recommendations carry NO confidence figure",
          all(r["confidence"] is None for r in arithmetic),
          str([(r["kind"], r["confidence"]) for r in arithmetic]))

    # Two samples is not a trend.
    two = [(date(2027, 1, 1), 1800.0), (date(2027, 1, 5), 1850.0)]
    check("two samples produce NO projection",
          oem_service.project_service_date(FakeInstall(hours=1850.0), model, two)
          is None)
    # Samples spanning less than a day are not a trend either.
    same_day = [(date(2027, 1, 1), 1800.0), (date(2027, 1, 1), 1810.0),
                (date(2027, 1, 1), 1820.0)]
    check("three samples inside one day produce NO projection",
          oem_service.project_service_date(FakeInstall(hours=1820.0), model,
                                           same_day) is None)

    # A real trend: 1800 -> 1900 over 10 days = 10 h/day, 100 h left = ~10 days.
    history = [(date(2027, 1, 1), 1800.0), (date(2027, 1, 5), 1840.0),
               (date(2027, 1, 8), 1870.0), (date(2027, 1, 11), 1900.0)]
    proj = oem_service.project_service_date(FakeInstall(hours=1900.0), model,
                                            history, date(2027, 1, 11))
    check("a real trend DOES produce a projection", proj is not None)
    check("...with a confidence derived from the sample",
          proj and 0 < proj["confidence"] <= 0.85, str(proj and proj["confidence"]))
    check("...whose evidence states the rate and the sample size",
          proj and "h/day" in proj["evidence"] and "samples" in proj["evidence"],
          str(proj and proj["evidence"]))
    check("...and which says in words that it is NOT a predictive model",
          proj and "not a predictive model" in proj["reason"])
    check("...projecting about 10 days", proj and "10 days" in proj["action"],
          str(proj and proj["action"]))

    # A machine whose hours are going nowhere gets no projection.
    flat = [(date(2027, 1, 1), 1800.0), (date(2027, 1, 5), 1800.0),
            (date(2027, 1, 9), 1800.0)]
    check("an idle machine produces NO projection",
          oem_service.project_service_date(FakeInstall(hours=1800.0), model, flat)
          is None)

    # Nothing anywhere may claim prediction.
    import inspect
    # The module may DISCUSS predictive maintenance in order to disclaim it; what
    # it must never do is CLAIM it. So the check looks at what reaches a user:
    # the text of the recommendations themselves.
    every = (oem_service.recommendations(FakeInstall(hours=2100.0), model)
             + oem_service.recommendations(FakeInstall(w_end=None), model)
             + [proj])
    blob = " ".join(json.dumps(r) for r in every if r).lower()
    for banned in ("predictive maintenance", "will fail", "failure predicted",
                   "predicted failure", "ai predicts"):
        check(f"no recommendation text claims {banned!r}",
              banned not in blob.replace("not a predictive model", "xx"))
    check("the projection explicitly disclaims being a model",
          "not a predictive model" in blob)


def test_state_is_inferred_only_where_the_profile_says_so():
    print()
    print("=" * 74)
    print("9. AMP INFERS MACHINE STATE ONLY FROM SIGNALS MARKED FOR IT")
    print("=" * 74)
    # A bool is NOT a state signal by accident. `door_open` is a boolean and says
    # nothing about whether the machine is running; inferring state from "it is a
    # bool" would make an open inspection hatch look like production.
    mixed = oem_telemetry.parse(json.dumps([
        {"name": "running", "source": "Run", "datatype": "bool",
         "state_signal": True},
        {"name": "door_open", "source": "Door", "datatype": "bool"},
        {"name": "pressure", "source": "P1", "datatype": "number"},
    ]))
    check("only the signal MARKED state_signal decides state",
          oem_telemetry.state_signals(mixed) == ["running"],
          str(oem_telemetry.state_signals(mixed)))

    none_marked = oem_telemetry.parse(json.dumps([
        {"name": "door_open", "source": "Door", "datatype": "bool"},
    ]))
    check("a profile with no state signal yields NOTHING, rather than a guess",
          oem_telemetry.state_signals(none_marked) == [],
          str(oem_telemetry.state_signals(none_marked)))


def test_the_service_queue_leads_with_the_worst():
    print()
    print("=" * 74)
    print("10. THE FLEET QUEUE LEADS WITH THE MOST SEVERE")
    print("=" * 74)
    model = FakeModel(2000)
    # Hand-derived, and the ORDER of this fixture is deliberate. Severity must be
    # the only thing that explains the output, so the input is arranged to
    # DISAGREE with every other ordering a careless implementation might produce:
    #
    #   input order       A(high)  B(low)   C(medium)   D(none)
    #   alphabetical      A(high)  B(low)   C(medium)
    #   reversed input    C(med)   B(low)   A(high)
    #   BY SEVERITY       A(high)  C(med)   B(low)      <- the only correct one
    #
    # A first attempt named these SN-OVERDUE / SN-QUIET / SN-WARR and listed them
    # healthy-first; reversing the list then produced the right answer by
    # accident, and a mutation that returned the queue unsorted went unnoticed.
    installs = [
        FakeInstall(serial="SN-A", hours=2100.0),                    # -> high
        FakeInstall(serial="SN-B", hours=500.0, w_end=None),         # -> low
        FakeInstall(serial="SN-C", hours=500.0,                      # -> medium
                    last_seen=datetime.utcnow() - timedelta(days=7)),
        FakeInstall(serial="SN-D", hours=500.0),                     # -> nothing
    ]
    recs = oem_service.fleet_recommendations(None, "OEM_ALPHA", installs,
                                             {1: model})
    order = [(r["machine"], r["severity"]) for r in recs]
    check("the queue is ordered by SEVERITY, not by input or by name",
          order == [("SN-A", "high"), ("SN-C", "medium"), ("SN-B", "low")],
          str(order))
    # CONTROL: the healthy machine is not padded into the queue to make it look
    # busy. A service desk that sees a row per machine stops reading the queue.
    check("CONTROL: the healthy machine produces no row at all",
          "SN-D" not in {r["machine"] for r in recs},
          str({r["machine"] for r in recs}))


def run_all():
    test_a_profile_keeps_machine_vocabulary_out_of_core()
    test_out_of_range_is_flagged_never_clamped()
    test_the_lifecycle_cannot_be_skipped()
    test_commissioning_names_what_is_missing()
    test_it_refuses_to_invent_a_service_interval()
    test_warranty_never_guesses()
    test_recommendations_carry_their_evidence()
    test_confidence_appears_only_where_it_means_something()
    test_state_is_inferred_only_where_the_profile_says_so()
    test_the_service_queue_leads_with_the_worst()


def test_oem_service():
    run_all()
    assert not failures, failures


if __name__ == "__main__":
    run_all()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        sys.exit(1)
    print("SERVICE INTELLIGENCE EXPLAINS ITSELF, AND REFUSES TO GUESS")
