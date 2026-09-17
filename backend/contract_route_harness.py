"""Shared harness for the service-contract route suites (ADR-0020).

WHAT THIS FILE IS
-----------------
The seven test_contract_*.py suites, mutate_service_contracts.py and
audit_oem_contracts_adversarial.py drive the REAL application (main.app, every
middleware included) over ASGI, against a disposable database. This module holds
what they share: the database, the HTTP client, tokens, a two-OEM /
three-factory seed, a plan-schema terms document, and the helpers that walk a
contract through its lifecycle.

STAND-INS FOR CODE THAT HAS NOT LANDED YET, AND HOW THEY ARE KEPT HONEST
-----------------------------------------------------------------------
The routes were built in parallel with the attribution engine (contract_terms,
contract_periods, contract_statements) and before the Phase 1 shared helpers
(oem_auth contract capabilities, oem_sharing.widen_grants /
bound_factory_read / contract_statement_visible, log_audit(commit=False,
tenant_code=...)). The routes call those by their PLANNED signatures. Until
they exist, `install()` provides stand-ins, under three rules:

  * A MODULE is stood in only when it cannot be found at all. The moment a real
    contract_statements.py exists it is used, whole, and never patched: a stub
    quietly filling a gap in a real module would hide exactly the integration
    defect a test is for.
  * A shared HELPER is stood in only while its real module lacks it, and each
    stand-in implements the plan's text, nothing more.
  * Every stand-in in use is NAMED on stdout at the end of the run
    (`stand_in_banner`). A green run with stand-ins is not a claim that the
    integrated system works; the banner says so.

Nothing here is imported by application code.
"""
import asyncio
import contextlib
import importlib
import importlib.util
import inspect
import json
import os
import re
import sys
import types
import unicodedata
from collections import namedtuple
from datetime import datetime, timedelta, timezone

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
STAND_INS = []


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


# ── Stand-ins ─────────────────────────────────────────────────────────

SETTLE_SECONDS_STAND_IN = 300 + 30 + 60
SPAN_GAP_STAND_IN = 300
SPAN_RETENTION_DAYS_STAND_IN = 400
BUCKETS = ("AVAILABLE", "OEM", "FACTORY", "DISPUTED", "UNMEASURED")


def _module_missing(name):
    return importlib.util.find_spec(name) is None


