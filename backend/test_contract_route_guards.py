"""Structural guards on the service-contract routes and their service (ADR-0021).

Behavioural suites prove what the routes do today. These guards make the rules
hard to break TOMORROW, by reading the source: a handler added without a gate, a
statement action added without the consent check, a transition written without
a condition, a second clock. Each guard asserts it found the targets it names,
so a rename cannot turn a guard into a check of nothing.

  1. Every handler in oem_contract_routes depends on oem_auth.require_oem with
     the capability its action needs; every handler in service_contract_routes
     depends on require_roles with the roles its action needs.
  2. The route files are thin: no models, no queries, no commits, no oem_code or
     tenant taken from a request.
  3. Every statement-content or statement-action function in service_contracts
     calls require_statement_access (consent) before anything else it does.
  4. Every UPDATE in service_contracts is conditional on the state it read, and
     every audit goes through the one two-party helper, into the caller's
     transaction (platform_routes.add_audit).
  5. One clock: service_contracts.utcnow is the only reader of the wall clock.
  6. The service imports the engine lazily, so the app boots without it.
  7. Both routers are mounted on the real app, exactly once.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_contract_route_guards.py
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")
    return condition


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def _tree(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as fh:
        source = fh.read()
    return source, ast.parse(source)


def _handlers(tree):
    """{function name: (http method, path, function node)} for @router.<method>(path)."""
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "router"
                    and dec.func.attr in ("get", "post", "put", "patch", "delete")):
                path = dec.args[0].value if dec.args else ""
                out[node.name] = (dec.func.attr.upper(), path, node)
    return out


def _gate(fn, factory):
    """The gate expression of a handler: the call inside Depends(...) among its
    defaults that is require_oem(...) or require_roles(...)."""
    for default in fn.args.defaults + fn.args.kw_defaults:
        if (isinstance(default, ast.Call) and isinstance(default.func, ast.Name)
                and default.func.id == "Depends" and default.args):
            inner = default.args[0]
            if not isinstance(inner, ast.Call):
                continue
            if factory == "require_oem" and isinstance(inner.func, ast.Attribute) \
                    and isinstance(inner.func.value, ast.Name) \
                    and inner.func.value.id == "oem_auth" and inner.func.attr == "require_oem":
                return [a.id for a in inner.args if isinstance(a, ast.Name)]
            if factory == "require_roles" and isinstance(inner.func, ast.Name) \
                    and inner.func.id == "require_roles":
                return [a.id for a in inner.args if isinstance(a, ast.Name)]
    return None


# What each handler must require. READ < MANAGE < SIGN for the OEM; READERS
# (Admin, Supervisor) or SIGNERS (Admin) for the factory.
OEM_GATES = {
    "list_contracts": "READ", "create_contract": "MANAGE", "get_contract": "READ",
    "edit_draft": "MANAGE", "propose": "SIGN", "withdraw": "SIGN",
    "draft_amendment": "MANAGE", "propose_amendment": "SIGN", "accept_amendment": "SIGN",
    "reject_amendment": "SIGN", "terminate": "SIGN", "periods": "READ", "preview": "READ",
    "compute": "MANAGE", "statement": "READ", "verify": "READ", "download": "READ",
    "accept_statement": "SIGN", "raise_dispute": "MANAGE", "disputes": "READ",
    "propose_resolution": "MANAGE", "accept_resolution": "SIGN",
    "withdraw_dispute": "MANAGE", "history": "READ",
}
FACTORY_GATES = {
    "list_contracts": "READERS", "get_contract": "READERS", "accept": "SIGNERS",
    "reject": "SIGNERS", "reason_vocabulary": "READERS", "draft_amendment": "SIGNERS",
    "propose_amendment": "SIGNERS", "accept_amendment": "SIGNERS",
    "reject_amendment": "SIGNERS", "terminate": "SIGNERS", "periods": "READERS",
    "preview": "READERS", "compute": "SIGNERS", "statement": "READERS", "verify": "READERS",
    "download": "READERS", "accept_statement": "SIGNERS", "raise_dispute": "SIGNERS",
    "disputes": "READERS", "propose_resolution": "SIGNERS", "accept_resolution": "SIGNERS",
    "withdraw_dispute": "SIGNERS", "history": "READERS",
}


def case_every_handler_is_gated():
    section("1. EVERY HANDLER IS GATED, WITH THE CAPABILITY ITS ACTION NEEDS")
    for module, factory, expected in (("oem_contract_routes.py", "require_oem", OEM_GATES),
                                      ("service_contract_routes.py", "require_roles",
                                       FACTORY_GATES)):
        _, tree = _tree(module)
        handlers = _handlers(tree)
        check(f"{module}: found exactly the {len(expected)} expected handlers",
              sorted(handlers) == sorted(expected),
              sorted(set(handlers) ^ set(expected)))
        for name, (method, path, fn) in sorted(handlers.items()):
            gate = _gate(fn, factory)
            check(f"{module} {method} {path or '/'} ({name}) requires {expected.get(name)}",
                  gate == [expected.get(name)], gate)
    import oem_contract_routes as o
    import service_contract_routes as f
    check("the OEM capability names are the planned ones",
          (o.READ, o.MANAGE, o.SIGN) == ("read_contracts", "manage_contracts",
                                         "sign_contracts"))
    check("factory readers are Admin and Supervisor; signers are Admin only",
          f.READERS == ["Admin", "Supervisor"] and f.SIGNERS == ["Admin"])


def case_route_files_are_thin():
    section("2. THE ROUTE FILES ARE THIN: NO MODELS, QUERIES, COMMITS OR REQUEST TENANTS")
    for module in ("oem_contract_routes.py", "service_contract_routes.py"):
        source, tree = _tree(module)
        imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                   for a in n.names}
        imports |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        check(f"{module} imports neither models nor tenancy nor oem_sharing",
              not imports & {"models", "tenancy", "oem_sharing", "platform_routes"},
              sorted(imports))
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        check(f"{module} never queries, executes, adds or commits",
              not attrs & {"query", "execute", "add", "commit", "flush", "rollback"},
              sorted(attrs & {"query", "execute", "add", "commit", "flush", "rollback"}))
        _, tree = _tree(module)
        params = {a.arg for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  for a in n.args.args}
        check(f"{module} takes no oem_code or tenant from the path or query",
              not params & {"oem_code", "tenant", "tenant_code", "factory_tenant_code"},
              sorted(params))
        handlers = _handlers(tree)
        calls_svc = [name for name, (_, _, fn) in handlers.items()
                     if any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                            and n.value.id == "svc" for n in ast.walk(fn))]
        check(f"{module}: every handler delegates to service_contracts",
              len(calls_svc) == len(handlers) and handlers, len(calls_svc))
    import schemas_contracts
    bodies = [c for c in vars(schemas_contracts).values()
              if isinstance(c, type) and issubclass(c, schemas_contracts._Body)
              and c is not schemas_contracts._Body]
    check("found the 11 request bodies", len(bodies) == 11, [b.__name__ for b in bodies])
    check("every request body forbids fields it does not name",
          all(b.model_config.get("extra") == "forbid" for b in bodies))
    check("no request body has an oem_code field",
          not [b for b in bodies if "oem_code" in b.model_fields])


def _function(tree, name):
    return next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name),
                None)


def _calls(node, name):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Name) and n.func.id == name)
                 or (isinstance(n.func, ast.Attribute) and n.func.attr == name))]


CONSENT_GATED = ("compute", "statement_view", "preview", "verify", "download",
                 "accept_statement", "raise_dispute", "propose_resolution",
                 "accept_resolution")


def case_statement_actions_check_consent_first():
    section("3. EVERY STATEMENT READ OR ACTION CHECKS CONSENT BEFORE IT TOUCHES THE STATEMENT")
    _, tree = _tree("service_contracts.py")
    found = 0
    for name in CONSENT_GATED:
        fn = _function(tree, name)
        if not check(f"service_contracts.{name} exists", fn is not None):
            continue
        found += 1
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        consent = _calls(fn, "require_statement_access")
        check(f"{name} calls require_statement_access", len(consent) == 1, len(consent))
        if not consent:
            continue
        first_touch = [n.lineno for n in calls
                       if isinstance(n.func, (ast.Name, ast.Attribute))
                       and getattr(n.func, "id", getattr(n.func, "attr", None))
                       in ("require_statement", "_run_compute", "_engine", "statement_payload",
                           "require_dispute")]
        check(f"...before it reads the statement, the dispute or the engine",
              not first_touch or consent[0].lineno < min(first_touch),
              (consent[0].lineno, first_touch))
    check(f"found all {len(CONSENT_GATED)} consent-gated functions",
          found == len(CONSENT_GATED), found)
    gate = _function(tree, "require_statement_access")
    visible = _calls(gate, "contract_statement_visible")
    check("the consent gate asks oem_sharing (the one consent rule), for the OEM side",
          len(visible) == 1 and isinstance(visible[0].func, ast.Attribute)
          and visible[0].func.value.id == "oem_sharing", ast.dump(gate)[:200])


def case_transitions_are_conditional_and_audited_once():
    section("4. EVERY UPDATE IS CONDITIONAL; EVERY AUDIT IS TWO-PARTY AND IN-TRANSACTION")
    source, tree = _tree("service_contracts.py")
    updates = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name) and n.func.id == "update"]
    check("found the service's UPDATE statements (at least 15)", len(updates) >= 15,
          len(updates))
    # Each update(...) is the innermost receiver of a .where(...).values(...) chain.
    unconditional = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "values":
            where = n.func.value
            if not (isinstance(where, ast.Call) and isinstance(where.func, ast.Attribute)
                    and where.func.attr == "where"):
                unconditional.append(n.lineno)
                continue
            text = ast.unparse(where)
            if not any(k in text for k in (".status ==", ".status==", "content_hash ==")):
                unconditional.append(n.lineno)
    check("every UPDATE's WHERE names the status (or hash and revision) it read",
          not unconditional, unconditional)
    add_calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "add_audit"]
    log_calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "log_audit"]
    check("add_audit is called in exactly one place (the _audit helper)",
          len(add_calls) == 1, len(add_calls))
    check("...and log_audit, which commits on its own, is never called here",
          not log_calls, [n.lineno for n in log_calls])
    if add_calls:
        kw = {k.arg: k.value for k in add_calls[0].keywords}
        check("...with an explicit tenant_code", "tenant_code" in kw)
    check("no db.commit() sits before an _audit call in the same function "
          "(audit rows join the business transaction)",
          _audits_after_commit(tree) == [], _audits_after_commit(tree))


def _audits_after_commit(tree):
    """Functions where an _audit(...) call comes after a db.commit() — an audit
    written after the business commit could survive its rollback, or vice versa."""
    bad = []
    for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
        # A commit IMMEDIATELY followed by `raise` persists a legitimate
        # recompute and then refuses: nothing after it runs, so no audit can
        # follow it. Every other commit must come after the last audit.
        refusing = set()
        for block in ast.walk(fn):
            for field in ("body", "orelse", "finalbody"):
                stmts = getattr(block, field, None)
                if not isinstance(stmts, list):
                    continue
                for a, b in zip(stmts, stmts[1:]):
                    if (isinstance(a, ast.Expr) and isinstance(a.value, ast.Call)
                            and isinstance(a.value.func, ast.Attribute)
                            and a.value.func.attr == "commit" and isinstance(b, ast.Raise)):
                        refusing.add(a.lineno)
        commits = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute) and n.func.attr == "commit"
                   and n.lineno not in refusing]
        audits = [n.lineno for n in _calls(fn, "_audit")]
        if any(c < a for c in commits for a in audits):
            bad.append(fn.name)
    return bad


def case_one_clock_and_a_lazy_engine():
    section("5-6. ONE CLOCK, AND THE APP BOOTS WITHOUT THE ENGINE")
    source, tree = _tree("service_contracts.py")
    clock_reads = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute)
                   and n.func.attr in ("utcnow", "now", "today", "time")
                   and isinstance(n.func.value, (ast.Name, ast.Attribute))
                   and getattr(n.func.value, "id", getattr(n.func.value, "attr", "")) in
                   ("datetime", "date", "time")]
    utcnow = _function(tree, "utcnow")
    inside = [n for n in clock_reads if utcnow and utcnow.lineno <= n.lineno
              <= utcnow.end_lineno]
    check("found service_contracts.utcnow", utcnow is not None)
    check("the wall clock is read exactly once, inside utcnow",
          len(clock_reads) == 1 and len(inside) == 1, [n.lineno for n in clock_reads])
    top_imports = {a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names}
    top_imports |= {n.module for n in tree.body if isinstance(n, ast.ImportFrom)}
    check("contract_statements is not imported at module level",
          "contract_statements" not in top_imports and "attribution_engine" not in top_imports,
          sorted(top_imports))
    engine = _function(tree, "_engine")
    check("...it is imported inside _engine, which says 503 when it is missing",
          engine is not None and "import contract_statements" in ast.unparse(engine)
          and "503" in ast.unparse(engine))
    check("service_contracts computes no money: no float and no Decimal",
          "float(" not in source and "Decimal" not in source)
    own = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    check("service_contracts keeps no copy of the engine's rules (which terms govern, "
          "whether an acceptance counts)",
          not own & {"governing_version", "accepted_versions", "acceptance_state",
                     "acceptance_is_valid"}, sorted(own & {"governing_version",
                                                           "accepted_versions",
                                                           "acceptance_state",
                                                           "acceptance_is_valid"}))
    engine_calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr in ("governing_version", "accepted_versions")
                    and isinstance(n.func.value, ast.Name) and n.func.value.id == "engine"]
    check("...and calls the engine's governing_version / accepted_versions instead "
          "(found at least 6 calls)", len(engine_calls) >= 6, len(engine_calls))
    for module in ("oem_contract_routes.py", "service_contract_routes.py"):
        src, tr = _tree(module)
        reads = [n for n in ast.walk(tr) if isinstance(n, ast.Attribute)
                 and n.attr in ("utcnow", "now") and isinstance(n.value, ast.Name)
                 and n.value.id == "datetime"]
        check(f"{module} reads no clock of its own", not reads)


def case_both_routers_are_mounted_once():
    section("7. BOTH ROUTERS ARE MOUNTED ON THE REAL APP, EXACTLY ONCE")
    import main
    import oem_contract_routes
    import service_contract_routes
    app_routes = [(r.path, tuple(sorted(r.methods))) for r in main.app.routes
                  if getattr(r, "methods", None)]
    for router, prefix, n in ((oem_contract_routes.router, "/oem/contracts", 24),
                              (service_contract_routes.router, "/service-contracts", 23)):
        ours = [(r.path, tuple(sorted(r.methods))) for r in router.routes]
        check(f"{prefix}: the router defines {n} routes", len(ours) == n, len(ours))
        mounted = [r for r in app_routes if r[0] == prefix or r[0].startswith(prefix + "/")]
        check(f"{prefix}: each is mounted exactly once, and nothing else lives there",
              sorted(mounted) == sorted(ours), sorted(set(mounted) ^ set(ours)))


def run_all():
    case_every_handler_is_gated()
    case_route_files_are_thin()
    case_statement_actions_check_consent_first()
    case_transitions_are_conditional_and_audited_once()
    case_one_clock_and_a_lazy_engine()
    case_both_routers_are_mounted_once()


def test_contract_route_guards():
    failures.clear()
    run_all()
    assert not failures, failures


if __name__ == "__main__":
    run_all()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        sys.exit(1)
    print("EVERY CONTRACT ROUTE IS GATED, THIN, CONSENT-CHECKED AND CONDITIONAL")
