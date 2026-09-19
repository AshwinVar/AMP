"""No Copilot tool can see more than the REST route it serves (ADR-0022).

A tool is a second door onto a read-model. The first door is the REST route the
dashboard calls, and that route already carries the product's decisions about
who may read it: its role dependency (require_roles) and its plan pack (the
route prefix in modules.json, enforced by PlanGateMiddleware). A tool that
admitted a role its route refuses, or skipped a pack its route is gated by,
would be a way round both, reached by typing a question.

So each tool names the route it mirrors (`Tool.mirrors`), and this file reads
the LIVE application to check, for every registered tool:

  1. the route exists (a GET or POST with exactly that path template) and
     authenticates (get_current_user is among its dependencies);
  2. if the route restricts roles, the tool restricts them too, to a SUBSET;
     a tool may be narrower than its route, never wider;
  3. the tool's pack, as run_tool computes it, is the pack the plan gate
     assigns to a real URL of that route.

And for the Copilot's own entry point: /copilot/ask authenticates and is in the
Intelligence pack, so every tool call inherits that gate too.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_copilot_tools_no_wider_than_routes.py
"""
import inspect
import re
import sys

import auth
import main
import module_manifest
from ai.tools import REGISTRY, registry

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def calls_of(dependant, acc):
    for d in dependant.dependencies:
        acc.append(d.call)
        calls_of(d, acc)
    return acc


def route_roles(route):
    """The roles a route's require_roles admits, or None if it admits any role."""
    allowed = None
    for c in calls_of(route.dependant, []):
        if "role_checker" in getattr(c, "__qualname__", ""):
            roles = set(inspect.getclosurevars(c).nonlocals.get("allowed_roles") or [])
            allowed = roles if allowed is None else (allowed & roles)
    return allowed


def main_():
    routes = {}
    for r in main.app.routes:
        for m in getattr(r, "methods", ()) or ():
            if m in ("GET", "POST"):
                routes.setdefault(getattr(r, "path", ""), r)

    print("=" * 74)
    print("EVERY TOOL AGAINST THE ROUTE IT MIRRORS")
    print("=" * 74)
    check("there are tools to check", len(REGISTRY) >= 15, str(len(REGISTRY)))
    for name in sorted(REGISTRY):
        t = REGISTRY[name]
        route = routes.get(t.mirrors)
        check(f"{name}: mirrors a real route ({t.mirrors})", route is not None)
        if route is None:
            continue
        check(f"{name}: that route authenticates", auth.get_current_user in calls_of(route.dependant, []))
        allowed = route_roles(route)
        if allowed is None:
            check(f"{name}: its route admits any role, so any tool roles are no wider", True)
        else:
            check(f"{name}: its route admits {sorted(allowed)}; the tool admits a subset",
                  bool(t.roles) and set(t.roles) <= allowed, f"tool roles {t.roles}")
        sample = re.sub(r"\{[^}]+\}", "1", t.mirrors)
        check(f"{name}: its pack is the plan gate's pack for {sample}",
              registry.pack_of(t) == module_manifest.pack_for_path(sample),
              f"{registry.pack_of(t)} vs {module_manifest.pack_for_path(sample)}")

    print()
    print("=" * 74)
    print("THE COPILOT'S OWN DOOR")
    print("=" * 74)
    ask = next((r for r in main.app.routes if getattr(r, "path", "") == "/copilot/ask"
                and "POST" in (getattr(r, "methods", ()) or ())), None)
    check("POST /copilot/ask exists", ask is not None)
    if ask is not None:
        check("/copilot/ask authenticates", auth.get_current_user in calls_of(ask.dependant, []))
    check("/copilot/ask is in the Intelligence pack", module_manifest.pack_for_path("/copilot/ask") == "intelligence")

    if failures:
        print(f"\n{len(failures)} FAILED")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main_()
