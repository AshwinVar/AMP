"""POST /register creates the first account, which is the Admin, and takes no role.

THE DEFECT
----------
The sign-up page offered Admin / Supervisor / Operator and sent the choice; the
handler shared the Admin's "add employee" schema (UserCreate, `role` required)
and then wrote `role="Admin"` whatever was sent. Choosing Operator and pressing
Register created an Admin of DEFAULT, and the toast said only "Account created!".
An input accepted, validated, transmitted and ignored -- the #687 shape.

THE RULE THIS FILE PINS
-----------------------
  1. THE SCHEMA   RegisterRequest carries username and password only; a role
                  (or any other key) is refused, never silently dropped.
  2. THE HANDLER  the first account is the Admin of DEFAULT; once any user
                  exists, registration is a 403 with the reason.
  3. THE PAGE     the sign-up page sends no role and says what the account is.

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_register_first_account_only.py
"""
import os
import sys

import pydantic
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core_routes
import models
import schemas
from database import Base

HERE = os.path.dirname(os.path.abspath(__file__))
failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def main_():
    print("=" * 74)
    print("1. THE SCHEMA: NO ROLE, AND A ROLE IS REFUSED RATHER THAN DROPPED")
    print("=" * 74)
    req = schemas.RegisterRequest(username="first", password="pw")
    check("username and password are enough", req.username == "first" and req.password == "pw")
    check("the schema declares no role", "role" not in schemas.RegisterRequest.model_fields)
    try:
        schemas.RegisterRequest(username="first", password="pw", role="Operator")
        check("a role in the body is refused (extra=forbid)", False, "no ValidationError")
    except pydantic.ValidationError as exc:
        check("a role in the body is refused (extra=forbid)", "role" in str(exc) and "extra" in str(exc).lower(),
              str(exc)[:200])
    check("the handler takes RegisterRequest, not the Admin's UserCreate",
          core_routes.register_user.__annotations__.get("user") is schemas.RegisterRequest,
          str(core_routes.register_user.__annotations__.get("user")))

    print()
    print("=" * 74)
    print("2. THE HANDLER: THE FIRST ACCOUNT IS THE ADMIN OF DEFAULT; THEN IT IS CLOSED")
    print("=" * 74)
    db = _session()
    created = core_routes.register_user(schemas.RegisterRequest(username="first", password="pw"), db=db)
    row = db.query(models.User).filter(models.User.username == "first").first()
    check("the first account is created", row is not None and created.username == "first")
    check("...as the Admin", row is not None and row.role == "Admin", str(getattr(row, "role", None)))
    check("...of DEFAULT", row is not None and row.tenant_code == "DEFAULT", str(getattr(row, "tenant_code", None)))
    check("...with the password hashed", row is not None and row.password != "pw")
    try:
        core_routes.register_user(schemas.RegisterRequest(username="second", password="pw"), db=db)
        check("a second registration is refused", False, "no HTTPException")
    except HTTPException as exc:
        check("a second registration is refused", exc.status_code == 403, str(exc.status_code))
        check("...and says an Admin must add you", "Admin" in str(exc.detail), str(exc.detail))
    check("...and created nothing", db.query(models.User).count() == 1, str(db.query(models.User).count()))
    db.close()

    print()
    print("=" * 74)
    print("3. THE PAGE SENDS NO ROLE AND SAYS WHAT THE ACCOUNT IS")
    print("=" * 74)
    page = os.path.join(HERE, "..", "frontend", "app", "register", "page.tsx")
    with open(page, encoding="utf-8") as f:
        src = f.read()
    check("no role select on the page", "<select" not in src and "setRole" not in src)
    check("the body carries username and password only", "role" not in src.split("JSON.stringify(")[1].split("})")[0])
    check("the page says the first account becomes the Admin", "becomes the Admin" in src)

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def test_register_first_account_only():
    assert main_() == 0, failures


if __name__ == "__main__":
    sys.exit(main_())
