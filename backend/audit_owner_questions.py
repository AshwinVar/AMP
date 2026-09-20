"""The owner's eight questions, asked of four factory shapes at once.

WHY THIS EXISTS, WHEN NINE SUITES ALREADY PASS
----------------------------------------------
Every surface built this sprint has its own suite, and each one is strict about
its own rules. What no per-surface suite can check is the rules that only exist
BETWEEN surfaces:

  * **Money.** ADR-0010 says a loss is shown in money only where the company set
    a unit value. Each card obeys that in its own tests. But the promise the
    owner hears is "AMP never invents a number", and that promise is broken the
    moment ONE of nine screens prices a loss the other eight refused to price.
    Nothing checks all nine against one unpriced factory.

  * **Isolation.** Each suite seeds its own tenant and proves its own query is
    scoped. A leak that only appears when two factories exist in one process,
    on a surface that was tested alone, is invisible to all of them.

  * **The empty plant.** The evaluation has already caught this twice: empty
    stock reported as "healthy", an empty plant reported as "All 0 machines
    running". Those are not arithmetic errors; they are a sentence written for
    a factory that has data, shown to one that has none. A brand-new tenant is
    the first thing a prospect sees, and it is the shape least likely to be
    seeded in a per-surface fixture.

So this audit asks the eight questions the sprint promised an owner could
answer, across four shapes, through the SAME functions the routes call:

    FACTORY_A   healthy, unit value set        -> money may appear
    FACTORY_B   in trouble, NO unit value      -> money must appear NOWHERE
    FACTORY_C   partial coverage, no plan      -> coverage stated, not implied
    FACTORY_D   brand new, nothing seeded      -> "not set up", never "healthy"

A, B and C come from copilot_eval.fixtures -- the three-factory environment
ADR-0022 already built, with colliding identifiers and per-tenant markers. This
audit adds only the fourth shape, because an empty tenant is what nothing else
seeds.

Run: python backend/audit_owner_questions.py
"""
import json
import os
import re
import sys

os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import models  # noqa: E402
import tenancy  # noqa: E402
from database import Base  # noqa: E402
from ai import evidence as ev  # noqa: E402
from copilot_eval import fixtures as F  # noqa: E402

FAILURES = []
CHECKS = 0

A, B, C = F.A, F.B, F.C
D = "FACTORY_D"                      # the shape nothing else seeds: brand new
SHAPES = (A, B, C, D)

# The eight questions the sprint promised an owner could answer, and the surface
# that answers each. A surface may answer more than one; a question with no
# surface is a promise with nothing behind it.
QUESTIONS = {
    "what is happening": ("command_centre", "brief"),
    "what is going wrong": ("command_centre", "brief"),
    "why": ("root_cause",),
    "what does it cost": ("command_centre", "root_cause"),
    "what is likely to become a problem": ("risk_radar", "shortage"),
    "what to do next": ("command_centre", "brief", "proactive"),
    "who does it": ("command_centre", "proactive"),
    "did the action help": ("outcomes",),
}


def check(label, condition, detail=""):
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{label}: {detail}")


def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    Session = sessionmaker(bind=engine)
    F.seed(Session)
    # The fourth shape: a company that has signed up and done nothing else. A
    # TenantConfig row and no unit value, which is what registration creates.
    db = Session()
    tok = tenancy.set_current_tenant(None)
    try:
        db.add(models.TenantConfig(tenant_code=D, unit_value_gbp=None))
        db.commit()
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()
    return Session


# ---------------------------------------------------------------------------
# Every surface, through the same call the route makes.
# ---------------------------------------------------------------------------
def surfaces(Session, tenant):
    """The nine payloads an owner's session produces, keyed by surface."""
    from ai.anomaly_sweep import build_anomaly_sweep
    from ai.brief import build_daily_brief
    from ai.command_centre import build_command_centre
    from ai.outcomes import build_outcome_summary
    from ai.proactive import build_proactive
    from ai.risk_radar import build_risk_radar
    from ai.root_cause import explain_production_gap
    from ai.shortage import build_shortage_impact
    from amp_ai import consent
    from amp_ai.telemetry_anomaly import service

    db = Session()
    tok = tenancy.set_current_tenant(tenant)
    try:
        out = {
            "command_centre": build_command_centre(db, tenant),
            "root_cause": explain_production_gap(db, tenant),
            "risk_radar": build_risk_radar(db, tenant),
            "brief": build_daily_brief(db, tenant),
            "shortage": build_shortage_impact(db, tenant),
            "proactive": build_proactive(db, tenant),
            # freeze=False: reading the owner's screen must not write.
            "outcomes": build_outcome_summary(db, tenant, freeze=False),
            "anomaly": build_anomaly_sweep(db, tenant, scorer=service.score_machine,
                                           gate=consent.DbConsentGate()),
        }
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()
    return out


