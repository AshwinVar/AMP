"""The Copilot's plant-board tools: a figure, or an honest nothing — never a zero.

WHY THIS SUITE EXISTS
---------------------
The plant board is the one read-model whose every figure is a CONVERSION away
from anything a machine holds. A controller counts shots; parts, kilograms and
rupees exist only once a person has entered a PartSpec for the part the fitted
mould makes. So the board has three conversions that can each be missing on
their own, and two series that are missing for everybody:

    parts       measured, always
    target      needs a declared cycle time
    kilograms   needs a part weight
    rupees      needs a price per piece
    power       NO SOURCE: no energy meter is fitted
    packing     NO SOURCE: nobody records packed quantities

`ai/plant_board.py` returns 0.0 for the conversions it cannot do, because a
chart needs a number to draw, and it marks power and packing unavailable with a
reason. A tool that passed those zeros through to the Copilot would report a
plant that consumed no material, earned nothing and used no electricity — three
false claims, each indistinguishable from a true reading once it is a figure in
a sentence. test_plant_board.py pins that property in the read-model; this file
pins that it SURVIVES into the Copilot's answer.

WHAT IS PINNED
  * a machine with no part spec still has its parts COUNTED, and its target, its
    kilograms and its rupees come back UNKNOWN with a reason — not 0
  * the three conversions are independent: a spec with a weight but no price
    yields kilograms and an unknown rate, not a rate of zero
  * power and packing are UNKNOWN for every tenant, carrying the builder's own
    reason and fix, and never a point that could be drawn at zero
  * a question about power is answered NOT MEASURED, not with a figure
  * every fact names the DAY or the MONTH it covers, never the tools' usual
    "last 7 days" default
  * the sentence AMP writes passes the grounding gate against its own evidence,
    and names no money at all where nothing is priced
  * the tenant is the principal's; another workspace's output is not on the board
  * the month tool's "no part assigned" bucket matches plant_board's own label

Run: DATABASE_URL="sqlite:///./ci_board_tool.db" python backend/test_copilot_plant_board_tool.py
"""
import os
import sys
from datetime import date, datetime, timedelta

# '₹' has no cp1252 code point and this suite prints money. Without this the
# suite dies on the line announcing its own success, on a Windows console, after
# every assertion has held (test_currency_single.py asserts this line is here).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("DATABASE_URL", "sqlite:///./ci_board_tool.db")

import database   # noqa: E402
import models     # noqa: E402
from ai import evidence as ev   # noqa: E402
from ai import grounding        # noqa: E402
from ai import plant_board      # noqa: E402
from ai.tools import Principal, run_tool   # noqa: E402
from currency import CURRENCY   # noqa: E402

failures = []
T = "BOARDTOOL-TEST"
OTHER = "BOARDTOOL-OTHER"
DAY = date(2026, 10, 7)

# The customer's own part (Shrinidhi Plastics' Ele clip): 56 cavities all active,
# a 14 s cycle, 0.33 g, Rs 0.09 a piece. 3600/14 x 56 = 14,400 parts an hour.
PRICED = dict(part_code="ELE-CLIP", part_name="Ele clip", material="PP H 110",
              part_weight_g=0.33, cavities=56, active_cavities=56,
              ideal_cycle_time_s=14.0, price_per_piece=0.09)
# A part somebody specced but never priced: kilograms derivable, money not.
UNPRICED = dict(part_code="CAP-02", part_name="Bottle cap", material="ABS NAT",
                part_weight_g=0.5, cavities=1, active_cavities=1,
                ideal_cycle_time_s=7.2, price_per_piece=0.0)


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def section(t):
    print("\n" + "=" * 74 + f"\n{t}\n" + "=" * 74)


def wipe(db):
    for tenant in (T, OTHER):
        for M in (models.ProductionRecord, models.ToolAsset, models.PartSpec, models.Machine):
            db.query(M).filter(M.tenant_code == tenant).delete()
    db.commit()


def machine(db, tenant, name):
    m = models.Machine(tenant_code=tenant, site="", name=name, status="Running")
    db.add(m)
    db.flush()
    return m


