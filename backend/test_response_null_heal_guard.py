"""One NULL row must never 500 a whole list endpoint. A guard, not four fixes.

THE DEFECT CLASS
----------------
A response schema types a field as non-Optional `str`/`int`, while the column
behind it is `Column(String, default="X")` WITHOUT `nullable=False`. A Python-side
`default=` only fills a value the INSERTER omitted — it does not fire on an
UPDATE that supplies an explicit None, and it does not exist at all for raw SQL
or a migration. So a real NULL is reachable, and then:

    PATCH /suppliers/1 {"status": null}
      -> XxxUpdate.status is Optional[str], so the body validates
      -> update_supplier setattr's over model_dump(exclude_unset=True), and an
         EXPLICIT null counts as "set"
      -> db.commit() happens BEFORE serialisation, so the NULL persists
      -> SupplierResponse.status is `str` -> pydantic ValidationError
      -> FastAPI raises ResponseValidationError -> 500

and every subsequent `GET /suppliers` 500s too, because one poisoned row is
serialised with the rest. **One bad row hides every good one, permanently, until
somebody repairs it in the database.**

WHY A GUARD RATHER THAN FOUR MORE FIXES
----------------------------------------
This has been fixed here at least thirteen times — #427, #430, #442, #445, #447,
#452, #456, #460, #465, #466, #468, #471, #472 — each time on the schema that
happened to be reported. `_coalesce_null_text` / `_coalesce_null_count` /
`_coalesce_null_int` exist precisely for it, and thirty heals are already in
place. A defect hunt then found four more (Supplier, CustomerOrder.priority,
PurchaseOrder.status, QualityInspection.status).

Enumerating the invariant instead finds **28**, which is the point: fixing the
reported instance leaves the next one to be reported. This file states the rule
once and fails on any field that breaks it, so the twenty-ninth cannot be
written.

THE RULE
--------
For every `<Model>Response` schema, every field that is
  * non-Optional in the schema, AND
  * backed by a column that is nullable with a Python-side default
must carry a `field_validator(..., mode="before")` that coalesces NULL.

Healing to the COLUMN'S OWN DECLARED DEFAULT is the honest reading: it is
exactly the value the inserter would have received, so a NULL renders as "no
value recorded" rather than as an error or an invented one.

NOT COVERED BY THIS RULE, deliberately:
  * `Optional[...]` fields — a NULL is already representable, nothing to heal.
  * columns with no Python-side default — there is no honest value to coalesce
    to, and inventing one would be fabricating data. Those need `Optional` on
    the schema instead, which is a different change.
  * WHICH value a heal picks. It is not always the column default, and the
    first version of this file wrongly asserted that it was. See section 2.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_response_null_heal_guard.py
"""
import inspect
import typing

from pydantic import BaseModel

import models
import schemas

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def _columns_by_model():
    out = {}
    for name, obj in vars(models).items():
        if inspect.isclass(obj) and hasattr(obj, "__table__"):
            out[name] = {c.name: c for c in obj.__table__.columns}
    return out


def _validated_fields(schema_cls):
    """Every field name covered by a field_validator on this schema."""
    dec = getattr(schema_cls, "__pydantic_decorators__", None)
    fields = set()
    if dec:
        for v in getattr(dec, "field_validators", {}).values():
            fields |= set(v.info.fields)
    return fields


def _is_optional(annotation) -> bool:
    return (typing.get_origin(annotation) is typing.Union
            and type(None) in typing.get_args(annotation))


def candidates():
    """Every (schema, field) the rule APPLIES to, healed or not.

    Shares its selector with unhealed() so the guard cannot quietly start
    inspecting the wrong set of columns. Mutation testing earned this: flipping
    `not col.nullable` to `col.nullable` made the sweep look at NON-nullable
    columns, find nothing to complain about, and PASS — a guard that had stopped
    guarding and said nothing. Section 3 now fails if this count collapses."""
    cols = _columns_by_model()
    out = []
    for sname, sobj in vars(schemas).items():
        if not (inspect.isclass(sobj) and issubclass(sobj, BaseModel)
                and sname.endswith("Response")):
            continue
        model_cols = cols.get(sname[:-len("Response")])
        if not model_cols:
            continue
        for fname, finfo in sobj.model_fields.items():
            if _is_optional(finfo.annotation):
                continue
            col = model_cols.get(fname)
            if col is None or not col.nullable or col.default is None:
                continue
            out.append((sname, fname))
    return sorted(out)


def unhealed():
    """Every response field that breaks the rule, as (schema, field, default)."""
    cols = _columns_by_model()
    out = []
    for sname, sobj in vars(schemas).items():
        if not (inspect.isclass(sobj) and issubclass(sobj, BaseModel)
                and sname.endswith("Response")):
            continue
        model_cols = cols.get(sname[:-len("Response")])
        if not model_cols:
            continue                      # a composed/DTO response, no table
        healed = _validated_fields(sobj)
        for fname, finfo in sobj.model_fields.items():
            if _is_optional(finfo.annotation) or fname in healed:
                continue
            col = model_cols.get(fname)
            if col is None or not col.nullable or col.default is None:
                continue
            out.append((sname, fname, getattr(col.default, "arg", col.default)))
    return sorted(out)


