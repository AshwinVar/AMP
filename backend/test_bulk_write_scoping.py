"""Architectural guard: every BULK write must be tenant-safe (ADR-0002).

The auto-scoping hook in tenancy.py rewrites SELECTs only::

    @event.listens_for(Session, "do_orm_execute")
    def _apply_tenant_filter(state):
        if not state.is_select:
            return

So `db.query(X).filter(...).update(...)` / `.delete()` — a bulk UPDATE/DELETE
that never issues a SELECT — is NOT auto-scoped. A bulk write without an
explicit tenant guard silently reaches into every tenant's rows. That is exactly
the bug found in the "mark all notifications read" endpoint (#365), where the
missing filter cleared four rows across two tenants instead of two.

Instance writes (`db.delete(row)`, `row.status = ...`) are safe: the row was
loaded by a SELECT, which IS scoped, so a foreign row is never found.

This test statically finds every bulk write in the backend and requires each to
be either:
  * explicitly tenant-guarded — an owning-tenant column is COMPARED in the same
    statement (not merely spelled in it; see ``_TENANT_PREDICATE``), or
  * listed in ``PARENT_GUARDED`` with the reason it is safe.

Adding a new bulk write therefore fails CI until it is proven safe, which is the
point: the hook cannot protect this path, so a human decision is recorded here.

Run:  python backend/test_bulk_write_scoping.py     (exit 0 = pass)
"""
import ast
import os
import re

BACKEND = os.path.dirname(os.path.abspath(__file__))

# Files that are one-shot operator scripts run by hand against a chosen database
# (never mounted on the API), so a request tenant does not exist for them.
SKIP_FILES = {
    # Audit harness, never mounted on the API. It deliberately performs an
    # unguarded bulk UPDATE to MEASURE that the ADR-0002 hook scopes SELECTs
    # only — the very property this file is the control for. Its writes are
    # confined to the three disposable FACTORY_* audit tenants.
    "audit_isolation.py",
    # Performance harness, never mounted on the API and never run against a real
    # database — it builds and tears down its own PERF_FACTORY tenant on a
    # disposable engine to count SQL statements. Its deletes are the teardown
    # between scales, not a product write path.
    "dashboard_perf.py",
    "reseed_inventory.py",   # local dev reseed
    "reset_factory.py",      # founder's factory reset (RESEED_FACTORY=1)
    # The AERON sales demo, rebuilt behind RESEED_DEMO_OEM. Exactly the
    # reset_factory precedent: a one-shot operator rebuild reached only through
    # an env flag, with no request and therefore no request tenant to scope to.
    # Its deletes ARE scoped, just not all by `tenant_code`: the OEM-side rows
    # are keyed by `oem_code == "AERON"` (they have no owning tenant at all) and
    # the child rows by their parent's id. What keeps it off a customer is
    # `_assert_demo_scope()`, which refuses to run unless the target tenant is
    # named DEMO_* — a stronger guarantee than a filter, because it fails before
    # any statement is issued rather than relying on each one being right.
    "demo_aeron.py",
    "offboard_tenant.py",    # purge — filters cls.tenant_code == code itself
    # Operational retention job: prunes by AGE across every tenant, on purpose.
    # It runs from a CLI with no request context, so there is no tenant to scope
    # to and scoping would be wrong — a per-tenant prune would leave the
    # unbounded tables unbounded for whichever tenant nobody ran it for. Its own
    # safety comes from a different direction: dry-run by default, an explicit
    # policy table, and a refusal to accept a policy on any table whose tenant
    # column is nullable (see retention.py) — so it can never delete rows a
    # tenant-scoped read would have hidden.
    "retention.py",
    # OEM fleet performance harness (ADR-0017). Never mounted on the API: it
    # builds and tears down its OWN fleet in a disposable scratch database, and
    # the tables it wipes (machine_installations, machine_models,
    # oem_organizations, oem_data_sharing_policies) are keyed by `oem_code`, not
    # `tenant_code` — there is no tenant filter to add, because these rows have
    # no owning tenant. It refuses to run on anything but a scratch PostgreSQL.
    "oem_perf.py",
    # Two-OEM / three-factory adversarial audit. Same category: it seeds and
    # attacks a disposable database and is never served.
    "audit_oem_adversarial.py",
    # The end-to-end pilot journey (ADR-0019). Every business step in it goes
    # through the real HTTP API on purpose — that is the point of the harness —
    # and its ONE unguarded write is the exception it documents in its own
    # docstring: the operating hours a machine reports arrive over MQTT
    # (ADR-0011), there is no HTTP route to post them, and there should not be
    # one. It runs against a disposable scratch database and is never served.
    "audit_oem_pilot_journey.py",
    # Preflight for the #245 backfill: it builds an adversarial fixture in a
    # disposable database and its one unguarded UPDATE is the fixture itself --
    # a row deliberately moved by "somebody else" so the rollback can be shown
    # to leave that decision alone. Never mounted on the API.
    "preflight_backfill_245.py",
}

