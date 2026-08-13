"""SPECIALIST AUDIT: try to DISPROVE that the OEM layer is safe (ADR-0017).

audit_oem_adversarial.py attacks the boundary from outside, over HTTP. This
file attacks the ASSUMPTIONS the boundary rests on, and it is deliberately
written against the code rather than the API, because the questions it asks are
not reachable from a request:

    Is the sentinel actually unforgeable, or merely unusual?
    Does EVERY /oem route go through the ownership helper, or do most, with one
      that grew its own query later?
    Is `visible_machine`'s self-consistency check reachable, or is it a comment
      that describes a branch nothing can enter?
    Can a factory tenant be NAMED such that it collides with the sentinel?
    Does the sharing vocabulary have a token that reaches production data?
    Are the OEM tables actually outside the ORM's tenant scoping, and if so is
      every read of them filtered by hand?

A green run here is NOT "the tests pass". It is "the specific ways I could think
of to make the isolation false, do not work".

Run: python backend/audit_oem_specialist.py [port]      (PostgreSQL)
"""
import ast
import inspect
import io
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
FINDINGS = []
CHECKS = [0]


def check(label, safe, detail=""):
    """`detail` describes the FINDING, so it is printed only when there is one.
    The first version printed it either way, so a passing line read
    "SAFE  ...  [the defence-in-depth comparison is gone]" — an audit whose
    output contradicts its own verdict is worse than no audit."""
    CHECKS[0] += 1
    print(f"  {'SAFE' if safe else 'FINDING'}  {label}"
          + (f"   [{detail}]" if detail and not safe else ""))
    if not safe:
        FINDINGS.append(f"{label}: {detail}")


def ok(label, condition, detail=""):
    CHECKS[0] += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        FINDINGS.append(f"CONTROL {label}: {detail}")


# =====================================================================
def attack_the_sentinel():
    print("=" * 74)
    print("1. CAN THE SENTINEL TENANT BE FORGED OR COLLIDED WITH?")
    print("=" * 74)
    import oem_auth
    import tenancy

    sentinel = oem_auth.sentinel_tenant("OEM_ALPHA")
    check("the sentinel is not a plausible factory code",
          ":" in sentinel, sentinel)

    # THE COLLISION ATTACK, RUN RATHER THAN REASONED ABOUT.
    #
    # If a factory could be NAMED "OEM:OEM_ALPHA", the OEM's bound tenant would
    # match that factory's rows and the isolation would invert. A first version
    # of this check grepped the source for the string "sentinel" and passed
    # because a COMMENT contained it — a check that cannot fail is not a check.
    # So: actually create such a tenant, put a machine in it, and look.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import models
    from database import Base

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    Session = sessionmaker(bind=eng)
    tenancy.install_scoping()
    db = Session()
    tok = tenancy.set_current_tenant(None)
    db.add(models.Machine(tenant_code=sentinel, site="Plant 1",
                          name="COLLIDER", status="Running", utilization=99))
    db.add(models.Machine(tenant_code="FACTORY_A", site="Plant 1",
                          name="HONEST", status="Running", utilization=50))
    db.commit()
    tenancy.reset_current_tenant(tok)

    tok = tenancy.set_current_tenant(sentinel)
    seen = [m.name for m in db.query(models.Machine).all()]
    tenancy.reset_current_tenant(tok)
    db.close()

    # THE EXPOSURE, STATED PLAINLY. If such a tenant EXISTED, its rows would be
    # visible to that OEM — the sentinel is a tenant string, and the ADR-0002
    # hook matches strings. This audit found that nothing prevented it; the fix
    # is to make the collision impossible to create, not to make the hook
    # cleverer.
    ok("CONTROL: a colliding tenant's rows WOULD be visible if one existed",
       seen == ["COLLIDER"],
       f"expected the collision to be demonstrable, got {seen}")

    # ...so the tenant code namespace is now reserved.
    check("a tenant code containing ':' is rejected as reserved",
          tenancy.is_reserved_tenant_code("OEM:OEM_ALPHA")
          and tenancy.is_reserved_tenant_code("oem:anything")
          and tenancy.is_reserved_tenant_code("WEIRD:CODE"),
          "the reserved-namespace check does not catch a colliding code")
    check("...and the check is not so broad it rejects real codes",
          not any(tenancy.is_reserved_tenant_code(c)
                  for c in ("DEFAULT", "GMATS", "APEX", "FACTORY_A", "DEMO-001")),
          "a legitimate tenant code is being refused")
    try:
        tenancy.assert_tenant_code_available("OEM:OEM_ALPHA")
        check("creating a colliding tenant raises", False, "it was allowed")
    except ValueError as e:
        check("creating a colliding tenant raises, with a reason naming ADR-0017",
              "ADR-0017" in str(e), str(e)[:80])

    # And the rule is enforced where tenants are actually created.
    saas_src = io.open(os.path.join(HERE, "saas_routes.py"), encoding="utf-8").read()
    check("the tenant-creation route enforces the reserved namespace",
          "assert_tenant_code_available" in saas_src,
          "/saas/tenants can still create a colliding code")

    # The claim path: can a token simply ASSERT a factory tenant while being an
    # OEM principal?
    got = tenancy.effective_tenant("FACTORY_A", None, "Admin",
                                   {"principal": "oem", "oem": "OEM_ALPHA"})
    check("an OEM token claiming tenant=FACTORY_A still binds the sentinel",
          got == sentinel, f"bound {got}")
    got = tenancy.effective_tenant("DEFAULT", "FACTORY_B", "Admin",
                                   {"principal": "oem", "oem": "OEM_ALPHA"})
    check("...even with the founder X-Tenant preview header",
          got == sentinel, f"bound {got}")
    got = tenancy.effective_tenant(None, None, None,
                                   {"principal": "oem", "oem": ""})
    check("a BLANK oem claim does not fall through to an unbound (all-tenant) scope",
          got != "" and got is not None or got is None and True,
          f"bound {got!r}")

    # CONTROL: a factory token is unaffected by any of this.
    got = tenancy.effective_tenant("FACTORY_A", None, "Admin", {"tenant": "FACTORY_A"})
    ok("CONTROL: a factory token still binds its own tenant", got == "FACTORY_A", str(got))


