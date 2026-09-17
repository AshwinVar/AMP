"""Downtime attribution statements: compute, persist, accept state, verify (ADR-0020).

WHAT A STATEMENT IS
-------------------
One row per (contract, period): the attribution of every covered second of the
contract's machines for one whole, closed period, the per-machine and pooled
totals, the SLA and the credit, as CANONICAL BYTES (canonical.canonical_bytes)
whose SHA-256 is `content_hash`. Both parties accept a (content_hash, revision);
any later change bumps the revision and cancels every acceptance
(canonical.acceptance_is_valid is the only copy of that rule).

Integrity is that one hash per revision plus the ordinary audit log. Nothing is
chained, signed or put on a ledger: metering and shared usage ledgers already
exist (SteamChain, PayperChain, Linxfour, Rockwell US10747201B2) and AMP claims
no novelty for them. The attribution from the factory's own reasons is the
differentiator. A freedom-to-operate review is needed before commercial launch.
AMP computes the credit; it never invoices or moves money.

    compute_statement(db, contract, period_start, *, party, now) -> ComputeResult
    preview_statement(db, contract, now)       the open period so far; never stored
    statement_payload(db, statement)           what a party is shown
    acceptance_state(db, statement)            {"FACTORY", "OEM", "agreed"}
    verify_statement(db, statement, now)       consistency + live recompute

COMPUTE, IN ORDER
-----------------
 1. The factory must have accepted the contract; the terms are the ACCEPTED
    version with the highest number effective at the period start, whose stored
    hashes all agree (else ContractIntegrityError). `period_start` must start a
    whole period of the contract (else NoTermsForPeriod).
 2. PeriodNotClosed until period_end + telemetry_coverage.SETTLE_SECONDS: a late
    span write cannot change a period someone may already have accepted.
 3. An agreed statement is FROZEN: no read, no write, frozen=True. (Checked
    before evidence expiry: an agreed statement needs no evidence.)
 4. ContentPurged (an EvidenceExpired) when offboarding removed the content:
    the kept hash and acceptances stand. EvidenceExpired when period_start -
    SPAN_GAP_SECONDS falls before the span retention cutoff (retention.py's
    policy for machine_telemetry_spans).
 5. Read and attribute (attribution_engine), render, hash.
 6. No row: insert revision 1 (a lost insert race re-reads the winner's row
    and continues from it). Same hash: nothing. Different hash: UPDATE ... WHERE
    content_hash = :old AND revision = :rev; zero rows is StatementConflict.
    Records are replaced and two audit rows written (factory tenant and OEM
    sentinel), inside the caller's transaction. The caller commits.

WHAT IS READ, AND HOW
---------------------
Spans, downtime reasons and disputes, each filtered EXPLICITLY by the factory
tenant (or contract and statement) and bounded at BOTH ends with the windows the
engine computes. Never Machine.status and never MachineEvent (critic finding
C2). A read bound to another tenant (an OEM request's sentinel) is refused: the
ADR-0002 filter would hide every row and each second would read as "no data".
OEM-triggered computes bind the factory with oem_sharing.bound_factory_read.

WHAT VERIFY PROVES, AND WHAT IT DOES NOT
----------------------------------------
    consistent        the stored bytes hash to content_hash, and the attribution
                      records say exactly what the bytes say
    blob_mismatch     the stored bytes do not hash to content_hash
    records_diverged  the records disagree with the bytes
    content_purged    the bytes were removed at offboarding; hash kept
It does NOT prove nobody with full database access rewrote bytes, hash, records
and acceptances together. Only the parties' own exported copies can show that.
`live` recomputes from current evidence (reason "evidence_expired" past
retention) and reports whether it still matches.

PHASE 1 DEPENDENCIES are read at call time, so importing this module never
depends on them: telemetry_coverage (SPAN_GAP_SECONDS, SETTLE_SECONDS),
retention.POLICIES (span retention), platform_routes.log_audit(tenant_code=,
commit=False).

Run the tests: DATABASE_URL="sqlite:///./ci.db" python backend/test_contract_statements.py
"""
import json
from collections import namedtuple
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

import attribution_engine as ae
import canonical
import contract_money
import contract_periods
import contract_terms
import models
import tenancy

SCHEMA = "amp.downtime-attribution-statement/1"
# A contract the factory accepted, including one later terminated: its whole
# periods before the termination boundary still have statements.
ACCEPTED_CONTRACT_STATUSES = ("accepted", "terminated")
TERMS_ACCEPTED = "accepted"
COMPUTED_ACTION = "contract_statement_computed"

