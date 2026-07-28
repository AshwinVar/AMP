"""Architectural guard: tenant-owned tables that are NOT auto-scoped (ADR-0002).

`tenancy.SCOPED_MODELS` is the set the `do_orm_execute` hook rewrites, so a read
of one of those models is filtered to the request tenant automatically. But a
model can carry a `tenant_code` column and simply never be registered — and then
every `db.query(Model).all()` on it quietly returns EVERY tenant's rows. Nothing
errors; the data just leaks. That is the same class of bug as the legacy
enterprise-inventory rows (#245), one layer up from the bulk-write hole that
`test_bulk_write_scoping.py` closes (#365/#366): that one is about writes the
hook skips, this one is about reads the hook was never asked to cover.

Ten models are deliberately outside the hook today (platform-level rows, rows
addressed by a globally-unique key, or rows guarded by hand). This test pins
that decision down two ways:

1. **Membership** — every model with a `tenant_code` column is either in
   SCOPED_MODELS or named in ``MANUALLY_SCOPED`` with the reason it is safe.
   Adding a new tenant-owned table therefore fails CI until someone decides
   which side it belongs on, instead of defaulting to "leaks".

2. **Call sites** — every function that reads one of those unscoped models must
   show a tenant boundary: the literal `tenant_code`, a known guard helper, or
   an entry in ``ALLOWED_CROSS_TENANT`` saying why it is global on purpose.

Known limit: check 2 is function-level, so a new unguarded read added *inside* a
function that already filters elsewhere would not be flagged. It catches the new
handler, which is where this bug actually shows up. Check 1 has no such gap.

Run:  python backend/test_unscoped_model_reads.py     (exit 0 = pass)
"""
import ast
import inspect
import os

import models
import tenancy

BACKEND = os.path.dirname(os.path.abspath(__file__))

# Models that carry tenant_code but are intentionally absent from SCOPED_MODELS.
# Key: class name -> why it is safe without the hook.
MANUALLY_SCOPED = {
    "User":
        "auth rows: looked up by globally-unique username at login, and the by-id "
        "admin handlers call _same_tenant_or_403; list_users filters by hand",
    "TenantConfig":
        "platform-level row, one per tenant — read by tenant_code as the key, and "
        "the cross-tenant listing is gated on the DEFAULT platform owner",
    "EventLog":
        "append-only event history; every consumer filters by tenant_code, and the "
        "startup RESEED_FACTORY flag check is a global operator concern",
    "AgentAction":
        "agent approvals — every read filters tenant_code explicitly (agent_routes, "
        "ai/handover, ai/insights)",
    "AgentPolicy":
        "one autonomy policy per tenant, always fetched by tenant_code as the key",
    # The GMATS inventory module predates the hook and guards by hand: the parent
    # row is fetched then _guard_record'd, and children are reached by parent id.
    "GmatsItem": "GMATS module guards by hand via _guard_record on the fetched row",
    "GmatsAlias": "reached only by item_id, after its parent item was _guard_record'd",
    "GmatsMIN": "GMATS module guards by hand via _guard_record on the fetched row",
    "GmatsProforma": "GMATS module guards by hand via _guard_record on the fetched row",
    "GmatsInvoice": "GMATS module guards by hand via _guard_record on the fetched row",
}

# Helpers that ARE a tenant boundary even though the caller never writes
# `tenant_code` itself.
GUARD_HELPERS = ("_same_tenant_or_403", "_guard_record")

# Reads of an unscoped model that are global on purpose.
# Key: "<file>::<function>" -> why crossing tenants is correct there.
# Kept minimal on purpose: an entry the sweep no longer needs is asserted away
# below, so a function that later grows a real tenant boundary (or disappears)
# cannot leave a blanket exemption behind for the next leak to hide under.
ALLOWED_CROSS_TENANT = {
    "core_routes.py::change_password":
        "resolves the caller's OWN username from their JWT; usernames are globally unique",
    "platform_routes.py::all_tenant_configs":
        "platform-owner listing of every client — gated on the DEFAULT tenant + Admin",
}

SKIP_FILES = {
    "reset_factory.py",
    "reseed_inventory.py",
    "offboard_tenant.py",
    "backfill_enterprise_tenants.py",
}


def _tenant_owned_models():
    """Every mapped model carrying a tenant_code column."""
    return {
        obj.__name__: obj
        for obj in vars(models).values()
        if inspect.isclass(obj) and hasattr(obj, "__tablename__") and hasattr(obj, "tenant_code")
    }


