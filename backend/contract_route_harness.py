"""Shared harness for the service-contract route suites (ADR-0020).

WHAT THIS FILE IS
-----------------
The seven test_contract_*.py suites, mutate_service_contracts.py and
audit_oem_contracts_adversarial.py drive the REAL application (main.app, every
middleware included) over ASGI, against a disposable database. This module holds
what they share: the database, the HTTP client, tokens, a two-OEM /
three-factory seed, a plan-schema terms document, and the helpers that walk a
contract through its lifecycle.

NO STAND-INS
------------
The routes run against the REAL engine (contract_terms, contract_periods,
contract_statements, attribution_engine) and the REAL shared pieces
(telemetry_coverage, retention's span policy, oem_auth's contract capabilities,
oem_sharing.widen_grants / bound_factory_read / contract_statement_visible,
platform_routes.log_audit(tenant_code=, commit=)). None of it is patched.
`require_integrated()` checks each piece is present before a suite starts and
fails the suite, naming what is missing, if one is not: a green run is a claim
about the integrated system and nothing less.

Nothing here is imported by application code.
"""
import asyncio
import contextlib
import importlib
import importlib.util
import inspect
import json
import os
import sys
from datetime import datetime, timedelta

from jose import jwt as pyjwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import auth
import canonical
import database
import models
import tenancy
from database import Base
from security import hash_password

_URL = os.environ.get("DATABASE_URL", "")
ON_POSTGRES = _URL.startswith("postgresql")
engine = (create_engine(_URL, pool_size=12, max_overflow=12) if ON_POSTGRES else
          create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool))
SessionLocal = sessionmaker(bind=engine)
ALGO = getattr(auth, "ALGORITHM", "HS256")

failures = []


FAIL_FAST = os.environ.get("CONTRACT_FAIL_FAST") == "1"


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")
        if FAIL_FAST:
            # mutate_service_contracts sets this: the first red check is all a
            # mutation run needs, and the rest of the suite is minutes.
            print(f"FAIL-FAST: {label}")
            sys.stdout.flush()
            os._exit(1)
    return condition


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


# ── The integrated system, checked rather than assumed ─────────────────

def require_integrated():
    """Fail loudly, naming each missing piece, unless every shared helper the
    contract routes call is the real one. Nothing is substituted."""
    import oem_auth
    import oem_sharing
    import platform_routes
    import retention

    missing = []
    if importlib.util.find_spec("telemetry_coverage") is None:
        missing.append("module telemetry_coverage")
    else:
        tc = importlib.import_module("telemetry_coverage")
        for name in ("SPAN_GAP_SECONDS", "SPAN_WRITE_RESOLUTION_SECONDS",
                     "SETTLE_SECONDS", "record_message"):
            if not hasattr(tc, name):
                missing.append(f"telemetry_coverage.{name}")
    if sum(p.model is models.MachineTelemetrySpan for p in retention.POLICIES) != 1:
        missing.append("retention policy for machine_telemetry_spans")
    for cap in ("read_contracts", "manage_contracts", "sign_contracts"):
        if not any(cap in caps for caps in oem_auth.ROLE_CAPABILITIES.values()):
            missing.append(f"oem_auth capability {cap}")
    params = inspect.signature(platform_routes.log_audit).parameters
    if "commit" not in params or "tenant_code" not in params:
        missing.append("platform_routes.log_audit(tenant_code=, commit=)")
    for name in ("contract_statement_visible", "bound_factory_read", "widen_grants"):
        if not hasattr(oem_sharing, name):
            missing.append(f"oem_sharing.{name}")
    if missing:
        raise AssertionError("the contract routes need these shared pieces, which are "
                             "missing: " + "; ".join(missing))


def stand_in_banner():
    print("RAN AGAINST THE REAL ENGINE AND REAL SHARED HELPERS (no stand-ins)")


# ── Application wiring ────────────────────────────────────────────────

main = None


def boot():
    """Check the integrated system is present, then wire the app (see wire)."""
    require_integrated()
    return wire()