# ---------------------------------------------------------------------------
# Walking a payload structurally, rather than trusting its prose.
# ---------------------------------------------------------------------------
MONEY_KEY = re.compile(r"(^|_)(money|cost|priced)(_|$)|_gbp$", re.I)


def walk(node, path=""):
    """Every (path, key, value) leaf in a payload."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk(v, f"{path}.{k}" if path else k)
            if not isinstance(v, (dict, list)):
                yield f"{path}.{k}" if path else k, k, v
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f"{path}[{i}]")


def money_figures(payload):
    """Every place this payload puts an actual NUMBER on money.

    Structural, not textual: a key whose name is about money holding a non-null
    number. `"priced": false` and `"cost": {"state": "NOT CONFIGURED"}` are not
    money figures -- they are AMP saying it will not give one, which is the
    behaviour being protected.
    """
    found = []
    for path, key, value in walk(payload):
        if MONEY_KEY.search(key) and isinstance(value, (int, float)) \
                and not isinstance(value, bool):
            found.append((path, value))
    return found


# Patterns, not literals: the defect the evaluation actually caught was "All 0
# machines are running", and a fixed string cannot express the count in it.
CLAIMS = {
    "healthy": r"healthy",
    "all N machines running": r"all \d+ machines? (?:are|is) running",
    "all running": r"\ball running\b",
    "on track": r"on track",
    "no problems": r"no problems",
    "nothing to report": r"nothing to report",
    "plant is clear": r"plant is clear",
}
NEGATION = re.compile(r"\b(not|never|cannot|can't|no|nothing|neither)\b", re.I)


def claims_made(text):
    """Which of the CLAIMS this text ASSERTS, as opposed to denies.

    A bare substring search encodes the wrong rule. "This is not a report that
    stock is healthy" contains "healthy" and is the OPPOSITE of the claim being
    guarded against -- it is AMP refusing to make it. So the unit is the
    sentence, and a sentence that negates is not an assertion.

    Deliberately crude, and deliberately strict: any NEW way of claiming a plant
    is fine still trips this, because a sentence has to actually contain a
    negation to be excused. The CONTROL below proves the scan still bites.
    """
    found = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        low = sentence.lower()
        if NEGATION.search(low):
            continue
        found += [name for name, pattern in CLAIMS.items() if re.search(pattern, low)]
    return found


def numbers_in(text):
    return re.findall(r"\d[\d,]*\.?\d*", text or "")


def prose(payload):
    """Every human-facing sentence in a payload, joined."""
    bits = []
    for _, key, value in walk(payload):
        if isinstance(value, str) and (
                key in ("headline", "summary", "note", "reason", "detail", "title",
                        "say", "phrase", "action", "why", "text", "label")):
            bits.append(value)
    return " ".join(bits)


def main():
    Session = session()
    print("=" * 74)
    print("THE OWNER'S EIGHT QUESTIONS, ACROSS FOUR FACTORY SHAPES")
    print("=" * 74)
    print("A healthy+priced | B in trouble, UNPRICED | C partial | D brand new")

    payloads = {t: surfaces(Session, t) for t in SHAPES}

    print()
    print("=" * 74)
    print("1. EVERY QUESTION HAS A SURFACE, AND EVERY SURFACE ANSWERS")
    print("=" * 74)
    # A surface that raises, returns None, or comes back without a state is not
    # an answer. An honest "I cannot tell you" IS one, and must be labelled.
    for question, needed in QUESTIONS.items():
        for surface in needed:
            check(f"'{question}' is answered by {surface}",
                  surface in payloads[A], f"no such surface: {surface}")
    for t in SHAPES:
        for surface, payload in payloads[t].items():
            check(f"{t}/{surface} returns a payload with a state",
                  isinstance(payload, dict) and payload.get("state") in ev.DATA_STATES,
                  f"state={payload.get('state') if isinstance(payload, dict) else payload!r}")
            check(f"{t}/{surface} says something in words",
                  bool((payload.get("headline") or payload.get("summary") or "").strip()),
                  json.dumps(payload)[:160])

    print()
    print("=" * 74)
    print("2. MONEY APPEARS ONLY WHERE A UNIT VALUE IS SET — ON EVERY SURFACE")
    print("=" * 74)
    # The control first: without it, an empty result below would prove nothing,
    # because a scan that finds money nowhere is indistinguishable from a scan
    # that is broken.
    priced = {s: money_figures(p) for s, p in payloads[A].items()}
    check("CONTROL: the priced factory DOES show money somewhere",
          any(priced.values()),
          "no surface priced anything for a factory with a unit value")
    for tenant in (B, D):
        for surface, payload in payloads[tenant].items():
            found = money_figures(payload)
            check(f"{tenant}/{surface} puts NO figure on money",
                  not found, str(found[:3]))
    check("...and the unpriced factory still SAYS why there is no figure",
          "unit value" in prose(payloads[B]["command_centre"]).lower(),
          prose(payloads[B]["command_centre"])[:200])

    print()
    print("=" * 74)
    print("3. NO FACTORY'S WORDS REACH ANOTHER'S SCREEN")
    print("=" * 74)
    # All four exist in one database, with the SAME machine names, line names
    # and work-order numbers. A query keyed on a name instead of on tenant and
    # name leaks here and nowhere else.
    for tenant in SHAPES:
        blob = json.dumps(payloads[tenant])
        for other, markers in F.MARKERS.items():
            if other == tenant:
                continue
            hits = [m for m in markers if m in blob]
            check(f"{tenant} shows none of {other}'s markers", not hits, str(hits[:3]))
    check("CONTROL: a factory DOES show its own markers",
          any(m in json.dumps(payloads[B]) for m in F.MARKERS[B]),
          "the marker search finds nothing at all, so it proves nothing")

    print()
    print("=" * 74)
    print("4. THE BRAND-NEW FACTORY IS NEVER CALLED HEALTHY")
    print("=" * 74)
    # The two defects the evaluation already caught, generalised: a sentence
    # written for a factory with data, shown to one with none.
    for surface, payload in payloads[D].items():
        wrong = claims_made(prose(payload))
        check(f"D/{surface} claims nothing about a plant it cannot see",
              not wrong, f"{wrong} in: {prose(payload)[:200]}")
        check(f"D/{surface} states an honest data state",
              payload.get("state") in (ev.NO_DATA, ev.NOT_CONFIGURED, ev.PARTIAL_DATA,
                                       ev.NOT_MEASURED, ev.INSUFFICIENT_HISTORY,
                                       ev.MODEL_NOT_VALIDATED),
              f"state={payload.get('state')}")
    # "0 machines" is a true count; "all 0 machines are running" is a claim
    # about machines that do not exist. The difference is a verb.
    empty_prose = prose(payloads[D]["command_centre"]).lower()
    check("D does not conjugate a verb over zero machines",
          not re.search(r"all \d+ machines (are|is) ", empty_prose), empty_prose[:160])
    # CONTROLS for the claim scan itself, in both directions. Without the first,
    # a scan that never fires would pass every check above; without the second,
    # the fix for it would be to make every sentence mention "not".
    # The second sentence is verbatim the shape of a defect the evaluation
    # already caught in this codebase ("All 0 machines are running").
    caught = claims_made("Stock is healthy. All 0 machines are running.")
    check("CONTROL: an asserted claim is caught",
          caught == ["healthy", "all N machines running"], str(caught))
    check("CONTROL: a denial of the same claim is not",
          not claims_made("This is not a report that stock is healthy."),
          str(claims_made("This is not a report that stock is healthy.")))

    print()
    print("=" * 74)
    print("5. PARTIAL COVERAGE IS STATED, NOT IMPLIED")
    print("=" * 74)
    # FACTORY_C has three machines and only two report. A plant-level figure
    # from two of three is a different claim from a plant-level figure, and
    # ADR-0014's contract says the difference must be on screen.
    c_blob = json.dumps(payloads[C]).lower()
    check("C says how much of the plant its figures came from",
          re.search(r"\d+ of \d+", c_blob) is not None,
          "no 'N of M' coverage phrase anywhere in C's payloads")
    for surface in ("command_centre", "brief"):
        said = prose(payloads[C][surface]).lower()
        check(f"C/{surface} does not present a partial figure as the whole plant",
              ("of 3" in said or "coverage" in said or "not measured" in said
               or "no data" in said or "partial" in said or "2 of" in said
               or payloads[C][surface]["state"] != ev.OK),
              f"state={payloads[C][surface]['state']}: {said[:200]}")

    print()
    print("=" * 74)
    print("6. READING THE OWNER'S SCREEN WRITES NOTHING")
    print("=" * 74)
    # Every surface above was built twice over (once per shape, in one process).
    # If any of them recorded something, a second read would differ -- and an
    # owner opening a dashboard would be changing the record they are reading.
    before = {t: json.dumps(payloads[t], sort_keys=True, default=str) for t in SHAPES}
    again = {t: surfaces(Session, t) for t in SHAPES}
    for t in SHAPES:
        a = json.loads(before[t])
        b = json.loads(json.dumps(again[t], sort_keys=True, default=str))
        for surface in a:
            # Timestamps move between the two reads; nothing else may.
            pa = re.sub(r"\d{4}-\d{2}-\d{2}[T ][\d:.]+", "<ts>",
                        json.dumps(a[surface], sort_keys=True))
            pb = re.sub(r"\d{4}-\d{2}-\d{2}[T ][\d:.]+", "<ts>",
                        json.dumps(b[surface], sort_keys=True))
            check(f"{t}/{surface} reads the same twice", pa == pb,
                  f"first={pa[:120]} second={pb[:120]}")

    print()
    print("=" * 74)
    if FAILURES:
        print(f"{len(FAILURES)} FAILED of {CHECKS}")
        for f in FAILURES:
            print(" -", f)
        print("=" * 74)
        return 1
    print(f"ALL {CHECKS} CHECKS PASSED")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