# Bulk writes that carry NO tenant_code of their own but are safe because the
# parent row was first loaded through a scoped SELECT (a foreign parent 404s),
# and the children are addressed only by that parent's primary key.
# Key: "<file>::<function>"  ->  why it is safe.
PARENT_GUARDED = {
    "work_orders_routes.py::delete_work_order":
        "the work order is fetched (scoped SELECT -> 404 if foreign) before its plans are deleted by work_order_id",
    "gmats_inventory_routes.py::gmats_delete_item":
        "the item is fetched + _guard_record'd before its aliases are deleted by item_id",
    "gmats_inventory_routes.py::gmats_void_min":
        "the MIN is fetched + _guard_record'd before its lines are deleted by min_id",
    "oem_claims.py::revoke":
        "the claim was fetched in the route filtered by `oem_code == principal['oem']` "
        "(a competitor's claim 404s before reaching here), and this UPDATE addresses "
        "it by PRIMARY KEY: `WHERE id = claim.id AND status = 'Pending'`. It is a "
        "conditional compare-and-set rather than a sweep -- the `status` predicate is "
        "what makes revoking safe against a simultaneous claim, and it is why this is "
        "an UPDATE and not a read-modify-write.",
    "oem_claims.py::accept":
        "The sibling of `revoke`, and it USED to be exempt by accident: its "
        "statement writes `claimed_tenant_code`, whose substring satisfied the old "
        "spelling check even though nothing was scoped by it. Now stated properly. "
        "The claim was fetched by `find_by_code` (a hash lookup that can only "
        "return the one row whose code was presented) and this UPDATE addresses it "
        "by PRIMARY KEY with `status = 'Pending'`, which is the compare-and-set "
        "that makes exactly one factory win a race. The second statement is scoped "
        "by `factory_tenant_code IS NULL` and so can only ever match a machine no "
        "factory owns -- it is incapable of touching a tenant's row. Both are "
        "proven by verify_pg_claim.py: 25 two-thread races, exactly one winner.",
}

_BULK_METHODS = {"update", "delete"}

# An OWNING-TENANT column used as a PREDICATE.
#
# Two columns name a row's owner. `tenant_code` is the usual one;
# `factory_tenant_code` is MachineInstallation's, deliberately named differently
# so the offboarding sweep cannot see it (ADR-0017) — filtering on it IS a real
# tenant scope and must count as one.
#
# `claimed_tenant_code` is NOT in this set. It records which factory accepted a
# claim; it does not own the claim row, and a predicate on it scopes nothing.
# The lookbehind is what tells the two apart: `claimed_` before `tenant_code` is
# a word character, so it cannot match, while the `.` before
# `factory_tenant_code` can. The trailing comparison is what stops a VALUES dict
# — `{"tenant_code": x}` sets a column, it does not restrict which rows.
_TENANT_COLUMNS = ("tenant_code", "factory_tenant_code")
_TENANT_PREDICATE = re.compile(
    r"(?<![_\w])(?:" + "|".join(_TENANT_COLUMNS) + r")\s*(==|!=|\.is_\(|\.in_\(|\.isnot\()")


def _is_query_chain(node) -> bool:
    """True when this call's receiver chain starts at a `.query(...)` call —
    i.e. a Query-level BULK operation, not `db.delete(instance)`."""
    cur = node.func.value if isinstance(node.func, ast.Attribute) else None
    while cur is not None:
        if isinstance(cur, ast.Call):
            f = cur.func
            if isinstance(f, ast.Attribute) and f.attr == "query":
                return True
            cur = f.value if isinstance(f, ast.Attribute) else None
        elif isinstance(cur, ast.Attribute):
            cur = cur.value
        else:
            return False
    return False