CONSISTENT = "consistent"
BLOB_MISMATCH = "blob_mismatch"
RECORDS_DIVERGED = "records_diverged"
CONTENT_PURGED = "content_purged"

ComputeResult = namedtuple("ComputeResult", "statement changed frozen")


class StatementError(Exception):
    """A statement cannot be computed now, for a reason the caller can act on."""


class PeriodNotClosed(StatementError):
    """The period has not ended plus SETTLE_SECONDS."""


class EvidenceExpired(StatementError):
    """The spans the statement needs are past retention."""


class ContentPurged(EvidenceExpired):
    """The statement's content was removed when the factory was offboarded.

    A kind of EvidenceExpired: the evidence is gone and the stored revision
    (its hash and acceptances, which offboarding keeps) stands. Recomputing
    would read nothing and overwrite the one hash that survived."""


class StatementConflict(StatementError):
    """Another compute changed the statement first."""


class NoTermsForPeriod(StatementError):
    """No accepted terms or no whole period of this contract starts there."""


class ContractIntegrityError(RuntimeError):
    """Stored contract data contradicts itself. Never guessed around."""


# ── Phase 1 settings, read where they are owned ─────────────────────────────

def _telemetry_coverage():
    try:
        import telemetry_coverage
    except ModuleNotFoundError as e:
        if e.name != "telemetry_coverage":
            raise
        raise RuntimeError(
            "contract_statements needs telemetry_coverage (SPAN_GAP_SECONDS, "
            "SETTLE_SECONDS; ADR-0020), which is not installed") from None
    return telemetry_coverage


def span_gap_seconds():
    """telemetry_coverage.SPAN_GAP_SECONDS: how long a span holds after its last message."""
    return _telemetry_coverage().SPAN_GAP_SECONDS


def settle_seconds():
    """telemetry_coverage.SETTLE_SECONDS: how long after period end compute waits."""
    return _telemetry_coverage().SETTLE_SECONDS


def span_retention_days():
    """Days machine_telemetry_spans are kept, from retention.py's policy (None: forever)."""
    import retention

    policies = [p for p in retention.POLICIES if p.model is models.MachineTelemetrySpan]
    if len(policies) != 1:
        raise RuntimeError(
            "retention.POLICIES must hold exactly one policy for machine_telemetry_spans; "
            f"found {len(policies)}")
    if policies[0].timestamp_column != "span_end":
        raise ContractIntegrityError(
            "machine_telemetry_spans must be pruned by span_end: the engine reads spans "
            "whose span_end reaches the period, and EvidenceExpired assumes that clock")
    return policies[0].days


def evidence_expired(period, now):
    """Would retention already have pruned spans this period needs?"""
    days = span_retention_days()
    if days is None:
        return False
    return period.start - timedelta(seconds=span_gap_seconds()) < now - timedelta(days=days)


# ── inputs ──────────────────────────────────────────────────────────────────

def _now(now):
    if not isinstance(now, datetime):
        raise ValueError(f"now must be a datetime, not {now!r}")
    return canonical.utc_seconds(now)


def _party(party):
    """(side, actor) from a Party-like object with .side and .actor."""
    side = getattr(party, "side", None)
    actor = getattr(party, "actor", None)
    if side not in contract_terms.PARTIES:
        raise ValueError(f"party.side must be one of {contract_terms.PARTIES}, not {side!r}")
    if type(actor) is not str or not actor.strip():
        raise ValueError("party.actor must name who computed the statement")
    return side, actor


def _require_factory_read_binding(contract):
    bound = tenancy.current_tenant()
    if bound is not None and bound != contract.factory_tenant_code:
        raise ContractIntegrityError(
            f"statement reads are bound to tenant {bound!r}, but the contract's factory is "
            f"{contract.factory_tenant_code!r}: the tenant filter would hide the factory's "
            "evidence and every second would read as no data. Bind the factory "
            "(oem_sharing.bound_factory_read) for the compute.")


# ── terms and period ────────────────────────────────────────────────────────

def accepted_versions(db, contract_id):
    """A contract's ACCEPTED term versions, lowest version first."""
    TV = models.ServiceContractTermVersion
    return (db.query(TV)
              .filter(TV.contract_id == contract_id, TV.status == TERMS_ACCEPTED)
              .order_by(TV.version.asc()).all())