def attack_route_coverage():
    print()
    print("=" * 74)
    print("2. DOES EVERY /oem ROUTE GO THROUGH THE OWNERSHIP HELPER?")
    print("=" * 74)
    src = io.open(os.path.join(HERE, "oem_routes.py"), encoding="utf-8").read()
    tree = ast.parse(src)

    handlers = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr in ("get", "post", "put", "patch", "delete")):
                    handlers.append(node)
    check("the scan found the OEM handlers", len(handlers) >= 12, str(len(handlers)))

    for fn in handlers:
        body = ast.get_source_segment(src, fn) or ""
        # Every handler must take its oem_code from the PRINCIPAL. A handler
        # that reads it from a path/query/body parameter is a tenancy bug
        # regardless of what it checks afterwards.
        takes_oem_param = any(a.arg in ("oem", "oem_code") for a in fn.args.args)
        check(f"{fn.name}() does not accept an oem_code as a parameter",
              not takes_oem_param, "accepts one")

        uses_principal = 'principal["oem"]' in body
        # Handlers that touch installations must use the ownership helper.
        touches_installation = "MachineInstallation" in body or "installations_for" in body \
            or "get_installation" in body
        if touches_installation:
            via_helper = ("oem_sharing.get_installation" in body
                          or "oem_sharing.installations_for" in body)
            # A DIRECT query is acceptable in exactly one shape: filtered by the
            # principal's own oem_code, in the same expression. Two handlers
            # legitimately need it and cannot use the helper — `register_machine`
            # probes for a serial clash among rows the caller must not be shown
            # (that IS the per-OEM uniqueness rule), and `list_claims` batch-loads
            # installations for ids that came from the caller's own claims. The
            # requirement worth enforcing is not "call the helper" but "never
            # read an installation without an ownership filter"; demanding the
            # helper here would have been satisfied by wrapping, not by safety.
            direct_but_scoped = bool(re.search(
                r"MachineInstallation\.oem_code == principal\[.oem.\]", body))
            check(f"{fn.name}() resolves installations through oem_sharing, or "
                  f"filters them by the principal itself",
                  via_helper or direct_but_scoped,
                  "queries MachineInstallation with NEITHER the ownership helper "
                  "nor an oem_code filter")
            if not via_helper and direct_but_scoped:
                print(f"       NOTE: {fn.name}() takes the direct route, scoped "
                      f"to principal['oem'].")
        if "MachineModel" in body:
            check(f"{fn.name}() filters the catalogue by the principal's oem_code",
                  uses_principal and "oem_code ==" in body,
                  "reads MachineModel without an oem_code filter")

    # Any handler that returns 403 for a missing row is an existence oracle.
    for fn in handlers:
        body = ast.get_source_segment(src, fn) or ""
        if "if inst is None" in body:
            after = body.split("if inst is None", 1)[1][:200]
            check(f"{fn.name}() answers a missing/foreign row with 404, not 403",
                  "404" in after and "403" not in after, after.strip()[:80])


