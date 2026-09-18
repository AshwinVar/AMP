"""Downtime attribution statements: compute, persist, accept state, verify (ADR-0021).

WHAT THIS PINS
--------------
contract_statements turns the attribution engine's segments into one statement
per contract period, against a real database:

  * A statement exists only for a whole, CLOSED (period_end + SETTLE_SECONDS)
    period of a contract the FACTORY has accepted, under the accepted terms
    version in force at the period start.
  * Recompute is idempotent (same hash: no revision, no audit). A changed hash
    is a conditional UPDATE on the old (hash, revision): revision + 1, records
    replaced, two audit rows (factory tenant + OEM sentinel). The loser of a
    race gets StatementConflict and writes nothing.
  * Acceptance counts only for the current hash AND revision (canonical's rule):
    raise a dispute and withdraw it, the bytes return, the old acceptance does
    not (C1). An agreed statement is frozen; verify reports live drift.
  * Past span retention, compute refuses (EvidenceExpired) and verify says so.
  * Every engine query filters the factory tenant explicitly and bounds time at
    both ends (captured SQL, plus a second factory's rows on the same machine
    id, plus rows just outside each bound, which the engine would refuse).
  * A request bound to another tenant (an OEM sentinel) is refused: the
    ADR-0002 filter would otherwise hide the evidence and every second would
    read as "no data".
  * The status history is the spans, never Machine.status or MachineEvent (C2).
  * Nothing from the factory's MES beyond the evidence allowlist reaches the
    statement.
  * verify distinguishes consistent, blob_mismatch, records_diverged and
    content_purged.

SHARED PIECES. SPAN_GAP_SECONDS and SETTLE_SECONDS belong to telemetry_coverage,
the span retention days to retention.py's policy table, and
log_audit(tenant_code=, commit=False) to platform_routes. The suite uses the
real ones, never a stand-in, and refuses to start if one is missing.

POSTGRESQL. With DATABASE_URL=postgresql://... the suite runs on a disposable
scratch database (pg_scratch), including the concurrent-update race.

Run: DATABASE_URL="sqlite:///./ci.db" python test_contract_statements.py
"""
import ast
import inspect
import io
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import close_all_sessions, sessionmaker  # noqa: E402



def _require_shared_pieces():
    """The real shared pieces or nothing: a stand-in here once leaked into every
    later suite of a one-process pytest run (a log_audit that refused commit=True)."""
    import models
    import platform_routes
    import retention
    import telemetry_coverage
    for name in ("SPAN_GAP_SECONDS", "SETTLE_SECONDS"):
        assert hasattr(telemetry_coverage, name), f"telemetry_coverage.{name} is missing"
    assert sum(p.model is models.MachineTelemetrySpan for p in retention.POLICIES) == 1, \
        "retention.py has no single policy for machine_telemetry_spans"
    params = inspect.signature(platform_routes.log_audit).parameters
    assert "commit" in params and "tenant_code" in params, \
        "platform_routes.log_audit(tenant_code=, commit=) is missing"


_require_shared_pieces()

import canonical  # noqa: E402
import contract_periods as cp  # noqa: E402
import contract_statements as cs  # noqa: E402
import contract_terms as ct  # noqa: E402
import models  # noqa: E402
import oem_auth  # noqa: E402
import tenancy  # noqa: E402
from contract_statements import (ContractIntegrityError, EvidenceExpired,  # noqa: E402
                                 NoTermsForPeriod, PeriodNotClosed, StatementConflict)
from database import Base  # noqa: E402

tenancy.install_scoping()

URL = os.environ.get("DATABASE_URL", "")
ON_PG = URL.startswith("postgresql")
TMP = None
if ON_PG:
    import pg_scratch
    SCRATCH = "amp_scratch_contract_statements"
    PG_VERSION = pg_scratch.ensure(db=SCRATCH)
    engine = create_engine(pg_scratch.scratch_url(db=SCRATCH))