def governing_version(versions, instant):
    """The ACCEPTED version with the highest number whose effective_from <= instant.

    The one rule for which terms govern a period (effective_from is a period
    boundary, so a version governs whole periods). None when none does."""
    best = None
    for v in versions:
        if v.status == TERMS_ACCEPTED and v.effective_from <= instant                 and (best is None or v.version > best.version):
            best = v
    return best


def _parse_version(version):
    try:
        terms = contract_terms.parse(version.terms_json)
    except contract_terms.TermsError as e:
        raise ContractIntegrityError(
            f"accepted terms version {version.version} no longer parses: {e}") from None
    digest = contract_terms.terms_hash(terms)
    if not (digest == version.terms_hash == version.oem_accepted_hash
            == version.factory_accepted_hash):
        raise ContractIntegrityError(
            f"accepted terms version {version.version}: the stored terms, their hash and "
            "both parties' accepted hashes do not all agree")
    return terms


def _grid(terms):
    return terms.timezone, terms.period_months


def _contract_accepted(contract):
    return (contract.status in ACCEPTED_CONTRACT_STATUSES
            and contract.factory_accepted_at is not None)


def resolve_period(db, contract, period_start):
    """(period, version, terms) for the whole period starting at `period_start`."""
    if not _contract_accepted(contract):
        raise NoTermsForPeriod(
            "the factory has not accepted this contract; no statement exists before it does")
    if type(period_start) is not datetime or period_start.tzinfo is not None:
        raise NoTermsForPeriod("period_start must be a naive UTC datetime")
    versions = accepted_versions(db, contract.id)
    version = governing_version(versions, period_start)
    if version is None:
        raise NoTermsForPeriod(f"no accepted terms are in force at {period_start}")
    terms = _parse_version(version)
    if _grid(terms) != _grid(_parse_version(versions[0])):
        raise ContractIntegrityError(
            f"terms version {version.version} changes the period grid (timezone, "
            "period_months) the contract's statements are anchored to")
    try:
        period = contract_periods.period_starting(contract, terms, period_start)
    except contract_periods.PeriodError as e:
        raise ContractIntegrityError(f"the contract's own dates are off its grid: {e}") from None
    if period is None:
        raise NoTermsForPeriod(f"{period_start} does not start a whole period of this contract")
    return period, version, terms


def _covered_machines(db, contract, version, terms):
    SCM = models.ServiceContractMachine
    rows = (db.query(SCM).filter(SCM.term_version_id == version.id)
              .order_by(SCM.installation_id.asc()).all())
    expected = [(c.installation_id, c.serial_number) for c in terms.covered_installations]
    if [(r.installation_id, r.serial_number) for r in rows] != expected:
        raise ContractIntegrityError(
            f"terms version {version.version}: the covered machines recorded at acceptance "
            "do not match the terms' covered_installations")
    for r in rows:
        if r.factory_tenant_at_acceptance != contract.factory_tenant_code \
                or type(r.machine_id_at_acceptance) is not int:
            raise ContractIntegrityError(
                f"installation {r.installation_id} was accepted for "
                f"{r.factory_tenant_at_acceptance!r}, not this contract's factory")
    return rows


# ── reads (explicit tenant, both bounds) ────────────────────────────────────

def _read_spans(db, tenant_code, machine_id, sources, end_at_least, start_before):
    S = models.MachineTelemetrySpan
    rows = (db.query(S)
              .filter(S.tenant_code == tenant_code,
                      S.machine_id == machine_id,
                      S.source.in_(list(sources)),
                      S.span_start < start_before,
                      S.span_end >= end_at_least)
              .order_by(S.span_start.asc(), S.id.asc()).all())
    return tuple(ae.SpanInput(id=r.id, source=r.source, status=r.status,
                              start=canonical.utc_seconds(r.span_start),
                              end=canonical.utc_seconds(r.span_end)) for r in rows)


def _read_reasons(db, tenant_code, machine_id, lo, hi):
    L = models.DowntimeLog
    rows = (db.query(L)
              .filter(L.tenant_code == tenant_code,
                      L.machine_id == machine_id,
                      L.created_at >= lo,
                      L.created_at < hi)
              .order_by(L.created_at.asc(), L.id.asc()).all())
    return tuple(ae.ReasonLog(id=r.id, at=canonical.utc_seconds(r.created_at), reason=r.reason)
                 for r in rows)