def wire():
    """Import the app and point every route module at the test database."""
    global main
    import main as _main
    main = _main
    database.SessionLocal = SessionLocal
    main.SessionLocal = SessionLocal
    for mod in ("oem_routes", "connected_equipment_routes", "oem_admin_routes",
                "machines_routes", "platform_routes", "oem_contract_routes",
                "service_contract_routes", "industrial_iot_routes"):
        try:
            __import__(mod)
        except ModuleNotFoundError:
            continue
        sys.modules[mod].SessionLocal = SessionLocal
    tenancy.install_scoping()
    return main


class Resp:
    def __init__(self, status, raw, headers):
        self.status = status
        self.raw = raw
        self.headers = headers
        try:
            self.body = json.loads(raw or b"{}")
        except Exception:
            self.body = {"_raw": raw[:200].decode("utf-8", "replace")}

    def __repr__(self):
        return f"{self.status} {str(self.body)[:300]}"


async def _call(method, path, tok=None, body=None, headers=None):
    raw = [(b"host", b"testserver")]
    payload = b"" if body is None else json.dumps(body).encode()
    if tok:
        raw.append((b"authorization", f"Bearer {tok}".encode()))
    for k, v in (headers or {}).items():
        raw.append((k.lower().encode(), v.encode()))
    if body is not None:
        raw.append((b"content-type", b"application/json"))
        raw.append((b"content-length", str(len(payload)).encode()))
    p, _, q = path.partition("?")
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": p, "raw_path": p.encode(),
             "query_string": q.encode(), "root_path": "", "headers": raw,
             "client": ("127.0.0.1", 5000), "server": ("testserver", 80)}
    chunks, status, hdrs = [], {}, {}

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(m):
        if m["type"] == "http.response.start":
            status["code"] = m["status"]
            for k, v in m.get("headers", []):
                hdrs[k.decode("latin-1").lower()] = v.decode("latin-1")
        elif m["type"] == "http.response.body":
            chunks.append(m.get("body", b""))

    await main.app(scope, receive, send)
    return Resp(status.get("code"), b"".join(chunks), hdrs)


def GET(path, tok=None, headers=None):
    return asyncio.run(_call("GET", path, tok, None, headers))


def POST(path, tok=None, body=None, headers=None):
    return asyncio.run(_call("POST", path, tok, {} if body is None else body, headers))


def PUT(path, tok=None, body=None, headers=None):
    return asyncio.run(_call("PUT", path, tok, {} if body is None else body, headers))


def status_of(make_request):
    """The HTTP status of a request, or "500 (<error>)" when the app raised
    instead of answering. An unhandled error is exactly what a refusal check
    exists to catch, so it must fail that check rather than abort the suite."""
    try:
        return make_request().status
    except Exception as e:
        return f"500 ({type(e).__name__}: {str(e)[:120]})"


def oem_token(sub, oem, role="OEM_ADMIN"):
    return pyjwt.encode({"sub": sub, "role": role, "principal": "oem", "oem": oem,
                         "exp": datetime.utcnow() + timedelta(hours=1)},
                        auth.SECRET_KEY, algorithm=ALGO)


def fac_token(sub, tenant, role="Admin"):
    return pyjwt.encode({"sub": sub, "role": role, "tenant": tenant,
                         "exp": datetime.utcnow() + timedelta(hours=1)},
                        auth.SECRET_KEY, algorithm=ALGO)


@contextlib.contextmanager
def unscoped():
    """A session with NO tenant bound, so a test sees every tenant's rows."""
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    try:
        yield db
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


@contextlib.contextmanager
def clock(at):
    """Pin service_contracts' clock (the only one the routes read)."""
    import service_contracts
    original = service_contracts.utcnow
    service_contracts.utcnow = lambda: at
    try:
        yield
    finally:
        service_contracts.utcnow = original


# ── Seed ──────────────────────────────────────────────────────────────
#
# OEM_ALPHA made SN-A1, SN-A2 (linked at FACTORY_A), SN-A3 (at FACTORY_A, NOT
# linked to a machine), SN-AB (at FACTORY_B), SN-A9 (unassigned stock).
# OEM_BETA made SN-B1 (at FACTORY_A). Things worth stealing are planted at every
# factory so a leak is visible to a needle scan.