def main():
    print("=" * 74)
    print("1. NO RESPONSE FIELD CAN 500 ON A NULL IT CAN LEGITIMATELY HOLD")
    print("=" * 74)
    bad = unhealed()
    for sname, fname, default in bad:
        print(f"      UNHEALED  {sname}.{fname}  (column default={default!r})")
    check(f"every non-Optional response field over a nullable defaulted column "
          f"is healed ({len(bad)} unhealed)", not bad,
          "; ".join(f"{s}.{f}" for s, f, _ in bad[:6]))

    print()
    print("=" * 74)
    print("2. EVERY HEAL ACTUALLY HEALS")
    print("=" * 74)
    # A validator that is registered but still lets None through is worse than
    # none at all: it looks like protection in a grep and 500s in production.
    #
    # This deliberately does NOT assert that the healed value equals the
    # column's declared default. The first version of this check did, and it
    # failed on two heals that are RIGHT: ProductionScheduleResponse
    # .estimated_minutes heals a NULL to 0 rather than the column's 480, and
    # says why in fifteen lines of comment — both readers of that column already
    # coalesce a NULL to 0, so healing to 480 would put the per-schedule list on
    # a different basis from the booked-minutes headline it sums into. Which
    # value is honest is a per-field judgement the code documents; that it is
    # not None is the invariant.
    cols = _columns_by_model()
    wrong = []
    for sname, sobj in vars(schemas).items():
        if not (inspect.isclass(sobj) and issubclass(sobj, BaseModel)
                and sname.endswith("Response")):
            continue
        model_cols = cols.get(sname[:-len("Response")])
        if not model_cols:
            continue
        for fname in _validated_fields(sobj):
            col = model_cols.get(fname)
            if col is None or col.default is None:
                continue
            declared = getattr(col.default, "arg", col.default)
            if callable(declared):
                continue                  # e.g. datetime.utcnow — not a constant
            try:
                healed_value = sobj.__pydantic_validator__.validate_python(
                    {**{f: _sample(model_cols.get(f), fi)
                        for f, fi in sobj.model_fields.items()},
                     fname: None}, from_attributes=False)
            except Exception as e:                    # noqa: BLE001 - report it
                wrong.append(f"{sname}.{fname}: a NULL still raises ({type(e).__name__})")
                continue
            if getattr(healed_value, fname) is None:
                wrong.append(f"{sname}.{fname}: validator registered but NULL survives it")
    check(f"a registered heal turns a NULL into a real value "
          f"({len(wrong)} broken)", not wrong, "; ".join(wrong[:4]))

    print()
    print("=" * 74)
    print("3. THE RULE ITSELF IS NOT VACUOUS")
    print("=" * 74)
    # If the enumeration found nothing to check, it proves nothing. Assert it
    # actually inspects a meaningful number of response schemas and fields.
    inspected = [s for s in vars(schemas)
                 if s.endswith("Response")
                 and inspect.isclass(getattr(schemas, s))
                 and issubclass(getattr(schemas, s), BaseModel)
                 and _columns_by_model().get(s[:-len("Response")])]
    check(f"the sweep inspects {len(inspected)} table-backed response schemas",
          len(inspected) >= 20, str(len(inspected)))
    total_heals = sum(len(_validated_fields(getattr(schemas, s))) for s in inspected)
    check(f"...covering {total_heals} healed fields in total", total_heals >= 30,
          str(total_heals))
    # The selector itself. Without this the guard can be made to inspect the
    # wrong column set — it then finds nothing to complain about and passes,
    # which is a guard that has stopped guarding without saying so.
    cands = candidates()
    check(f"the rule applies to {len(cands)} real fields, so it is checking "
          f"something", len(cands) >= 28, str(len(cands)))
    check("...and every one of them is healed",
          len(cands) == len(cands) - len(unhealed()), str(len(unhealed())))

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


def _sample(col, field_info):
    """A minimal valid value for a field, so a schema can be constructed with
    one deliberate NULL in it."""
    ann = field_info.annotation
    if _is_optional(ann):
        return None
    origin = typing.get_origin(ann)
    if origin is list:
        return []
    if ann is int:
        return 0
    if ann is float:
        return 0.0
    if ann is bool:
        return False
    if ann is str:
        return "x"
    import datetime as _dt
    if ann is _dt.datetime:
        return _dt.datetime(2026, 1, 1)
    if ann is _dt.date:
        return _dt.date(2026, 1, 1)
    return None


def test_response_null_heal_guard():
    """The pytest entry point — a suite exposing only main() contributes
    nothing to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
