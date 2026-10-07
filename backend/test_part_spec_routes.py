"""The part master: typed in by a person, so every field is a way to be wrong.

A part spec turns a shot count into kilograms and rupees for every hour the
plant runs. A weight of zero makes a month's material consumption vanish; a
cycle time of zero makes the ideal rate infinite; active cavities above total
cavities overstates output for the life of the tool. None of those is noticed
until somebody disputes an invoice, so each is refused at the door and named.

ALSO PINNED: a new effective date SUPERSEDES rather than overwrites. The
customer's own part table has a "From date" column — a price is a fact about a
period, and editing in place would move last month's revenue because this
month's price changed.

Run: DATABASE_URL="sqlite:///./ci_psr.db" python backend/test_part_spec_routes.py
"""
import ast
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("DATABASE_URL", "sqlite:///./ci_psr.db")

import database  # noqa: E402
import models    # noqa: E402
import part_spec_routes as R  # noqa: E402
from fastapi import HTTPException  # noqa: E402

failures = []
T = "PSR-TEST"
ADMIN = {"tenant": T, "role": "Admin", "username": "admin"}   # "tenant" is the JWT claim


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def section(t):
    print("\n" + "=" * 74 + f"\n{t}\n" + "=" * 74)


GOOD = {"part_code": "ELE-CLIP", "part_name": "Ele clip", "material": "PP H 110",
        "part_weight_g": 0.33, "cavities": 56, "active_cavities": 56,
        "ideal_cycle_time_s": 14.0, "price_per_piece": 0.09,
        "effective_from": "2026-01-01"}


_case = [0]


def refuses(db, body, field, label):
    """The field under test must be the ONLY reason this is refused.

    Each case gets its own part code, so a 409 from colliding with an earlier
    spec can never stand in for the 400 being asserted. Without that, mutating
    the weight check still "failed" the test — but on the uniqueness constraint,
    which would have hidden a validation order bug behind a passing guard.
    """
    _case[0] += 1
    body = {**body, "part_code": body.get("part_code") or f"CASE-{_case[0]}"}
    if body["part_code"] == GOOD["part_code"]:
        body["part_code"] = f"CASE-{_case[0]}"
    try:
        R.create_part_spec(body, db=db, current_user=ADMIN)
        check(label, False, "it was accepted")
    except HTTPException as e:
        check(label, e.status_code == 400 and field in str(e.detail),
              f"{e.status_code}: {e.detail}")


# ── The gate the direct calls below cannot see ────────────────────────
#
# Every check in main() calls the handler as a plain function, which is how a
# standalone script reaches the logic — but it hands `current_user` in directly
# and so sails straight past FastAPI's Depends. The Admin-only gate is therefore
# invisible to those checks: delete `require_roles(["Admin"])` from the route and
# all of them still pass while any Operator could restate the plant's revenue.
#
# So read the gate out of the source, which is the same approach
# test_contract_route_guards.py takes for the contract routes.
GATES = {
    "list_part_specs": None,            # readable by anyone signed in
    "list_tool_assignments": None,
    "create_part_spec": ["Admin"],      # restates revenue and output
    "assign_tool": ["Admin"],
}


def _depends_gate(fn):
    """What this handler's Depends(...) defaults amount to: the role list of a
    require_roles([...]) gate, or None for plain get_current_user."""
    for default in fn.args.defaults + fn.args.kw_defaults:
        if not (isinstance(default, ast.Call) and isinstance(default.func, ast.Name)
                and default.func.id == "Depends" and default.args):
            continue
        inner = default.args[0]
        if isinstance(inner, ast.Name) and inner.id == "get_current_user":
            return None
        if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                and inner.func.id == "require_roles" and inner.args):
            roles = inner.args[0]
            if isinstance(roles, (ast.List, ast.Tuple)):
                return [e.value for e in roles.elts if isinstance(e, ast.Constant)]
    return "NO GATE"


