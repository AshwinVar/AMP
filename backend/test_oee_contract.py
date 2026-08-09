"""Golden datasets for the canonical OEE contract (ADR-0014).

Every expected value below is derived BY HAND from the definitions in
oee_contract's docstring, written out longhand in the comment beside it. None is
copied from a program run. That is the point of a golden dataset: if the
implementation and the expectation are both produced by the same code, the test
says only that the code equals itself.

WHAT WAS MEASURED BEFORE THIS EXISTED
-------------------------------------
    surface                                       window                OEE
    ----------------------------------------------------------------------
    /oee-summary  (the dashboard)                 last 7 days           100%
    ai_copilot LLM context                        last 10 records        10%
    pooled over everything in the table           all time               17%

and, separately, a machine losing its network connection:

    plant OEE while the worst machine still reports : 67%
    plant OEE after its gateway goes silent         : 94%   (+27 points)

Run: DATABASE_URL="sqlite:///./ci.db" python test_oee_contract.py
"""
import os
import sys
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import oee_contract as OC
import tenancy
from database import Base

_URL = os.environ.get("DATABASE_URL", "")
if _URL.startswith("postgresql"):
    engine = create_engine(_URL)
else:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
SessionLocal = sessionmaker(bind=engine)

failures = []
_open = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def fresh_db():
    while _open:
        try:
            _open.pop().close()
        except Exception:
            pass
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    _open.append(db)
    return db


NOW = datetime(2026, 8, 9, 12, 0, 0)


def seed(db, tenant, machines, records):
    """machines: [name]; records: [(machine_name, when, planned, runtime, ideal,
    total, good)]"""
    tok = tenancy.set_current_tenant(None)
    ids = {}
    for name in machines:
        m = models.Machine(tenant_code=tenant, site="", name=name,
                           status="Running", utilization=50, downtime="0 min")
        db.add(m)
        db.flush()
        ids[name] = m.id
    for name, when, planned, runtime, ideal, total, good in records:
        db.add(models.ProductionRecord(
            tenant_code=tenant, machine_id=ids[name], planned_minutes=planned,
            runtime_minutes=runtime, ideal_cycle_time_seconds=ideal,
            total_count=total, good_count=good, rejected_count=max(total - good, 0),
            created_at=when))
    db.commit()
    tenancy.reset_current_tenant(tok)
    return ids


# =====================================================================
def test_golden_1_a_perfect_shift():
    """480 planned, 480 run, 60s ideal, 480 made, 480 good.

        A = 480 / 480                     = 1.0
        P = (60 x 480) / (480 x 60)
          = 28800 / 28800                 = 1.0
        Q = 480 / 480                     = 1.0
        OEE = 1.0 x 1.0 x 1.0             = 1.0   -> 100%
    """
    db = fresh_db()
    seed(db, "T", ["M1"], [("M1", NOW - timedelta(hours=1), 480, 480, 60, 480, 480)])
    print("=" * 74)
    print("GOLDEN 1. A PERFECT SHIFT")
    print("=" * 74)
    r = OC.as_percentages(OC.plant_oee(db, "T", OC.OeeWindow(7, now=NOW)))
    check("A=100 P=100 Q=100 OEE=100",
          (r["availability"], r["performance"], r["quality"], r["oee"]) == (100, 100, 100, 100),
          str((r["availability"], r["performance"], r["quality"], r["oee"])))


def test_golden_2_the_textbook_example():
    """The standard worked example every OEE reference uses.

        480 planned, 400 run (80 min of stops), 30s ideal, 600 made, 570 good

        A = 400 / 480                     = 0.833333...
        P = (30 x 600) / (400 x 60)
          = 18000 / 24000                 = 0.75
        Q = 570 / 600                     = 0.95
        OEE = 0.833333 x 0.75 x 0.95      = 0.59375   -> 59%
        A -> 83%,  P -> 75%,  Q -> 95%
    """
    db = fresh_db()
    seed(db, "T", ["M1"], [("M1", NOW - timedelta(hours=1), 480, 400, 30, 600, 570)])
    print()
    print("=" * 74)
    print("GOLDEN 2. THE TEXTBOOK EXAMPLE")
    print("=" * 74)
    r = OC.as_percentages(OC.plant_oee(db, "T", OC.OeeWindow(7, now=NOW)))
    check("A=83 P=75 Q=95 OEE=59",
          (r["availability"], r["performance"], r["quality"], r["oee"]) == (83, 75, 95, 59),
          str((r["availability"], r["performance"], r["quality"], r["oee"])))


