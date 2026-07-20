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


if __name__ == "__main__":
    test_audit_logs_single_owner_no_shadow()
    print("ALL AUDIT CONSOLIDATION TESTS PASSED")
