"""Editing a recipe's own lines must not 500 on the unique component key.

WHAT WAS MEASURED ON MASTER
---------------------------
PATCH /bom/{id} with a `components` list replaces the recipe wholesale:

    bom.components.clear()
    for p in parsed:
        bom.components.append(BomComponent(...))

bom_components carries UNIQUE (bom_id, component_code) (uq_bom_component). The
clear() queues DELETEs and the append() queues INSERTs, but SQLAlchemy's unit of
work emits the INSERTs first in a single flush. So the moment a replacement reuses
a component_code the recipe already had — changing STEEL from 2.0 to 2.5 kg per
unit, the single most ordinary BOM edit — the new STEEL line collides with the
old one that has not been deleted yet:

    sqlite3.IntegrityError: UNIQUE constraint failed:
        bom_components.bom_id, bom_components.component_code

which leaves the handler as an unhandled HTTP 500 on a routine save. A brand-new
component_code never collided, which is why the existing BOM suite (which only
ever PATCHes {"active": false}) never caught it.

The fix flushes the DELETEs before the INSERTs (db.flush() after clear()), inside
the request's transaction so a later failure still rolls the whole edit back.

Run: DATABASE_URL="sqlite:///./ci.db" python test_bom_component_replace.py
"""
import os

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import bom_routes
import models
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

TENANT = "ACME"


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


def admin():
    return {"sub": "admin", "role": "Admin", "tenant": TENANT}


def create(db, **payload):
    tok = tenancy.set_current_tenant(TENANT)
    try:
        return bom_routes.create_bom(payload, db=db, current_user=admin())
    finally:
        tenancy.reset_current_tenant(tok)


def update(db, bom_id, **payload):
    tok = tenancy.set_current_tenant(TENANT)
    try:
        return bom_routes.update_bom(bom_id, payload, db=db, current_user=admin())
    finally:
        tenancy.reset_current_tenant(tok)


def components_of(rows):
    """{component_code: quantity_per_unit} from a serialised BOM response, dropping
    the empty placeholder row a component-less BOM returns."""
    return {r["component_code"]: r["quantity_per_unit"]
            for r in rows if r["component_code"]}


def run():
    # ── the bug: re-editing a line that keeps its own component_code ──────────
    db = fresh_db()
    created = create(db, part_number="SHAFT-001", revision="A",
                     components=[{"component_code": "STEEL", "quantity_per_unit": 2.0, "unit": "kg"},
                                 {"component_code": "PAINT", "quantity_per_unit": 0.1, "unit": "L"}])
    bom_id = created[0]["id"]
    check("a two-line recipe is created", components_of(created) == {"STEEL": 2.0, "PAINT": 0.1},
          str(components_of(created)))

    # Change STEEL's rate and KEEP PAINT — both codes are reused. On master this
    # raised IntegrityError -> HTTP 500.
    try:
        rows = update(db, bom_id,
                      components=[{"component_code": "STEEL", "quantity_per_unit": 2.5, "unit": "kg"},
                                  {"component_code": "PAINT", "quantity_per_unit": 0.1, "unit": "L"}])
        got = components_of(rows)
        # Independently derived: the new recipe is exactly {STEEL: 2.5, PAINT: 0.1}.
        check("editing a kept component's quantity no longer 500s", got == {"STEEL": 2.5, "PAINT": 0.1},
              str(got))
    except HTTPException as e:
        check("editing a kept component's quantity no longer 500s", False,
              f"HTTP {e.status_code}: {e.detail}")

    # And the stored rows are exactly those two — not four (old + new), no orphans.
    tok = tenancy.set_current_tenant(None)
    stored = db.query(models.BomComponent).filter(models.BomComponent.bom_id == bom_id).all()
    tenancy.reset_current_tenant(tok)
    stored_map = {c.component_code: c.quantity_per_unit for c in stored}
    check("the parts replace the whole: exactly the new lines remain, no duplicates",
          stored_map == {"STEEL": 2.5, "PAINT": 0.1}, f"{len(stored)} rows: {stored_map}")

    # ── a recipe that keeps one code, drops one, adds one ────────────────────
    db = fresh_db()
    created = create(db, part_number="VALVE-9", revision="A",
                     components=[{"component_code": "BRASS", "quantity_per_unit": 1.0, "unit": "kg"},
                                 {"component_code": "SEAL", "quantity_per_unit": 2.0, "unit": "pc"}])
    bom_id = created[0]["id"]
    rows = update(db, bom_id,
                  components=[{"component_code": "BRASS", "quantity_per_unit": 1.25, "unit": "kg"},
                              {"component_code": "SPRING", "quantity_per_unit": 4.0, "unit": "pc"}])
    got = components_of(rows)
    check("keep-one / drop-one / add-one lands exactly the new set",
          got == {"BRASS": 1.25, "SPRING": 4.0}, str(got))

    # ── replacing with an empty recipe (produces, consumes nothing) ──────────
    db = fresh_db()
    created = create(db, part_number="BLANK-1", revision="A",
                     components=[{"component_code": "FOIL", "quantity_per_unit": 1.0, "unit": "kg"}])
    bom_id = created[0]["id"]
    rows = update(db, bom_id, components=[])
    check("clearing every component leaves a component-less recipe (one blank row)",
          components_of(rows) == {}, str(rows))
    tok = tenancy.set_current_tenant(None)
    n = db.query(models.BomComponent).filter(models.BomComponent.bom_id == bom_id).count()
    tenancy.reset_current_tenant(tok)
    check("no component rows survive an empty replacement", n == 0, f"{n} left")

    # ── the same recipe saved twice unchanged (idempotent re-save) ───────────
    db = fresh_db()
    created = create(db, part_number="REP-1", revision="A",
                     components=[{"component_code": "WIRE", "quantity_per_unit": 3.0, "unit": "m"}])
    bom_id = created[0]["id"]
    recipe = [{"component_code": "WIRE", "quantity_per_unit": 3.0, "unit": "m"}]
    update(db, bom_id, components=recipe)
    rows = update(db, bom_id, components=recipe)      # save the identical recipe again
    check("saving the identical recipe twice stays {WIRE: 3.0}",
          components_of(rows) == {"WIRE": 3.0}, str(components_of(rows)))

    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("ALL BOM COMPONENT-REPLACE TESTS PASSED")


if __name__ == "__main__":
    run()