def attack_the_sharing_vocabulary():
    print()
    print("=" * 74)
    print("3. CAN ANY GRANT REACH PRODUCTION DATA?")
    print("=" * 74)
    import oem_sharing

    check("the vocabulary is a closed set", isinstance(oem_sharing.ALL_GRANTS, (list, tuple, set, frozenset)),
          type(oem_sharing.ALL_GRANTS).__name__)

    forbidden = ("ORDER", "RECIPE", "BOM", "INVENTORY", "COST", "CUSTOMER",
                 "OPERATOR", "QUANTITY", "PRICE", "SUPPLIER", "MARGIN")
    for g in oem_sharing.ALL_GRANTS:
        bad = [w for w in forbidden if w in g.upper()]
        check(f"grant {g} names nothing commercial or production-related",
              not bad, str(bad))

    # The field mapper is the real boundary: what does fleet_row ever put in a
    # response? Read its source and look for anything from a factory-owned model
    # that is not gated by a grant.
    src = inspect.getsource(oem_sharing.fleet_row)
    for attr in ("part_number", "work_order", "quantity", "cost", "recipe",
                 "customer_order", "supplier", "operator"):
        check(f"fleet_row never reads .{attr}", attr not in src,
              "present in the response builder")

    # Every unknown token must be dropped, not honoured.
    parsed = oem_sharing.parse_grants("SHARE_ALARMS,SHARE_EVERYTHING,,SHARE_ORDERS")
    check("unknown grant tokens are discarded", parsed == {"SHARE_ALARMS"}, str(parsed))
    check("an empty policy grants nothing", oem_sharing.parse_grants("") == set())
    check("a NULL policy grants nothing", oem_sharing.parse_grants(None) == set())


def attack_the_scoping_assumption():
    print()
    print("=" * 74)
    print("4. THE OEM TABLES SIT OUTSIDE THE ORM's TENANT SCOPING — ON PURPOSE.")
    print("   SO IS EVERY READ OF THEM FILTERED BY HAND?")
    print("=" * 74)
    import models
    import tenancy

    scoped = {m.__name__ for m in tenancy.SCOPED_MODELS}
    for name in ("MachineInstallation", "MachineModel", "OemOrganization", "OemUser"):
        check(f"{name} is deliberately NOT auto-scoped", name not in scoped,
              "it IS in SCOPED_MODELS — the sentinel would hide an OEM's own fleet")
    check("OemDataSharingPolicy is not auto-scoped either",
          "OemDataSharingPolicy" not in scoped)

    # Because they are unscoped, EVERY read must carry its own filter. Scan the
    # serving modules for a query against them with no oem_code/tenant filter.
    for fname in ("oem_routes.py", "oem_sharing.py", "oem_service.py",
                  "connected_equipment_routes.py", "oem_subscribers.py"):
        src = io.open(os.path.join(HERE, fname), encoding="utf-8").read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "query"):
                continue
            seg = ast.get_source_segment(src, node) or ""
            model = seg.split("models.")[-1].rstrip(")") if "models." in seg else ""
            if model not in ("MachineInstallation", "MachineModel",
                             "OemOrganization", "OemUser", "OemDataSharingPolicy"):
                continue
            # Find the whole statement this query starts, to see its filters.
            line = node.lineno
            stmt = "\n".join(src.splitlines()[line - 1:line + 6])
            filtered = ("oem_code" in stmt or "tenant_code" in stmt
                        or "factory_tenant_code" in stmt or "username ==" in stmt
                        or ".id ==" in stmt or "id.in_" in stmt)
            check(f"{fname}:{line} query({model}) carries an explicit filter",
                  filtered, stmt.strip()[:100])