def fit(db, tenant, m, spec, tool_no):
    """Give a machine a part spec and the mould that names it."""
    db.add(models.PartSpec(tenant_code=tenant, effective_from=date(2026, 1, 1), **spec))
    db.add(models.ToolAsset(tenant_code=tenant, tool_no=tool_no, name=f"{spec['part_name']} mould",
                            machine_id=m.id, cavities=spec["active_cavities"],
                            part_code=spec["part_code"], status="Active"))


def produce(db, tenant, m, hour, parts):
    db.add(models.ProductionRecord(
        tenant_code=tenant, machine_id=m.id, total_count=parts, good_count=parts,
        rejected_count=0, planned_minutes=60, runtime_minutes=55, ideal_cycle_time_seconds=14,
        created_at=datetime.combine(DAY, datetime.min.time()) + timedelta(hours=hour)))
    db.commit()


def setup(db):
    """Three machines, three different truths, and a second workspace that must
    stay invisible.

      IMM-01  specced AND priced   14,400 parts in one hour, exactly its ideal
      IMM-02  specced, NOT priced     800 parts over two hours
      IMM-03  no mould at all       1,000 parts and nothing derivable
    """
    wipe(db)
    one = machine(db, T, "IMM-01")
    two = machine(db, T, "IMM-02")
    three = machine(db, T, "IMM-03")
    fit(db, T, one, PRICED, "MLD-ELE")
    fit(db, T, two, UNPRICED, "MLD-CAP")
    db.commit()
    produce(db, T, one, 9, 14400)
    produce(db, T, two, 9, 400)
    produce(db, T, two, 10, 400)
    produce(db, T, three, 9, 1000)
    # Another workspace, same machine names, its own priced spec and its own
    # output. Nothing of it may appear on this tenant's board.
    other = machine(db, OTHER, "IMM-01")
    fit(db, OTHER, other, PRICED, "MLD-ELE")
    db.commit()
    produce(db, OTHER, other, 9, 999999)
    return one, two, three


def ask(db, tool, args=None, tenant=T):
    return run_tool(db, Principal(tenant=tenant, role="Admin", username="tester"), tool, args or {})


def facts_of(result):
    return {f.key: f for f in result.facts}


def unknown(f):
    """An honestly-absent figure: no value, labelled UNKNOWN, with a reason."""
    return f is not None and f.value is None and f.provenance == ev.UNKNOWN and bool(f.detail)