def test_golden_3_pooling_beats_averaging():
    """Two machines of very different size — the case that distinguishes pooling.

        M1 (large, perfect): planned 480, run 480, ideal 60, made 480, good 480
        M2 (tiny, terrible): planned  60, run  30, ideal 60, made  10, good   5

        sums: planned 540, runtime 510, total 490, good 485
              ideal_seconds = 60x480 + 60x10 = 28800 + 600 = 29400

        A = 510 / 540                     = 0.944444...
        P = 29400 / (510 x 60)
          = 29400 / 30600                 = 0.960784...
        Q = 485 / 490                     = 0.989796...
        OEE = 0.944444 x 0.960784 x 0.989796
            = 0.898098...                 -> 90%

    For contrast, per-machine OEE: M1 = 100%, M2 = A 0.5 x P 0.3333 x Q 0.5
    = 8.33% -> 8%. A naive average of those is (100 + 8) / 2 = 54%. The pooled
    answer is 90%. Thirty-six points apart, and the pooled one is right: M2
    contributed 10 parts out of 490.
    """
    db = fresh_db()
    seed(db, "T", ["M1", "M2"], [
        ("M1", NOW - timedelta(hours=1), 480, 480, 60, 480, 480),
        ("M2", NOW - timedelta(hours=1), 60, 30, 60, 10, 5),
    ])
    print()
    print("=" * 74)
    print("GOLDEN 3. POOLING, NOT AVERAGING")
    print("=" * 74)
    r = OC.as_percentages(OC.plant_oee(db, "T", OC.OeeWindow(7, now=NOW)))
    check("A=94 P=96 Q=99 OEE=90",
          (r["availability"], r["performance"], r["quality"], r["oee"]) == (94, 96, 99, 90),
          str((r["availability"], r["performance"], r["quality"], r["oee"])))
    check("CONTROL: the naive average (54) is NOT what is reported",
          r["oee"] != 54, f"got {r['oee']}")


def test_golden_4_unplanned_is_not_zero():
    """A weekend with no shift scheduled.

        planned 0, runtime 0, made 0, good 0

    Availability = runtime / planned is UNDEFINED, not zero: nothing was
    scheduled, so there was nothing to be efficient at. Reporting 0% invents a
    loss. has_data must be False.
    """
    db = fresh_db()
    seed(db, "T", ["M1"], [("M1", NOW - timedelta(hours=1), 0, 0, 60, 0, 0)])
    print()
    print("=" * 74)
    print("GOLDEN 4. AN UNPLANNED PERIOD IS NOT 0% OEE")
    print("=" * 74)
    raw = OC.plant_oee(db, "T", OC.OeeWindow(7, now=NOW))
    check("has_data is False — a row exists but nothing was measurable",
          raw["has_data"] is False, str(raw["has_data"]))
    check("availability is None (undefined), not 0.0",
          raw["availability"] is None, repr(raw["availability"]))
    check("OEE is None, not 0", raw["oee"] is None, repr(raw["oee"]))
    pct = OC.as_percentages(raw)
    check("rendered, it is None rather than a measured 0",
          pct["oee"] is None, repr(pct["oee"]))


def test_golden_5_ran_but_made_nothing_IS_zero():
    """The control for golden 4: this really is a measured zero.

        planned 480, runtime 480, made 0, good 0

        A = 480/480 = 1.0
        P = (60 x 0) / (480 x 60) = 0 / 28800 = 0.0
        Q = undefined (nothing made)

    The machine was scheduled and ran the whole shift and produced nothing.
    Availability is a real 100%, performance a real 0%. Quality is still
    undefined — you cannot have a defect rate with no units.
    """
    db = fresh_db()
    seed(db, "T", ["M1"], [("M1", NOW - timedelta(hours=1), 480, 480, 60, 0, 0)])
    print()
    print("=" * 74)
    print("GOLDEN 5. SCHEDULED, RAN, PRODUCED NOTHING — A REAL ZERO")
    print("=" * 74)
    raw = OC.plant_oee(db, "T", OC.OeeWindow(7, now=NOW))
    check("has_data is True — 480 minutes were scheduled", raw["has_data"] is True)
    check("availability is a measured 1.0", raw["availability"] == 1.0,
          repr(raw["availability"]))
    check("performance is a measured 0.0", raw["performance"] == 0.0,
          repr(raw["performance"]))
    check("quality is None — no units, so no defect rate",
          raw["quality"] is None, repr(raw["quality"]))
    check("CONTROL: golden 4 and golden 5 are distinguishable",
          raw["has_data"] is True, "both would read as 0% under the old contract")