S = {}
NEEDLES = ("SECRET-PART", "SECRET-NOTE", "SECRETNAME", "secret-operator",
           "Secret Customer")

TOKENS = {}


def seed():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    S.clear()
    with unscoped() as db:
        for code, name in (("OEM_ALPHA", "Alpha Compressors"), ("OEM_BETA", "Beta Pumps")):
            db.add(models.OemOrganization(oem_code=code, name=name))
        for user, oem, role in (("alpha_admin", "OEM_ALPHA", "OEM_ADMIN"),
                                ("alpha_mgr", "OEM_ALPHA", "OEM_SERVICE_MANAGER"),
                                ("alpha_eng", "OEM_ALPHA", "OEM_SERVICE_ENGINEER"),
                                ("alpha_view", "OEM_ALPHA", "OEM_VIEWER"),
                                ("beta_admin", "OEM_BETA", "OEM_ADMIN")):
            db.add(models.OemUser(oem_code=oem, username=user,
                                  password=hash_password("x"), role=role))
        for t in ("FACTORY_A", "FACTORY_B", "FACTORY_C"):
            low = t.lower()
            for role in ("Admin", "Supervisor", "Operator"):
                db.add(models.User(username=f"{low}_{role.lower()}",
                                   password=hash_password("x"), role=role,
                                   tenant_code=t, is_active=True))
            db.add(models.WorkOrder(tenant_code=t, work_order_no="WO-1",
                                    part_number=f"{t}-SECRET-PART", batch_number="B",
                                    target_quantity=5, status="Planned"))
            db.add(models.CustomerOrder(tenant_code=t, order_no=f"SO-{t}",
                                        customer_name="Secret Customer",
                                        product_name="P", order_quantity=1,
                                        due_date=datetime.utcnow().date()))
        db.flush()
        machines = {}
        for t, name in (("FACTORY_A", "A1"), ("FACTORY_A", "A2"), ("FACTORY_A", "A3"),
                        ("FACTORY_A", "B1"), ("FACTORY_B", "AB")):
            m = models.Machine(tenant_code=t, site="Plant", name=f"{name}-SECRETNAME",
                               status="Running", utilization=77)
            db.add(m)
            db.flush()
            machines[name] = m.id
        S["machines"] = machines
        model_ids = {}
        for oem in ("OEM_ALPHA", "OEM_BETA"):
            mm = models.MachineModel(oem_code=oem, family="Compressor", model_code="X200",
                                     name=f"{oem} X200", service_interval_hours=2000)
            db.add(mm)
            db.flush()
            model_ids[oem] = mm.id
        inst = {}
        for oem, serial, tenant, machine in (
                ("OEM_ALPHA", "SN-A1", "FACTORY_A", machines["A1"]),
                ("OEM_ALPHA", "SN-A2", "FACTORY_A", machines["A2"]),
                ("OEM_ALPHA", "SN-A3", "FACTORY_A", None),
                ("OEM_ALPHA", "SN-AB", "FACTORY_B", machines["AB"]),
                ("OEM_ALPHA", "SN-A9", None, None),
                ("OEM_BETA", "SN-B1", "FACTORY_A", machines["B1"])):
            row = models.MachineInstallation(
                oem_code=oem, serial_number=serial, model_id=model_ids[oem],
                factory_tenant_code=tenant, machine_id=machine,
                status="Active" if tenant else "Manufactured", site="Plant" if tenant else "")
            db.add(row)
            db.flush()
            inst[serial] = row.id
        S["inst"] = inst
        # FACTORY_A already shares alarms with ALPHA: accepting a contract must
        # WIDEN this, never replace it.
        db.add(models.OemDataSharingPolicy(oem_code="OEM_ALPHA", tenant_code="FACTORY_A",
                                           grants="SHARE_ALARMS", updated_by="fa"))
        db.add(models.OemDataSharingPolicy(oem_code="OEM_ALPHA", tenant_code="FACTORY_B",
                                           grants="", updated_by="fb"))
        for t, mid in (("FACTORY_A", machines["A1"]), ("FACTORY_B", machines["AB"])):
            db.add(models.DowntimeLog(tenant_code=t, machine_id=mid, reason="No material",
                                      duration="12 min", notes="SECRET-NOTE by secret-operator"))
        db.commit()

    TOKENS.update({
        "alpha": oem_token("alpha_admin", "OEM_ALPHA", "OEM_ADMIN"),
        "alpha_mgr": oem_token("alpha_mgr", "OEM_ALPHA", "OEM_SERVICE_MANAGER"),
        "alpha_eng": oem_token("alpha_eng", "OEM_ALPHA", "OEM_SERVICE_ENGINEER"),
        "alpha_view": oem_token("alpha_view", "OEM_ALPHA", "OEM_VIEWER"),
        "beta": oem_token("beta_admin", "OEM_BETA", "OEM_ADMIN"),
        "fa": fac_token("factory_a_admin", "FACTORY_A", "Admin"),
        "fa_super": fac_token("factory_a_supervisor", "FACTORY_A", "Supervisor"),
        "fa_op": fac_token("factory_a_operator", "FACTORY_A", "Operator"),
        "fb": fac_token("factory_b_admin", "FACTORY_B", "Admin"),
        "fc": fac_token("factory_c_admin", "FACTORY_C", "Admin"),
    })


