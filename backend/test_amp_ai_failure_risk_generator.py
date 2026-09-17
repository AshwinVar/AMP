"""The synthetic machine fleet the failure-risk model is trained on.

WHY THIS SUITE EXISTS
---------------------
The failure-risk base model never sees customer data: it is trained on machines
invented by ``amp_ai/failure_risk/synthetic.py``. Everything the model later
claims rests on that generator, so the generator is tested BEFORE any feature
or model code exists, and on the properties that would make an evaluation
meaningless if they broke:

  1  DETERMINISM   same (n, days, seed, variant) -> byte-identical fleet; a
                   different seed -> a different fleet
  2  CONSISTENCY   timelines are sorted, status transitions chain
                   (old_status == previous new_status), utilisation is a
                   percentage, maintenance rows are well-formed, the duration
                   strings it writes parse back to the minutes it meant
  3  PREVALENCE    3-10% of eligible machine-weeks contain a new breakdown
  4  PHYSICS       wear raises the hazard; preventive and corrective work
                   lower wear (checked against the HIDDEN wear trace, which
                   only tests may read)
  5  REALISM       record gaps, machines without inspections, nuisance
                   stoppers, skipped and unrecorded PMs actually occur
  6  VARIANTS      each misspecification variant changes what it says it
                   changes, and an unknown variant is refused
  7  INDEPENDENCE  the generator imports nothing from the rule-based scorer,
                   the AI platform or the database layer, so its labels
                   cannot be produced by the rules the model is compared with

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_failure_risk_generator.py
"""
import hashlib
import os
import time

from amp_ai.core import purity
from amp_ai.failure_risk import history as H
from amp_ai.failure_risk import synthetic as S
from duration import parse_duration_to_minutes

BACKEND = os.path.dirname(os.path.abspath(__file__))
SYNTHETIC_PY = os.path.join(BACKEND, "amp_ai", "failure_risk", "synthetic.py")

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc as e:  # noqa: BLE001
        return e
    return None


def digest(fleet):
    h = hashlib.sha256()
    for mh in fleet.histories:
        h.update(repr((mh.machine_id, mh.name, mh.state_at_window_start, mh.events, mh.downtime,
                       mh.production, mh.inspections, mh.maintenance)).encode("utf-8"))
    return h.hexdigest()


def machine_weeks(fleet, first_day=H.LOOKBACK_DAYS):
    """(machine_id, day, wear_at_as_of, label) for every eligible weekly as-of date."""
    rows = []
    for mh in fleet.histories:
        truth = fleet.truth[mh.machine_id]
        for day in range(first_day, fleet.days - H.HORIZON_DAYS + 1, 7):
            as_of = S.as_of_for_day(day)
            if H.in_breakdown_at(mh, as_of):
                continue
            rows.append((mh.machine_id, day, truth.wear[day - 1], H.breakdown_in_horizon(mh, as_of)))
    return rows


# --------------------------------------------------------------------------- 1
def section_determinism():
    print("\n1. Determinism")
    a = S.generate_fleet(10, 160, seed=11)
    b = S.generate_fleet(10, 160, seed=11)
    c = S.generate_fleet(10, 160, seed=12)
    check("same seed -> identical fleet", digest(a) == digest(b))
    check("same seed -> identical hidden wear", all(a.truth[i].wear == b.truth[i].wear for i in a.truth))
    check("different seed -> different fleet", digest(a) != digest(c))
    sub = S.generate_fleet(10, 160, seed=11, machine_ids=[3, 7])
    full = {mh.machine_id: mh for mh in a.histories}
    same = all(repr((mh.events, mh.downtime, mh.production, mh.maintenance))
               == repr((full[mh.machine_id].events, full[mh.machine_id].downtime,
                        full[mh.machine_id].production, full[mh.machine_id].maintenance))
               for mh in sub.histories)
    check("generating a subset of machines reproduces those machines exactly",
          [mh.machine_id for mh in sub.histories] == [3, 7] and same)
    check("a non-int seed is refused", raises(TypeError, S.generate_fleet, 2, 30, seed="1") is not None)
    check("an unknown variant is refused", raises(ValueError, S.generate_fleet, 2, 30, seed=1, variant="nope") is not None)
    check("a machine id outside the fleet is refused",
          raises(ValueError, S.generate_fleet, 2, 30, seed=1, machine_ids=[3]) is not None)