def test_golden_6_scrap():
    """100 made, 90 good.  Q = 90/100 = 0.9 -> 90%."""
    db = fresh_db()
    seed(db, "T", ["M1"], [("M1", NOW - timedelta(hours=1), 480, 480, 60, 100, 90)])
    print()
    print("=" * 74)
    print("GOLDEN 6. SCRAP")
    print("=" * 74)
    r = OC.as_percentages(OC.plant_oee(db, "T", OC.OeeWindow(7, now=NOW)))
    check("quality = 90%", r["quality"] == 90, str(r["quality"]))


def test_golden_7_impossible_values_are_clamped_both_ways():
    """Legacy rows the ingest guards cannot retro-fix.

        good 120 of 100 made      -> Q would be 1.2, clamped to 1.0  -> 100%
        runtime 600 of 480 planned-> A would be 1.25, clamped to 1.0 -> 100%
        good -20                  -> Q would be -0.2, clamped to 0.0 ->   0%

    A percentage the data cannot support is a lie in either direction.
    """
    db = fresh_db()
    print()
    print("=" * 74)
    print("GOLDEN 7. IMPOSSIBLE VALUES CLAMP TO [0, 100]")
    print("=" * 74)
    over_q = OC.as_percentages(OC.oee_from_sums(480, 480, 100, 120, 60 * 100))
    check("good > total clamps quality to 100", over_q["quality"] == 100,
          str(over_q["quality"]))
    over_a = OC.as_percentages(OC.oee_from_sums(480, 600, 100, 100, 60 * 100))
    check("runtime > planned clamps availability to 100",
          over_a["availability"] == 100, str(over_a["availability"]))
    neg = OC.as_percentages(OC.oee_from_sums(480, 480, 100, -20, 60 * 100))
    check("a negative good count clamps quality to 0", neg["quality"] == 0,
          str(neg["quality"]))


def test_golden_8_the_window_is_half_open():
    """[start, end) — so adjacent windows tile without sharing a record.

    A record exactly ON the start boundary is IN. A record exactly on the end
    boundary is OUT. Previously each caller chose, and a record on the cutoff
    could be counted twice across a week-over-week pair.
    """
    db = fresh_db()
    w = OC.OeeWindow(7, now=NOW)
    seed(db, "T", ["M1"], [
        ("M1", w.start, 480, 480, 60, 480, 480),                     # exactly at start
        ("M1", w.end, 480, 10, 60, 10, 1),                           # exactly at end
        ("M1", w.start - timedelta(seconds=1), 480, 10, 60, 10, 1),  # just before
    ])
    print()
    print("=" * 74)
    print("GOLDEN 8. THE WINDOW IS [start, end)")
    print("=" * 74)
    r = OC.plant_oee(db, "T", w)
    # Only the boundary-start record is inside, so the sums are exactly its own.
    check("the record ON the start boundary is included",
          r["planned_minutes"] == 480 and r["total_count"] == 480,
          f"planned={r['planned_minutes']} total={r['total_count']}")
    check("the record ON the end boundary is excluded",
          r["runtime_minutes"] == 480, f"runtime={r['runtime_minutes']}")
    check("the record before the window is excluded",
          OC.as_percentages(r)["oee"] == 100, str(OC.as_percentages(r)["oee"]))

    # CONTROL: two adjacent windows share no record.
    prior = OC.OeeWindow(7, now=w.start)
    p = OC.plant_oee(db, "T", prior)
    check("CONTROL: the prior window picks up the earlier record and not this one",
          p["planned_minutes"] == 480 and p["runtime_minutes"] == 10,
          f"planned={p['planned_minutes']} runtime={p['runtime_minutes']}")


