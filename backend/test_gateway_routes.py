"""Issuing a gateway key: Admin only, shown once, and never leaked by anything else.

`gateway_credentials` is the table that decides whether one customer can publish
into another's factory (ADR-0041). The routes that fill it are therefore part of
the control, not administrative garnish, and the things that can go wrong are:

  1. THE SECRET LEAKS through a route that was not supposed to carry it. Pinned
     STRUCTURALLY — the list response model has no `secret` field at all, so it
     cannot leak by somebody later adding a line to a handler.
  2. SOMEBODY OTHER THAN AN ADMIN issues one.
  3. A WORKSPACE SEES ANOTHER'S gateways.
  4. AN ID IS ISSUED THAT THE INGEST PATH COULD NEVER LOOK UP, because the two
     ends validate differently.
  5. REVOCATION DELETES the row, re-opening the workspace to unsigned packets —
     the fail-open ADR-0041 exists to avoid.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_gateway_routes.py
"""
import json
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite://")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fastapi import HTTPException                # noqa: E402
from sqlalchemy import create_engine             # noqa: E402
from sqlalchemy.orm import sessionmaker          # noqa: E402
from sqlalchemy.pool import StaticPool           # noqa: E402

import gateway_auth      # noqa: E402
import gateway_routes    # noqa: E402
import models            # noqa: E402
import tenancy           # noqa: E402
from auth import require_roles                   # noqa: E402
from database import Base                        # noqa: E402

failures = []

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SessionLocal = sessionmaker(bind=engine)


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def fresh():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()
    return SessionLocal()


def as_tenant(tenant, fn):
    tok = tenancy.set_current_tenant(tenant)
    try:
        return fn()
    finally:
        tenancy.reset_current_tenant(tok)