# --------------------------------------------------------------------------- 2
def section_consistency(fleet):
    print("\n2. Consistency of the generated records")
    end_limit = S.day_start(fleet.days + 3)
    sorted_ok = times_ok = chain_ok = util_ok = maint_ok = prod_ok = insp_ok = True
    detail = ""
    for mh in fleet.histories:
        for name in ("events", "downtime", "production", "inspections"):
            ts = [r[0] for r in getattr(mh, name)]
            if ts != sorted(ts):
                sorted_ok, detail = False, f"{mh.machine_id}.{name}"
            if ts and (ts[0] < S.EPOCH or ts[-1] >= end_limit):
                times_ok, detail = False, f"{mh.machine_id}.{name} {ts[0]} .. {ts[-1]}"
        previous = S.INITIAL_STATUS
        for ts, old, new, util in mh.events:
            if old != previous or old == new:
                chain_ok, detail = False, f"{mh.machine_id} {ts} {old}->{new} after {previous}"
                break
            previous = new
            if type(util) is not int or not 0 <= util <= 100:
                util_ok = False
        for planned, completed, reactive, is_open in mh.maintenance:
            if not (type(reactive) is bool and type(is_open) is bool):
                maint_ok = False
            if is_open and completed is not None:
                maint_ok = False
            if not is_open and completed is None:
                maint_ok = False
            if completed is not None and completed < planned and not reactive:
                maint_ok = False
        planned_dates = [m[0] for m in mh.maintenance]
        if planned_dates != sorted(planned_dates):
            sorted_ok, detail = False, f"{mh.machine_id}.maintenance"
        for ts, planned_min, runtime, ideal, total, good, rejected in mh.production:
            if not (0 <= runtime <= planned_min and ideal > 0 and total == good + rejected
                    and min(total, good, rejected) >= 0):
                prod_ok = False
        for ts, inspected, failed in mh.inspections:
            if not 0 <= failed <= inspected:
                insp_ok = False
    check("every record list is sorted by time", sorted_ok, detail)
    check("timestamps lie inside the simulated period", times_ok, detail)
    check("status transitions chain (old_status == previous new_status, no self-transition)", chain_ok, detail)
    check("event utilisation is an integer percentage", util_ok)
    check("maintenance rows: bool flags, open <=> no completion date", maint_ok)
    check("production rows: 0 <= runtime <= planned, total = good + rejected", prod_ok)
    check("inspection rows: 0 <= failed <= inspected", insp_ok)
    statuses = {e[2] for mh in fleet.histories for e in mh.events}
    check("the timeline uses AMP's canonical statuses",
          statuses <= {"Running", "Idle", "Breakdown", "Maintenance"} and {"Running", "Breakdown"} <= statuses,
          repr(statuses))
    bad = [m for m in list(range(0, 400)) + [599, 1440, 2999]
           if parse_duration_to_minutes(S.format_duration(m)) != m]
    check("format_duration round-trips through duration.parse_duration_to_minutes", not bad, repr(bad[:5]))
    reasons = {d[1] for mh in fleet.histories for d in mh.downtime}
    check("corrective repairs are logged with reason 'Breakdown' as a plant would", "Breakdown" in reasons)
    check("the downtime vocabulary is more than breakdowns", len(reasons) >= 6, repr(sorted(reasons)))