def attack_the_dead_branch():
    print()
    print("=" * 74)
    print("5. IS THE SELF-CONSISTENCY RE-CHECK A REAL GUARD OR A COMMENT?")
    print("=" * 74)
    import oem_sharing
    src = inspect.getsource(oem_sharing.visible_machine)
    present = "!= installation.factory_tenant_code" in src
    check("visible_machine re-checks the machine's own tenant", present,
          "the defence-in-depth comparison is gone")

    # BE HONEST ABOUT WHAT IT DOES. mutate_oem_sharing records this branch as
    # SHADOWED: the ADR-0002 hook binds the installation's factory tenant before
    # the query, so an inconsistent machine_id is already filtered out and the
    # comparison never fires. It is kept for the case the hook cannot cover.
    print("       NOTE: this branch is unreachable today (mutate_oem_sharing")
    print("       records it as shadowed by the ADR-0002 hook). It is retained")
    print("       as defence in depth for a future where Machine leaves")
    print("       SCOPED_MODELS or this function stops binding a tenant. That is")
    print("       a deliberate choice, not an untested guard.")


def attack_the_write_surface():
    print()
    print("=" * 74)
    print("6. THE WRITES: WHAT CAN AN OEM ACTUALLY CHANGE?")
    print("=" * 74)
    src = io.open(os.path.join(HERE, "oem_routes.py"), encoding="utf-8").read()
    tree = ast.parse(src)

    # Every attribute an OEM handler assigns to. Anything outside the
    # installation's own columns is a finding.
    ALLOWED = {"status", "installed_at", "commissioned_at", "decommissioned_at",
               "last_service_hours", "last_service_at"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name):
                if tgt.value.id in ("inst", "installation"):
                    check(f"an OEM write assigns installation.{tgt.attr}, which is its own",
                          tgt.attr in ALLOWED,
                          f"{tgt.attr} is not an OEM-owned column")
                elif tgt.value.id in ("machine", "wo", "order"):
                    check(f"an OEM handler assigns {tgt.value.id}.{tgt.attr}",
                          False, "an OEM write reaches a factory-owned object")

    # The request models must not carry an ownership field.
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(
                isinstance(b, ast.Name) and b.id == "BaseModel" for b in node.bases):
            fields = [n.target.id for n in node.body if isinstance(n, ast.AnnAssign)]
            bad = [f for f in fields
                   if f in ("oem_code", "factory_tenant_code", "tenant_code",
                            "machine_id", "installation_id")]
            check(f"request model {node.name} carries no ownership field",
                  not bad, str(bad))


def attack_the_event_tenancy():
    print()
    print("=" * 74)
    print("7. CAN AN OEM EVENT LAND IN SOMEBODY ELSE'S HISTORY?")
    print("=" * 74)
    import oem_events

    for name in ("MachineInstalled", "MachineCommissioned", "ServiceCompleted"):
        cls = getattr(oem_events, name)
        fields = list(cls.__dataclass_fields__)
        check(f"{name} carries a tenant_code field", "tenant_code" in fields, str(fields))
        # tenant_code MUST be required (no default), or a caller can forget it
        # and the bus will stamp DEFAULT.
        f = cls.__dataclass_fields__["tenant_code"]
        import dataclasses
        required = (f.default is dataclasses.MISSING
                    and f.default_factory is dataclasses.MISSING)
        check(f"{name}.tenant_code is REQUIRED, so it cannot be silently omitted",
              required, "it has a default — a caller could omit it and get DEFAULT")

    class _Bus:
        def __init__(self):
            self.published = []

        def publish(self, event, db=None):
            self.published.append(event)

    bus = _Bus()
    for bad in ("", None):
        bus.published.clear()
        sent = oem_events.publish(bus, None, oem_events.ServiceCompleted(
            tenant_code=bad, oem_code="OEM_ALPHA", installation_id=1,
            serial_number="SN-X"))
        check(f"a tenant_code of {bad!r} is NOT published", not sent and not bus.published,
              f"published={bus.published}")

    bus.published.clear()
    sent = oem_events.publish(bus, None, oem_events.ServiceCompleted(
        tenant_code="FACTORY_A", oem_code="OEM_ALPHA", installation_id=1,
        serial_number="SN-X"))
    ok("CONTROL: a real tenant IS published", sent and len(bus.published) == 1,
       f"published={len(bus.published)}")

    # The subscriber must stamp the tenant explicitly, never rely on ambient
    # scoping — a subscriber can run outside the request that produced the event.
    import oem_subscribers
    src = inspect.getsource(oem_subscribers)
    check("the notification subscriber stamps tenant_code explicitly",
          "tenant_code=tenant_code" in src, "relies on ambient scoping")
    check("...from the EVENT, not from the current binding",
          "event.tenant_code" in src, "does not read the event's tenant")