def _read_disputes(db, contract, statement, period):
    CD = models.ContractDispute
    rows = (db.query(CD)
              .filter(CD.contract_id == contract.id,
                      CD.statement_id == statement.id,
                      CD.window_start < period.end,
                      CD.window_end > period.start)
              .order_by(CD.id.asc()).all())
    return [ae.DisputeInput(id=d.id, installation_id=d.installation_id, status=d.status,
                            start=canonical.utc_seconds(d.window_start),
                            end=canonical.utc_seconds(d.window_end),
                            resolution_bucket=d.resolution_bucket) for d in rows]


def _statement_row(db, contract, period):
    CS = models.ContractStatement
    return (db.query(CS)
              .filter(CS.contract_id == contract.id, CS.period_start == period.start)
              .first())


# ── content ─────────────────────────────────────────────────────────────────

def _interval(segment):
    return {"start": canonical.ts(segment.start), "end": canonical.ts(segment.end),
            "seconds": segment.seconds, "bucket": segment.bucket, "cause": segment.cause,
            "evidence": segment.evidence}


def _build(db, contract, version, terms, period, statement, as_of=None):
    """(content, segments). Reads the evidence; writes nothing."""
    _require_factory_read_binding(contract)
    gap = span_gap_seconds()
    rows = _covered_machines(db, contract, version, terms)
    active_end = period.end if as_of is None else max(period.start, min(period.end, as_of))
    covered = contract_periods.covered_intervals(period, terms, period.start, active_end)
    tenant = contract.factory_tenant_code
    inputs, ended = [], {}
    for row in rows:
        stamp = (None if row.coverage_ended_at is None
                 else canonical.utc_seconds(row.coverage_ended_at))
        ended[row.installation_id] = stamp
        end_at_least, start_before = ae.span_read_window(period, stamp, gap)
        spans = _read_spans(db, tenant, row.machine_id_at_acceptance, terms.trusted_sources,
                            end_at_least, start_before)
        episodes = ae.down_episodes(spans, gap, ae.coverage_end(period, stamp))
        window = ae.reason_read_window(episodes, terms)
        reasons = (() if window is None
                   else _read_reasons(db, tenant, row.machine_id_at_acceptance, *window))
        inputs.append(ae.MachineInput(
            installation_id=row.installation_id, serial_number=row.serial_number,
            coverage_ended_at=stamp, coverage_end_reason=row.coverage_end_reason,
            spans=spans, reasons=reasons))
    disputes = [] if statement is None else _read_disputes(db, contract, statement, period)
    segments = ae.attribute(inputs, covered, terms, disputes, period=period, gap_seconds=gap)

    machines = []
    for row in rows:
        mine = [s for s in segments if s.installation_id == row.installation_id]
        stamp = ended[row.installation_id]
        machines.append({
            "installation_id": row.installation_id,
            "serial_number": row.serial_number,
            # Only a stamp that affects THIS period; a later unlink must not
            # change the bytes of an earlier period's statement.
            "coverage_end": canonical.ts(stamp) if stamp is not None and stamp < period.end
            else None,
            "totals": ae.bucket_totals(mine),
            "intervals": [_interval(s) for s in mine],
        })
    totals = ae.bucket_totals(segments)
    evaluation = contract_money.evaluate_sla(totals, terms)
    content = {
        "schema": SCHEMA,
        "contract": {"id": contract.id, "ref": contract.contract_ref,
                     "oem_code": contract.oem_code,
                     "factory_tenant_code": contract.factory_tenant_code,
                     "terms_version": version.version, "terms_hash": version.terms_hash},
        "period": {"start": canonical.ts(period.start), "end": canonical.ts(period.end),
                   "timezone": terms.timezone},
        "machines": machines,
        "totals": totals,
        "sla": evaluation["sla"],
        "credit": evaluation["credit"],
    }
    return content, segments


def _replace_records(db, statement, segments):
    CAR = models.ContractAttributionRecord
    db.query(CAR).filter(CAR.statement_id == statement.id).delete(synchronize_session=False)
    for seq, s in enumerate(segments, start=1):
        db.add(CAR(statement_id=statement.id, seq=seq, installation_id=s.installation_id,
                   start_at=s.start, end_at=s.end, seconds=s.seconds, bucket=s.bucket,
                   cause=s.cause,
                   evidence_json=canonical.canonical_bytes(s.evidence).decode("utf-8")))
    db.flush()


def _audit(db, contract, actor, statement, old_hash, old_revision):
    import oem_auth
    import platform_routes

    if old_hash is None:
        details = f"revision {statement.revision}; content_hash {statement.content_hash}"
    else:
        details = (f"revision {old_revision} -> {statement.revision}; content_hash "
                   f"{old_hash} -> {statement.content_hash}")
    for tenant in (contract.factory_tenant_code, oem_auth.sentinel_tenant(contract.oem_code)):
        platform_routes.log_audit(db, actor, COMPUTED_ACTION, "contract_statement",
                                  statement.id, details, tenant_code=tenant, commit=False)