def case_writes_are_admin_only():
    section("0. THE WRITES ARE ADMIN-GATED IN THE SOURCE")
    with open(os.path.join(HERE, "part_spec_routes.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    handlers = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and isinstance(dec.func.value, ast.Name)
                        and dec.func.value.id == "router"):
                    handlers[node.name] = node
    # A rename must not turn this guard into a check of nothing.
    check("the four handlers this guard names are the handlers that exist",
          sorted(handlers) == sorted(GATES), str(sorted(set(handlers) ^ set(GATES))))
    for name, want in sorted(GATES.items()):
        if name not in handlers:
            continue
        got = _depends_gate(handlers[name])
        check(f"{name} is gated on {want or 'any signed-in user'}", got == want, repr(got))


def main():
    case_writes_are_admin_only()
    models.Base.metadata.create_all(bind=database.engine)
    db = database.SessionLocal()
    try:
        for M in (models.ToolAsset, models.PartSpec, models.Machine):
            db.query(M).filter(M.tenant_code == T).delete()
        db.commit()

        section("1. A GOOD SPEC, AND THE RATE IT IMPLIES")
        row = R.create_part_spec(dict(GOOD), db=db, current_user=ADMIN)
        # 3600/14 = 257.14 shots/hr x 56 cavities
        check("the spec is stored and the ideal rate derived",
              row["ideal_parts_per_hour"] == 14400, str(row["ideal_parts_per_hour"]))
        check("rupees are kept to the paisa", row["price_per_piece"] == 0.09,
              str(row["price_per_piece"]))

        section("2. THE FIELDS THAT SILENTLY DESTROY A MONTH")
        refuses(db, {**GOOD, "part_weight_g": 0}, "part_weight_g",
                "a weight of zero is refused (it would vanish a month of material)")
        refuses(db, {**GOOD, "ideal_cycle_time_s": 0}, "ideal_cycle_time_s",
                "a cycle time of zero is refused (the ideal rate would be infinite)")
        refuses(db, {**GOOD, "part_weight_g": -1}, "part_weight_g",
                "a negative weight is refused")
        refuses(db, {**GOOD, "part_weight_g": "heavy"}, "part_weight_g",
                "a non-numeric weight is refused")
        refuses(db, {**GOOD, "part_weight_g": float("inf")}, "part_weight_g",
                "infinity is refused — it prints as a figure and means nothing")
        refuses(db, {**GOOD, "part_code": "  "}, "part_code",
                "a blank part code is refused")
        refuses(db, {**GOOD, "effective_from": "01/01/2026"}, "effective_from",
                "a non-ISO date is refused rather than guessed")

        section("3. MORE ACTIVE CAVITIES THAN THE MOULD HAS")
        try:
            R.create_part_spec({**GOOD, "part_code": "X1", "cavities": 16,
                                "active_cavities": 18}, db=db, current_user=ADMIN)
            check("active above total is refused", False, "accepted")
        except HTTPException as e:
            check("active above total is refused",
                  e.status_code == 400 and "cannot exceed" in str(e.detail), str(e.detail))

        section("4. ZERO PRICE IS ALLOWED, AND MEANS UNPRICED")
        row = R.create_part_spec({**GOOD, "part_code": "NOPRICE", "price_per_piece": 0},
                                 db=db, current_user=ADMIN)
        check("a part may be tracked before a price is agreed",
              row["price_per_piece"] == 0.0, str(row["price_per_piece"]))

        section("5. A NEW DATE SUPERSEDES; IT DOES NOT OVERWRITE")
        R.create_part_spec({**GOOD, "price_per_piece": 0.12,
                            "effective_from": "2026-06-01"}, db=db, current_user=ADMIN)
        kept = (db.query(models.PartSpec)
                  .filter(models.PartSpec.tenant_code == T,
                          models.PartSpec.part_code == "ELE-CLIP").all())
        check("both prices are on record", len(kept) == 2, f"{len(kept)} rows")
        check("...and the old one is untouched",
              sorted(s.price_per_piece for s in kept) == [0.09, 0.12],
              str(sorted(s.price_per_piece for s in kept)))
        try:
            R.create_part_spec(dict(GOOD), db=db, current_user=ADMIN)   # same date again
            check("the same part on the same date is refused", False, "accepted")
        except HTTPException as e:
            check("the same part on the same date is refused", e.status_code == 409,
                  f"{e.status_code}: {e.detail}")

        section("6. MOULD CHANGE: A TOOL CANNOT CROSS WORKSPACES")
        m = models.Machine(tenant_code=T, site="", name="IMM-01", status="Idle")
        other = models.Machine(tenant_code="SOMEONE-ELSE", site="", name="THEIRS",
                               status="Idle")
        db.add_all([m, other])
        db.flush()
        tool = models.ToolAsset(tenant_code=T, tool_no="MLD-1", name="Ele mould",
                                cavities=56, status="Active")
        db.add(tool)
        # Another workspace's part and tool, to prove the listings filter rather
        # than simply happening to hold only our rows.
        db.add(models.ToolAsset(tenant_code="SOMEONE-ELSE", tool_no="THEIR-MLD",
                                name="Not ours", cavities=4, status="Active"))
        db.add(models.PartSpec(
            tenant_code="SOMEONE-ELSE", part_code="THEIR-PART", part_name="Not ours",
            material="SECRET ABS", part_weight_g=1.0, cavities=1, active_cavities=1,
            ideal_cycle_time_s=10.0, price_per_piece=99.0,
            effective_from=date(2026, 1, 1)))
        db.commit()

        out = R.assign_tool(tool.id, {"machine_id": m.id, "part_code": "ELE-CLIP"},
                            db=db, current_user=ADMIN)
        check("a mould change records machine and part",
              out["machine_id"] == m.id and out["part_code"] == "ELE-CLIP", str(out))
        try:
            R.assign_tool(tool.id, {"machine_id": other.id}, db=db, current_user=ADMIN)
            check("a tool cannot be fitted to another workspace's machine", False, "accepted")
        except HTTPException as e:
            check("a tool cannot be fitted to another workspace's machine",
                  e.status_code == 400, f"{e.status_code}: {e.detail}")
        out = R.assign_tool(tool.id, {"machine_id": None}, db=db, current_user=ADMIN)
        check("a tool can go back to the tool room", out["machine_id"] is None, str(out))

        section("7. THE LIST FEEDS THE DROPDOWNS")
        listing = R.list_part_specs(db=db, current_user=ADMIN)
        check("specs are listed", len(listing["specs"]) >= 3, str(len(listing["specs"])))
        check("...and materials are offered for a dropdown",
              "PP H 110" in listing["materials"], str(listing["materials"]))
        assigns = R.list_tool_assignments(db=db, current_user=ADMIN)
        check("tools and machines are listed for the mould-change form",
              assigns["tools"] and assigns["machines"],
              f"{len(assigns['tools'])} tools / {len(assigns['machines'])} machines")
        check("...and only this workspace's machines",
              all(x["name"] != "THEIRS" for x in assigns["machines"]),
              str(assigns["machines"]))
        check("...and only this workspace's tools",
              all(x["tool_no"] != "THEIR-MLD" for x in assigns["tools"]),
              str([x["tool_no"] for x in assigns["tools"]]))
        # A part master is commercial data: another plant's price list must not
        # appear in a dropdown here.
        check("another workspace's part never appears in the spec list",
              all(s["part_code"] != "THEIR-PART" for s in listing["specs"]),
              str([s["part_code"] for s in listing["specs"]]))
        check("...nor its material in the material dropdown",
              "SECRET ABS" not in listing["materials"], str(listing["materials"]))

        for M in (models.ToolAsset, models.PartSpec, models.Machine):
            db.query(M).filter(M.tenant_code.in_([T, "SOMEONE-ELSE"])).delete(
                synchronize_session=False)
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    main()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED - part master routes")