def _stand_in_contract_terms():
    import analytics_engine
    import machine_status

    mod = types.ModuleType("contract_terms")
    mod.__stand_in__ = True
    dec = re.compile(r"^\d{1,12}\.\d{2}$")
    required = ("schema", "currency", "period_months", "period_fee", "timezone",
                "term_months", "coverage", "sla_target_pct", "credit_tiers",
                "min_measured_pct", "trusted_sources", "status_defaults",
                "reason_map", "generic_reasons", "reason_lead_seconds",
                "termination_notice_days", "covered_installations")

    class TermsError(ValueError):
        def __init__(self, field, msg):
            super().__init__(f"{field}: {msg}")
            self.field = field
            self.msg = msg

    class Terms:
        def __init__(self, data):
            self.data = data

    def reason_key(s):
        return unicodedata.normalize(
            "NFC", analytics_engine.normalize_downtime_reason(s)).strip().casefold()

    def down_status_keys():
        return sorted(set(machine_status.VALID_MACHINE_STATUSES) - {"Running", "Idle"})

    def _int(raw, field, minimum=0):
        v = raw.get(field)
        if type(v) is not int or v < minimum:
            raise TermsError(field, f"must be an integer >= {minimum}")
        return v

    def _dec(raw, field, pct=False):
        v = raw.get(field)
        if type(v) is not str or not dec.match(v):
            raise TermsError(field, "must be decimal text like 97.00")
        if pct and float(v.replace(".", "")) > 10000:
            raise TermsError(field, "must be <= 100.00")
        return v

    def parse(raw):
        from zoneinfo import ZoneInfo
        if type(raw) is not dict:
            raise TermsError("terms", "must be an object")
        extra = sorted(set(raw) - set(required))
        if extra:
            raise TermsError(extra[0], "unknown field")
        for f in required:
            if f not in raw:
                raise TermsError(f, "is required")
        if raw["schema"] != 1:
            raise TermsError("schema", "must be 1")
        if raw["currency"] != "INR":
            raise TermsError("currency", "must be INR")
        pm = _int(raw, "period_months", 1)
        if pm not in (1, 3):
            raise TermsError("period_months", "must be 1 or 3")
        tm = _int(raw, "term_months", 1)
        if tm % pm:
            raise TermsError("term_months", "must be a multiple of period_months")
        _dec(raw, "period_fee")
        try:
            ZoneInfo(raw["timezone"])
        except Exception:
            raise TermsError("timezone", "unknown timezone")
        if raw["coverage"] != {"mode": "24x7"} and (
                type(raw["coverage"]) is not dict or raw["coverage"].get("mode") != "weekly"):
            raise TermsError("coverage", "mode must be 24x7 or weekly")
        _dec(raw, "sla_target_pct", pct=True)
        _dec(raw, "min_measured_pct", pct=True)
        if type(raw["credit_tiers"]) is not list:
            raise TermsError("credit_tiers", "must be a list")
        ts_ = raw["trusted_sources"]
        if type(ts_) is not list or not ts_ or "manual" in ts_:
            raise TermsError("trusted_sources", "must be non-empty and exclude manual")
        sd = raw["status_defaults"]
        if type(sd) is not dict or sorted(sd) != down_status_keys():
            raise TermsError("status_defaults", "keys must be exactly the down statuses")
        if any(v not in ("OEM", "FACTORY", "DISPUTED") for v in sd.values()):
            raise TermsError("status_defaults", "values must be OEM, FACTORY or DISPUTED")
        rm = raw["reason_map"]
        if type(rm) is not dict or any(v not in ("OEM", "FACTORY") for v in rm.values()):
            raise TermsError("reason_map", "values must be OEM or FACTORY")
        gr = raw["generic_reasons"]
        if type(gr) is not list:
            raise TermsError("generic_reasons", "must be a list")
        gr = sorted({reason_key(x) for x in gr})
        if "breakdown" not in gr or "unknown" not in gr:
            raise TermsError("generic_reasons", "must include breakdown and unknown")
        _int(raw, "reason_lead_seconds")
        _int(raw, "termination_notice_days")
        ci = raw["covered_installations"]
        if type(ci) is not list or not ci:
            raise TermsError("covered_installations", "must be a non-empty list")
        seen = set()
        for row in ci:
            if (type(row) is not dict or type(row.get("installation_id")) is not int
                    or type(row.get("serial_number")) is not str
                    or set(row) != {"installation_id", "serial_number"}):
                raise TermsError("covered_installations",
                                 "each entry is {installation_id, serial_number}")
            if row["installation_id"] in seen:
                raise TermsError("covered_installations", "duplicate installation")
            seen.add(row["installation_id"])
        data = json.loads(json.dumps(raw))
        data["reason_map"] = {reason_key(k): v for k, v in rm.items()}
        data["generic_reasons"] = gr
        data["covered_installations"] = sorted(ci, key=lambda r: r["installation_id"])
        return Terms(data)

    def terms_canonical(terms):
        return json.loads(json.dumps(terms.data))

    def terms_hash(terms):
        return canonical.content_hash(terms_canonical(terms))

    mod.TermsError = TermsError
    mod.Terms = Terms
    mod.parse = parse
    mod.terms_canonical = terms_canonical
    mod.terms_hash = terms_hash
    mod.reason_key = reason_key
    mod.down_status_keys = down_status_keys
    return mod


def _stand_in_contract_periods():
    mod = types.ModuleType("contract_periods")
    mod.__stand_in__ = True
    Period = namedtuple("Period", "start end")

    def _tz(terms):
        import contract_terms
        return contract_terms.terms_canonical(terms)["timezone"]

    def month_start_utc(terms, year, month):
        from zoneinfo import ZoneInfo
        y, m = divmod(year * 12 + (month - 1), 12)
        local = datetime(y, m + 1, 1, tzinfo=ZoneInfo(_tz(terms)))
        return local.astimezone(timezone.utc).replace(tzinfo=None)

    def periods(terms, starts_at, effective_end):
        from zoneinfo import ZoneInfo
        import contract_terms
        pm = contract_terms.terms_canonical(terms)["period_months"]
        local = starts_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(_tz(terms)))
        out, i = [], 0
        while True:
            start = month_start_utc(terms, local.year, local.month + i * pm)
            if start >= effective_end:
                return out
            out.append(Period(start, month_start_utc(terms, local.year,
                                                     local.month + (i + 1) * pm)))
            i += 1

    def period_starting(contract, terms, start):
        end = contract.ends_at
        if contract.termination_effective_at is not None:
            end = min(end, contract.termination_effective_at)
        for p in periods(terms, contract.starts_at, end):
            if p.start == start:
                return p
        return None

    def covered_intervals(period, terms, active_start, active_end):
        lo, hi = max(period.start, active_start), min(period.end, active_end)
        return [(lo, hi)] if lo < hi else []

    mod.Period = Period
    mod.month_start_utc = month_start_utc
    mod.periods = periods
    mod.period_starting = period_starting
    mod.covered_intervals = covered_intervals
    return mod


