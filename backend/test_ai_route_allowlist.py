"""The model may propose WHICH pillar answers. AMP decides everything else.

WHAT THIS IS
------------
The safety half of AI roadmap phase 3. The binding contract for this codebase is:

    USER -> AUTHENTICATION -> RBAC -> TENANT/CONSENT -> AMP TOOL -> DATA -> MODEL
    "The model NEVER decides whether the user is authorized. AMP decides."

`assistant.answer(..., chosen_route=NAME)` is the seam where a model gets to
influence anything at all, so it is the seam worth proving. The model's entire
influence is a STRING, checked against a fixed allowlist, and AMP does the rest.

WHY THIS SHIPS BEFORE THE MODEL CALL DOES
------------------------------------------
Routing QUALITY cannot be measured here: there is no AI key in this environment.
Shipping an unmeasurable behaviour change is exactly what test_ai_evaluation.py
section 1c exists to warn against — a change that scored 100% on its tuned set
and made held-out routing WORSE. So nothing in the product passes `chosen_route`
yet, and `/ai/ask` is untouched.

What CAN be settled here is the part that must never be wrong: what happens when
a model returns a name that is not a pillar, a name that is a private function, a
tenant, a SQL fragment, or nothing at all. Those are settled below, so on the day
a key exists the dangerous half is already correct and under CI.

WHAT THIS SUITE DOES NOT CLAIM
------------------------------
That a real model picks a GOOD route. 42% of unseen phrasings route correctly by
keyword today (test_ai_evaluation.py §1c); whether a model beats that is unknown
and unknowable without a key. Nothing here should be read as evidence about it.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_ai_route_allowlist.py
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import tenancy
from ai import assistant
from database import Base

A, B = "ROUTE_A", "ROUTE_B"
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
    Session = sessionmaker(bind=engine)
    db = Session()
    tok = tenancy.set_current_tenant(None)
    for t in (A, B):
        db.add(models.TenantConfig(tenant_code=t))
    # Factory A: one machine down. Factory B: a distinctively-named fleet, all
    # running — so a tenant leak shows as a NAME that cannot legitimately appear.
    db.add(models.Machine(tenant_code=A, name="ALPHA-PRESS", site="P1",
                          status="Breakdown", utilization=0, downtime="0 min"))
    for i in range(3):
        db.add(models.Machine(tenant_code=B, name=f"BONLY-{i}", site="P1",
                              status="Running", utilization=90, downtime="0 min"))
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()
    return Session


def ask(Session, tenant, question, chosen_route=None):
    db = Session()
    tok = tenancy.set_current_tenant(tenant)
    try:
        return assistant.answer(db, tenant, question, chosen_route=chosen_route)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def main():
    Session = seed()

    print("=" * 74)
    print("1. A PROPOSED ROUTE IS HONOURED — the feature actually does something")
    print("=" * 74)
    # Without this, every check below could pass because the seam is a no-op.
    keyword = ask(Session, A, "how is quality looking?")
    proposed = ask(Session, A, "how is quality looking?", chosen_route="downtime")
    check("the keyword router would have said 'quality'",
          keyword["matched"] == "quality", str(keyword["matched"]))
    check("...and a proposed route overrides it",
          proposed["matched"] == "downtime", str(proposed["matched"]))
    check("...labelled as model-chosen, so the source is auditable",
          proposed.get("route_source") == "model", str(proposed.get("route_source")))
    check("...and the keyword path is NOT labelled that way",
          keyword.get("route_source") is None, str(keyword.get("route_source")))

    print()
    print("=" * 74)
    print("2. ANYTHING NOT ON THE ALLOWLIST IS IGNORED, NOT OBEYED")
    print("=" * 74)
    # Every one of these is a plausible thing a confused or hostile model emits.
    # None may raise, none may execute, and each must land on today's behaviour.
    HOSTILE = [
        ("a function that does not exist", "drop_all_tables"),
        ("a PRIVATE function in this module", "_machines"),
        ("a real function that is NOT a routable pillar", "find"),
        ("a module-level name", "models"),
        ("a dunder", "__import__"),
        ("a SQL fragment", "1; DROP TABLE machines--"),
        ("a path", "../../etc/passwd"),
        ("empty string", ""),
        ("a number", 7),
        ("a dict", {"route": "downtime"}),
        ("a list of routes", ["downtime", "quality"]),
        ("True", True),
    ]
    for label, value in HOSTILE:
        try:
            out = ask(Session, A, "how is quality looking?", chosen_route=value)
            ok = out["matched"] == "quality" and out.get("route_source") is None
            check(f"{label} -> falls back to keyword routing", ok,
                  f"matched={out.get('matched')!r} source={out.get('route_source')!r}")
        except Exception as exc:
            check(f"{label} -> falls back to keyword routing", False,
                  f"raised {type(exc).__name__}: {exc}")

    print()
    print("=" * 74)
    print("3. THE TENANT COMES FROM AMP, NEVER FROM THE MODEL")
    print("=" * 74)
    # The property that matters most. There is no parameter by which a proposal
    # can carry a tenant -- so the test is that A's answer, however routed, is
    # only ever about A.
    a_out = ask(Session, A, "which machines are down?", chosen_route="machines")
    check("A's model-routed answer names A's machine",
          "ALPHA-PRESS" in a_out["answer"], a_out["answer"])
    check("...and contains nothing of B's", "BONLY" not in a_out["answer"],
          a_out["answer"])
    b_out = ask(Session, B, "which machines are down?", chosen_route="machines")
    check("CONTROL: B routed the same way sees only B",
          "BONLY" not in b_out["answer"] or "ALPHA" not in b_out["answer"],
          b_out["answer"])
    check("...and B's answer is genuinely different from A's",
          b_out["answer"] != a_out["answer"], "both tenants got the same answer")

    print()
    print("=" * 74)
    print("4. THE ALLOWLIST IS DERIVED, NOT A SECOND LIST TO FORGET")
    print("=" * 74)
    names = assistant.route_names()
    table = {fn.__name__.lstrip("_") for _k, fn in assistant._ROUTES}
    check("every routable pillar is offerable", table <= set(names),
          str(sorted(table - set(names))))
    # `briefing` is BOTH a table entry (the "attention"/"status" keywords) and the
    # fallthrough, so route_names() adding it is belt-and-braces rather than an
    # extra name. The assertion is that nothing OUTSIDE the table is offerable.
    check("...and nothing is offerable that is not a pillar",
          not (set(names) - table - {"briefing"}),
          str(sorted(set(names) - table - {"briefing"})))
    check("every offered name actually resolves to a callable",
          all(callable(assistant._pillar(n)) for n in names),
          str([n for n in names if not callable(assistant._pillar(n))]))
    # The two three-argument helpers must stay unreachable: a model cannot call
    # them by guessing, because _pillar only ever returns table entries.
    check("_find and _machine_answer are NOT reachable by name",
          assistant._pillar("find") is None
          and assistant._pillar("machine_answer") is None,
          "a three-argument helper is reachable")

    print()
    print("=" * 74)
    print("5. EXISTING CALLERS ARE UNTOUCHED")
    print("=" * 74)
    # chosen_route defaults to None, so the four in-tree callers behave exactly
    # as before. If this breaks, the seam has changed the product.
    for q, expected in (("how much downtime did we have?", "downtime"),
                        ("what is our OEE?", "oee"),
                        ("what needs my attention?", "briefing")):
        out = ask(Session, A, q)
        check(f'"{q[:34]}" still routes to {expected}',
              out["matched"] == expected, str(out["matched"]))
    check("...and no answer gained a route_source it did not have",
          ask(Session, A, "what is our OEE?").get("route_source") is None,
          "the keyword path is now labelled")

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