def test_golden_9_coverage_exposes_a_silent_machine():
    """The survivorship fix.

    Three machines; the worst one's gateway goes silent, so it has no records in
    the window. The pooled figure necessarily improves — that part is
    unavoidable, the data is gone. What must NOT happen is that improvement
    being presented as a whole-plant number.
    """
    db = fresh_db()
    w = OC.OeeWindow(7, now=NOW)
    recs = []
    for d in range(7):
        when = NOW - timedelta(days=d, hours=1)
        recs.append(("GOOD-1", when, 480, 460, 60, 460, 455))
        recs.append(("GOOD-2", when, 480, 455, 60, 455, 450))
    seed(db, "T", ["GOOD-1", "GOOD-2", "SILENT-3"], recs)

    print()
    print("=" * 74)
    print("GOLDEN 9. A SILENT MACHINE IS VISIBLE IN THE COVERAGE")
    print("=" * 74)
    r = OC.plant_oee(db, "T", w)
    cov = r["coverage"]
    check("3 machines expected", cov["machines_expected"] == 3,
          str(cov["machines_expected"]))
    check("only 2 reported", cov["machines_reporting"] == 2,
          str(cov["machines_reporting"]))
    check("coverage is 67%, not silently 100%", cov["coverage_pct"] == 67,
          str(cov["coverage_pct"]))
    check("the result declares itself incomplete", cov["complete"] is False)

    # CONTROL: when every machine reports, coverage says so — otherwise
    # "complete is False" above would be satisfied by a flag that is never True.
    tok = tenancy.set_current_tenant(None)
    mid = db.query(models.Machine).filter(models.Machine.name == "SILENT-3").first().id
    db.add(models.ProductionRecord(
        tenant_code="T", machine_id=mid, planned_minutes=480, runtime_minutes=100,
        ideal_cycle_time_seconds=60, total_count=100, good_count=60,
        rejected_count=40, created_at=NOW - timedelta(hours=1)))
    db.commit()
    tenancy.reset_current_tenant(tok)
    r2 = OC.plant_oee(db, "T", w)
    check("CONTROL: once it reports, coverage is 100% and complete",
          r2["coverage"]["coverage_pct"] == 100 and r2["coverage"]["complete"] is True,
          str(r2["coverage"]))
    check("CONTROL: and the OEE drops, because the bad machine is back in",
          OC.as_percentages(r2)["oee"] < OC.as_percentages(r)["oee"],
          f"{OC.as_percentages(r)['oee']} -> {OC.as_percentages(r2)['oee']}")


def test_golden_10_isolation_between_tenants():
    """Two tenants, same machine names, different production. Neither sees the
    other's minutes — the contract filters by tenant explicitly, not through the
    ADR-0002 hook, because exports and the AI context builder call it with
    nothing bound."""
    db = fresh_db()
    w = OC.OeeWindow(7, now=NOW)
    seed(db, "A", ["CNC-01"], [("CNC-01", NOW - timedelta(hours=1), 480, 480, 60, 480, 480)])
    seed(db, "B", ["CNC-01"], [("CNC-01", NOW - timedelta(hours=1), 480, 240, 60, 240, 120)])

    print()
    print("=" * 74)
    print("GOLDEN 10. TENANT ISOLATION, WITH NO TENANT BOUND")
    print("=" * 74)
    check("CONTROL: nothing is bound, so the ORM hook cannot help",
          tenancy.current_tenant() is None, str(tenancy.current_tenant()))
    a = OC.as_percentages(OC.plant_oee(db, "A", w))
    b = OC.as_percentages(OC.plant_oee(db, "B", w))
    # B: A = 240/480 = 0.5, P = (60x240)/(240x60) = 1.0, Q = 120/240 = 0.5
    #    OEE = 0.5 x 1.0 x 0.5 = 0.25 -> 25%
    check("tenant A reads 100%", a["oee"] == 100, str(a["oee"]))
    check("tenant B reads 25%", b["oee"] == 25, str(b["oee"]))
    check("each sees only its own machine",
          a["coverage"]["machines_expected"] == 1 and b["coverage"]["machines_expected"] == 1,
          f"A={a['coverage']} B={b['coverage']}")