def _stand_in_contract_statements():
    """A small attribution engine: spans -> AVAILABLE/down default, gaps ->
    UNMEASURED, disputes overlaid. Enough for the ROUTES' behaviour to be
    exercised; the real engine's rules are tested in its own suites."""
    from sqlalchemy import update

    import oem_auth
    import platform_routes

    mod = types.ModuleType("contract_statements")
    mod.__stand_in__ = True
    ComputeResult = namedtuple("ComputeResult", "statement changed frozen")

    class PeriodNotClosed(Exception):
        pass

    class EvidenceExpired(Exception):
        pass

    class StatementConflict(Exception):
        pass

    class NoTermsForPeriod(Exception):
        pass

    def _terms(version):
        import contract_terms
        return contract_terms.parse(json.loads(version.terms_json))

    def _version_for(db, contract, at):
        return (db.query(models.ServiceContractTermVersion)
                  .filter(models.ServiceContractTermVersion.contract_id == contract.id,
                          models.ServiceContractTermVersion.status == "accepted",
                          models.ServiceContractTermVersion.effective_from <= at)
                  .order_by(models.ServiceContractTermVersion.version.desc()).first())

    def acceptance_state(db, statement):
        rows = (db.query(models.ContractStatementAcceptance)
                  .filter(models.ContractStatementAcceptance.statement_id == statement.id)
                  .order_by(models.ContractStatementAcceptance.id.asc()).all())
        out = {"FACTORY": None, "OEM": None}
        for r in rows:
            if canonical.acceptance_is_valid(r, statement) and r.party in out:
                out[r.party] = r
        out["agreed"] = out["FACTORY"] is not None and out["OEM"] is not None
        return out

    def _segments(db, contract, version, terms_canon, period):
        machines = (db.query(models.ServiceContractMachine)
                      .filter(models.ServiceContractMachine.term_version_id == version.id)
                      .order_by(models.ServiceContractMachine.installation_id.asc()).all())
        statement = (db.query(models.ContractStatement)
                       .filter(models.ContractStatement.contract_id == contract.id,
                               models.ContractStatement.period_start == period.start)
                       .first())
        disputes = []
        if statement is not None:
            disputes = (db.query(models.ContractDispute)
                          .filter(models.ContractDispute.statement_id == statement.id,
                                  models.ContractDispute.status != "withdrawn")
                          .order_by(models.ContractDispute.id.asc()).all())
        out = []
        for m in machines:
            end = period.end
            if m.coverage_ended_at is not None:
                end = min(end, m.coverage_ended_at)
            spans = (db.query(models.MachineTelemetrySpan)
                       .filter(models.MachineTelemetrySpan.tenant_code == contract.factory_tenant_code,
                               models.MachineTelemetrySpan.machine_id == m.machine_id_at_acceptance,
                               models.MachineTelemetrySpan.source.in_(terms_canon["trusted_sources"]),
                               models.MachineTelemetrySpan.span_start < end,
                               models.MachineTelemetrySpan.span_end
                               >= period.start - timedelta(seconds=SPAN_GAP_STAND_IN))
                       .order_by(models.MachineTelemetrySpan.span_start.asc(),
                                 models.MachineTelemetrySpan.id.asc()).all())
            cuts = {period.start, end, period.end}
            holds = []
            for i, s in enumerate(spans):
                nxt = next((t.span_start for t in spans[i + 1:] if t.source == s.source), None)
                h_end = s.span_end + timedelta(seconds=SPAN_GAP_STAND_IN)
                if nxt is not None:
                    h_end = min(h_end, nxt)
                holds.append((s, s.span_start, h_end))
                cuts.update((s.span_start, h_end))
            for d in disputes:
                if d.installation_id == m.installation_id:
                    cuts.update((d.window_start, d.window_end))
            points = sorted(c for c in cuts if period.start <= c <= period.end)
            intervals = []
            for a, b in zip(points, points[1:]):
                if a >= b:
                    continue
                if a >= end:
                    seg = ("UNMEASURED", "installation_unlinked", {"linkage": {
                        "coverage_ended_at": canonical.ts(m.coverage_ended_at),
                        "reason": m.coverage_end_reason}})
                else:
                    live = [(s, hs, he) for s, hs, he in holds if hs <= a and b <= he]
                    statuses = {s.status for s, _, _ in live}
                    ev = {"telemetry_spans": [{
                        "id": s.id, "source": s.source, "status": s.status,
                        "start": canonical.ts(max(s.span_start, period.start)),
                        "end": canonical.ts(min(s.span_end, period.end))} for s, _, _ in live]}
                    if not live:
                        seg = ("UNMEASURED", "no_telemetry", {})
                    elif len(statuses) > 1:
                        seg = ("DISPUTED", "conflicting_status", ev)
                    elif statuses & {"Running", "Idle"}:
                        seg = ("AVAILABLE", "status", ev)
                    else:
                        status = next(iter(statuses))
                        bucket = terms_canon["status_defaults"].get(status, "DISPUTED")
                        seg = (bucket, f"default:{status}", ev)
                for d in disputes:
                    if (d.installation_id == m.installation_id
                            and d.window_start <= a and b <= d.window_end):
                        dev = {"disputes": [{"id": d.id, "status": d.status,
                                             "resolution_bucket": d.resolution_bucket}]}
                        if d.status == "resolved":
                            seg = (d.resolution_bucket, f"agreed_override:#{d.id}", dev)
                        else:
                            seg = ("DISPUTED", f"open_dispute:#{d.id}", dev)
                if intervals and intervals[-1]["end_dt"] == a and (
                        intervals[-1]["bucket"], intervals[-1]["cause"],
                        intervals[-1]["evidence"]) == seg:
                    intervals[-1]["end_dt"] = b
                else:
                    intervals.append({"start_dt": a, "end_dt": b, "bucket": seg[0],
                                      "cause": seg[1], "evidence": seg[2]})
            out.append((m, intervals))
        return out

    def _content(db, contract, version, period):
        import contract_terms
        tc = contract_terms.terms_canonical(_terms(version))
        machines, totals = [], {b: 0 for b in BUCKETS}
        records = []
        for m, intervals in _segments(db, contract, version, tc, period):
            mt = {b: 0 for b in BUCKETS}
            rendered = []
            for iv in intervals:
                secs = int((iv["end_dt"] - iv["start_dt"]).total_seconds())
                mt[iv["bucket"]] += secs
                totals[iv["bucket"]] += secs
                rendered.append({"start": canonical.ts(iv["start_dt"]),
                                 "end": canonical.ts(iv["end_dt"]), "seconds": secs,
                                 "bucket": iv["bucket"], "cause": iv["cause"],
                                 "evidence": iv["evidence"]})
                records.append((m.installation_id, iv, secs))
            machines.append({"installation_id": m.installation_id,
                             "serial_number": m.serial_number,
                             "coverage_end": (canonical.ts(m.coverage_ended_at)
                                              if m.coverage_ended_at else None),
                             "totals": mt, "intervals": rendered})
        covered = sum(totals.values())
        u, f, o, d = (totals["UNMEASURED"], totals["FACTORY"], totals["OEM"],
                      totals["DISPUTED"])
        min_pct = int(tc["min_measured_pct"].replace(".", ""))
        if covered == 0:
            state = "not_evaluable"
        elif (covered - u) * 10000 < min_pct * covered:
            state = "not_evaluable"
        elif covered - u - f == 0:
            state = "not_evaluable"
        elif d > 0:
            state = "pending_disputes"
        else:
            base = covered - u - f
            target = int(tc["sla_target_pct"].replace(".", ""))
            state = "breached" if (base - o) * 10000 < target * base else "met"
        content = {
            "schema": "amp.downtime-attribution-statement/1",
            "contract": {"id": contract.id, "ref": contract.contract_ref,
                         "oem_code": contract.oem_code,
                         "factory_tenant_code": contract.factory_tenant_code,
                         "terms_version": version.version,
                         "terms_hash": version.terms_hash},
            "period": {"start": canonical.ts(period.start), "end": canonical.ts(period.end),
                       "timezone": tc["timezone"]},
            "machines": machines, "totals": totals,
            "sla": {"state": state, "target_pct": tc["sla_target_pct"]},
            "credit": {"currency": tc["currency"], "period_fee": tc["period_fee"],
                       "amount": None},
        }
        return content, records

    def _audit_both(db, contract, party, actor, statement, details):
        for tenant in (contract.factory_tenant_code,
                       oem_auth.sentinel_tenant(contract.oem_code)):
            platform_routes.log_audit(db, actor, "contract_statement_computed",
                                      "contract_statement", statement.id, details,
                                      tenant_code=tenant, commit=False)

    def _write_records(db, statement, records):
        (db.query(models.ContractAttributionRecord)
           .filter(models.ContractAttributionRecord.statement_id == statement.id)
           .delete(synchronize_session=False))
        for seq, (inst_id, iv, secs) in enumerate(records, start=1):
            db.add(models.ContractAttributionRecord(
                statement_id=statement.id, seq=seq, installation_id=inst_id,
                start_at=iv["start_dt"], end_at=iv["end_dt"], seconds=secs,
                bucket=iv["bucket"], cause=iv["cause"],
                evidence_json=canonical.canonical_bytes(iv["evidence"]).decode("utf-8")))

    def _period(db, contract, period_start):
        import contract_periods
        v1 = (db.query(models.ServiceContractTermVersion)
                .filter(models.ServiceContractTermVersion.contract_id == contract.id,
                        models.ServiceContractTermVersion.version == 1).first())
        if v1 is None:
            raise NoTermsForPeriod("no terms")
        p = contract_periods.period_starting(contract, _terms(v1), period_start)
        if p is None:
            raise NoTermsForPeriod("not a period of this contract")
        return p

    def compute_statement(db, contract, period_start, *, party, now):
        period = _period(db, contract, period_start)
        if now < period.end + timedelta(seconds=SETTLE_SECONDS_STAND_IN):
            raise PeriodNotClosed("period not closed")
        if (period.start - timedelta(seconds=SPAN_GAP_STAND_IN)
                < now - timedelta(days=SPAN_RETENTION_DAYS_STAND_IN)):
            raise EvidenceExpired("evidence expired")
        version = _version_for(db, contract, period.start)
        if version is None:
            raise NoTermsForPeriod("no accepted terms")
        statement = (db.query(models.ContractStatement)
                       .filter(models.ContractStatement.contract_id == contract.id,
                               models.ContractStatement.period_start == period.start)
                       .first())
        if statement is not None and acceptance_state(db, statement)["agreed"]:
            return ComputeResult(statement, False, True)
        content, records = _content(db, contract, version, period)
        blob = canonical.canonical_bytes(content)
        new_hash = canonical.sha256_hex(blob)
        actor = f"{party}"
        if statement is None:
            statement = models.ContractStatement(
                contract_id=contract.id, term_version_id=version.id,
                period_start=period.start, period_end=period.end, revision=1,
                content_hash=new_hash, canonical_json=blob.decode("utf-8"),
                computed_at=now, computed_by_party=party, computed_by=actor,
                updated_at=now)
            db.add(statement)
            db.flush()
            _write_records(db, statement, records)
            _audit_both(db, contract, party, actor, statement,
                        f"revision=1 hash={new_hash}")
            return ComputeResult(statement, True, False)
        if statement.content_hash == new_hash:
            return ComputeResult(statement, False, False)
        old_hash, old_rev = statement.content_hash, statement.revision
        rc = db.execute(
            update(models.ContractStatement)
            .where(models.ContractStatement.id == statement.id,
                   models.ContractStatement.content_hash == old_hash,
                   models.ContractStatement.revision == old_rev)
            .values(content_hash=new_hash, canonical_json=blob.decode("utf-8"),
                    revision=old_rev + 1, term_version_id=version.id,
                    computed_at=now, computed_by_party=party, computed_by=actor,
                    updated_at=now)
            .execution_options(synchronize_session=False)).rowcount
        if rc != 1:
            raise StatementConflict("statement changed concurrently")
        db.refresh(statement)
        _write_records(db, statement, records)
        _audit_both(db, contract, party, actor, statement,
                    f"revision={old_rev}->{old_rev + 1} hash={old_hash}->{new_hash}")
        return ComputeResult(statement, True, False)

    def preview_statement(db, contract, now):
        import contract_periods
        v1 = (db.query(models.ServiceContractTermVersion)
                .filter(models.ServiceContractTermVersion.contract_id == contract.id,
                        models.ServiceContractTermVersion.version == 1).first())
        end = contract.ends_at
        if contract.termination_effective_at is not None:
            end = min(end, contract.termination_effective_at)
        for p in contract_periods.periods(_terms(v1), contract.starts_at, end):
            if p.start <= now < p.end:
                version = _version_for(db, contract, p.start)
                if version is None:
                    return None
                return {"preview": True, "content": _content(db, contract, version, p)[0]}
        return None

    def statement_payload(db, statement):
        state = acceptance_state(db, statement)
        accs = (db.query(models.ContractStatementAcceptance)
                  .filter(models.ContractStatementAcceptance.statement_id == statement.id)
                  .order_by(models.ContractStatementAcceptance.id.asc()).all())
        return {
            "id": statement.id, "contract_id": statement.contract_id,
            "period_start": canonical.ts(statement.period_start),
            "period_end": canonical.ts(statement.period_end),
            "revision": statement.revision, "content_hash": statement.content_hash,
            "content": (json.loads(statement.canonical_json)
                        if statement.canonical_json is not None else None),
            "acceptances": [{"party": a.party, "actor": a.actor,
                             "accepted_at": canonical.ts(a.accepted_at),
                             "content_hash": a.content_hash, "revision": a.revision,
                             "valid": canonical.acceptance_is_valid(a, statement)}
                            for a in accs],
            "agreed": state["agreed"],
        }

    def verify_statement(db, statement, now):
        blob_hash = (canonical.sha256_hex(statement.canonical_json.encode("utf-8"))
                     if statement.canonical_json is not None else None)
        return {"stored_hash": statement.content_hash, "blob_hash": blob_hash,
                "consistency": ("content_purged" if blob_hash is None else
                                "consistent" if blob_hash == statement.content_hash
                                else "blob_mismatch"),
                "agreed": acceptance_state(db, statement)["agreed"]}

    for name, value in list(locals().items()):
        if not name.startswith("_") and name != "mod":
            setattr(mod, name, value)
    return mod