def test_every_tenant_owned_model_is_scoped_or_documented():
    owned = _tenant_owned_models()
    scoped = {m.__name__ for m in tenancy.SCOPED_MODELS}

    undecided = sorted(set(owned) - scoped - set(MANUALLY_SCOPED))
    assert not undecided, (
        "These models carry a tenant_code column but are neither in "
        "tenancy.SCOPED_MODELS nor documented in MANUALLY_SCOPED — so every read "
        "of them returns EVERY tenant's rows with nothing to flag it:\n  "
        + "\n  ".join(undecided)
        + "\nAdd the model to SCOPED_MODELS (preferred), or to MANUALLY_SCOPED "
          "with the reason it is safe without the hook.")

    # The allowlist must not rot: an entry that is now scoped (or gone) is stale.
    stale = sorted(n for n in MANUALLY_SCOPED if n not in owned or n in scoped)
    assert not stale, (
        f"MANUALLY_SCOPED entries that are now auto-scoped or no longer exist "
        f"(remove them): {stale}")

    assert all(MANUALLY_SCOPED.values()), "every MANUALLY_SCOPED entry needs a reason"
    print(f"PASS all {len(owned)} tenant-owned models are scoped ({len(scoped)}) "
          f"or documented ({len(MANUALLY_SCOPED)})")


def _query_targets(call):
    """Model names passed to a `.query(...)` call, else []."""
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and call.func.attr == "query"):
        return []
    out = []
    for a in call.args:
        if isinstance(a, ast.Attribute):
            out.append(a.attr)
        elif isinstance(a, ast.Name):
            out.append(a.id)
    return out


def _unguarded_reads(path, watched, src=None):
    """[(function, models, lineno)] for functions reading a watched model with
    no visible tenant boundary."""
    src = src if src is not None else open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    hits = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        touched = set()
        for node in ast.walk(fn):
            touched.update(m for m in _query_targets(node) if m in watched)
        if not touched:
            continue
        body = ast.get_source_segment(src, fn) or ""
        if "tenant_code" in body or any(h in body for h in GUARD_HELPERS):
            continue
        hits.append((fn.name, sorted(touched), fn.lineno))
    return hits


def test_reads_of_unscoped_models_are_tenant_guarded():
    watched = set(MANUALLY_SCOPED)
    unguarded, exempted = [], set()
    for name in sorted(os.listdir(BACKEND)):
        if not name.endswith(".py") or name.startswith("test_") or name in SKIP_FILES:
            continue
        for func, touched, lineno in _unguarded_reads(os.path.join(BACKEND, name), watched):
            key = f"{name}::{func}"
            if key in ALLOWED_CROSS_TENANT:
                exempted.add(key)
                continue
            unguarded.append(f"{name}:{lineno} {func}() reads {', '.join(touched)}")

    assert not unguarded, (
        "These functions read a model the ADR-0002 hook does NOT scope, with no "
        "visible tenant boundary — so they return other tenants' rows:\n  "
        + "\n  ".join(unguarded)
        + "\nFilter on tenant_code (use request_tenant(current_user)), call a guard "
          "helper, or record it in ALLOWED_CROSS_TENANT with the reason it is global.")

    unused = sorted(set(ALLOWED_CROSS_TENANT) - exempted)
    assert not unused, (
        "ALLOWED_CROSS_TENANT entries the sweep no longer needs — delete them, or "
        "the exemption silently covers the next leak in that function:\n  "
        + "\n  ".join(unused))
    assert all(ALLOWED_CROSS_TENANT.values()), "every exemption needs a reason"
    print(f"PASS every read of an unscoped tenant model is guarded "
          f"({len(exempted)} documented cross-tenant reads, none stale)")


def test_the_guard_detects_a_new_unscoped_model():
    """A tenant-owned model that nobody registered must FAIL the membership check."""
    owned = {"Machine": object, "Shipment": object}      # Shipment: brand new, forgotten
    scoped = {"Machine"}
    undecided = sorted(set(owned) - scoped - set(MANUALLY_SCOPED))
    assert undecided == ["Shipment"], undecided
    print("PASS the membership check flags a new tenant-owned model that was never registered")


def test_the_guard_detects_an_unguarded_read():
    """Prove the sweep fails on a leak rather than only passing on clean code."""
    leaky = (
        "def list_all_agent_actions(db):\n"
        "    return db.query(models.AgentAction).all()\n"
    )
    guarded = (
        "def list_my_agent_actions(db, tenant):\n"
        "    return db.query(models.AgentAction).filter(\n"
        "        models.AgentAction.tenant_code == tenant).all()\n"
    )
    helper_guarded = (
        "def get_one(db, item_id, current_user):\n"
        "    item = db.query(models.GmatsItem).filter(models.GmatsItem.id == item_id).first()\n"
        "    _guard_record(current_user, item.tenant_code)\n"
        "    return item\n"
    )
    watched = set(MANUALLY_SCOPED)
    assert [h[0] for h in _unguarded_reads("<mem>", watched, src=leaky)] == ["list_all_agent_actions"]
    assert _unguarded_reads("<mem>", watched, src=guarded) == [], "explicit filter must pass"
    assert _unguarded_reads("<mem>", watched, src=helper_guarded) == [], "guard helper must pass"
    print("PASS the sweep flags an unguarded read and clears both guarded forms")


if __name__ == "__main__":
    test_every_tenant_owned_model_is_scoped_or_documented()
    test_reads_of_unscoped_models_are_tenant_guarded()
    test_the_guard_detects_a_new_unscoped_model()
    test_the_guard_detects_an_unguarded_read()
    print("\nALL UNSCOPED-MODEL READ TESTS PASSED")