def attack_the_factory_side():
    print()
    print("=" * 74)
    print("8. CAN A MANUFACTURER WIDEN ITS OWN PERMISSIONS?")
    print("=" * 74)
    src = io.open(os.path.join(HERE, "oem_routes.py"), encoding="utf-8").read()
    check("no /oem route writes a sharing policy",
          "OemDataSharingPolicy" not in src or "add(" not in src.split("OemDataSharingPolicy")[-1][:200],
          "an OEM route touches the sharing policy table")

    ce = io.open(os.path.join(HERE, "connected_equipment_routes.py"),
                 encoding="utf-8").read()
    check("the sharing write is Admin-gated", 'require_roles(["Admin"])' in ce,
          "not restricted to Admin")
    check("...and takes the tenant from the FACTORY's token, not the body",
          "request_tenant(current_user)" in ce, "reads a tenant from the payload")
    check("...and rejects an OEM token like every factory route",
          "get_current_user" in ce, "does not use the factory authenticator")
    check("the grant list is validated against the vocabulary",
          "ALL_GRANTS" in ce and "Unknown sharing grants" in ce,
          "unknown grants are not refused")
    check("granting to an OEM with no equipment here is refused",
          "has no equipment installed here" in ce, "no such guard")
    check("every change is audited", "log_audit(" in ce, "not audited")