# --------------------------------------------------------------------------- 3
def section_prevalence(fleet):
    print("\n3. Prevalence of new breakdowns per eligible machine-week")
    rows = machine_weeks(fleet)
    positives = sum(r[3] for r in rows)
    rate = positives / len(rows)
    print(f"     {len(rows)} machine-weeks, {positives} with a new breakdown -> {rate:.2%}")
    check("prevalence is between 3% and 10%", 0.03 <= rate <= 0.10, f"{rate:.4f}")
    check("the as-of time is inside the first shift (10:00)", S.as_of_for_day(200).hour == 10)
    return rows


# --------------------------------------------------------------------------- 4
def section_physics(fleet, rows):
    print("\n4. Physics: wear raises hazard; maintenance lowers wear")
    a = S.ARCHETYPES[0]
    h = [S.daily_hazard(w, eta=1.0, k=a.shape_k, base=0.002, load=0.8, gamma=S.LOAD_STRESS_GAMMA, shock=1.0)
         for w in (0.0, 0.25, 0.5, 1.0, 1.5)]
    check("daily_hazard is strictly increasing in wear", all(x < y for x, y in zip(h, h[1:])), repr(h))
    over = S.daily_hazard(0.5, eta=1.0, k=3.0, base=0.002, load=1.0, gamma=S.LOAD_STRESS_GAMMA, shock=1.0)
    under = S.daily_hazard(0.5, eta=1.0, k=3.0, base=0.002, load=0.8, gamma=S.LOAD_STRESS_GAMMA, shock=1.0)
    check("running above 85% load raises hazard; below it does not", over > under and
          under == S.daily_hazard(0.5, eta=1.0, k=3.0, base=0.002, load=0.5, gamma=S.LOAD_STRESS_GAMMA, shock=1.0))

    ordered = sorted(rows, key=lambda r: r[2])
    q = len(ordered) // 4
    low = ordered[:q]
    high = ordered[-q:]
    low_rate = sum(r[3] for r in low) / len(low)
    high_rate = sum(r[3] for r in high) / len(high)
    print(f"     breakdown rate: lowest wear quartile {low_rate:.2%}, highest {high_rate:.2%}")
    check("machine-weeks in the highest wear quartile break down >= 2x as often as the lowest",
          high_rate >= 2.0 * low_rate and high_rate > 0, f"{low_rate:.4f} vs {high_rate:.4f}")

    pm = [r for t in fleet.truth.values() for r in t.pm_resets]
    corrective = [r for t in fleet.truth.values() for r in t.corrective_resets]
    check("one wear reset is recorded per performed PM and per breakdown",
          len(pm) == sum(len(t.pm_days) for t in fleet.truth.values())
          and len(corrective) == sum(len(t.breakdown_days) for t in fleet.truth.values()))
    pm_share = [after / before for _, before, after in pm if before > 0]
    cm_share = [after / before for _, before, after in corrective if before > 0]
    check("every performed PM removes 70-95% of wear", len(pm) > 50 and all(0.05 <= s <= 0.30 + 1e-12 for s in pm_share),
          f"n={len(pm)} range={min(pm_share):.3f}..{max(pm_share):.3f}")
    check("every corrective repair removes only 30-70% of wear (imperfect repair)",
          len(corrective) > 50 and all(0.30 <= s <= 0.70 + 1e-12 for s in cm_share),
          f"n={len(corrective)} range={min(cm_share):.3f}..{max(cm_share):.3f}")
    before = [t.wear[d - 1] for t in fleet.truth.values() for d in t.pm_days if d >= 1]
    after = [t.wear[d] for t in fleet.truth.values() for d in t.pm_days if d >= 1]
    check("in the daily wear trace, end-of-day wear on PM days averages well below the day before",
          sum(after) < 0.6 * sum(before), f"{sum(after) / len(after):.3f} vs {sum(before) / len(before):.3f}")