def test_golden_11_every_surface_reports_the_same_number():
    """The 90-point divergence, pinned.

    Recent week excellent, older history dreadful. A surface windowing by ROW
    COUNT sees only the old rows; a surface windowing by TIME sees the good
    week. Measured before the contract existed:

        /oee-summary  (the dashboard)     last 7 days     100%
        ai_copilot LLM context            last 10 records  10%

    Both must now answer with the same window and the same number.

    SEEDED AGAINST THE LIVE CLOCK, not the fixed NOW the other goldens use.
    ai_copilot builds its context with datetime.utcnow() and takes no window
    argument, so a fixture dated even a few hours ahead of the real clock falls
    outside the window entirely and the test measures nothing. That is the same
    clock-basis trap test_date_basis_guard exists for, reached from the other
    direction.
    """
    import ai.oee as dashboard
    import ai_copilot

    live = datetime.utcnow()
    db = fresh_db()
    recs = []
    for d in range(7):                       # a perfect recent week
        recs.append(("M1", live - timedelta(days=d, hours=1), 480, 480, 60, 480, 480))
    for d in range(30, 120):                 # a dreadful older history
        recs.append(("M1", live - timedelta(days=d, hours=1), 480, 100, 60, 100, 50))
    seed(db, "T", ["M1"], recs)

    print()
    print("=" * 74)
    print("GOLDEN 11. THE DASHBOARD AND THE AI ASSISTANT AGREE")
    print("=" * 74)

    tok = tenancy.set_current_tenant("T")
    dash = dashboard.build_oee_summary(db, "T")["plant"]["oee"]
    context = ai_copilot._build_factory_context(db, "T")
    tenancy.reset_current_tenant(tok)

    line = next((l for l in context.splitlines() if l.startswith("PLANT OEE")), "")
    check("the AI context states a plant OEE at all", bool(line), repr(context[:120]))
    check("it names the window it used", "last 7 days" in line, line)
    check(f"it reports the dashboard's number ({dash}%)", f": {dash}%" in line, line)

    # CONTROL: the all-time answer is genuinely different, so agreeing on the
    # 7-day number is agreement on the WINDOW and not a coincidence of the data.
    all_time = OC.as_percentages(
        OC.plant_oee(db, "T", OC.OeeWindow(None, now=live)))["oee"]
    check("CONTROL: the all-time figure differs, so the window is what matters",
          all_time != dash, f"7-day={dash} all-time={all_time}")
    check("the AI context does NOT report the all-time figure",
          f": {all_time}%" not in line, line)


def test_golden_12_incomplete_coverage_is_stated_to_the_model():
    """A silent machine must be disclosed in the AI context, not just the API.

    The model answers "how is the plant doing" in prose. If it is handed a
    partial figure with no qualifier, it will state it as a whole-plant fact.
    """
    import ai_copilot

    live = datetime.utcnow()
    db = fresh_db()
    recs = []
    for d in range(1, 7):
        recs.append(("GOOD-1", live - timedelta(days=d, hours=1), 480, 460, 60, 460, 455))
    seed(db, "T", ["GOOD-1", "SILENT-2", "SILENT-3"], recs)

    print()
    print("=" * 74)
    print("GOLDEN 12. PARTIAL COVERAGE IS STATED, NOT HIDDEN")
    print("=" * 74)
    tok = tenancy.set_current_tenant("T")
    context = ai_copilot._build_factory_context(db, "T")
    tenancy.reset_current_tenant(tok)
    line = next((l for l in context.splitlines() if l.startswith("PLANT OEE")), "")
    check("the context says how many machines were measured",
          "1 of 3 machines" in line, line)
    check("and gives the coverage percentage", "33%" in line, line)

    # CONTROL: with every machine reporting, no qualifier is added — otherwise
    # the assertion above would pass on a string that always says it.
    tok = tenancy.set_current_tenant(None)
    for name in ("SILENT-2", "SILENT-3"):
        mid = db.query(models.Machine).filter(models.Machine.name == name).first().id
        db.add(models.ProductionRecord(
            tenant_code="T", machine_id=mid, planned_minutes=480,
            runtime_minutes=460, ideal_cycle_time_seconds=60, total_count=460,
            good_count=455, rejected_count=5,
            created_at=live - timedelta(hours=2)))
    db.commit()
    tenancy.reset_current_tenant(tok)
    tok = tenancy.set_current_tenant("T")
    full = ai_copilot._build_factory_context(db, "T")
    tenancy.reset_current_tenant(tok)
    full_line = next((l for l in full.splitlines() if l.startswith("PLANT OEE")), "")
    check("CONTROL: full coverage adds no qualifier",
          "coverage" not in full_line, full_line)


if __name__ == "__main__":
    test_golden_1_a_perfect_shift()
    test_golden_2_the_textbook_example()
    test_golden_3_pooling_beats_averaging()
    test_golden_4_unplanned_is_not_zero()
    test_golden_5_ran_but_made_nothing_IS_zero()
    test_golden_6_scrap()
    test_golden_7_impossible_values_are_clamped_both_ways()
    test_golden_8_the_window_is_half_open()
    test_golden_9_coverage_exposes_a_silent_machine()
    test_golden_10_isolation_between_tenants()
    test_golden_11_every_surface_reports_the_same_number()
    test_golden_12_incomplete_coverage_is_stated_to_the_model()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        sys.exit(1)
    print("ALL OEE GOLDEN DATASETS MATCH THE HAND-COMPUTED VALUES")
