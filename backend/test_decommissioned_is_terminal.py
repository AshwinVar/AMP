"""A scrapped machine must not come back.

THE DEFECT
----------
`oem_service.LIFECYCLE` says `"Decommissioned": ()` — nothing leaves it. That
invariant is enforced only for writes that go through `transition()`, and the
two writes that change which PARTY holds a machine deliberately do not:

    oem_claims.accept                              -> "Assigned"
    connected_equipment_routes.release_installation -> "Sold"

The bypass is correct for the reason already documented — the row count of a
single conditional UPDATE is what makes a double-claim impossible, and
fetch-then-mutate-then-commit would lose that. What the documentation missed is
that bypassing the state machine also bypasses its TERMINALITY. Measured, before
this fix:

    factory scraps a machine        status -> "Decommissioned"  (terminal)
    factory releases it             status -> "Sold"            (sellable!)
    OEM issues a claim              usable() -> True
    another factory accepts         status -> "Assigned"

At no point does any party see that the machine was condemned. In a factory,
equipment scrapped for a fault being recommissioned at another site is not a
tidiness problem.

NOT A SECURITY DEFECT
---------------------
No tenant reads another tenant's data here, and every step is an action the
acting party is authorised to take. It is an INTEGRITY defect: the system
forgets a fact it recorded, and each party individually sees a legitimate
screen.

WHERE THE GUARD BELONGS
-----------------------
Inside the conditional UPDATE, not in a pre-check beside it. This codebase's own
rule is that the row count IS the decision; a status read before the write is
the fetch-then-mutate race the design exists to avoid. `usable()` is still
updated, because it drives the message the caller sees — but it is the WHERE
clause that has to be right, and section 3 proves the UPDATE refuses even when
the pre-check is bypassed entirely.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_decommissioned_is_terminal.py
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import connected_equipment_routes as cer
import models
import oem_claims
import oem_service
import tenancy
from database import Base

A, B = "FACT_A", "FACT_B"
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def fresh():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    Session = sessionmaker(bind=engine)
    db = Session()
    tok = tenancy.set_current_tenant(None)
    for t in (A, B):
        db.add(models.TenantConfig(tenant_code=t))
    model = models.MachineModel(oem_code="ACME", model_code="M1", name="Press")
    db.add(model)
    db.flush()
    db.commit()
    # Read the id BEFORE closing: commit expires the instance, and a detached
    # one cannot refresh itself.
    model_id = model.id
    tenancy.reset_current_tenant(tok)
    db.close()
    return Session, model_id


def make_installation(Session, model_id, status, tenant):
    db = Session()
    tok = tenancy.set_current_tenant(None)
    inst = models.MachineInstallation(
        oem_code="ACME", serial_number=f"SN-{status}-{tenant}", model_id=model_id,
        factory_tenant_code=tenant, site="P1" if tenant else "", status=status,
        decommissioned_at=datetime.utcnow() if status == "Decommissioned" else None)
    db.add(inst)
    db.commit()
    iid = inst.id
    tenancy.reset_current_tenant(tok)
    db.close()
    return iid


def status_of(Session, iid):
    db = Session()
    tok = tenancy.set_current_tenant(None)
    row = db.query(models.MachineInstallation).filter_by(id=iid).first()
    out = (row.status, row.factory_tenant_code)
    tenancy.reset_current_tenant(tok)
    db.close()
    return out


def release(Session, iid, tenant):
    db = Session()
    tok = tenancy.set_current_tenant(tenant)
    user = {"tenant_code": tenant, "username": "admin", "role": "Admin", "sub": "admin"}
    try:
        return cer.release_installation(installation_id=iid, db=db, current_user=user), None
    except Exception as exc:                       # HTTPException or otherwise
        return None, exc
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def main():
    print("=" * 74)
    print("1. RELEASING A SCRAPPED MACHINE MUST NOT MAKE IT SELLABLE")
    print("=" * 74)
    Session, model_id = fresh()
    iid = make_installation(Session, model_id, "Decommissioned", A)
    check("LIFECYCLE really does call Decommissioned terminal",
          oem_service.LIFECYCLE["Decommissioned"] == (),
          str(oem_service.LIFECYCLE["Decommissioned"]))
    # Pin the behaviour of is_terminal directly, not only through its callers.
    check("is_terminal: Decommissioned yes, live states no, unknown/NULL no",
          oem_service.is_terminal("Decommissioned") is True
          and oem_service.is_terminal("Active") is False
          and oem_service.is_terminal(None) is False
          and oem_service.is_terminal("Nonsense") is False,
          "; ".join(f"{v!r}->{oem_service.is_terminal(v)}"
                    for v in ("Decommissioned", "Active", None, "Nonsense")))
    out, exc = release(Session, iid, A)
    status, holder = status_of(Session, iid)
    check("a released scrapped machine is NOT 'Sold'", status != "Sold",
          f"status is {status!r} — back in sellable stock")
    check("...it is still Decommissioned", status == "Decommissioned", f"got {status!r}")
    check("...and the factory has still let go of it", holder is None, f"holder {holder!r}")
    # The response is what the operator reads. It used to say "the manufacturer
    # can issue a new installation code for its next site" for a machine that is
    # now refused exactly that, which would be a promise the system declines.
    check("...and the response does not report it as Sold",
          (out or {}).get("lifecycle_status") == "Decommissioned",
          f"reported {(out or {}).get('lifecycle_status')!r}")
    check("...nor promise it can be issued to another site",
          "cannot be issued" in (out or {}).get("message", ""),
          f"message: {(out or {}).get('message')!r}")

    # CONTROL: releasing a LIVE machine must still work exactly as before, or
    # the fix above has broken the feature instead of correcting it.
    live = make_installation(Session, model_id, "Active", A)
    out, exc = release(Session, live, A)
    status, holder = status_of(Session, live)
    check("CONTROL: releasing an ACTIVE machine still returns it to stock",
          exc is None and status == "Sold" and holder is None,
          f"exc={exc!r} status={status!r} holder={holder!r}")

    print()
    print("=" * 74)
    print("2. A SCRAPPED MACHINE MUST NOT BE CLAIMABLE BY A NEW FACTORY")
    print("=" * 74)
    Session, model_id = fresh()
    scrapped = make_installation(Session, model_id, "Decommissioned", None)
    db = Session()
    tok = tenancy.set_current_tenant(None)
    inst = db.query(models.MachineInstallation).filter_by(id=scrapped).first()
    claim = models.MachineClaim(
        oem_code="ACME", installation_id=inst.id, token_hash="a" * 64,
        code_hint="AB12", status=oem_claims.PENDING,
        expires_at=datetime.utcnow() + timedelta(days=7))
    db.add(claim)
    db.commit()
    check("usable() refuses a claim on a scrapped machine",
          oem_claims.usable(claim, B, inst) is False, "usable() said True")
    accepted = oem_claims.accept(db, claim, inst, B, "admin")
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()
    status, holder = status_of(Session, scrapped)
    check("accept() refuses it too", accepted is False, "accept() returned True")
    check("...and the machine is still scrapped and unassigned",
          (status, holder) == ("Decommissioned", None), f"{status!r} / {holder!r}")

    print()
    print("=" * 74)
    print("3. THE GUARD IS IN THE UPDATE, NOT ONLY IN THE PRE-CHECK")
    print("=" * 74)
    # accept() is called with an installation object the caller already loaded.
    # If the only guard were `usable()`, a caller that skipped it — or a row that
    # changed between the check and the write — would still get through. Call
    # accept() DIRECTLY, with no usable() in front of it, and the conditional
    # UPDATE must still refuse.
    Session, model_id = fresh()
    scrapped = make_installation(Session, model_id, "Decommissioned", None)
    db = Session()
    tok = tenancy.set_current_tenant(None)
    inst = db.query(models.MachineInstallation).filter_by(id=scrapped).first()
    claim = models.MachineClaim(
        oem_code="ACME", installation_id=inst.id, token_hash="b" * 64,
        code_hint="CD34", status=oem_claims.PENDING,
        expires_at=datetime.utcnow() + timedelta(days=7))
    db.add(claim)
    db.commit()
    direct = oem_claims.accept(db, claim, inst, B, "admin")
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()
    status, holder = status_of(Session, scrapped)
    check("accept() called WITHOUT usable() still refuses", direct is False,
          "the pre-check was the only guard")
    check("...leaving the machine untouched",
          (status, holder) == ("Decommissioned", None), f"{status!r} / {holder!r}")

    # CONTROL: the same call on a healthy machine must succeed, or section 3
    # would pass simply because accept() had stopped working.
    Session, model_id = fresh()
    ok_inst = make_installation(Session, model_id, "Sold", None)
    db = Session()
    tok = tenancy.set_current_tenant(None)
    inst = db.query(models.MachineInstallation).filter_by(id=ok_inst).first()
    claim = models.MachineClaim(
        oem_code="ACME", installation_id=inst.id, token_hash="c" * 64,
        code_hint="EF56", status=oem_claims.PENDING,
        expires_at=datetime.utcnow() + timedelta(days=7))
    db.add(claim)
    db.commit()
    good = oem_claims.usable(claim, B, inst)
    won = oem_claims.accept(db, claim, inst, B, "admin")
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()
    status, holder = status_of(Session, ok_inst)
    check("CONTROL: a SOLD machine is still claimable", good is True and won is True,
          f"usable={good} accept={won}")
    check("...and lands on the new factory as Assigned",
          (status, holder) == ("Assigned", B), f"{status!r} / {holder!r}")

    # WHY accept()'s WHERE clause is an OR against NULL rather than a plain
    # inequality. It cannot be shown with a row: status is NOT NULL and SQLite
    # enforces it, so a NULL-status installation cannot be created here at all.
    # What CAN be shown is the SQL semantics that make the OR necessary — in
    # three-valued logic `NULL != 'x'` is NULL, not true, so a bare inequality
    # silently drops the row. The repo's own convention is that a nullable=False
    # column can still hold NULL in a row written by raw SQL or a migration
    # (transition() reads `installation.status or "Manufactured"` for exactly
    # this reason), so the branch is defensive, not decorative.
    from sqlalchemy import text
    db = Session()
    plain = db.execute(text(
        "SELECT CASE WHEN (NULL != 'Decommissioned') THEN 1 ELSE 0 END")).scalar()
    with_or = db.execute(text(
        "SELECT CASE WHEN (NULL IS NULL OR NULL != 'Decommissioned') "
        "THEN 1 ELSE 0 END")).scalar()
    db.close()
    check("a bare `status != 'Decommissioned'` would DROP a NULL-status row",
          plain == 0, f"got {plain!r} — the OR would be unnecessary")
    check("...and the OR form keeps it", with_or == 1, f"got {with_or!r}")
    # Assert on the SHIPPED predicate, not on a literal retyped here. Without
    # this, replacing the OR in accept() with a bare inequality passed every
    # other check in this file — the branch cannot be reached by a row, so only
    # the compiled SQL can hold it honest.
    clause = str(oem_claims.not_terminal_clause().compile(
        compile_kwargs={"literal_binds": True}))
    check("accept()'s own predicate tolerates a NULL status",
          "IS NULL" in clause.upper(), f"compiled to: {clause}")
    check("...and still excludes Decommissioned", "Decommissioned" in clause,
          f"compiled to: {clause}")

    print()
    print("=" * 74)
    print("4. THE WHOLE JOURNEY, END TO END")
    print("=" * 74)
    # The path that motivated all of the above: scrap -> release -> re-issue.
    # Each step is an action its actor is entitled to take, which is why no
    # single screen looks wrong.
    Session, model_id = fresh()
    iid = make_installation(Session, model_id, "Active", A)
    db = Session()
    tok = tenancy.set_current_tenant(None)
    inst = db.query(models.MachineInstallation).filter_by(id=iid).first()
    oem_service.transition(inst, "Decommissioned")
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()
    release(Session, iid, A)

    db = Session()
    tok = tenancy.set_current_tenant(None)
    inst = db.query(models.MachineInstallation).filter_by(id=iid).first()
    claim = models.MachineClaim(
        oem_code="ACME", installation_id=inst.id, token_hash="d" * 64,
        code_hint="GH78", status=oem_claims.PENDING,
        expires_at=datetime.utcnow() + timedelta(days=7))
    db.add(claim)
    db.commit()
    reissued = oem_claims.accept(db, claim, inst, B, "admin")
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()
    status, holder = status_of(Session, iid)
    check("a machine scrapped by one factory cannot be recommissioned at another",
          reissued is False and holder != B,
          f"reissued={reissued} status={status!r} now held by {holder!r}")

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