# --------------------------------------------------------------------------- 5
def section_realism(fleet):
    print("\n5. Realism knobs actually occur")
    n = len(fleet.histories)
    gap_machines = 0
    for mh in fleet.histories:
        days = [(r[0] - S.EPOCH).days for r in mh.production]
        if any(b - a >= 14 for a, b in zip(days, days[1:])):
            gap_machines += 1
    share = gap_machines / n
    check("about 20% of machines have multi-week gaps in production records", 0.10 <= share <= 0.32, f"{share:.2f}")
    no_insp = sum(1 for mh in fleet.histories if not mh.inspections) / n
    check("some machines have no inspections, most do", 0.10 <= no_insp <= 0.50, f"{no_insp:.2f}")
    nuisance = sum(1 for t in fleet.truth.values() if t.nuisance) / n
    check("some machines are nuisance stoppers", 0.05 <= nuisance <= 0.30, f"{nuisance:.2f}")
    skipped = sum(len(t.skipped_pm_days) for t in fleet.truth.values())
    unrecorded = sum(len(t.unrecorded_pm_days) for t in fleet.truth.values())
    performed = sum(len(t.pm_days) for t in fleet.truth.values())
    check("PMs are skipped ~15% of the time", 0.08 <= skipped / (skipped + performed) <= 0.22,
          f"{skipped}/{skipped + performed}")
    check("~10% of performed PMs are never closed in the system", 0.04 <= unrecorded / performed <= 0.16,
          f"{unrecorded}/{performed}")
    pending = sum(len(t.pending_pm_days) for t in fleet.truth.values())
    open_rows = sum(1 for mh in fleet.histories for m in mh.maintenance if m[3])
    check("skipped, unrecorded and still-pending PMs are exactly the open task rows",
          open_rows == skipped + unrecorded + pending, f"{open_rows} vs {skipped}+{unrecorded}+{pending}")
    reactive = sum(1 for mh in fleet.histories for m in mh.maintenance if m[2])
    breakdowns = sum(len(t.breakdown_days) for t in fleet.truth.values())
    check("every breakdown produces one reactive (corrective) task", reactive == breakdowns, f"{reactive} vs {breakdowns}")
    check("the fleet has all five archetypes",
          {t.archetype for t in fleet.truth.values()} == {a.name for a in S.ARCHETYPES})


# --------------------------------------------------------------------------- 6
def _wear_reject_gap(fleet):
    """Mean reject rate in the highest wear quartile minus the lowest: the SIZE of the symptom."""
    pairs = []
    for mh in fleet.histories:
        truth = fleet.truth[mh.machine_id]
        for ts, planned, runtime, ideal, total, good, rejected in mh.production:
            day = (ts - S.EPOCH).days
            if total >= 50 and 0 <= day < fleet.days:
                pairs.append((truth.wear[day], rejected / total))
    pairs.sort()
    q = len(pairs) // 4
    return sum(p[1] for p in pairs[-q:]) / q - sum(p[1] for p in pairs[:q]) / q


def _quartile_ratio(fleet):
    rows = machine_weeks(fleet)
    ordered = sorted(rows, key=lambda r: r[2])
    q = len(ordered) // 4
    low = sum(r[3] for r in ordered[:q]) / q
    high = sum(r[3] for r in ordered[-q:]) / q
    return high / max(low, 1e-9), sum(r[3] for r in rows) / len(rows)