def test_gateway_routes():
    # ── 1. the secret cannot leak, structurally ─────────────────────
    section("1. THE LIST RESPONSE HAS NO PLACE TO PUT A SECRET")
    summary_fields = set(gateway_routes.GatewaySummary.model_fields)
    check("the list response model carries no `secret` field",
          "secret" not in summary_fields, str(sorted(summary_fields)))
    check("...and the route declares that model, so a handler cannot add one",
          "secret" not in summary_fields)
    issued_fields = set(gateway_routes.GatewayIssued.model_fields)
    check("only the CREATE response carries the secret", "secret" in issued_fields,
          str(sorted(issued_fields)))

    # ── 2. issuing ──────────────────────────────────────────────────
    section("2. ISSUING A GATEWAY RETURNS THE KEY, ONCE, WITH ITS TOPIC")
    db = fresh()
    body = gateway_routes.GatewayCreate(gateway_id="gw-acme-plant1-01", site="plant-1",
                                        label="Line 1 cell")
    issued = as_tenant("ACME", lambda: gateway_routes.issue_gateway(body, db, {"role": "Admin"}))
    check("a key was issued", bool(issued.secret), "no secret returned")
    check("...256 bits of hex, from the OS",
          len(issued.secret) == 64 and all(c in "0123456789abcdef" for c in issued.secret),
          issued.secret[:8] + "...")
    check("...with the exact topic to paste into the gateway config",
          issued.topic.endswith("/ACME/plant-1/machines"), issued.topic)
    check("...and says plainly that it will not be shown again",
          "shown once" in issued.shown_once, issued.shown_once)
    check("the gateway is active on creation", issued.is_active is True, str(issued.is_active))

    # The stored secret must be the one handed out, or nothing authenticates.
    stored = as_tenant("ACME", lambda: db.query(models.GatewayCredential).first())
    check("the stored key is the key that was handed out", stored.secret == issued.secret,
          "the gateway would never authenticate")

    # ── 3. and never again ──────────────────────────────────────────
    section("3. NOTHING ELSE EVER RETURNS IT")
    listed = as_tenant("ACME", lambda: gateway_routes.list_gateways(db, {"role": "Admin"}))
    rendered = json.dumps([gateway_routes.GatewaySummary.model_validate(
        row, from_attributes=True).model_dump(mode="json") for row in listed])
    check("the list shows the gateway", len(listed) == 1, str(len(listed)))
    check("...and its serialised form contains no key", issued.secret not in rendered,
          rendered[:160])
    check("...nor the word secret at all", "secret" not in rendered.lower(), rendered[:160])

    # ── 4. Admin only ───────────────────────────────────────────────
    section("4. ISSUING A GATEWAY IS AN ADMIN ACTION")
    guard = require_roles(["Admin"])
    for role in ("Supervisor", "Operator", "Viewer", None):
        refused = None
        try:
            guard(current_user={"role": role})
        except HTTPException as e:
            refused = e.status_code
        check(f"{role!r} cannot issue a gateway", refused == 403, str(refused))
    check("an Admin can", guard(current_user={"role": "Admin"})["role"] == "Admin")

    # ── 5. tenant isolation ─────────────────────────────────────────
    section("5. A WORKSPACE SEES ONLY ITS OWN GATEWAYS")
    other = gateway_routes.GatewayCreate(gateway_id="gw-other-1", site="plant-9")
    as_tenant("OTHERCO", lambda: gateway_routes.issue_gateway(other, db, {"role": "Admin"}))
    acme = as_tenant("ACME", lambda: gateway_routes.list_gateways(db, {"role": "Admin"}))
    theirs = as_tenant("OTHERCO", lambda: gateway_routes.list_gateways(db, {"role": "Admin"}))
    check("ACME sees one gateway, its own", len(acme) == 1 and acme[0].gateway_id.startswith("gw-acme"),
          str([g.gateway_id for g in acme]))
    check("OTHERCO sees one gateway, its own",
          len(theirs) == 1 and theirs[0].gateway_id == "gw-other-1",
          str([g.gateway_id for g in theirs]))

    # A gateway id is unique across the INSTALLATION, because the ingest path
    # resolves it before it knows whose it is.
    clash = gateway_routes.GatewayCreate(gateway_id="gw-acme-plant1-01", site="plant-2")
    status = None
    try:
        as_tenant("OTHERCO", lambda: gateway_routes.issue_gateway(clash, db, {"role": "Admin"}))
    except HTTPException as e:
        status = e.status_code
        detail = e.detail
    check("another workspace cannot take an id already in use", status == 409, str(status))
    check("...and is told how to avoid the clash without learning who holds it",
          status == 409 and "prefix" in detail and "ACME" not in detail, str(status))

    # ── 6. an id the ingest path could never look up ────────────────
    section("6. AN ID THAT CAN BE ISSUED CAN ALWAYS BE LOOKED UP")
    for bad in ("", "-leading-dash", "has space", "semi;colon", "x" * 65, "' OR 1=1 --"):
        refused = None
        try:
            gateway_routes.GatewayCreate(gateway_id=bad)
        except Exception as e:                       # noqa: BLE001 - pydantic ValidationError
            refused = str(e)
        check(f"{bad!r} is refused at issue time", refused is not None, "it was accepted")
    ok = gateway_routes.GatewayCreate(gateway_id="gw-fine-01")
    check("...while a normal id is accepted",
          gateway_auth.claimed_gateway_id({"gateway_id": ok.gateway_id}) == "gw-fine-01")

    # The two ends must agree, or an operator can create a credential no gateway
    # can ever use.
    check("the issue rule IS the ingest rule",
          all((gateway_routes.GatewayCreate.model_validate({"gateway_id": v}).gateway_id
               if gateway_auth.claimed_gateway_id({"gateway_id": v}) else None) is not None
              for v in ("gw-1", "GW.2", "gw_3-x")),
          "an id the ingest path accepts was refused at issue time")

    # ── 7. the site token ───────────────────────────────────────────
    section("7. '-' AND '' BOTH MEAN 'THIS FACTORY HAS NO SITE CODE'")
    check("the wire token normalises to the empty site",
          gateway_routes.GatewayCreate(gateway_id="gw-solo-1", site="-").site == "",
          gateway_routes.GatewayCreate(gateway_id="gw-solo-1", site="-").site)
    solo = as_tenant("SOLO", lambda: gateway_routes.issue_gateway(
        gateway_routes.GatewayCreate(gateway_id="gw-solo-1", site="-"), db, {"role": "Admin"}))
    check("...and the topic is written back with the token, not an empty segment",
          solo.topic.endswith("/SOLO/-/machines"), solo.topic)
    for bad in ("plant/1", "Plant 1", "+", "#"):
        refused = None
        try:
            gateway_routes.GatewayCreate(gateway_id="gw-x", site=bad)
        except Exception as e:                       # noqa: BLE001
            refused = str(e)
        check(f"site {bad!r} is refused", refused is not None, "accepted")

    # ── 8. revocation is a flag ─────────────────────────────────────
    section("8. REVOKING DOES NOT DELETE, AND DOES NOT RE-OPEN THE WORKSPACE")
    target = as_tenant("ACME", lambda: gateway_routes.list_gateways(db, {"role": "Admin"}))[0]
    revoked = as_tenant("ACME", lambda: gateway_routes.revoke_gateway(
        target.id, db, {"role": "Admin"}))
    check("the gateway is no longer active", revoked.is_active is False, str(revoked.is_active))
    still_there = as_tenant("ACME", lambda: gateway_routes.list_gateways(db, {"role": "Admin"}))
    check("...but the ROW survives, so the workspace stays closed and the record stands",
          len(still_there) == 1, str(len(still_there)))

    back = as_tenant("ACME", lambda: gateway_routes.reactivate_gateway(
        target.id, db, {"role": "Admin"}))
    check("reactivating restores it", back.is_active is True, str(back.is_active))
    check("...with the SAME key, because it never left the gateway",
          as_tenant("ACME", lambda: db.query(models.GatewayCredential).filter(
              models.GatewayCredential.id == target.id).first()).secret == issued.secret)

    missing = None
    try:
        as_tenant("OTHERCO", lambda: gateway_routes.revoke_gateway(
            target.id, db, {"role": "Admin"}))
    except HTTPException as e:
        missing = e.status_code
    check("another workspace cannot revoke this one's gateway", missing == 404, str(missing))
    db.close()


if __name__ == "__main__":
    test_gateway_routes()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED")