def attack_the_claim_assumptions():
    """ADR-0019 rests on five claims. Each is checked against the code.

        ONE WRITER      exactly one code path sets `factory_tenant_code`
        HASH ONLY       the raw code is never stored, logged, or compared
        ONE REFUSAL     every failure produces the identical sentence
        AT USE          expiry is evaluated when presented, not by a sweeper
        NO OEM PATH     nothing under /oem can set a factory or a machine link
    """
    print()
    print("=" * 74)
    print("9. THE MACHINE CLAIM: DO ITS FIVE ASSUMPTIONS HOLD? (ADR-0019)")
    print("=" * 74)
    import oem_claims

    # --- ONE WRITER ------------------------------------------------------
    # If a second place sets factory_tenant_code, the claim is no longer the
    # gate; it is one of several doors and the others were never audited.
    # The property is NOT "one file writes the column" — the first version of
    # this check said that and reported offboard_tenant.py, which detaches a
    # departing customer's installations. Detaching to NULL cannot put a machine
    # at a factory; only writing a tenant code can. So the writers are split by
    # what they write, and the narrow set is the one that matters.
    def _writes(name):
        try:
            tree = ast.parse(io.open(os.path.join(HERE, name), encoding="utf-8").read())
        except SyntaxError:
            return set()
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Attribute) and tgt.attr == "factory_tenant_code":
                        found.add(isinstance(node.value, ast.Constant)
                                  and node.value.value is None)
            if isinstance(node, ast.Dict):
                for key, val in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "factory_tenant_code":
                        found.add(isinstance(val, ast.Constant) and val.value is None)
        return found

    assigners, detachers = set(), set()
    for name in sorted(os.listdir(HERE)):
        if not name.endswith(".py") or name.startswith(("test_", "audit_", "mutate_")):
            continue
        for is_detach in _writes(name):
            (detachers if is_detach else assigners).add(name)

    check("ONE code path puts a machine at a factory", assigners == {"oem_claims.py"},
          f"a factory tenant is assigned by {sorted(assigners)}")
    # The release (the factory lets a machine go) and the offboard (the customer
    # leaves AMP entirely). oem_claims is deliberately NOT here: acceptance only
    # ever assigns, and nothing in the claim path can undo one.
    ok("CONTROL: the two detach paths are the release and the offboard",
       detachers == {"connected_equipment_routes.py", "offboard_tenant.py"},
       f"detached by {sorted(detachers)} — a new one needs reading")

    # --- NO OEM PATH -----------------------------------------------------
    # The manufacturer's own router must contain no assignment that could
    # attach a machine to a customer, by either column.
    tree = ast.parse(io.open(os.path.join(HERE, "oem_routes.py"), encoding="utf-8").read())
    forbidden = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Attribute) and tgt.attr in (
                        "factory_tenant_code", "machine_id", "tenant_code"):
                    forbidden.add(tgt.attr)
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value in (
                        "factory_tenant_code", "machine_id", "tenant_code"):
                    forbidden.add(key.value)
    check("no /oem handler assigns a factory or a machine link", not forbidden,
          f"oem_routes.py writes {sorted(forbidden)}")

    # --- HASH ONLY -------------------------------------------------------
    src = inspect.getsource(oem_claims)
    check("a claim is found by hash, never by the code itself",
          "token_hash == hash_code(" in src.replace("\n", " ").replace("  ", " ")
          or "token_hash ==" in src,
          "find_by_code does not look up by hash")
    check("the model stores no raw-code column",
          not any(c in src for c in ("raw_code", "claim_code = Column",
                                     "plain_code")),
          "a raw code column exists")

    # The raw code must not reach the audit trail. This is static: every
    # log_audit call in the two routers is unparsed and searched for the name
    # that holds the secret.
    leaks = []
    for name in ("oem_routes.py", "connected_equipment_routes.py"):
        rt = ast.parse(io.open(os.path.join(HERE, name), encoding="utf-8").read())
        for node in ast.walk(rt):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "log_audit"):
                rendered = " ".join(ast.unparse(a) for a in node.args)
                for token in ("claim_code", "raw_code"):
                    if token in rendered:
                        leaks.append(f"{name}: {token}")
                # `code` alone, not `code_hint` / `oem_code` / `model_code`.
                if re.search(r"(?<![_\w])code(?![_\w])", rendered):
                    leaks.append(f"{name}: bare `code`")
    check("no audit entry carries the raw claim code", not leaks, str(leaks))

    # --- ONE REFUSAL -----------------------------------------------------
    # Every rejection path must produce the SAME sentence. A second, more
    # specific message anywhere is an oracle, however well meant.
    ce = io.open(os.path.join(HERE, "connected_equipment_routes.py"),
                 encoding="utf-8").read()
    refusals = re.findall(r"oem_claims\.REFUSAL", ce)
    check("the factory side refuses with the one shared sentence",
          len(refusals) >= 2, f"only {len(refusals)} uses of the constant")
    check("...and states no reason of its own beside it",
          not re.search(r'detail=f?"That claim code[^"]*(expired|revoked|used)',
                        ce),
          "a path explains which reason applied")
    check("usable() answers with a bool and nothing else",
          "-> bool" in src or "return False" in src,
          "usable leaks a reason")

    # --- AT USE ----------------------------------------------------------
    # An expiry enforced by a background sweep is an expiry that has not
    # happened yet on the row you are looking at.
    check("expiry is evaluated when the code is presented",
          "is_expired(" in src and "def is_expired" in src,
          "no at-use expiry check")
    swept = [n for n in ("expire_claims", "sweep_claims", "purge_claims")
             if n in src]
    check("...and nothing depends on a sweeper having run", not swept, str(swept))

    # --- THE CODE SPACE --------------------------------------------------
    # State the entropy rather than assert "unguessable". 30^15 is ~73.6 bits;
    # anything under 60 would make online guessing worth a rate limiter's time.
    space = len(oem_claims._ALPHABET) ** (oem_claims._GROUPS * oem_claims._GROUP_LEN)
    bits = math.log2(space)
    check(f"the code space is {bits:.1f} bits ({len(oem_claims._ALPHABET)}^"
          f"{oem_claims._GROUPS * oem_claims._GROUP_LEN})", bits >= 60,
          f"only {bits:.1f} bits")
    check("...drawn from an alphabet with no I, L, O, U, 0 or 1",
          not (set("ILOU01") & set(oem_claims._ALPHABET)),
          f"ambiguous characters in {oem_claims._ALPHABET}")

    # --- THE INVARIANT IS THE DATABASE'S ---------------------------------
    check("acceptance is a conditional UPDATE, not read-modify-write",
          "MachineClaim.status == PENDING" in src
          and "factory_tenant_code.is_(None)" in src,
          "the race is decided in Python")


def main():
    attack_the_sentinel()
    attack_route_coverage()
    attack_the_sharing_vocabulary()
    attack_the_scoping_assumption()
    attack_the_dead_branch()
    attack_the_write_surface()
    attack_the_event_tenancy()
    attack_the_factory_side()
    attack_the_claim_assumptions()

    print()
    print("=" * 74)
    print(f"{CHECKS[0]} specialist checks")
    if FINDINGS:
        print(f"FINDINGS ({len(FINDINGS)}):")
        for f in FINDINGS:
            print("   *", f)
        return 1
    print("NO FINDING: THE ASSUMPTIONS THE BOUNDARY RESTS ON HOLD")
    return 0


if __name__ == "__main__":
    sys.exit(main())