def section_variants():
    print("\n6. Misspecification variants")
    check("the five documented variants exist",
          S.VARIANTS == ("main", "weak_symptoms", "shock_driven", "logging_gaps", "novel_archetype"))
    main = S.generate_fleet(60, 400, seed=5)
    weak = S.generate_fleet(60, 400, seed=5, variant="weak_symptoms")
    shock = S.generate_fleet(60, 400, seed=5, variant="shock_driven")
    gaps = S.generate_fleet(60, 400, seed=5, variant="logging_gaps")
    novel = S.generate_fleet(30, 200, seed=5, variant="novel_archetype")

    g_main, g_weak = _wear_reject_gap(main), _wear_reject_gap(weak)
    print(f"     reject-rate gap, highest vs lowest wear quartile: main {g_main:.4f}, weak_symptoms {g_weak:.4f}")
    check("weak_symptoms: the wear-driven reject excess is at most half of main's", 0 < g_weak <= 0.5 * g_main,
          f"{g_main:.4f} {g_weak:.4f}")
    check("weak_symptoms shares main's breakdowns exactly (common random numbers)",
          all(main.truth[i].breakdown_days == weak.truth[i].breakdown_days for i in main.truth)
          and all(a.events == b.events for a, b in zip(main.histories, weak.histories)))
    check("weak_symptoms changes the symptoms", any(a.production != b.production
                                                    for a, b in zip(main.histories, weak.histories)))

    r_main, p_main = _quartile_ratio(main)
    r_shock, p_shock = _quartile_ratio(shock)
    print(f"     wear quartile risk ratio: main {r_main:.2f}, shock_driven {r_shock:.2f}; "
          f"prevalence {p_main:.3f} vs {p_shock:.3f}")
    check("shock_driven: wear explains far less of the failure risk", r_shock < 0.6 * r_main, f"{r_main:.2f} {r_shock:.2f}")
    check("shock_driven keeps prevalence in the same 3-10% band", 0.03 <= p_shock <= 0.10, f"{p_shock:.4f}")

    n_main = sum(len(mh.downtime) for mh in main.histories)
    n_gaps = sum(len(mh.downtime) for mh in gaps.histories)
    share = n_gaps / n_main
    check("logging_gaps: about 40% of downtime rows go unlogged", 0.54 <= share <= 0.66, f"kept {share:.3f}")
    same_events = all(a.events == b.events for a, b in zip(main.histories, gaps.histories))
    check("logging_gaps leaves the status timeline (and so the labels) untouched", same_events)

    check("novel_archetype: every machine is the held-out archetype",
          {t.archetype for t in novel.truth.values()} == {S.HELD_OUT_ARCHETYPE.name})
    check("the held-out archetype is not one of the training archetypes",
          S.HELD_OUT_ARCHETYPE.name not in {a.name for a in S.ARCHETYPES})


# --------------------------------------------------------------------------- 7
def section_independence():
    print("\n7. Independence from the rule-based scorer and the application")
    forbidden = {"predictive_engine", "ai", "models", "database", "work_order_status", "sqlalchemy",
                 "tenancy", "factory_simulator", "industrial_adapters", "oem_telemetry"}
    err = raises(purity.PurityViolation, purity.assert_no_forbidden_imports, [SYNTHETIC_PY], forbidden, min_files=1)
    check("synthetic.py (and what it imports) never imports the scorer, ai or the DB layer", err is None, str(err))
    err = raises(purity.PurityViolation, purity.assert_stdlib_only, [SYNTHETIC_PY], min_files=1)
    check("synthetic.py is standard library only", err is None, str(err))
    text = open(SYNTHETIC_PY, encoding="utf-8").read()
    check("synthetic.py does not mention the rule scorer's module or functions",
          "predictive_engine" not in text.replace("NEVER predictive_engine", "")
          and "calculate_predictive_risk" not in text)


def main():
    print("=" * 74)
    print("AMP-native AI failure risk: the synthetic fleet generator")
    print("=" * 74)
    started = time.time()
    section_determinism()
    fleet = S.generate_fleet(80, 400, seed=20260917)
    print(f"\n   (80 machines x 400 days generated in {time.time() - started:.1f} s)")
    section_consistency(fleet)
    rows = section_prevalence(fleet)
    section_physics(fleet, rows)
    section_realism(fleet)
    section_variants()
    section_independence()
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


def test_amp_ai_failure_risk_generator():
    """pytest entry point; CI runs this file as a standalone script."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