def _install_module(name, factory):
    if _module_missing(name):
        sys.modules[name] = factory()
        STAND_INS.append(f"module {name} (not present; plan-signature stand-in)")
    else:
        importlib.import_module(name)


_ORIGINALS = {}


def install():
    """Provide stand-ins for whatever has not landed. Idempotent."""
    import oem_auth
    import oem_sharing
    import platform_routes

    if STAND_INS:
        return
    _install_module("contract_terms", _stand_in_contract_terms)
    _install_module("contract_periods", _stand_in_contract_periods)
    _install_module("contract_statements", _stand_in_contract_statements)

    if not any("read_contracts" in caps for caps in oem_auth.ROLE_CAPABILITIES.values()):
        _ORIGINALS["caps"] = {k: set(v) for k, v in oem_auth.ROLE_CAPABILITIES.items()}
        for role in oem_auth.OEM_ROLES:
            oem_auth.ROLE_CAPABILITIES[role].add("read_contracts")
        for role in (oem_auth.OEM_ADMIN, oem_auth.OEM_SERVICE_MANAGER):
            oem_auth.ROLE_CAPABILITIES[role].add("manage_contracts")
        oem_auth.ROLE_CAPABILITIES[oem_auth.OEM_ADMIN].add("sign_contracts")
        STAND_INS.append("oem_auth contract capabilities (plan section 5 table)")

    if "commit" not in inspect.signature(platform_routes.log_audit).parameters:
        original = platform_routes.log_audit
        _ORIGINALS["log_audit"] = original

        def log_audit(db, actor, action, entity_type=None, entity_id=None,
                      details=None, *, tenant_code=None, commit=True):
            if not commit:
                # C6: no try/except, no rollback: a failure raises into the
                # caller's transaction.
                db.add(models.AuditLog(actor=actor or "system", action=action,
                                       entity_type=entity_type, entity_id=entity_id,
                                       details=details, tenant_code=tenant_code))
                return
            if tenant_code is None:
                return original(db, actor, action, entity_type, entity_id, details)
            try:
                db.add(models.AuditLog(actor=actor or "system", action=action,
                                       entity_type=entity_type, entity_id=entity_id,
                                       details=details, tenant_code=tenant_code))
                db.commit()
            except Exception:
                db.rollback()

        platform_routes.log_audit = log_audit
        STAND_INS.append("platform_routes.log_audit(tenant_code=, commit=)")

    if not hasattr(oem_sharing, "contract_statement_visible"):
        def contract_statement_visible(db, contract):
            return oem_sharing.SHARE_DOWNTIME in oem_sharing.grants_for(
                db, contract.oem_code, contract.factory_tenant_code)
        oem_sharing.contract_statement_visible = contract_statement_visible
        STAND_INS.append("oem_sharing.contract_statement_visible")

    if not hasattr(oem_sharing, "bound_factory_read"):
        @contextlib.contextmanager
        def bound_factory_read(tenant_code):
            token = tenancy.set_current_tenant(tenant_code)
            try:
                yield
            finally:
                tenancy.reset_current_tenant(token)
        oem_sharing.bound_factory_read = bound_factory_read
        STAND_INS.append("oem_sharing.bound_factory_read")

    if not hasattr(oem_sharing, "widen_grants"):
        def widen_grants(db, oem_code, tenant_code, grants, actor, *, context):
            policy = (db.query(models.OemDataSharingPolicy)
                        .filter(models.OemDataSharingPolicy.oem_code == oem_code,
                                models.OemDataSharingPolicy.tenant_code == tenant_code)
                        .first())
            before = policy.grants if policy else "(no policy)"
            existing = oem_sharing.parse_grants(policy.grants) if policy else set()
            if policy is None:
                policy = models.OemDataSharingPolicy(oem_code=oem_code,
                                                     tenant_code=tenant_code)
                db.add(policy)
            policy.grants = ",".join(sorted(existing | set(grants)))
            policy.updated_by = actor
            db.flush()
            platform_routes.log_audit(
                db, actor, "oem_sharing_changed", "oem_data_sharing_policy", policy.id,
                f"oem={oem_code} before={before!r} after={policy.grants!r} ({context})",
                tenant_code=tenant_code, commit=False)
            return policy
        oem_sharing.widen_grants = widen_grants
        STAND_INS.append("oem_sharing.widen_grants")


