"""Founder-only diagnostics need the founder: an Admin of the platform workspace.

THE DEFECT
----------
Two responses carry data only the founder may see:

* /platform/status adds `sim`: the simulator's tenant allowlist (SIM_TENANTS),
  which names other tenants, with its heartbeat;
* /ai/status adds `last_error`: the last copilot failure, process-wide, so it
  can come from any tenant's request, and "error strings can carry upstream
  details a client workspace shouldn't see".

Both were gated on the WORKSPACE alone (`current_user.tenant == DEFAULT`), on
routes any role may call. Every login inside the founder's workspace (an
Operator demo account, a Supervisor) got both. tenancy.effective_tenant fixed
exactly this class for previews ("ROLE, NOT JUST WORKSPACE"); these two were
never brought in line. (An OEM token, which carries no tenant claim at all,
would also have passed `get("tenant", "DEFAULT") == "DEFAULT"`, but
get_current_user refuses OEM principals on every factory route first.)

THE RULE
--------
tenancy.is_founder(current_user): the token's OWN tenant is the platform workspace
AND its role is Admin. Fail-closed on a missing role or tenant. Both diagnostics,
and the plan/licence edit in PATCH /tenant-config, ask it.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_founder_diagnostics_need_the_founder.py
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import ai_copilot
import core_routes
import tenancy
from database import Base

failures = []

FOUNDER = {"sub": "founder", "tenant": "DEFAULT", "role": "Admin"}
NOT_FOUNDERS = {
    "a founder-workspace Operator (a demo login)": {"sub": "op", "tenant": "DEFAULT", "role": "Operator"},
    "a founder-workspace Supervisor": {"sub": "sup", "tenant": "DEFAULT", "role": "Supervisor"},
    "a founder-workspace token with no role": {"sub": "x", "tenant": "DEFAULT"},
    "a customer's Admin": {"sub": "cust", "tenant": "APEX", "role": "Admin"},
    "an Admin token with no tenant claim": {"sub": "y", "role": "Admin"},
}


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def main():
    db = _session()

    section("1. /platform/status: THE SIM ALLOWLIST NAMES OTHER TENANTS")
    check("CONTROL: the founder sees it", "sim" in core_routes.platform_status(db=db, current_user=FOUNDER))
    for who, user in NOT_FOUNDERS.items():
        check(f"{who} does not", "sim" not in core_routes.platform_status(db=db, current_user=user))

    section("2. /ai/status: THE LAST COPILOT ERROR IS PROCESS-WIDE")
    saved = ai_copilot._LAST_LLM_ERROR
    ai_copilot._LAST_LLM_ERROR = {"at": "2026-09-18T15:00:00", "provider": "anthropic",
                                  "error": "upstream detail from another tenant's request"}
    try:
        check("CONTROL: the founder sees it", "last_error" in ai_copilot.ai_status(current_user=FOUNDER))
        for who, user in NOT_FOUNDERS.items():
            check(f"{who} does not", "last_error" not in ai_copilot.ai_status(current_user=user))
    finally:
        ai_copilot._LAST_LLM_ERROR = saved

    section("3. ONE RULE")
    check("is_founder: the platform workspace's Admin", tenancy.is_founder(FOUNDER) is True)
    check("is_founder: nobody else",
          [who for who, user in NOT_FOUNDERS.items() if tenancy.is_founder(user)] == [],
          str([who for who, user in NOT_FOUNDERS.items() if tenancy.is_founder(user)]))
    check("is_founder: no user at all is not the founder",
          tenancy.is_founder(None) is False and tenancy.is_founder({}) is False)

    section("4. A FOUNDER PREVIEWING A CUSTOMER IS STILL THE FOUNDER")
    # The company switcher binds the customer's tenant for the request; the
    # founder's own claim is unchanged, and that is what the rule reads.
    tok = tenancy.set_current_tenant("APEX")
    try:
        check("is_founder reads the token's claim, not the previewed tenant",
              tenancy.is_founder(FOUNDER) is True and tenancy.request_tenant(FOUNDER) == "APEX")
        check("...so the founder still sees the sim diagnostics while previewing",
              "sim" in core_routes.platform_status(db=db, current_user=FOUNDER))
    finally:
        tenancy.reset_current_tenant(tok)
    db.close()

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


def test_founder_diagnostics_need_the_founder():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