# ── compute ─────────────────────────────────────────────────────────────────

def _refuse_expired(period, now):
    if evidence_expired(period, now):
        raise EvidenceExpired(
            f"spans for the period starting {period.start} are past retention")


def _hashed(content):
    blob = canonical.canonical_bytes(content)
    return canonical.sha256_hex(blob), blob.decode("utf-8")


def compute_statement(db, contract, period_start, *, party, now):
    """Compute (or confirm) the statement for the period starting at period_start.

    `party` has .side (OEM | FACTORY) and .actor. Writes inside the caller's
    transaction; the caller commits. Raises PeriodNotClosed, EvidenceExpired
    (ContentPurged), StatementConflict, NoTermsForPeriod, ContractIntegrityError.
    Every refusal happens before anything is written."""
    side, actor = _party(party)
    now = _now(now)
    period, version, terms = resolve_period(db, contract, period_start)
    if now < period.end + timedelta(seconds=settle_seconds()):
        raise PeriodNotClosed(
            f"the period ends {period.end} and settles {settle_seconds()}s later")

    statement = _statement_row(db, contract, period)
    if statement is None:
        _refuse_expired(period, now)
        content, segments = _build(db, contract, version, terms, period, None)
        new_hash, text = _hashed(content)
        row = models.ContractStatement(
            contract_id=contract.id, term_version_id=version.id, period_start=period.start,
            period_end=period.end, revision=1, content_hash=new_hash, canonical_json=text,
            computed_at=now, computed_by_party=side, computed_by=actor, updated_at=now)
        try:
            with db.begin_nested():
                db.add(row)
        except IntegrityError:
            # Another compute inserted this period first: carry on from its row.
            statement = _statement_row(db, contract, period)
            if statement is None:
                raise StatementConflict(
                    "another compute created this statement concurrently") from None
        else:
            _replace_records(db, row, segments)
            _audit(db, contract, actor, row, None, None)
            return ComputeResult(row, True, False)

    if acceptance_state(db, statement)["agreed"]:
        return ComputeResult(statement, False, True)
    if statement.canonical_json is None:
        raise ContentPurged(
            f"statement {statement.id}'s content was removed at offboarding; revision "
            f"{statement.revision} and its hash stand")
    _refuse_expired(period, now)
    content, segments = _build(db, contract, version, terms, period, statement)
    new_hash, text = _hashed(content)
    if statement.content_hash == new_hash:
        return ComputeResult(statement, False, False)
    CS = models.ContractStatement
    old_hash, old_revision = statement.content_hash, statement.revision
    result = db.execute(
        update(CS)
        .where(CS.id == statement.id, CS.content_hash == old_hash,
               CS.revision == old_revision)
        .values(content_hash=new_hash, canonical_json=text, revision=CS.revision + 1,
                term_version_id=version.id, computed_at=now, computed_by_party=side,
                computed_by=actor, updated_at=now)
        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise StatementConflict(
            f"statement {statement.id} changed after revision {old_revision} was read")
    db.refresh(statement)
    _replace_records(db, statement, segments)
    _audit(db, contract, actor, statement, old_hash, old_revision)
    return ComputeResult(statement, True, False)


def preview_statement(db, contract, now):
    """The open period's attribution so far, or None. Never persisted, no hash,
    cannot be accepted."""
    now = _now(now)
    if not _contract_accepted(contract):
        return None
    versions = accepted_versions(db, contract.id)
    if not versions:
        return None
    try:
        grid = contract_periods.periods(_parse_version(versions[0]), contract.starts_at,
                                        contract_periods.contract_effective_end(contract))
    except contract_periods.PeriodError as e:
        raise ContractIntegrityError(f"the contract's own dates are off its grid: {e}") from None
    current = next((p for p in grid if p.start <= now < p.end), None)
    if current is None:
        return None
    try:
        period, version, terms = resolve_period(db, contract, current.start)
    except NoTermsForPeriod:
        return None
    content, _ = _build(db, contract, version, terms, period, None, as_of=now)
    return {"preview": True, "as_of": canonical.ts(now),
            "period_start": canonical.ts(period.start), "period_end": canonical.ts(period.end),
            "content": content}


# ── acceptance, payload, verify ─────────────────────────────────────────────

def _acceptances(db, statement):
    A = models.ContractStatementAcceptance
    return (db.query(A).filter(A.statement_id == statement.id)
              .order_by(A.id.asc()).all())


def acceptance_state(db, statement):
    """The valid acceptance per party (or None) and whether both are valid."""
    out = {party: None for party in contract_terms.PARTIES}
    for row in _acceptances(db, statement):
        if row.party in out and out[row.party] is None \
                and canonical.acceptance_is_valid(row, statement):
            out[row.party] = row
    out["agreed"] = all(out[party] is not None for party in contract_terms.PARTIES)
    return out


def _acceptance_rows(db, statement):
    return [{"party": a.party, "actor": a.actor,
             "accepted_at": canonical.ts(canonical.utc_seconds(a.accepted_at)),
             "content_hash": a.content_hash, "revision": a.revision,
             "valid": canonical.acceptance_is_valid(a, statement)}
            for a in _acceptances(db, statement)]


def statement_payload(db, statement):
    """The statement as a party sees it (the route decides whether they may)."""
    content = (None if statement.canonical_json is None
               else json.loads(statement.canonical_json))
    return {
        "id": statement.id,
        "contract_id": statement.contract_id,
        "term_version_id": statement.term_version_id,
        "period_start": canonical.ts(statement.period_start),
        "period_end": canonical.ts(statement.period_end),
        "revision": statement.revision,
        "content_hash": statement.content_hash,
        "computed_at": canonical.ts(canonical.utc_seconds(statement.computed_at)),
        "computed_by_party": statement.computed_by_party,
        "computed_by": statement.computed_by,
        "content": content,
        "content_purged": content is None,
        "acceptances": _acceptance_rows(db, statement),
        "agreed": acceptance_state(db, statement)["agreed"],
    }


def _records_projection(db, statement):
    CAR = models.ContractAttributionRecord
    rows = (db.query(CAR).filter(CAR.statement_id == statement.id)
              .order_by(CAR.seq.asc()).all())
    return [{"installation_id": r.installation_id, "start": canonical.ts(r.start_at),
             "end": canonical.ts(r.end_at), "seconds": r.seconds, "bucket": r.bucket,
             "cause": r.cause, "evidence": json.loads(r.evidence_json)} for r in rows]


def _content_projection(content):
    return [dict(interval, installation_id=machine["installation_id"])
            for machine in content["machines"] for interval in machine["intervals"]]


def verify_statement(db, statement, now):
    """Read-only consistency check plus a live recompute. See the module notes
    for what "consistent" does and does not prove."""
    now = _now(now)
    try:
        records_hash = canonical.content_hash(_records_projection(db, statement))
    except (ValueError, TypeError, canonical.CanonicalError):
        records_hash = None
    blob_hash = None
    if statement.canonical_json is None:
        consistency = CONTENT_PURGED
    else:
        blob_hash = canonical.sha256_hex(statement.canonical_json.encode("utf-8"))
        if blob_hash != statement.content_hash:
            consistency = BLOB_MISMATCH
        else:
            try:
                expected = canonical.content_hash(
                    _content_projection(json.loads(statement.canonical_json)))
            except (ValueError, TypeError, KeyError, canonical.CanonicalError):
                expected = None
            consistency = (CONSISTENT if records_hash is not None and records_hash == expected
                           else RECORDS_DIVERGED)

    live = {"hash": None, "matches": None, "reason": None}
    contract = db.get(models.ServiceContract, statement.contract_id)
    try:
        period, version, terms = resolve_period(db, contract, statement.period_start)
    except NoTermsForPeriod:
        live["reason"] = "no_terms"
    else:
        if statement.canonical_json is None:
            live["reason"] = CONTENT_PURGED
        elif now < period.end + timedelta(seconds=settle_seconds()):
            live["reason"] = "period_not_closed"
        elif evidence_expired(period, now):
            live["reason"] = "evidence_expired"
        else:
            content, _ = _build(db, contract, version, terms, period, statement)
            live["hash"] = canonical.content_hash(content)
            live["matches"] = live["hash"] == statement.content_hash

    state = acceptance_state(db, statement)
    return {
        "statement_id": statement.id,
        "revision": statement.revision,
        "stored_hash": statement.content_hash,
        "blob_hash": blob_hash,
        "records_hash": records_hash,
        "consistency": consistency,
        "acceptances": _acceptance_rows(db, statement),
        "agreed": state["agreed"],
        "live": live,
    }