# ── Terms and lifecycle helpers ───────────────────────────────────────

def now_utc():
    return canonical.utc_seconds(datetime.utcnow())


def month_label(offset):
    """'YYYY-MM' `offset` months from the current UTC month."""
    n = now_utc()
    y, m = divmod(n.year * 12 + (n.month - 1) + offset, 12)
    return f"{y:04d}-{m + 1:02d}"


def terms(serials, **overrides):
    t = {"schema": 1, "currency": "INR", "period_months": 1, "period_fee": "40000.00",
         "timezone": "Asia/Kolkata", "term_months": 12,
         "coverage": {"mode": "24x7"},
         "sla_target_pct": "97.00",
         "credit_tiers": [{"below_pct": "97.00", "credit_pct": "5.00"},
                          {"below_pct": "95.00", "credit_pct": "10.00"}],
         "min_measured_pct": "90.00",
         "trusted_sources": ["mqtt"],
         "status_defaults": {"Breakdown": "OEM", "Maintenance": "FACTORY",
                             "Offline": "DISPUTED"},
         "reason_map": {"no material": "FACTORY", "power failure": "FACTORY",
                        "motor overheating": "OEM"},
         "generic_reasons": ["breakdown", "unknown"], "reason_lead_seconds": 600,
         "termination_notice_days": 30,
         "covered_installations": [{"installation_id": S["inst"][s], "serial_number": s}
                                   for s in serials]}
    t.update(overrides)
    return t


_ref_counter = [0]


def new_ref():
    _ref_counter[0] += 1
    return f"AMC-{_ref_counter[0]:04d}"


def draft(tok=None, serials=("SN-A1",), tenant="FACTORY_A", start_offset=-3,
          ref=None, **term_overrides):
    body = {"contract_ref": ref or new_ref(), "title": "Annual maintenance",
            "contract_type": "AMC", "factory_tenant_code": tenant,
            "start_month": month_label(start_offset),
            "terms": terms(serials, **term_overrides)}
    return POST("/oem/contracts", tok or TOKENS["alpha"], body)


def contract_detail(cid, tok=None):
    return GET(f"/oem/contracts/{cid}", tok or TOKENS["alpha"])


def version_hash(cid, version=1, tok=None):
    r = contract_detail(cid, tok)
    for v in r.body.get("versions", []):
        if v["version"] == version:
            return v["terms_hash"]
    return None


def propose(cid, tok=None):
    return POST(f"/oem/contracts/{cid}/propose", tok or TOKENS["alpha"],
                {"terms_hash": version_hash(cid)})


def factory_hash(cid, tok=None, version=1):
    """The terms hash as the FACTORY sees it — what it actually accepts."""
    r = GET(f"/service-contracts/{cid}", tok or TOKENS["fa"])
    for v in r.body.get("versions", []) if isinstance(r.body, dict) else []:
        if v["version"] == version:
            return v["terms_hash"]
    return None


