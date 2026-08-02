"""Regression guard: /audit-logs is single-owned by platform_routes (ADR-0009).

main.py used to carry its own GET /audit-logs (any authenticated user, limit 500)
and POST /audit-logs. The GET was dead: platform_routes registers GET /audit-logs
first (Admin-only, limit 200), so main's copy was shadowed and never served — the
same shadowed-duplicate class of bug as the dead /health (#142).

The audit domain is consolidated in platform_routes (which also owns the
log_audit helper): the dead GET was removed and the POST moved there. This test
asserts /audit-logs has exactly one GET and one POST, both owned by
platform_routes, so the shadow can't silently come back.

Run:  python backend/test_audit_consolidation.py     (exit 0 = pass)
"""
import main


def test_audit_logs_single_owner_no_shadow():
    gets, posts = [], []
    for r in main.app.routes:
        if getattr(r, "path", "") != "/audit-logs":
            continue
        methods = {m for m in r.methods if m != "HEAD"}
        if "GET" in methods:
            gets.append(r.endpoint.__module__)
        if "POST" in methods:
            posts.append(r.endpoint.__module__)
    assert gets == ["platform_routes"], f"GET /audit-logs should be single + platform_routes, got {gets}"
    assert posts == ["platform_routes"], f"POST /audit-logs should be single + platform_routes, got {posts}"
    assert not hasattr(main, "get_audit_logs"), "the dead shadowed main.get_audit_logs must stay removed"
    assert not hasattr(main, "create_audit_log"), "create_audit_log moved to platform_routes; must not be on main"
    print("PASS /audit-logs single-owned by platform_routes (GET+POST); no shadow, no main-local copies")


def _sess():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import models
    from database import Base
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _role_verdict(method, user):
    """Run the dependency the ROUTE declares, for real.

    A hardcoded require_roles([...]) here would only test my guess at the gate,
    and calling the handler directly skips Depends entirely — the mistake that
    made a properly-gated endpoint look wide open while verifying #438.
    """
    from fastapi import HTTPException
    for r in main.app.routes:
        if getattr(r, "path", "") != "/audit-logs" or method not in r.methods:
            continue
        for dep in r.dependant.dependencies:
            if "role_checker" not in getattr(dep.call, "__qualname__", ""):
                continue
            try:
                dep.call(current_user=user)
            except HTTPException as e:
                return e.status_code
        return 200
    raise AssertionError(f"no {method} /audit-logs route found")


def test_the_actor_comes_from_the_token_not_the_payload():
    """THE BUG. `models.AuditLog(**payload.model_dump())` took `actor` verbatim from
    the request body, so an audit row had no binding to the identity that wrote it.
    A Supervisor could post {"actor": "alice_admin", ...} and produce a row
    indistinguishable from the ones log_audit() writes with current_user["sub"].

    Not merely a curl path either: the dashboard's report form posted
    `actor: reportForm.requested_by`, a free-text input, so client-typed
    attribution was the normal path.
    """
    import platform_routes as PR
    import schemas
    db = _sess()
    caller = {"sub": "real_admin", "role": "Admin", "tenant": "DEFAULT"}

    row = PR.create_audit_log(
        schemas.AuditLogCreate(action="delete_user", entity_type="user",
                               entity_id=42, details="removed per policy"),
        db=db, current_user=caller)
    assert row.actor == "real_admin", row.actor
    print("PASS the audit actor is stamped from the caller's token")


def test_a_client_supplied_actor_cannot_override_it():
    """Belt and braces: `actor` is gone from AuditLogCreate, so a client that still
    sends it is ignored rather than believed. Pydantic drops the unknown field, and
    even if it were re-added the handler's keyword would win."""
    import platform_routes as PR
    import schemas
    db = _sess()
    caller = {"sub": "real_admin", "role": "Admin", "tenant": "DEFAULT"}

    assert "actor" not in schemas.AuditLogCreate.model_fields, (
        "AuditLogCreate must not accept an actor — it reads as a working field "
        "while being overwritten, and accepting it is what allowed forgery")
    payload = schemas.AuditLogCreate.model_validate(
        {"actor": "alice_admin", "action": "delete_user"})
    row = PR.create_audit_log(payload, db=db, current_user=caller)
    assert row.actor == "real_admin", row.actor
    print("PASS a client-supplied actor is ignored, not believed")


def test_nobody_writes_into_a_trail_they_cannot_read():
    """The old gates were asymmetric: require_roles(["Admin","Supervisor"]) to
    write, require_roles(["Admin"]) to read. A Supervisor could append to a trail
    she was forbidden to see.

    Verified safe to narrow before doing it: the only frontend caller is the report
    form, which renders inside the "enterprise" view and that view is in the
    frontend's ADMIN_ONLY_VIEWS; every internal caller uses log_audit() directly
    rather than this route.
    """
    supervisor = {"sub": "sup_mallory", "role": "Supervisor", "tenant": "DEFAULT"}
    operator = {"sub": "op", "role": "Operator", "tenant": "DEFAULT"}
    admin = {"sub": "boss", "role": "Admin", "tenant": "DEFAULT"}

    assert _role_verdict("POST", supervisor) == 403, "a Supervisor must not write the trail"
    assert _role_verdict("GET", supervisor) == 403
    assert _role_verdict("POST", operator) == 403
    assert _role_verdict("POST", admin) == 200, "an Admin must still be able to write"
    assert _role_verdict("GET", admin) == 200
    print("PASS the write gate matches the read gate (Admin only)")


def test_a_missing_sub_claim_falls_back_rather_than_writing_null():
    """A token without `sub` must record "system", not NULL — AuditLogResponse
    types actor as a str, so a NULL would 500 on serialisation.

    Two things keep it non-NULL: the route's `or "system"` and the model's
    `Column(String, default="system")`. Mutation testing showed each alone is
    sufficient, so removing just the route's `or` does NOT fail this test —
    the `default=` is a PYTHON-side default, and SQLAlchemy applies it when the
    attribute is None at flush. Only removing BOTH is observable, and that
    mutation IS caught. Belt-and-braces on purpose (it mirrors log_audit's own
    `actor or "system"` idiom); this test pins the observable contract, not
    either individual expression.
    """
    import platform_routes as PR
    import schemas
    db = _sess()
    row = PR.create_audit_log(
        schemas.AuditLogCreate(action="something"),
        db=db, current_user={"role": "Admin", "tenant": "DEFAULT"})
    assert row.actor == "system", row.actor
    print("PASS a token with no sub claim records 'system', not NULL")


if __name__ == "__main__":
    test_audit_logs_single_owner_no_shadow()
    test_the_actor_comes_from_the_token_not_the_payload()
    test_a_client_supplied_actor_cannot_override_it()
    test_nobody_writes_into_a_trail_they_cannot_read()
    test_a_missing_sub_claim_falls_back_rather_than_writing_null()
    print("ALL AUDIT CONSOLIDATION TESTS PASSED")