def _bulk_writes(path):
    """[(function_name, source_segment, lineno)] for each bulk write in a file."""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    # map each node to its enclosing function for reporting / allowlisting
    enclosing = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(fn):
                enclosing.setdefault(child, fn.name)

    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _BULK_METHODS and _is_query_chain(node)):
            found.append((enclosing.get(node, "<module>"),
                          ast.get_source_segment(src, node) or "",
                          node.lineno))
    return found


def test_every_bulk_write_is_tenant_guarded():
    unguarded = []
    checked = 0
    for name in sorted(os.listdir(BACKEND)):
        if not name.endswith(".py") or name.startswith("test_") or name in SKIP_FILES:
            continue
        path = os.path.join(BACKEND, name)
        for func, segment, lineno in _bulk_writes(path):
            checked += 1
            # THE GUARD IS A FILTER TEST, NOT A SPELLING TEST. This used to be
            # `if "tenant_code" in segment`, which two things satisfied without
            # being scoped at all: a write whose VALUES dict sets
            # `{"tenant_code": ...}` with no predicate, and a write naming a
            # different column that merely contains the substring — the machine
            # claim's `claimed_tenant_code` passed this way for a whole release.
            # Both are now flagged and must be justified in PARENT_GUARDED.
            if _TENANT_PREDICATE.search(segment):
                continue                                    # explicitly guarded
            if f"{name}::{func}" in PARENT_GUARDED:
                continue                                    # documented parent guard
            unguarded.append(f"{name}:{lineno} in {func}() -> {segment.splitlines()[0][:90]}")

    assert not unguarded, (
        "Bulk UPDATE/DELETE without a tenant guard (the ADR-0002 hook only scopes "
        "SELECTs, so these reach EVERY tenant's rows). Add an explicit "
        "`tenant_code == request_tenant(current_user)` filter, or — if a parent row "
        "was already fetched through a scoped SELECT — record it in PARENT_GUARDED "
        "with the reason:\n  " + "\n  ".join(unguarded))
    assert checked >= 4, f"expected to find the known bulk writes, only saw {checked}"
    print(f"PASS all {checked} bulk writes are tenant-guarded or documented parent-guarded")


def test_the_guard_actually_detects_an_unscoped_bulk_write():
    """The guard must FAIL on a leak, not just pass on clean code — so prove it
    against a synthetic unscoped bulk write rather than trusting the sweep."""
    import tempfile
    leak = (
        "def clear_everything(db):\n"
        "    db.query(models.Notification).filter(models.Notification.status != 'Read')"
        ".update({models.Notification.status: 'Read'})\n"
    )
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "leaky_routes.py")
        open(p, "w", encoding="utf-8").write(leak)
        hits = _bulk_writes(p)
    assert len(hits) == 1, hits
    func, segment, _ = hits[0]
    assert func == "clear_everything"
    assert "tenant_code" not in segment, "the synthetic leak must look unguarded"
    print("PASS the guard detects an unscoped bulk write (it fails on leaks, not just passes on clean code)")


def test_instance_writes_are_not_flagged():
    """`db.delete(row)` is an instance write — the row came from a scoped SELECT,
    so it must NOT be reported (a guard that cried wolf would get disabled)."""
    import tempfile
    safe = (
        "def delete_one(db, row_id):\n"
        "    row = db.query(models.Machine).filter(models.Machine.id == row_id).first()\n"
        "    db.delete(row)\n"
    )
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "safe_routes.py")
        open(p, "w", encoding="utf-8").write(safe)
        assert _bulk_writes(p) == [], "db.delete(instance) must not be treated as a bulk write"
    print("PASS instance deletes are not flagged (no false positives)")


if __name__ == "__main__":
    test_every_bulk_write_is_tenant_guarded()
    test_the_guard_actually_detects_an_unscoped_bulk_write()
    test_instance_writes_are_not_flagged()
    print("\nALL BULK-WRITE SCOPING TESTS PASSED")