def factory_accept(cid, tok=None, grant=True, terms_hash=None):
    # The hash as the accepting factory sees it; when that token cannot see the
    # contract (another factory, an OEM token), the drafting OEM's view, so the
    # refusal under test is the route's and not a missing hash.
    body = {"terms_hash": terms_hash or factory_hash(cid, tok) or version_hash(cid)}
    if grant is not None:
        body["grant_downtime_sharing"] = grant
    return POST(f"/service-contracts/{cid}/accept", tok or TOKENS["fa"], body)


def active_contract(serials=("SN-A1",), tenant="FACTORY_A", start_offset=-3,
                    fac_tok=None, **term_overrides):
    """Draft, propose and accept. Returns the contract id."""
    r = draft(serials=serials, tenant=tenant, start_offset=start_offset,
              **term_overrides)
    assert r.status == 200, r
    cid = r.body["id"]
    p = propose(cid)
    assert p.status == 200, p
    a = factory_accept(cid, tok=fac_tok)
    assert a.status == 200, a
    return cid


def periods(cid, tok=None):
    r = GET(f"/oem/contracts/{cid}/periods", tok or TOKENS["alpha"])
    assert r.status == 200, r
    return r.body["periods"]


def parse_ts(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")


def add_span(tenant, machine_id, start, end, status="Running", source="mqtt"):
    with unscoped() as db:
        row = models.MachineTelemetrySpan(tenant_code=tenant, machine_id=machine_id,
                                          source=source, status=status,
                                          span_start=start, span_end=end,
                                          message_count=10)
        db.add(row)
        db.commit()
        return row.id


def measured_periods(cid, machine_key="A1", tenant="FACTORY_A", gap_hours=1):
    """Running spans over the first two periods, with a `gap_hours` hole ten
    days into the first. Returns (p0, p1, gap_start, gap_end) as datetimes."""
    ps = periods(cid)
    p0s, p0e = parse_ts(ps[0]["start"]), parse_ts(ps[0]["end"])
    p1e = parse_ts(ps[1]["end"])
    gap_start = p0s + timedelta(days=10)
    gap_end = gap_start + timedelta(hours=gap_hours)
    mid = S["machines"][machine_key]
    add_span(tenant, mid, p0s, gap_start)
    add_span(tenant, mid, gap_end, p1e + timedelta(days=1))
    return (p0s, p0e), (p0e, p1e), gap_start, gap_end


def compute(cid, period_start, tok=None, oem=True):
    if oem:
        return POST(f"/oem/contracts/{cid}/statements/compute", tok or TOKENS["alpha"],
                    {"period_start": period_start})
    return POST(f"/service-contracts/{cid}/statements/compute", tok or TOKENS["fa"],
                {"period_start": period_start})


def accept_statement(cid, sid, content_hash, revision, tok=None, oem=True):
    base = "/oem/contracts" if oem else "/service-contracts"
    return POST(f"{base}/{cid}/statements/{sid}/accept",
                tok or (TOKENS["alpha"] if oem else TOKENS["fa"]),
                {"content_hash": content_hash, "revision": revision})


def audit_rows(action=None):
    with unscoped() as db:
        q = db.query(models.AuditLog)
        if action:
            q = q.filter(models.AuditLog.action == action)
        return [(r.tenant_code, r.action, r.entity_type, r.entity_id, r.details, r.actor)
                for r in q.order_by(models.AuditLog.id.asc()).all()]


def notifications():
    with unscoped() as db:
        return [(n.tenant_code, n.notification_type, n.title, n.message)
                for n in db.query(models.Notification)
                .order_by(models.Notification.id.asc()).all()]


def grants(oem="OEM_ALPHA", tenant="FACTORY_A"):
    import oem_sharing
    with unscoped() as db:
        return oem_sharing.grants_for(db, oem, tenant)


def finish(suite_banner):
    print()
    stand_in_banner()
    print("=" * 74)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        sys.exit(1)
    print(suite_banner)