def main():   # noqa: C901 - one section per pinned property, read top to bottom
    models.Base.metadata.create_all(bind=database.engine)
    db = database.SessionLocal()
    try:
        setup(db)
        day = DAY.isoformat()
        res = ask(db, "get_plant_board", {"on": day})
        F = facts_of(res)

        section("1. A PRICED MACHINE: PARTS, TARGET, KILOGRAMS AND RUPEES")
        check("the tool answered", not res.refused, f"{res.state}: {res.summary}")
        # 3600/14 x 56 active cavities = 14,400 an hour.
        check("the target comes from the spec's cycle and ACTIVE cavities",
              F["board.machine_1.target"].value == 14400, str(F.get("board.machine_1.target")))
        check("...and is DERIVED, not measured",
              F["board.machine_1.target"].provenance == ev.DERIVED)
        check("IMM-01 is the machine with the most parts",
              F["board.machine_1"].value == "IMM-01", str(F["board.machine_1"].value))
        check("its parts are a MEASURED count",
              F["board.machine_1.parts"].value == 14400
              and F["board.machine_1.parts"].provenance == ev.MEASURED)
        # 14,400 x 0.33 g = 4,752 g = 4.752 kg
        check("14,400 parts at 0.33 g is 4.752 kg", F["board.machine_1.kg"].value == 4.752,
              str(F["board.machine_1.kg"].value))
        # 14,400 x Rs 0.09 = Rs 1,296 in one 8-hour block = Rs 162 an hour
        check("the shift-hour rate is the block's revenue over its 8 hours",
              F["board.machine_1.rate"].value == 162.0, str(F["board.machine_1.rate"].value))
        check("...carried in the platform's currency",
              F["board.machine_1.rate"].unit == CURRENCY, F["board.machine_1.rate"].unit)
        check("the plant's best rate names where it was",
              F["board.best_rate"].value == 162.0
              and F["board.best_rate_where"].value == "IMM-01 shift 2",
              f"{F['board.best_rate'].value} at {F['board.best_rate_where'].value}")
        check("the day's revenue is priced output only", F["board.revenue"].value == 1296.0,
              str(F["board.revenue"].value))
        # 4.752 (IMM-01) + 0.4 (IMM-02); IMM-03 contributes nothing because
        # nothing about it is known, which is not the same as contributing zero.
        check("plant kilograms add up the specced machines", F["board.kg"].value == 5.152,
              str(F["board.kg"].value))
        check("...and say what they do NOT cover",
              "not a plant total" in F["board.kg"].detail, F["board.kg"].detail)

        section("2. A MACHINE WITH NO PART SPEC: COUNTED, NEVER CONVERTED")
        check("IMM-03 is on the board at all", F["board.machine_2"].value == "IMM-03",
              str(F["board.machine_2"].value))
        check("its parts ARE counted", F["board.machine_2.parts"].value == 1000
              and F["board.machine_2.parts"].provenance == ev.MEASURED,
              str(F["board.machine_2.parts"]))
        for what in ("target", "kg", "rate"):
            f = F[f"board.machine_2.{what}"]
            check(f"...its {what} is UNKNOWN with a reason, NOT 0", unknown(f),
                  f"value={f.value!r} provenance={f.provenance}")
            check(f"...the {what} reason says it is underivable rather than empty",
                  "underivable" in f.detail or "cannot be converted" in f.detail, f.detail)
        check("the board counts the machines it cannot convert",
              F["board.unspecified"].value == 1, str(F["board.unspecified"].value))
        check("...and every machine's parts are still in the plant total",
              F["board.parts"].value == 14400 + 800 + 1000, str(F["board.parts"].value))

        section("3. THE THREE CONVERSIONS ARE INDEPENDENT")
        # IMM-02 has a weight and a cycle but no price. Kilograms and a target
        # exist; money does not. A tool that treated "specced" as one flag would
        # either invent a rate of zero or throw the kilograms away.
        check("IMM-02 is the third machine shown", F["board.machine_3"].value == "IMM-02",
              str(F["board.machine_3"].value))
        check("an unpriced spec still yields a target",
              F["board.machine_3.target"].value == 500, str(F["board.machine_3.target"].value))
        # 800 x 0.5 g = 400 g = 0.4 kg
        check("...and still yields kilograms", F["board.machine_3.kg"].value == 0.4,
              str(F["board.machine_3.kg"].value))
        check("...but its money rate is UNKNOWN, not 0", unknown(F["board.machine_3.rate"]),
              f"value={F['board.machine_3.rate'].value!r}")
        check("...and the reason names the missing price",
              "price" in F["board.machine_3.rate"].detail, F["board.machine_3.rate"].detail)

        section("4. POWER AND PACKING: NO SOURCE, NEVER ZERO")
        for key in ("power", "packing"):
            f = F[f"board.{key}"]
            check(f"{key} is UNKNOWN with a reason, not a figure", unknown(f),
                  f"value={f.value!r} provenance={f.provenance}")
            check(f"...{key} is never reported as 0", f.value != 0 and f.value is None, repr(f.value))
            check(f"...{key} says it is unavailable rather than empty",
                  "not as 0" in f.detail, f.detail)
        check("the power reason is the builder's own, not a copy",
              plant_board.day(db, T, DAY)["power"]["reason"] in F["board.power"].detail,
              F["board.power"].detail)
        check("the fix is carried too, so the answer says what would measure it",
              plant_board.day(db, T, DAY)["power"]["fix"] in F["board.power"].detail)

        section("5. EVERY FACT NAMES THE DAY IT COVERS")
        stale = [k for k, f in F.items() if f.window != day]
        check("no fact falls back to the tools' usual 7-day window", not stale, str(stale[:4]))
        check("the threshold that decides 'below target' is a RULE, and is stated",
              F["board.acceptable"].provenance == ev.RULE
              and F["board.acceptable"].value == round(plant_board.ACCEPTABLE * 100),
              str(F["board.acceptable"]))
        check("the worst machine is one with a target, and its hours are a RULE",
              F["board.worst_machine"].value == "IMM-01"
              and F["board.worst_machine_hours"].provenance == ev.RULE,
              f"{F['board.worst_machine'].value} / {F['board.worst_machine_hours'].value}")
        check("a board with two unmeasured series never claims to be complete",
              res.state == ev.PARTIAL_DATA, res.state)

        section("6. THE SENTENCE IS GROUNDED IN ITS OWN EVIDENCE")
        g = grounding.check(res.summary, res.to_dict()["facts"], "what was our shift rate")
        check("every number AMP shows is in the evidence", g.passed,
              f"ungrounded={g.ungrounded_numbers} unknown={g.unknown_identifiers}")
        check("the sentence says power and packing have no source",
              "power" in res.summary.lower() and "no source" in res.summary.lower(), res.summary)

        section("7. ANOTHER WORKSPACE'S OUTPUT IS NOT ON THIS BOARD")
        check("the other tenant's 999,999 parts are nowhere in the result",
              "999999" not in str(res.to_dict()) and "999,999" not in res.summary)
        other = ask(db, "get_plant_board", {"on": day}, tenant=OTHER)
        check("...and that tenant sees its own figure instead",
              facts_of(other)["board.parts"].value == 999999,
              str(facts_of(other)["board.parts"].value))

        section("8. NOTHING SPECCED AT ALL: UNKNOWN EVERYWHERE, AND NO MONEY WORD")
        wipe(db)
        bare = machine(db, T, "IMM-09")
        produce(db, T, bare, 9, 500)
        res2 = ask(db, "get_plant_board", {"on": day})
        F2 = facts_of(res2)
        check("the shot count survives", F2["board.parts"].value == 500, str(F2["board.parts"].value))
        for key in ("board.kg", "board.revenue", "board.best_rate", "board.worst_machine_hours"):
            check(f"{key} is UNKNOWN, not 0", unknown(F2[key]), f"value={F2[key].value!r}")
        check("the state says NOT MEASURED rather than reporting an empty plant",
              res2.state == ev.NOT_MEASURED, res2.state)
        # A factory with no price must not see a money figure at all. The
        # evaluation enforces the same rule for a factory with no unit value.
        money = [f.key for f in res2.facts
                 if f.unit == CURRENCY and isinstance(f.value, (int, float))]
        check("no rupee FIGURE is produced where nothing is priced", not money, str(money))
        check("...and the sentence names no currency either", CURRENCY not in res2.summary,
              res2.summary)
        check("the sentence is still grounded",
              grounding.check(res2.summary, res2.to_dict()["facts"]).passed, res2.summary)

        section("9. A QUESTION ABOUT POWER IS ANSWERED 'NOT MEASURED'")
        p = ask(db, "get_plant_power")
        PF = facts_of(p)
        check("the result's state is NOT MEASURED", p.state == ev.NOT_MEASURED, p.state)
        check("it is not a refusal: the answer is a fact about the plant, not an error",
              not p.refused)
        for key in ("board.power", "board.packing"):
            check(f"{key} is UNKNOWN with a reason", unknown(PF[key]), f"value={PF[key].value!r}")
        check("it carries no figure of any kind",
              not [f for f in p.facts if isinstance(f.value, (int, float))],
              str([f.key for f in p.facts if isinstance(f.value, (int, float))]))
        # Asserts the PROPERTY the label claims, not one phrasing of it. This
        # pinned the literal "no energy meter", which stopped being the wording
        # the moment power became measurable -- a test of a sentence rather than
        # of a behaviour.
        said = p.summary.lower()
        check("the sentence says why, and what would measure it",
              "power" in said and "meter" in said and "cannot report" in said, p.summary)
        check("it is grounded", grounding.check(p.summary, p.to_dict()["facts"]).passed, p.summary)

        section("9b. WHEN A MACHINE DOES MEASURE POWER, THE COPILOT REPORTS IT")
        # The board and the copilot must not disagree about the same fact. Power
        # was unavailable by construction until a controller turned out to have
        # been counting kWh per hour for fourteen months; a copilot still saying
        # "AMP cannot report power" beside a chart drawing it is worse than
        # either surface being silent.
        setup(db)
        metered = db.query(models.Machine).filter(
            models.Machine.tenant_code == T, models.Machine.name == "IMM-01").first()
        # get_plant_power asks the board about TODAY, so the fixture has to be
        # today -- seeding it on DAY would test an empty board and pass for the
        # wrong reason.
        today = datetime.utcnow().date()
        for hour, kwh in ((9, 3.8), (10, 3.6)):
            db.add(models.ProductionRecord(
                tenant_code=T, machine_id=metered.id, total_count=200, good_count=200,
                rejected_count=0, planned_minutes=60, runtime_minutes=60,
                ideal_cycle_time_seconds=14, energy_kwh=kwh,
                created_at=datetime.combine(today, datetime.min.time())
                + timedelta(hours=hour)))
        db.commit()

        q = ask(db, "get_plant_power")
        QF = facts_of(q)
        check("power comes back MEASURED, not unknown",
              QF["board.power"].value == 7.4, str(QF["board.power"].value))
        check("...with the kWh unit kept", QF["board.power"].unit == "kWh",
              str(QF["board.power"].unit))
        check("...and the sentence states the figure",
              "7.4" in q.summary and "kwh" in q.summary.lower(), q.summary)
        check("...no longer claiming AMP cannot report it",
              "cannot report power" not in q.summary.lower(), q.summary)
        check("PACKING is still honestly unknown beside it",
              unknown(QF["board.packing"]), str(QF["board.packing"].value))
        check("...so the state says part of the answer is missing",
              q.state == ev.PARTIAL_DATA, str(q.state))
        check("it is grounded", grounding.check(q.summary, q.to_dict()["facts"]).passed,
              q.summary)

        section("10. THE MONTH TABLES KEEP THE SAME PROPERTY")
        setup(db)
        m = ask(db, "get_plant_board_month", {"year": DAY.year, "month": DAY.month})
        MF = facts_of(m)
        window = f"{DAY.year}-{DAY.month:02d}"
        check("the month answered", not m.refused, f"{m.state}: {m.summary}")
        check("every fact names the month", not [k for k, f in MF.items() if f.window != window],
              str([k for k, f in MF.items() if f.window != window][:4]))
        check("the month totals every part made",
              MF["month.parts"].value == 14400 + 800 + 1000, str(MF["month.parts"].value))
        # 14,400 x 0.33 g + 800 x 0.5 g = 4.752 + 0.4 kg
        check("kilograms are the sum over the materials", MF["month.kg"].value == 5.152,
              str(MF["month.kg"].value))
        check("output with no spec is counted and named as such",
              MF["month.unassigned"].value == 1000, str(MF["month.unassigned"].value))
        check("...and is not silently folded into a part's total",
              all(MF[k].value != _bucket() for k in MF if k.endswith(("part_1", "part_2", "part_3"))),
              "a named part row carried the unassigned bucket's label")
        check("the month's money is priced output only", MF["month.revenue"].value == 1296.0,
              str(MF["month.revenue"].value))
        for key in ("power", "packing"):
            check(f"the month's {key} is UNKNOWN too", unknown(MF[f"month.{key}"]),
                  f"value={MF[f'month.{key}'].value!r}")
        check("the month sentence is grounded",
              grounding.check(m.summary, m.to_dict()["facts"]).passed, m.summary)

        section("11. A MONTH WITH NO SPEC AT ALL IS UNKNOWN, NOT EMPTY")
        wipe(db)
        bare = machine(db, T, "IMM-09")
        produce(db, T, bare, 9, 500)
        m2 = ask(db, "get_plant_board_month", {"year": DAY.year, "month": DAY.month})
        MF2 = facts_of(m2)
        check("the parts are counted", MF2["month.parts"].value == 500, str(MF2["month.parts"].value))
        check("kilograms are UNKNOWN, not 0 kg", unknown(MF2["month.kg"]),
              f"value={MF2['month.kg'].value!r}")
        check("the money is UNKNOWN, not 0", unknown(MF2["month.revenue"]),
              f"value={MF2['month.revenue'].value!r}")
        check("the plant's monthly rate is UNKNOWN, not 0", unknown(MF2["month.rate"]),
              f"value={MF2['month.rate'].value!r}")
        check("the state says NOT MEASURED", m2.state == ev.NOT_MEASURED, m2.state)
        check("no rupee figure appears",
              not [f.key for f in m2.facts
                   if f.unit == CURRENCY and isinstance(f.value, (int, float))])

        section("12. THE DAY ARGUMENT IS THE ROUTE'S, AND A BAD ONE IS REFUSED")
        setup(db)
        check("an explicit ISO day is read",
              facts_of(ask(db, "get_plant_board", {"on": day}))["board.date"].value == day)
        today = ask(db, "get_plant_board", {"on": "today"})
        check("\"today\" is accepted",
              facts_of(today)["board.date"].value == datetime.utcnow().date().isoformat())
        yday = ask(db, "get_plant_board", {"on": "yesterday"})
        check("\"yesterday\" is the day before it",
              facts_of(yday)["board.date"].value
              == (datetime.utcnow().date() - timedelta(days=1)).isoformat())
        bad = ask(db, "get_plant_board", {"on": "last Tuesday"})
        check("a day the REST route would reject is refused here too",
              bad.state == ev.INVALID_ARGUMENTS and not bad.facts, f"{bad.state}: {bad.summary}")
        check("...and the refusal says what a day looks like", "YYYY-MM-DD" in bad.summary,
              bad.summary)
        # Scope is AMP's, from the authenticated principal. The registry refuses a
        # parameter that names one, so there is nothing to pass.
        sneak = ask(db, "get_plant_board", {"on": day, "tenant": OTHER})
        check("an argument naming another workspace is refused, not ignored",
              sneak.state == ev.INVALID_ARGUMENTS, f"{sneak.state}: {sneak.summary}")

        section("13. THE MONTH'S UNASSIGNED LABEL IS plant_board's OWN")
        wipe(db)
        bare = machine(db, T, "IMM-09")
        produce(db, T, bare, 9, 7)
        rows = plant_board.month(db, T, DAY.year, DAY.month)["itemwise_production"]
        check("plant_board still labels unattributed output exactly as the tool expects",
              any(r["part"] == _bucket() for r in rows),
              f"the tool reads {_bucket()!r}; the builder returned {[r['part'] for r in rows]}")

        section("14. THE TWO ENDS: AN EMPTY WORKSPACE, AND A FULLY SPECCED ONE")
        wipe(db)
        empty = ask(db, "get_plant_board", {"on": day})
        check("a workspace with no machines says NO DATA, not a plant of zeroes",
              empty.state == ev.NO_DATA, empty.state)
        check("...and counts its machines rather than inventing a figure",
              facts_of(empty)["board.machines"].value == 0)
        check("...with no kilogram or money fact at all to be misread",
              not [f.key for f in empty.facts if f.unit in ("kg", CURRENCY)],
              str([f.key for f in empty.facts]))
        check("...and the sentence is grounded",
              grounding.check(empty.summary, empty.to_dict()["facts"]).passed, empty.summary)

        # The other end: every part made IS attributable, so the month must not
        # carry the "made with no part spec" clause at all.
        wipe(db)
        only = machine(db, T, "IMM-01")
        fit(db, T, only, PRICED, "MLD-ELE")
        db.commit()
        produce(db, T, only, 9, 14400)
        full = ask(db, "get_plant_board_month", {"year": DAY.year, "month": DAY.month})
        check("a fully specced month reports nothing unassigned",
              facts_of(full)["month.unassigned"].value == 0,
              str(facts_of(full)["month.unassigned"].value))
        check("...and its sentence does not claim unspecced output",
              "no part spec" not in full.summary, full.summary)
        check("...and is still grounded",
              grounding.check(full.summary, full.to_dict()["facts"]).passed, full.summary)
        wipe(db)
    finally:
        db.close()


def _bucket():
    from ai.tools import factory
    return factory._NO_PART_BUCKET


# ── Collected by pytest as well as run as a script ────────────────────
#
# CI's per-file runner (`python test_copilot_plant_board_tool.py`) is the
# contract and is unchanged. The separate coverage job collects every suite into
# ONE pytest process to compute the branch-coverage floor, and pytest only
# collects module-level `test_*` functions -- a suite with none counts every line
# it exercises as untested. conftest.py describes the intended shape.
def test_everything():
    """The whole suite as one case, failing with whatever it recorded."""
    code = None
    try:
        code = main()
    except SystemExit as exc:          # several suites exit from inside main()
        code = exc.code
    assert not failures, "\n  " + "\n  ".join(str(f) for f in failures)
    assert code in (0, None), f"the suite exited with {code}"


if __name__ == "__main__":
    main()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED - the Copilot's plant-board tools")
    print("=" * 74)