def uninstall():
    """Undo the helper stand-ins (module stand-ins stay in sys.modules)."""
    import oem_auth
    import oem_sharing
    import platform_routes

    if "caps" in _ORIGINALS:
        oem_auth.ROLE_CAPABILITIES.clear()
        oem_auth.ROLE_CAPABILITIES.update(_ORIGINALS.pop("caps"))
    if "log_audit" in _ORIGINALS:
        platform_routes.log_audit = _ORIGINALS.pop("log_audit")
    for name in ("contract_statement_visible", "bound_factory_read", "widen_grants"):
        if any(name in s for s in STAND_INS) and hasattr(oem_sharing, name):
            delattr(oem_sharing, name)
    STAND_INS[:] = [s for s in STAND_INS if s.startswith("module ")]


def stand_in_banner():
    if not STAND_INS:
        print("RAN AGAINST THE REAL ENGINE AND REAL SHARED HELPERS (no stand-ins)")
        return
    print("RAN WITH STAND-INS for code that has not landed (plan signatures):")
    for s in STAND_INS:
        print("   -", s)


# ── Application wiring ────────────────────────────────────────────────

main = None


def boot():
    """Import the app and point every route module at the test database."""
    global main
    install()
    import main as _main
    main = _main
    database.SessionLocal = SessionLocal
    main.SessionLocal = SessionLocal
    for mod in ("oem_routes", "connected_equipment_routes", "oem_admin_routes",
                "machines_routes", "platform_routes", "oem_contract_routes",
                "service_contract_routes"):
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