else:
    TMP = tempfile.mkdtemp(prefix="amp_contract_statements_")
    # A FILE, not :memory:, so two sessions are two connections (the race test).
    engine = create_engine(f"sqlite:///{TMP}/statements.db")

    @event.listens_for(engine, "connect")
    def _fast_disposable_file(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA synchronous=OFF")
        cursor.execute("PRAGMA journal_mode=MEMORY")
        cursor.close()
Session = sessionmaker(bind=engine, autoflush=False)

OEM_CODE = "OEM_A"
FAC = "FAC_A"
FAC_B = "FAC_B"
SENTINEL = oem_auth.sentinel_tenant(OEM_CODE)
FACTORY = SimpleNamespace(side="FACTORY", actor="fac_admin")
OEM = SimpleNamespace(side="OEM", actor="oem:OEM_A:admin")
GAP = cs.span_gap_seconds()
SETTLE = cs.settle_seconds()


def fresh():
    # A session left open by the previous test holds locks PostgreSQL's DROP
    # waits on for ever.
    close_all_sessions()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return Session()


def terms_doc(installations, **over):
    doc = {
        "schema": 1, "currency": "INR", "period_months": 1, "period_fee": "40000.00",
        "timezone": "Asia/Kolkata", "term_months": 12, "coverage": {"mode": "24x7"},
        "sla_target_pct": "97.00",
        "credit_tiers": [{"below_pct": "97.00", "credit_pct": "5.00"},
                         {"below_pct": "95.00", "credit_pct": "10.00"}],
        "min_measured_pct": "90.00", "trusted_sources": ["mqtt"],
        "status_defaults": {"Breakdown": "OEM", "Maintenance": "FACTORY",
                            "Offline": "DISPUTED"},
        "reason_map": {"no material": "FACTORY", "motor overheating": "OEM"},
        "generic_reasons": ["breakdown", "unknown"], "reason_lead_seconds": 600,
        "termination_notice_days": 30,
        "covered_installations": [{"installation_id": i, "serial_number": s}
                                  for i, s in installations],
    }
    doc.update(over)
    return doc


def add_version(db, contract, terms, number, effective_from, status="accepted"):
    h = ct.terms_hash(terms)
    v = models.ServiceContractTermVersion(
        contract_id=contract.id, version=number, terms_json=ct.terms_json_text(terms),
        terms_hash=h, effective_from=effective_from, status=status,
        proposed_by_party="OEM", proposed_by=OEM.actor, proposed_at=effective_from,
        oem_accepted_by=OEM.actor, oem_accepted_at=effective_from, oem_accepted_hash=h,
        factory_accepted_by=FACTORY.actor, factory_accepted_at=effective_from,
        factory_accepted_hash=h)
    db.add(v)
    db.flush()
    for c in terms.covered_installations:
        inst = db.get(models.MachineInstallation, c.installation_id)
        db.add(models.ServiceContractMachine(
            term_version_id=v.id, installation_id=c.installation_id,
            machine_id_at_acceptance=inst.machine_id,
            factory_tenant_at_acceptance=inst.factory_tenant_code,
            serial_number=c.serial_number))
    db.flush()
    return v


class World:
    pass


def world(db, **over):
    w = World()
    w.db = db
    w.m1 = models.Machine(tenant_code=FAC, name="NEEDLE_MACHINE_ONE", status="Running",
                          utilization=64, line="NEEDLE_LINE", site="")
    w.m2 = models.Machine(tenant_code=FAC, name="Press 2", status="Running", site="")
    w.mb = models.Machine(tenant_code=FAC_B, name="Other factory press", status="Running",
                          site="")
    db.add_all([w.m1, w.m2, w.mb])
    db.flush()
    model = models.MachineModel(oem_code=OEM_CODE, family="", model_code="AER-90",
                                name="Aeron 90")
    db.add(model)
    db.flush()
    w.i1 = models.MachineInstallation(oem_code=OEM_CODE, serial_number="AER-0001",
                                      model_id=model.id, factory_tenant_code=FAC,
                                      machine_id=w.m1.id)
    w.i2 = models.MachineInstallation(oem_code=OEM_CODE, serial_number="AER-0002",
                                      model_id=model.id, factory_tenant_code=FAC,
                                      machine_id=w.m2.id)
    db.add_all([w.i1, w.i2])
    db.flush()
    w.doc = terms_doc([(w.i1.id, "AER-0001"), (w.i2.id, "AER-0002")], **over)
    w.terms = ct.parse(w.doc)
    starts = cp.month_start_utc(w.terms, 2026, 1)
    w.contract = models.ServiceContract(
        oem_code=OEM_CODE, factory_tenant_code=FAC, contract_ref="AMC-2026-001",
        title="Compressor AMC", contract_type="AMC", status="accepted", starts_at=starts,
        ends_at=cp.month_start_utc(w.terms, 2027, 1), created_by=OEM.actor,
        created_at=starts - timedelta(days=20), proposed_at=starts - timedelta(days=15),
        factory_accepted_by=FACTORY.actor, factory_accepted_at=starts - timedelta(days=10))
    db.add(w.contract)
    db.flush()
    w.v1 = add_version(db, w.contract, w.terms, 1, starts)
    w.P = cp.period_starting(w.contract, w.terms, cp.month_start_utc(w.terms, 2026, 3))
    w.APRIL = cp.period_starting(w.contract, w.terms, cp.month_start_utc(w.terms, 2026, 4))
    w.SEC = (w.P.end - w.P.start) // timedelta(seconds=1)
    w.NOW = w.P.end + timedelta(days=1)
    db.commit()
    return w


def at(w, offset):
    return w.P.start + timedelta(seconds=offset)


def add_span(w, machine, status, start, end, source="mqtt", tenant=FAC, base=None):
    base = base or w.P.start
    row = models.MachineTelemetrySpan(
        tenant_code=tenant, machine_id=machine.id, source=source, status=status,
        span_start=base + timedelta(seconds=start), span_end=base + timedelta(seconds=end),
        message_count=1)
    w.db.add(row)
    w.db.flush()
    return row


def add_log(w, machine, offset, reason, tenant=FAC, notes=None, duration="30 min"):
    row = models.DowntimeLog(tenant_code=tenant, machine_id=machine.id, reason=reason,
                             duration=duration, notes=notes,
                             created_at=at(w, offset) + timedelta(microseconds=250000))
    w.db.add(row)
    w.db.flush()
    return row


def running_all_month(w):
    add_span(w, w.m1, "Running", -100, w.SEC + 100)
    add_span(w, w.m2, "Running", -100, w.SEC + 100)
    w.db.commit()


def compute(w, now=None, party=FACTORY, period=None):
    result = cs.compute_statement(w.db, w.contract, (period or w.P).start, party=party,
                                  now=now or w.NOW)
    w.db.commit()
    return result


def content(statement):
    return json.loads(statement.canonical_json)


def intervals(statement, installation_id):
    for m in content(statement)["machines"]:
        if m["installation_id"] == installation_id:
            return [(canonical_offset(i["start"], statement), canonical_offset(i["end"], statement),
                     i["bucket"], i["cause"]) for i in m["intervals"]]
    raise AssertionError(f"no machine {installation_id}")


def canonical_offset(text, statement):
    return (datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ") - statement.period_start) \
        // timedelta(seconds=1)


def accept(w, statement, party):
    row = models.ContractStatementAcceptance(
        statement_id=statement.id, party=party.side, actor=party.actor, accepted_at=w.NOW,
        content_hash=statement.content_hash, revision=statement.revision)
    w.db.add(row)
    w.db.commit()
    return row


def add_dispute(w, statement, installation, start, end, status="open", bucket=None):
    row = models.ContractDispute(
        contract_id=w.contract.id, statement_id=statement.id, installation_id=installation.id,
        window_start=at(w, start), window_end=at(w, end), raised_by_party="FACTORY",
        raised_by=FACTORY.actor, raised_at=w.NOW, reason="the machine was waiting for material",
        proposed_bucket="FACTORY", status=status, resolution_bucket=bucket)
    w.db.add(row)
    w.db.commit()
    return row


def audit_rows(w, action="contract_statement_computed"):
    return (w.db.query(models.AuditLog).filter(models.AuditLog.action == action)
            .order_by(models.AuditLog.id.asc()).all())


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc as e:
        return str(e) or exc.__name__
    return None


# --------------------------------------------------------------------------
# 1. compute, persist, audit
# --------------------------------------------------------------------------

def test_compute_persists_revision_one_with_records_and_two_audit_rows():
    w = world(fresh())
    running_all_month(w)
    r = compute(w, party=OEM)
    st = r.statement
    assert (r.changed, r.frozen, st.revision) == (True, False, 1), r
    assert canonical.sha256_hex(st.canonical_json.encode("utf-8")) == st.content_hash
    assert canonical.canonical_bytes(content(st)) == st.canonical_json.encode("utf-8")
    c = content(st)
    assert c["schema"] == "amp.downtime-attribution-statement/1", c["schema"]
    assert c["contract"] == {"id": w.contract.id, "ref": "AMC-2026-001", "oem_code": OEM_CODE,
                             "factory_tenant_code": FAC, "terms_version": 1,
                             "terms_hash": w.v1.terms_hash}, c["contract"]
    assert c["period"] == {"start": canonical.ts(w.P.start), "end": canonical.ts(w.P.end),
                           "timezone": "Asia/Kolkata"}, c["period"]
    assert [m["serial_number"] for m in c["machines"]] == ["AER-0001", "AER-0002"]
    assert c["totals"]["covered_seconds"] == c["totals"]["available_seconds"] == 2 * w.SEC
    assert c["sla"]["state"] == "met" and c["credit"]["amount"] == "0.00", (c["sla"], c["credit"])
    assert (st.computed_by_party, st.computed_by, st.computed_at) == ("OEM", OEM.actor, w.NOW)
    records = (w.db.query(models.ContractAttributionRecord)
               .filter_by(statement_id=st.id).order_by(models.ContractAttributionRecord.seq).all())
    flat = [(m["installation_id"], i) for m in c["machines"] for i in m["intervals"]]
    assert [r_.seq for r_ in records] == list(range(1, len(flat) + 1))
    for rec, (inst, iv) in zip(records, flat):
        assert (rec.installation_id, canonical.ts(rec.start_at), canonical.ts(rec.end_at),
                rec.seconds, rec.bucket, rec.cause, json.loads(rec.evidence_json)) == \
            (inst, iv["start"], iv["end"], iv["seconds"], iv["bucket"], iv["cause"],
             iv["evidence"]), (rec, iv)
    rows = audit_rows(w)
    assert sorted(x.tenant_code for x in rows) == sorted([FAC, SENTINEL]), rows
    for x in rows:
        assert (x.actor, x.entity_type, x.entity_id) == (OEM.actor, "contract_statement", st.id)
        assert st.content_hash in x.details and "revision" in x.details, x.details
        for leak in ("AVAILABLE", "seconds", "Running", str(w.SEC)):
            assert leak not in x.details, (leak, x.details)
    v = cs.verify_statement(w.db, st, w.NOW)
    assert v["consistency"] == "consistent" and v["live"]["matches"] is True, v
    print("PASS compute writes revision 1: canonical bytes hash to content_hash, records "
          "mirror the intervals, one audit row per party with hashes and no figures")


def test_recompute_is_idempotent():
    w = world(fresh())
    running_all_month(w)
    first = compute(w).statement
    record_ids = [r.id for r in w.db.query(models.ContractAttributionRecord).all()]
    audits = len(audit_rows(w))
    again = compute(w, now=w.NOW + timedelta(days=3), party=OEM)
    assert (again.changed, again.frozen) == (False, False), again
    assert (again.statement.revision, again.statement.content_hash, again.statement.computed_by) \
        == (1, first.content_hash, FACTORY.actor)
    assert len(audit_rows(w)) == audits
    assert [r.id for r in w.db.query(models.ContractAttributionRecord).all()] == record_ids
    print("PASS recomputing the same evidence changes nothing: same hash and revision, no "
          "audit row, records untouched")


def test_new_evidence_bumps_the_revision_and_cancels_acceptances():
    w = world(fresh())
    running_all_month(w)
    st = compute(w).statement
    h1 = st.content_hash
    accept(w, st, FACTORY)
    assert cs.acceptance_state(w.db, st)["FACTORY"] is not None
    w.db.query(models.MachineTelemetrySpan).filter_by(machine_id=w.m2.id).delete()
    add_span(w, w.m2, "Running", -100, 50000)
    add_span(w, w.m2, "Breakdown", 50000, w.SEC + 100)
    w.db.commit()
    audits = len(audit_rows(w))
    r = compute(w, party=OEM)
    assert (r.changed, r.statement.revision) == (True, 2) and r.statement.content_hash != h1, r
    state = cs.acceptance_state(w.db, r.statement)
    assert state == {"FACTORY": None, "OEM": None, "agreed": False}, state
    payload = cs.statement_payload(w.db, r.statement)
    assert [a["valid"] for a in payload["acceptances"]] == [False], payload["acceptances"]
    rows = audit_rows(w)[audits:]
    assert len(rows) == 2 and all(h1 in x.details and r.statement.content_hash in x.details
                                  and "2" in x.details for x in rows), rows
    assert intervals(r.statement, w.i2.id)[-1] == (50000, w.SEC, "OEM",
                                                   "status_default:Breakdown")
    print("PASS a new span changes the hash: revision 2, records replaced, the earlier "
          "acceptance no longer counts, audited with old and new hash")


def test_a_withdrawn_dispute_restores_the_bytes_but_not_the_acceptance():
    w = world(fresh())
    running_all_month(w)
    st = compute(w).statement
    h1 = st.content_hash
    old = accept(w, st, FACTORY)
    d = add_dispute(w, st, w.i1, 1000, 2000)
    r2 = compute(w)
    assert (r2.statement.revision, r2.statement.content_hash != h1) == (2, True), r2
    assert (1000, 2000, "DISPUTED", f"open_dispute:#{d.id}") in intervals(r2.statement, w.i1.id)
    d.status, d.closed_at = "withdrawn", w.NOW
    w.db.commit()
    r3 = compute(w)
    assert (r3.statement.revision, r3.statement.content_hash) == (3, h1), r3
    state = cs.acceptance_state(w.db, r3.statement)
    assert state["FACTORY"] is None and not canonical.acceptance_is_valid(old, r3.statement), state
    new = accept(w, r3.statement, FACTORY)
    state = cs.acceptance_state(w.db, r3.statement)
    assert state["FACTORY"] is not None and state["FACTORY"].id == new.id, state
    print("PASS C1: accept, dispute, withdraw -> the bytes hash back to revision 1's at "
          "revision 3; the old acceptance stays invalid; re-accepting revision 3 counts")


def test_an_agreed_statement_is_frozen_and_verify_reports_drift():
    w = world(fresh())
    running_all_month(w)
    st = compute(w).statement
    accept(w, st, FACTORY)
    accept(w, st, OEM)
    assert cs.acceptance_state(w.db, st)["agreed"] is True
    add_span(w, w.m1, "Breakdown", 7000, 9000, source="mqtt")
    w.db.commit()
    audits = len(audit_rows(w))
    r = compute(w)
    assert (r.changed, r.frozen, r.statement.revision) == (False, True, 1), r
    assert len(audit_rows(w)) == audits
    v = cs.verify_statement(w.db, r.statement, w.NOW)
    assert v["consistency"] == "consistent" and v["agreed"] is True, v
    assert v["live"]["matches"] is False and v["live"]["hash"] != st.content_hash, v["live"]
    assert [a["valid"] for a in v["acceptances"]] == [True, True], v["acceptances"]
    print("PASS an agreed statement is frozen (no write, no audit); verify's live recompute "
          "reports the drift")


# --------------------------------------------------------------------------
# 2. when a statement may exist
# --------------------------------------------------------------------------

def test_a_purged_statement_is_never_recomputed():
    w = world(fresh())
    running_all_month(w)
    st = compute(w).statement
    h1 = st.content_hash
    accept(w, st, FACTORY)
    # Offboarding: bytes and records removed, hash and acceptances kept, the
    # factory's spans purged with the tenant.
    st.canonical_json = None
    w.db.query(models.ContractAttributionRecord).delete()
    w.db.query(models.MachineTelemetrySpan).delete()
    w.db.commit()
    audits = len(audit_rows(w))
    assert raises(cs.ContentPurged, compute, w)
    assert issubclass(cs.ContentPurged, EvidenceExpired)
    w.db.rollback()
    w.db.expire_all()
    st = w.db.query(models.ContractStatement).one()
    assert (st.revision, st.content_hash, st.canonical_json) == (1, h1, None), st
    assert len(audit_rows(w)) == audits
    v = cs.verify_statement(w.db, st, w.NOW)
    assert v["consistency"] == "content_purged" and v["live"]["reason"] == "content_purged", v
    assert [a["valid"] for a in v["acceptances"]] == [True], v["acceptances"]
    print("PASS a statement whose content offboarding removed is never recomputed "
          "(ContentPurged, an EvidenceExpired): the kept hash and acceptance stand")


def test_a_period_is_closed_only_after_the_settle_time():
    w = world(fresh())
    running_all_month(w)
    early = w.P.end + timedelta(seconds=SETTLE - 1)
    assert raises(PeriodNotClosed, compute, w, now=early)
    assert w.db.query(models.ContractStatement).count() == 0
    assert raises(PeriodNotClosed, compute, w, now=w.P.start + timedelta(days=3))
    assert compute(w, now=w.P.end + timedelta(seconds=SETTLE)).changed is True
    print(f"PASS compute is refused until period_end + SETTLE_SECONDS ({SETTLE}s), and "
          "allowed from that instant")


def test_evidence_past_span_retention_is_refused_and_verify_says_so():
    w = world(fresh())
    running_all_month(w)
    days = cs.span_retention_days()
    assert type(days) is int and days > 0, days
    limit = w.P.start - timedelta(seconds=GAP) + timedelta(days=days)
    st = compute(w).statement
    assert compute(w, now=limit).changed is False
    assert raises(EvidenceExpired, compute, w, now=limit + timedelta(seconds=1))
    v = cs.verify_statement(w.db, st, limit + timedelta(seconds=1))
    assert v["live"] == {"hash": None, "matches": None, "reason": "evidence_expired"}, v["live"]
    assert v["consistency"] == "consistent", v
    accept(w, st, FACTORY)
    accept(w, st, OEM)
    r = compute(w, now=limit + timedelta(days=30))
    assert r.frozen is True, r
    print(f"PASS past {days} days of span retention compute raises EvidenceExpired (not at "
          "the limit itself); verify reports live evidence_expired; an agreed one is frozen")


def test_no_statement_without_factory_acceptance_or_outside_the_grid():
    w = world(fresh())
    running_all_month(w)
    w.contract.status = "proposed"
    w.db.commit()
    assert raises(NoTermsForPeriod, compute, w)
    w.contract.status, w.contract.factory_accepted_at = "accepted", None
    w.db.commit()
    assert raises(NoTermsForPeriod, compute, w)
    w.contract.factory_accepted_at = w.contract.starts_at - timedelta(days=10)
    w.db.commit()
    for bad in (w.P.start + timedelta(hours=1), w.P.start.replace(tzinfo=timezone.utc),
                w.contract.starts_at - timedelta(days=31), "2026-03-01"):
        assert raises(NoTermsForPeriod, cs.compute_statement, w.db, w.contract, bad,
                      party=FACTORY, now=w.NOW), bad
    w.contract.termination_effective_at = w.APRIL.start
    w.db.commit()
    assert raises(NoTermsForPeriod, compute, w, period=w.APRIL,
                  now=w.APRIL.end + timedelta(days=1))
    assert compute(w).changed is True
    assert w.db.query(models.ContractStatement).count() == 1
    print("PASS no statement before the factory accepts, for an instant that does not start "
          "a period, for an aware or non-datetime start, or after termination")


def test_the_terms_in_force_at_the_period_start_apply():
    w = world(fresh())
    add_span(w, w.m1, "Breakdown", -100, w.SEC + 100)
    add_span(w, w.m2, "Running", -100, w.SEC + 100)
    add_span(w, w.m1, "Breakdown", -100, 20 * 86400, base=w.APRIL.start)
    add_span(w, w.m2, "Running", -100, 20 * 86400, base=w.APRIL.start)
    doc2 = dict(w.doc, status_defaults={"Breakdown": "FACTORY", "Maintenance": "FACTORY",
                                        "Offline": "DISPUTED"})
    add_version(w.db, w.contract, ct.parse(doc2), 2, w.APRIL.start)
    doc3 = dict(w.doc, status_defaults={"Breakdown": "DISPUTED", "Maintenance": "FACTORY",
                                        "Offline": "DISPUTED"})
    add_version(w.db, w.contract, ct.parse(doc3), 3, w.P.start, status="proposed")
    w.db.commit()
    march = compute(w).statement
    april = compute(w, period=w.APRIL, now=w.APRIL.end + timedelta(days=1)).statement
    assert content(march)["contract"]["terms_version"] == 1
    assert intervals(march, w.i1.id)[0][2:] == ("OEM", "status_default:Breakdown")
    assert content(april)["contract"]["terms_version"] == 2
    assert intervals(april, w.i1.id)[0][2:] == ("FACTORY", "status_default:Breakdown")
    assert april.term_version_id != march.term_version_id
    print("PASS each period uses the highest ACCEPTED version effective at its start; a "
          "proposed amendment changes nothing")


def test_stored_data_that_contradicts_itself_is_refused():
    def fails(mutate):
        w = world(fresh())
        running_all_month(w)
        mutate(w)
        w.db.commit()
        return raises(ContractIntegrityError, compute, w)

    assert fails(lambda w: setattr(w.v1, "terms_hash", "f" * 64))
    assert fails(lambda w: setattr(w.v1, "factory_accepted_hash", "e" * 64))
    assert fails(lambda w: setattr(w.v1, "oem_accepted_hash", None))
    assert fails(lambda w: w.db.query(models.ServiceContractMachine)
                 .filter_by(installation_id=w.i2.id).delete())
    assert fails(lambda w: w.db.query(models.ServiceContractMachine)
                 .filter_by(installation_id=w.i1.id)
                 .update({"factory_tenant_at_acceptance": FAC_B}))
    assert fails(lambda w: w.db.query(models.ServiceContractMachine)
                 .filter_by(installation_id=w.i1.id).update({"serial_number": "AER-9999"}))

    def regrid(w):
        doc = dict(w.doc, period_months=3)
        add_version(w.db, w.contract, ct.parse(doc), 2, w.P.start)
    assert fails(regrid)
    print("PASS a terms hash, an acceptance hash, the covered machines or the period grid "
          "that disagree with the accepted terms are refused as integrity errors")


# --------------------------------------------------------------------------
# 3. what the engine reads
# --------------------------------------------------------------------------

def test_a_read_bound_to_another_tenant_is_refused():
    w = world(fresh())
    running_all_month(w)
    for bound in (SENTINEL, FAC_B):
        token = tenancy.set_current_tenant(bound)
        try:
            assert raises(ContractIntegrityError, compute, w), bound
        finally:
            tenancy.reset_current_tenant(token)
        w.db.rollback()
    token = tenancy.set_current_tenant(FAC)
    try:
        st = compute(w).statement
    finally:
        tenancy.reset_current_tenant(token)
    assert content(st)["totals"]["available_seconds"] == 2 * w.SEC
    print("PASS a compute bound to the OEM sentinel or another factory is refused (the "
          "ADR-0002 filter would read no evidence); bound to the factory it reads it")


_BOUND_PATTERNS = {
    "machine_telemetry_spans": (r"machine_telemetry_spans\.tenant_code = ",
                                r"machine_telemetry_spans\.machine_id = ",
                                r"machine_telemetry_spans\.source IN ",
                                r"machine_telemetry_spans\.span_start < ",
                                r"machine_telemetry_spans\.span_end >= "),
    "downtime_logs": (r"downtime_logs\.tenant_code = ", r"downtime_logs\.machine_id = ",
                      r"downtime_logs\.created_at >= ", r"downtime_logs\.created_at < "),
    "contract_disputes": (r"contract_disputes\.contract_id = ",
                          r"contract_disputes\.statement_id = ",
                          r"contract_disputes\.window_start < ",
                          r"contract_disputes\.window_end > "),
}


def _values(params):
    out = []
    items = params.values() if isinstance(params, dict) else (params or ())
    for v in items:
        if isinstance(v, (list, tuple)):
            out.extend(v)
        elif isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", v):
            out.append(datetime.strptime(v[:19], "%Y-%m-%d %H:%M:%S"))
        else:
            out.append(v)
    return out


def test_every_engine_read_is_tenant_filtered_and_bounded_at_both_ends():
    w = world(fresh())
    # The evidence: Running, a Breakdown episode [50000, 60000) explained by the
    # factory's "no material" 500s before it, Running again.
    add_span(w, w.m1, "Running", -100, 49990)
    add_span(w, w.m1, "Breakdown", 50000, 59700)
    add_span(w, w.m1, "Running", 60000, w.SEC + 100)
    add_span(w, w.m2, "Running", -100, w.SEC + 100)
    add_log(w, w.m1, 49500, "No Material", notes="NEEDLE_NOTE", duration="NEEDLE_DURATION")
    # Rows each bound must exclude; the engine refuses any of them if read.
    add_span(w, w.m1, "Breakdown", -5000, -GAP - 1)                 # ended before the window
    add_span(w, w.m1, "Breakdown", w.SEC, w.SEC + 900)              # starts at period end
    add_span(w, w.m1, "Breakdown", 0, w.SEC, source="iot")          # untrusted source
    add_log(w, w.m1, 49399, "motor overheating")                    # before the reason window
    add_log(w, w.m1, 60000, "motor overheating")                    # at its end
    # Another factory's rows on the SAME machine id.
    add_span(w, w.m1, "Breakdown", 0, w.SEC, tenant=FAC_B)
    add_log(w, w.m1, 55000, "motor overheating", tenant=FAC_B)
    w.db.add(models.ProductionRecord(tenant_code=FAC, machine_id=w.m1.id, planned_minutes=480,
                                     runtime_minutes=400, ideal_cycle_time_seconds=30,
                                     total_count=918273, good_count=918000, rejected_count=273,
                                     created_at=at(w, 1000)))
    w.db.commit()

    captured = []

    def capture(conn, cursor, statement, params, context, executemany):
        captured.append((statement, params))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        st = compute(w).statement
        d = add_dispute(w, st, w.i2, 100, 200)
        captured.clear()
        st = compute(w).statement
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert intervals(st, w.i1.id) == [(0, 50000, "AVAILABLE", "status:Running"),
                                      (50000, 60000, "FACTORY", "reason:no material"),
                                      (60000, w.SEC, "AVAILABLE", "status:Running")], \
        intervals(st, w.i1.id)
    assert (100, 200, "DISPUTED", f"open_dispute:#{d.id}") in intervals(st, w.i2.id)
    found = {}
    for sql, params in captured:
        for table, patterns in _BOUND_PATTERNS.items():
            if re.search(rf"FROM {table}\b", sql):
                found[table] = found.get(table, 0) + 1
                for pattern in patterns:
                    assert re.search(pattern, sql), (table, pattern, sql)
                values = _values(params)
                if table == "machine_telemetry_spans":
                    assert FAC in values and w.P.end in values \
                        and w.P.start - timedelta(seconds=GAP) in values, (sql, values)
                if table == "downtime_logs":
                    assert FAC in values and at(w, 49400) in values and at(w, 60000) in values, \
                        (sql, values)
                if table == "contract_disputes":
                    assert w.P.start in values and w.P.end in values, (sql, values)
    assert found.get("machine_telemetry_spans") == 2 and found.get("downtime_logs") == 1 \
        and found.get("contract_disputes") == 1, found
    text = st.canonical_json + "".join(
        r.evidence_json + r.cause for r in w.db.query(models.ContractAttributionRecord).all())
    for needle in ("NEEDLE", "918273", "Other factory press", "Press 2", '"iot"'):
        assert needle not in text, needle
    print("PASS every span, reason and dispute read filters the factory tenant and bounds "
          "time at both ends (captured SQL and parameters); another factory's rows on the "
          "same machine id, rows just outside each bound, untrusted sources and MES fields "
          "(notes, duration, names, line, counts) never reach the statement")


def test_manual_status_and_machine_events_do_not_move_attribution():
    w = world(fresh())
    running_all_month(w)
    h1 = compute(w).statement.content_hash
    # C2: a manual PATCH sets Breakdown (Machine.status + a manual MachineEvent);
    # MQTT then reports Breakdown too, which writes NO MachineEvent because the
    # shared status did not change. None of that is the mqtt span history.
    w.m1.status = "Breakdown"
    w.db.add(models.MachineEvent(tenant_code=FAC, machine_id=w.m1.id,
                                 machine_name=w.m1.name, old_status="Running",
                                 new_status="Breakdown", source="manual",
                                 created_at=at(w, 1000)))
    w.db.commit()
    r = compute(w)
    assert (r.changed, r.statement.content_hash) == (False, h1), r
    print("PASS C2: Machine.status and MachineEvent rows (manual or otherwise) do not move "
          "the attribution; only trusted spans do")


def test_linkage_ends_coverage_and_a_later_stamp_leaves_earlier_periods_alone():
    w = world(fresh())
    running_all_month(w)
    h1 = compute(w).statement.content_hash
    row = w.db.query(models.ServiceContractMachine).filter_by(installation_id=w.i1.id).one()
    row.coverage_ended_at = w.P.end + timedelta(days=10, microseconds=500)
    row.coverage_end_reason = "machine_changed"
    w.db.commit()
    assert compute(w).statement.content_hash == h1
    row.coverage_ended_at = at(w, 100000) + timedelta(microseconds=999999)
    w.db.commit()
    add_span(w, w.m1, "Breakdown", 150000, 160000)     # after the unlink: never read
    w.db.commit()
    st = compute(w).statement
    assert intervals(st, w.i1.id) == [(0, 100000, "AVAILABLE", "status:Running"),
                                      (100000, w.SEC, "UNMEASURED", "installation_unlinked")], \
        intervals(st, w.i1.id)
    machine = content(st)["machines"][0]
    assert machine["coverage_end"] == canonical.ts(at(w, 100000)), machine["coverage_end"]
    assert machine["intervals"][1]["evidence"] == {"linkage": {
        "coverage_ended_at": canonical.ts(at(w, 100000)), "reason": "machine_changed"}}
    print("PASS time from coverage_ended_at is UNMEASURED installation_unlinked and nothing "
          "after it is read; a stamp after the period leaves that period's hash unchanged")


def test_extending_a_span_after_the_period_leaves_the_hash_unchanged():
    w = world(fresh())
    running_all_month(w)
    h1 = compute(w).statement.content_hash
    span = w.db.query(models.MachineTelemetrySpan).filter_by(machine_id=w.m2.id).one()
    span.span_end = w.P.end + timedelta(seconds=5000, microseconds=7)
    span.message_count = 99
    w.db.commit()
    assert compute(w).statement.content_hash == h1
    print("PASS a span extended past the period end (and its message count) leaves the "
          "statement unchanged")


def test_the_pooled_sla_and_credit_are_exact():
    w = world(fresh())
    add_span(w, w.m1, "Running", -100, 99990)
    add_span(w, w.m1, "Breakdown", 100000, 299700)
    add_span(w, w.m1, "Running", 300000, w.SEC)
    add_span(w, w.m2, "Running", -100, w.SEC + 100)
    w.db.commit()
    c = content(compute(w).statement)
    assert c["machines"][0]["totals"]["oem_seconds"] == 200000, c["machines"][0]["totals"]
    assert c["machines"][1]["totals"]["available_seconds"] == w.SEC
    assert c["totals"] == {"covered_seconds": 2 * w.SEC, "available_seconds": 2 * w.SEC - 200000,
                           "oem_seconds": 200000, "factory_seconds": 0, "disputed_seconds": 0,
                           "unmeasured_seconds": 0}, c["totals"]
    assert (c["sla"]["state"], c["sla"]["availability_pct"], c["sla"]["base_seconds"]) == \
        ("breached", "96.27", 2 * w.SEC), c["sla"]
    assert (c["credit"]["tier_below_pct"], c["credit"]["credit_pct"], c["credit"]["amount"]) == \
        ("97.00", "5.00", "2000.00"), c["credit"]
    print("PASS the SLA is pooled over the contract's machines: 200000 OEM seconds of "
          "5356800 is 96.27%, breached, 5.00% of INR 40000.00 = 2000.00")


# --------------------------------------------------------------------------
# 4. races, verify, payload, preview
# --------------------------------------------------------------------------

def test_the_loser_of_a_concurrent_recompute_gets_statement_conflict():
    w = world(fresh())
    running_all_month(w)
    compute(w)
    add_span(w, w.m1, "Breakdown", 5000, 6000, source="mqtt")
    w.db.commit()
    cid = w.contract.id
    loser = Session()
    loser_contract = loser.get(models.ServiceContract, cid)
    fired = []

    def winner_commits_first(state):
        if state.is_update and not fired and "contract_statements" in str(state.statement):
            fired.append(True)
            winner = Session()
            try:
                won = cs.compute_statement(winner, winner.get(models.ServiceContract, cid),
                                           w.P.start, party=OEM, now=w.NOW)
                assert won.changed and won.statement.revision == 2, won
                winner.commit()
            finally:
                winner.close()

    event.listen(loser, "do_orm_execute", winner_commits_first)
    try:
        assert raises(StatementConflict, cs.compute_statement, loser, loser_contract, w.P.start,
                      party=FACTORY, now=w.NOW)
        loser.rollback()
    finally:
        event.remove(loser, "do_orm_execute", winner_commits_first)
        loser.close()
    assert fired
    w.db.expire_all()
    st = w.db.query(models.ContractStatement).one()
    assert (st.revision, st.computed_by) == (2, OEM.actor), st
    assert len(audit_rows(w)) == 4
    assert cs.verify_statement(w.db, st, w.NOW)["consistency"] == "consistent"
    print("PASS the loser of a concurrent recompute gets StatementConflict and writes nothing; "
          f"the winner's revision 2 stands ({'PostgreSQL' if ON_PG else 'SQLite'})")


def test_a_compute_that_read_before_agreement_cannot_overwrite_it():
    # Why the conditional UPDATE names the REVISION as well as the hash (C1).
    w = world(fresh())
    running_all_month(w)
    st = compute(w).statement
    h1, cid = st.content_hash, w.contract.id
    stale = Session()
    stale_contract = stale.get(models.ServiceContract, cid)
    # Hold the reference: the identity map is weak, and a collected row would be
    # re-read fresh, which is not the race under test.
    stale_row = stale.query(models.ContractStatement).filter_by(contract_id=cid).one()
    assert stale_row.revision == 1
    d = add_dispute(w, st, w.i1, 1000, 2000)
    assert compute(w).statement.revision == 2
    d.status, d.closed_at = "withdrawn", w.NOW
    w.db.commit()
    st = compute(w).statement
    assert (st.revision, st.content_hash) == (3, h1), st
    accept(w, st, FACTORY)
    accept(w, st, OEM)
    add_span(w, w.m1, "Breakdown", 7000, 9000)
    w.db.commit()
    try:
        assert raises(StatementConflict, cs.compute_statement, stale, stale_contract, w.P.start,
                      party=FACTORY, now=w.NOW)
        stale.rollback()
    finally:
        stale.close()
    w.db.expire_all()
    st = w.db.query(models.ContractStatement).one()
    assert (st.revision, st.content_hash) == (3, h1), st
    assert cs.acceptance_state(w.db, st)["agreed"] is True
    print("PASS a compute that read revision 1 cannot overwrite revision 3, agreed with the "
          "same bytes: the update names the revision, not only the hash")


def test_an_insert_race_rereads_the_winning_row():
    w = world(fresh())
    running_all_month(w)
    winner = Session()
    cs.compute_statement(winner, winner.get(models.ServiceContract, w.contract.id), w.P.start,
                         party=OEM, now=w.NOW)
    winner.commit()
    winner.close()
    real = cs._statement_row
    calls = []

    def missed_it_once(db, contract, period):
        calls.append(1)
        return None if len(calls) == 1 else real(db, contract, period)

    cs._statement_row = missed_it_once
    try:
        r = compute(w)
    finally:
        cs._statement_row = real
    assert len(calls) == 2 and (r.changed, r.statement.revision) == (False, 1), (calls, r)
    assert w.db.query(models.ContractStatement).count() == 1
    print("PASS an insert that loses the race on (contract, period_start) re-reads the "
          "winner's row and carries on from it")


def test_verify_tells_consistency_failures_apart():
    w = world(fresh())
    running_all_month(w)
    st = compute(w).statement
    good = st.canonical_json
    st.canonical_json = good.replace('"AVAILABLE"', '"OEM"', 1)
    w.db.commit()
    assert cs.verify_statement(w.db, st, w.NOW)["consistency"] == "blob_mismatch"
    st.canonical_json = good
    rec = w.db.query(models.ContractAttributionRecord).filter_by(seq=1).one()
    rec.bucket = "OEM"
    w.db.commit()
    v = cs.verify_statement(w.db, st, w.NOW)
    assert v["consistency"] == "records_diverged" and v["records_hash"] != v["stored_hash"], v
    rec.bucket = "AVAILABLE"
    rec.evidence_json = "not json"
    w.db.commit()
    assert cs.verify_statement(w.db, st, w.NOW)["consistency"] == "records_diverged"
    rec.evidence_json = json.dumps(json.loads(good)["machines"][0]["intervals"][0]["evidence"])
    w.db.commit()
    assert cs.verify_statement(w.db, st, w.NOW)["consistency"] == "consistent"
    w.db.query(models.ContractAttributionRecord).filter_by(seq=2).delete()
    w.db.commit()
    assert cs.verify_statement(w.db, st, w.NOW)["consistency"] == "records_diverged"
    st.canonical_json = None
    w.db.commit()
    v = cs.verify_statement(w.db, st, w.NOW)
    assert (v["consistency"], v["blob_hash"], v["stored_hash"]) == \
        ("content_purged", None, st.content_hash), v
    print("PASS verify reports blob_mismatch, records_diverged (changed, unparseable or "
          "missing record), consistent, and content_purged")


def test_the_payload_carries_acceptances_with_their_validity():
    w = world(fresh())
    running_all_month(w)
    st = compute(w).statement
    accept(w, st, OEM)
    p = cs.statement_payload(w.db, st)
    assert (p["id"], p["revision"], p["content_hash"], p["agreed"]) == \
        (st.id, 1, st.content_hash, False), p
    assert (p["period_start"], p["period_end"]) == (canonical.ts(w.P.start), canonical.ts(w.P.end))
    assert p["content"] == content(st) and p["content_purged"] is False
    assert [(a["party"], a["revision"], a["valid"]) for a in p["acceptances"]] == \
        [("OEM", 1, True)], p["acceptances"]
    json.dumps(p)
    print("PASS statement_payload carries the content, each acceptance with its validity, "
          "and whether the statement is agreed")


def test_a_preview_is_never_persisted():
    w = world(fresh())
    running_all_month(w)
    now = w.P.start + timedelta(days=5, microseconds=3)
    p = cs.preview_statement(w.db, w.contract, now)
    assert p["preview"] is True and p["as_of"] == canonical.ts(canonical.utc_seconds(now)), p
    assert p["content"]["period"]["start"] == canonical.ts(w.P.start)
    assert p["content"]["totals"]["covered_seconds"] == 2 * 5 * 86400, p["content"]["totals"]
    assert "content_hash" not in p
    assert w.db.query(models.ContractStatement).count() == 0
    assert len(audit_rows(w)) == 0
    assert cs.preview_statement(w.db, w.contract, w.contract.starts_at - timedelta(days=1)) is None
    w.contract.status = "proposed"
    w.db.commit()
    assert cs.preview_statement(w.db, w.contract, now) is None
    print("PASS a preview covers the open period up to now, carries no hash, writes nothing, "
          "and does not exist before the factory accepts")


# --------------------------------------------------------------------------
# 5. structural
# --------------------------------------------------------------------------

def test_contract_statements_structure():
    path = os.path.join(HERE, "contract_statements.py")
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    nodes = list(ast.walk(tree))
    functions = {n.name: n for n in nodes if isinstance(n, ast.FunctionDef)}
    for required in ("compute_statement", "preview_statement", "statement_payload",
                     "acceptance_state", "verify_statement", "_read_spans", "_read_reasons",
                     "_read_disputes", "_statement_row"):
        assert required in functions, f"structural scan did not find {required}"
    floats = [n.lineno for n in nodes
              if (isinstance(n, ast.Constant) and type(n.value) is float)
              or (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                  and n.func.id == "float")
              or (isinstance(n, (ast.BinOp, ast.AugAssign)) and isinstance(n.op, ast.Div))
              or (isinstance(n, ast.Attribute) and n.attr == "total_seconds")]
    assert not floats, f"float, '/' or total_seconds() at lines {floats}"
    names = {n.id for n in nodes if isinstance(n, ast.Name)} | \
        {n.attr for n in nodes if isinstance(n, ast.Attribute)}
    for banned in ("MachineEvent", "OeeWindow", "utcnow"):
        assert banned not in names, f"contract_statements names {banned}"
    body = list(ast.walk(functions["acceptance_state"]))
    assert any(isinstance(n, ast.Attribute) and n.attr == "acceptance_is_valid" for n in body), \
        "acceptance_state must call canonical.acceptance_is_valid"
    compared = [n for n in body if isinstance(n, ast.Compare)
                for side in [n.left, *n.comparators]
                if isinstance(side, ast.Attribute) and side.attr in ("content_hash", "revision")]
    assert not compared, "acceptance_state re-implements the acceptance rule"
    print(f"PASS contract_statements.py: {len(functions)} functions found; no float, no "
          "MachineEvent, no OeeWindow; acceptance_state delegates to canonical")


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items())
             if k.startswith("test_") and callable(v)]
    try:
        for test in tests:
            test()
    finally:
        engine.dispose()
        if TMP:
            shutil.rmtree(TMP, ignore_errors=True)
    where = f"PostgreSQL ({PG_VERSION.split(',')[0]})" if ON_PG else "SQLite"
    print(f"ALL {len(tests)} CONTRACT STATEMENT TESTS PASSED on {where}")
