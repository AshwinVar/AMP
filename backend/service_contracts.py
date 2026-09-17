"""Service contracts with agreed downtime attribution: the domain service (ADR-0020).

WHAT THIS PRODUCT IS, AND IS NOT
--------------------------------
A machine maker and a factory already sign service contracts: annual maintenance
contracts, warranties, uptime clauses. At month end they argue about whose fault
the downtime was. AMP issues a statement that attributes every covered minute of
a covered machine to AVAILABLE, OEM (machine fault), FACTORY (the factory's own
stop, from its OWN MES downtime reasons), DISPUTED or UNMEASURED ("No data",
never counted as uptime or downtime), and both parties accept the exact hashed
revision. Attribution agreed from the factory's own records is the
differentiator. Metering, pay-per-use and shared usage ledgers already exist
(SteamChain, PayperChain, Linxfour, Rockwell US10747201B2) and nothing here
claims novelty for them; there is no ledger, chain, smart contract or usage
billing, and AMP never moves money. A freedom-to-operate review is needed before
commercial launch.

WHY ONE SERVICE AND TWO THIN ROUTE FILES
---------------------------------------
oem_contract_routes (the manufacturer, `require_oem`) and service_contract_routes
(the factory, `require_roles`) expose the same contract from two sides. Every
rule lives here, once, keyed on a `Party`. A rule written twice is a rule that
drifts, and on a two-party contract the drift is a party bound to something it
never agreed to.

THE RULES, IN ONE PLACE EACH
----------------------------
  * VISIBILITY. The OEM sees a contract iff `oem_code` is its own. The factory
    sees it iff `factory_tenant_code` is its request tenant AND it was proposed:
    a draft (even a withdrawn one) was never shown to anybody. Everything else
    is a 404 with one message, so a probe cannot tell "not yours" from "none".
  * CONSENT. Statement content, verify, download, preview, compute, statement
    acceptance and dispute actions need SHARE_DOWNTIME for an OEM, read at
    request time through oem_sharing.contract_statement_visible. Terms, versions,
    periods, the dispute list and history stay visible to a party: they are the
    contract, not the factory's data.
  * EXPLICIT ACCEPTANCE. Nothing is metered before the factory accepts, and it
    accepts with the terms hash it saw AND `grant_downtime_sharing: true`. The
    grant WIDENS the existing policy (oem_sharing.widen_grants); it never
    replaces it.
  * BOTH PARTIES FOR EVERY CHANGE. Terms change only by an amendment its author
    proposes with a hash and the other party accepts with the same hash.
  * ROW COUNTS DECIDE. Every transition is a conditional UPDATE on the state it
    was read in; zero rows is a 409, never a silent overwrite.
  * AUDIT IN THE SAME TRANSACTION. log_audit(commit=False) writes one row in the
    factory's tenant and one in the OEM's sentinel tenant, inside the business
    transaction, so a failed commit leaves neither the change nor its record.
  * TIME. Naive UTC, whole seconds, half-open. `utcnow` below is the ONLY clock
    the routes read (tests pin it).

What the engine owns (contract_statements, attribution_engine): the statement
content, its hash, revisions, the acceptance-state rule (via
canonical.acceptance_is_valid) and the compute/verify audit. This module never
builds statement content and never decides whether an acceptance is valid.
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi import HTTPException, Response
from sqlalchemy import and_, or_, update
from sqlalchemy.exc import IntegrityError

import canonical
import contract_periods
import contract_terms
import models
import oem_auth
import oem_events
import oem_sharing
import platform_routes
import tenancy
from events import event_bus

OEM = contract_terms.OEM
FACTORY = contract_terms.FACTORY
SIDES = (OEM, FACTORY)

CONTRACT_TYPES = ("AMC", "WARRANTY", "UPTIME_CLAUSE")

# Stored contract statuses. "active", "not_started" and "ended" are derived.
DRAFT, PROPOSED, ACCEPTED, REJECTED, WITHDRAWN, TERMINATED = (
    "draft", "proposed", "accepted", "rejected", "withdrawn", "terminated")
BINDING_STATUSES = (ACCEPTED, TERMINATED)

# Dispute statuses.
OPEN, RESOLUTION_PROPOSED, RESOLVED = "open", "resolution_proposed", "resolved"
LIVE_DISPUTE_STATUSES = (OPEN, RESOLUTION_PROPOSED)

# The period grid of a contract. An amendment that changed one of these would
# move every boundary under statements that already exist.
GRID_FIELDS = ("currency", "period_months", "timezone", "term_months")

# The reason vocabulary looks back this far. Bounded at both ends.
VOCABULARY_LOOKBACK_DAYS = 90

MAX_LIST = 500
# Every id column here is an INTEGER. A path id beyond it is not a row that
# might exist: on SQLite binding it raises OverflowError (a 500), and it must
# get exactly the answer a missing row gets, so it is refused before any query.
MAX_ROW_ID = 2 ** 31 - 1
MAX_HISTORY = 1000
# One contract covers a customer's fleet of one manufacturer's machines, not a
# catalogue. Each covered installation costs queries at every acceptance, so an
# unbounded list is a request that can hold row locks for as long as it likes.
MAX_COVERED_INSTALLATIONS = 200

HISTORY_ENTITY_TYPES = ("service_contract", "contract_term_version",
                        "contract_statement", "contract_dispute")

NOT_FOUND = "Contract not found"
WITHHELD = {
    "withheld": True,
    "reason": "sharing withdrawn by factory",
    "message": ("This factory does not currently share downtime with you "
                "(SHARE_DOWNTIME). Statement content and statement actions are "
                "withheld until it does; the contract terms stay visible."),
}

_TS = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_MONTH = re.compile(r"\A([0-9]{4})-(0[1-9]|1[0-2])\Z")


class Refused(HTTPException):
    """A contract rule said no. An HTTPException so the routes stay thin; the
    status is the rule's, never a generic 400."""

    def __init__(self, status_code, detail):
        super().__init__(status_code=status_code, detail=detail)


def utcnow():
    """The one clock of the contract routes: naive UTC, whole seconds."""
    return canonical.utc_seconds(datetime.utcnow())


# ── Parties ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Party:
    side: str                 # OEM | FACTORY
    oem_code: str             # set for an OEM party
    tenant_code: str          # set for a FACTORY party
    actor: str                # the username
    role: str

    @property
    def audit_actor(self):
        if self.side == OEM:
            return f"oem:{self.oem_code}:{self.actor}"
        return self.actor

    def for_engine(self):
        """What compute_statement records as the computing party: the side, and
        the same actor label every other contract audit row carries."""
        return SimpleNamespace(side=self.side, actor=self.audit_actor)

    def audit_tenant(self):
        """The tenant this party's own audit rows and history live in."""
        if self.side == OEM:
            return oem_auth.sentinel_tenant(self.oem_code)
        return self.tenant_code


def for_oem(principal):
    return Party(OEM, principal["oem"], None, principal["username"], principal["role"])


def for_factory(current_user):
    tenant = tenancy.request_tenant(current_user)
    # get_current_user already refuses OEM tokens; this refuses a sentinel that
    # arrived any other way, because acting "as" OEM:<code> on the factory side
    # would let a manufacturer accept its own contract.
    if not tenant or oem_auth.is_sentinel(tenant):
        raise Refused(403, "Not a factory session")
    actor = current_user.get("sub") or current_user.get("username") or "?"
    return Party(FACTORY, None, tenant, actor, current_user.get("role"))


def other_side(side):
    return FACTORY if side == OEM else OEM


# ── Small helpers ────────────────────────────────────────────────────

def _ts(dt):
    return canonical.ts(canonical.utc_seconds(dt)) if dt is not None else None


def parse_ts(text, field):
    if type(text) is not str or not _TS.match(text):
        raise Refused(422, {"field": field,
                            "message": "must be a UTC instant YYYY-MM-DDTHH:MM:SSZ"})
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise Refused(422, {"field": field, "message": "is not a real instant"}) from None


def _parse_terms(raw, status=422):
    try:
        return contract_terms.parse(raw)
    except contract_terms.TermsError as e:
        raise Refused(status, {"field": e.field, "message": e.msg}) from None


def _terms_of(version):
    return _parse_terms(version.terms_json, status=500)


def _engine():
    """The attribution engine, imported when first needed.

    Lazily, so a build without it still boots; a request that needs it gets an
    honest 503 rather than a statement invented by something else."""
    try:
        import contract_statements
    except ModuleNotFoundError as e:
        if e.name != "contract_statements":
            raise
        raise Refused(503, "The downtime attribution engine is not installed in "
                           "this build") from None
    return contract_statements


def _audit(db, party, contract, action, entity_type, entity_id, details,
           sides=SIDES):
    """One audit row per named party, inside the caller's transaction."""
    for side in sides:
        tenant = (contract.factory_tenant_code if side == FACTORY
                  else oem_auth.sentinel_tenant(contract.oem_code))
        platform_routes.log_audit(db, party.audit_actor, action, entity_type,
                                  entity_id, details, tenant_code=tenant,
                                  commit=False)


def _publish(db, event):
    oem_events.publish(event_bus, db, event)


def _commit_or_conflict(db, message):
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise Refused(409, message) from None


# ── Reading contracts ────────────────────────────────────────────────

def _contract_query(db, party):
    q = db.query(models.ServiceContract)
    if party.side == OEM:
        return q.filter(models.ServiceContract.oem_code == party.oem_code)
    return q.filter(models.ServiceContract.factory_tenant_code == party.tenant_code,
                    models.ServiceContract.proposed_at.isnot(None),
                    models.ServiceContract.status != DRAFT)


def _row_id(value):
    """An id that could be a row: an int in 1..MAX_ROW_ID (bool is not an id)."""
    return type(value) is int and 1 <= value <= MAX_ROW_ID


def get_contract(db, party, contract_id):
    """The contract if this party may see it, else None (-> one 404)."""
    if not _row_id(contract_id):
        return None
    return (_contract_query(db, party)
            .filter(models.ServiceContract.id == contract_id).first())


def require_contract(db, party, contract_id, *, for_update=False):
    if not _row_id(contract_id):
        raise Refused(404, NOT_FOUND)
    q = _contract_query(db, party).filter(models.ServiceContract.id == contract_id)
    if for_update:
        q = q.with_for_update()
    contract = q.first()
    if contract is None:
        raise Refused(404, NOT_FOUND)
    return contract


def _versions(db, contract_id):
    return (db.query(models.ServiceContractTermVersion)
              .filter(models.ServiceContractTermVersion.contract_id == contract_id)
              .order_by(models.ServiceContractTermVersion.version.asc()).all())


def _version(db, contract_id, number):
    return (db.query(models.ServiceContractTermVersion)
              .filter(models.ServiceContractTermVersion.contract_id == contract_id,
                      models.ServiceContractTermVersion.version == number).first())


def _version_visible(version, party):
    """A version is its author's alone until it is proposed."""
    return version.proposed_at is not None or version.proposed_by_party == party.side


def _grid_terms(db, contract):
    """Version 1's terms: the period grid, fixed for the contract's life."""
    return _terms_of(_version(db, contract.id, 1))


def accepted_versions(db, contract_id):
    return (db.query(models.ServiceContractTermVersion)
              .filter(models.ServiceContractTermVersion.contract_id == contract_id,
                      models.ServiceContractTermVersion.status == ACCEPTED)
              .order_by(models.ServiceContractTermVersion.version.asc()).all())


def governing_version(versions, instant):
    """The accepted version with the highest number whose effective_from <= instant."""
    best = None
    for v in versions:
        if v.status == ACCEPTED and v.effective_from <= instant \
                and (best is None or v.version > best.version):
            best = v
    return best


def contract_state(contract, now):
    if contract.status not in BINDING_STATUSES:
        return contract.status
    if now < contract.starts_at:
        return "not_started"
    if now >= contract_periods.contract_effective_end(contract):
        return "ended"
    return "active"


def version_view(version):
    return {
        "version": version.version,
        "status": version.status,
        "terms_hash": version.terms_hash,
        "terms": json.loads(version.terms_json),
        "effective_from": _ts(version.effective_from),
        "proposed_by_party": version.proposed_by_party,
        "proposed_by": version.proposed_by,
        "proposed_at": _ts(version.proposed_at),
        "oem_accepted_by": version.oem_accepted_by,
        "oem_accepted_at": _ts(version.oem_accepted_at),
        "oem_accepted_hash": version.oem_accepted_hash,
        "factory_accepted_by": version.factory_accepted_by,
        "factory_accepted_at": _ts(version.factory_accepted_at),
        "factory_accepted_hash": version.factory_accepted_hash,
        "decision_note": version.decision_note,
    }


def _summary(contract, now):
    return {
        "id": contract.id,
        "contract_ref": contract.contract_ref,
        "title": contract.title,
        "contract_type": contract.contract_type,
        "oem_code": contract.oem_code,
        "factory_tenant_code": contract.factory_tenant_code,
        "status": contract.status,
        "state": contract_state(contract, now),
        "starts_at": _ts(contract.starts_at),
        "ends_at": _ts(contract.ends_at),
        "effective_end": _ts(contract_periods.contract_effective_end(contract)),
        "termination_effective_at": _ts(contract.termination_effective_at),
    }


def contract_view(db, party, contract, now=None):
    now = now or utcnow()
    out = _summary(contract, now)
    out.update({
        "created_by": contract.created_by,
        "created_at": _ts(contract.created_at),
        "proposed_at": _ts(contract.proposed_at),
        "factory_accepted_by": contract.factory_accepted_by,
        "factory_accepted_at": _ts(contract.factory_accepted_at),
        "terminated_by_party": contract.terminated_by_party,
        "terminated_by": contract.terminated_by,
        "termination_reason": contract.termination_reason,
        "updated_at": _ts(contract.updated_at),
        "downtime_shared": oem_sharing.SHARE_DOWNTIME in oem_sharing.grants_for(
            db, contract.oem_code, contract.factory_tenant_code),
        "versions": [version_view(v) for v in _versions(db, contract.id)
                     if _version_visible(v, party)],
    })
    return out


def list_contracts(db, party):
    now = utcnow()
    rows = (_contract_query(db, party)
              .order_by(models.ServiceContract.id.asc()).limit(MAX_LIST).all())
    return {"contracts": [_summary(c, now) for c in rows]}


# ── Coverage ─────────────────────────────────────────────────────────

def coverage_ranges(db, contract):
    """{installation_id: [(start, end), ...]} this BINDING contract covers.

    Walks the period grid: each period is governed by one accepted version, and
    an installation is covered over that period (up to its coverage_ended_at)
    iff the governing version snapshotted it."""
    if contract.status not in BINDING_STATUSES:
        return {}
    versions = accepted_versions(db, contract.id)
    if not versions:
        return {}
    rows = (db.query(models.ServiceContractMachine)
              .filter(models.ServiceContractMachine.term_version_id.in_(
                  [v.id for v in versions])).all())
    by_version = {}
    for r in rows:
        by_version.setdefault(r.term_version_id, []).append(r)
    grid = _grid_terms(db, contract)
    out = {}
    for p in contract_periods.periods(grid, contract.starts_at,
                                      contract_periods.contract_effective_end(contract)):
        gov = governing_version(versions, p.start)
        if gov is None:
            continue
        for r in by_version.get(gov.id, []):
            end = p.end if r.coverage_ended_at is None else min(p.end, r.coverage_ended_at)
            if end <= p.start:
                continue
            spans = out.setdefault(r.installation_id, [])
            if spans and spans[-1][1] == p.start:
                spans[-1] = (spans[-1][0], end)
            else:
                spans.append((p.start, end))
    return out


def _covered_elsewhere(db, contract, installation_ids, start, end):
    """Installation ids another binding contract covers anywhere in [start, end)."""
    if not installation_ids or start >= end:
        return set()
    others = (db.query(models.ServiceContract)
                .join(models.ServiceContractTermVersion,
                      models.ServiceContractTermVersion.contract_id
                      == models.ServiceContract.id)
                .join(models.ServiceContractMachine,
                      models.ServiceContractMachine.term_version_id
                      == models.ServiceContractTermVersion.id)
                .filter(models.ServiceContractMachine.installation_id.in_(installation_ids),
                        models.ServiceContractTermVersion.status == ACCEPTED,
                        models.ServiceContract.status.in_(BINDING_STATUSES),
                        models.ServiceContract.id != contract.id)
                .distinct().order_by(models.ServiceContract.id.asc()).all())
    clash = set()
    for other in others:
        ranges = coverage_ranges(db, other)
        for iid in installation_ids:
            if any(s < end and start < e for s, e in ranges.get(iid, [])):
                clash.add(iid)
    return clash


def _check_owned(db, oem_code, tenant, terms, status):
    """Every covered installation is THIS manufacturer's, at THAT factory, with
    the serial the terms name. One message for every failure: a competitor's
    installation id must look exactly like one that does not exist."""
    if len(terms.covered_installations) > MAX_COVERED_INSTALLATIONS:
        raise Refused(422, {"field": "covered_installations",
                            "message": f"at most {MAX_COVERED_INSTALLATIONS} "
                                       "installations per contract"})
    for c in terms.covered_installations:
        rows = oem_sharing.installations_for(db, oem_code, tenant_code=tenant,
                                             installation_id=c.installation_id)
        if not rows or rows[0].serial_number != c.serial_number:
            raise Refused(status, {
                "field": "covered_installations",
                "message": (f"installation {c.installation_id} ({c.serial_number}) "
                            f"is not this manufacturer's equipment at {tenant}")})


def _lock_and_check_coverage(db, contract, terms, start, end):
    """Lock the covered installation rows (id order) and re-check each at the
    moment of acceptance: this factory's, this manufacturer's, linked to a
    machine that is really this factory's, and not covered by another binding
    contract over [start, end). Returns {installation_id: installation}."""
    ids = sorted(c.installation_id for c in terms.covered_installations)
    rows = (db.query(models.MachineInstallation)
              .filter(models.MachineInstallation.id.in_(ids))
              .order_by(models.MachineInstallation.id.asc())
              .with_for_update().all())
    by_id = {r.id: r for r in rows}
    problems = []
    with oem_sharing.bound_factory_read(contract.factory_tenant_code):
        for c in terms.covered_installations:
            r = by_id.get(c.installation_id)
            if (r is None or r.oem_code != contract.oem_code
                    or r.factory_tenant_code != contract.factory_tenant_code
                    or r.serial_number != c.serial_number):
                problems.append(f"{c.serial_number} is no longer this manufacturer's "
                                f"equipment at {contract.factory_tenant_code}")
                continue
            machine = None
            if r.machine_id is not None:
                machine = (db.query(models.Machine)
                             .filter(models.Machine.id == r.machine_id,
                                     models.Machine.tenant_code
                                     == contract.factory_tenant_code).first())
            if machine is None:
                problems.append(f"{c.serial_number} is not linked to a machine on the "
                                "shop floor (link it under Connected Equipment)")
    if problems:
        raise Refused(409, {"message": "Coverage cannot be accepted",
                            "problems": problems})
    clash = _covered_elsewhere(db, contract, ids, start, end)
    if clash:
        serials = [c.serial_number for c in terms.covered_installations
                   if c.installation_id in clash]
        raise Refused(409, {"message": "A machine can be covered by one contract at "
                                       "a time",
                            "problems": [f"{s} is already covered by another contract "
                                         "over these months" for s in serials]})
    return by_id


def _snapshot_coverage(db, version, terms, installations):
    for c in terms.covered_installations:
        inst = installations[c.installation_id]
        db.add(models.ServiceContractMachine(
            term_version_id=version.id, installation_id=inst.id,
            machine_id_at_acceptance=inst.machine_id,
            factory_tenant_at_acceptance=inst.factory_tenant_code,
            serial_number=inst.serial_number))


# ── Drafting and proposing (the manufacturer) ────────────────────────

def _validated_draft(db, party, body):
    if body.contract_type not in CONTRACT_TYPES:
        raise Refused(422, {"field": "contract_type",
                            "message": "must be one of " + ", ".join(CONTRACT_TYPES)})
    m = _MONTH.match(body.start_month) if type(body.start_month) is str else None
    if m is None or int(m.group(1)) < 2000:
        raise Refused(422, {"field": "start_month", "message": "must be YYYY-MM"})
    tenant = body.factory_tenant_code.strip()
    if not tenant or tenancy.is_reserved_tenant_code(tenant):
        raise Refused(422, {"field": "factory_tenant_code",
                            "message": "is not a factory"})
    ref, title = body.contract_ref.strip(), body.title.strip()
    if not ref or not title:
        raise Refused(422, {"field": "contract_ref" if not ref else "title",
                            "message": "must not be blank"})
    terms = _parse_terms(body.terms)
    _check_owned(db, party.oem_code, tenant, terms, 422)
    year, month = int(m.group(1)), int(m.group(2))
    starts_at = contract_periods.month_start_utc(terms, year, month)
    ends_at = contract_periods.month_start_utc(terms, year, month + terms.term_months)
    return ref, title, tenant, terms, starts_at, ends_at


def _ref_taken(db, oem_code, ref, exclude_id=None):
    q = (db.query(models.ServiceContract)
           .filter(models.ServiceContract.oem_code == oem_code,
                   models.ServiceContract.contract_ref == ref))
    if exclude_id is not None:
        q = q.filter(models.ServiceContract.id != exclude_id)
    return q.first() is not None


def create_draft(db, party, body):
    now = utcnow()
    ref, title, tenant, terms, starts_at, ends_at = _validated_draft(db, party, body)
    if _ref_taken(db, party.oem_code, ref):
        raise Refused(409, f"You already have a contract with reference {ref}")
    contract = models.ServiceContract(
        oem_code=party.oem_code, factory_tenant_code=tenant, contract_ref=ref,
        title=title, contract_type=body.contract_type, status=DRAFT,
        starts_at=starts_at, ends_at=ends_at, created_by=party.actor,
        created_at=now, updated_at=now)
    db.add(contract)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise Refused(409, f"You already have a contract with reference {ref}") from None
    version = models.ServiceContractTermVersion(
        contract_id=contract.id, version=1,
        terms_json=contract_terms.terms_json_text(terms),
        terms_hash=contract_terms.terms_hash(terms), effective_from=starts_at,
        status=DRAFT, proposed_by_party=OEM, proposed_by=party.actor)
    db.add(version)
    db.flush()
    _audit(db, party, contract, "contract_drafted", "service_contract", contract.id,
           f"ref={ref} factory={tenant} version=1 terms_hash={version.terms_hash}",
           sides=(OEM,))
    _commit_or_conflict(db, f"You already have a contract with reference {ref}")
    return contract_view(db, party, contract, now)


def edit_draft(db, party, contract_id, body):
    now = utcnow()
    contract = require_contract(db, party, contract_id)
    if contract.status != DRAFT:
        raise Refused(409, "Only a draft can be edited; propose an amendment instead")
    ref, title, tenant, terms, starts_at, ends_at = _validated_draft(db, party, body)
    if _ref_taken(db, party.oem_code, ref, exclude_id=contract.id):
        raise Refused(409, f"You already have a contract with reference {ref}")
    new_hash = contract_terms.terms_hash(terms)
    try:
        rc = db.execute(
            update(models.ServiceContract)
            .where(models.ServiceContract.id == contract.id,
                   models.ServiceContract.status == DRAFT)
            .values(contract_ref=ref, title=title, contract_type=body.contract_type,
                    factory_tenant_code=tenant, starts_at=starts_at, ends_at=ends_at,
                    updated_at=now)
            .execution_options(synchronize_session=False)).rowcount
    except IntegrityError:
        db.rollback()
        raise Refused(409, f"You already have a contract with reference {ref}") from None
    rv = db.execute(
        update(models.ServiceContractTermVersion)
        .where(models.ServiceContractTermVersion.contract_id == contract.id,
               models.ServiceContractTermVersion.version == 1,
               models.ServiceContractTermVersion.status == DRAFT)
        .values(terms_json=contract_terms.terms_json_text(terms), terms_hash=new_hash,
                effective_from=starts_at)
        .execution_options(synchronize_session=False)).rowcount
    if rc != 1 or rv != 1:
        db.rollback()
        raise Refused(409, "The draft changed while you were editing it")
    _audit(db, party, contract, "contract_draft_edited", "service_contract",
           contract.id, f"ref={ref} factory={tenant} version=1 terms_hash={new_hash}",
           sides=(OEM,))
    _commit_or_conflict(db, f"You already have a contract with reference {ref}")
    db.refresh(contract)
    return contract_view(db, party, contract, now)


def propose_contract(db, party, contract_id, terms_hash):
    now = utcnow()
    contract = require_contract(db, party, contract_id)
    if contract.status != DRAFT:
        raise Refused(409, f"This contract is {contract.status}, not a draft")
    v1 = _version(db, contract.id, 1)
    if v1.terms_hash != terms_hash:
        raise Refused(409, {"message": "These are not the terms you are proposing; "
                                       "reload and review them",
                            "terms_hash": v1.terms_hash})
    _check_owned(db, contract.oem_code, contract.factory_tenant_code,
                 _terms_of(v1), 409)
    rc = db.execute(
        update(models.ServiceContract)
        .where(models.ServiceContract.id == contract.id,
               models.ServiceContract.status == DRAFT)
        .values(status=PROPOSED, proposed_at=now, updated_at=now)
        .execution_options(synchronize_session=False)).rowcount
    rv = db.execute(
        update(models.ServiceContractTermVersion)
        .where(models.ServiceContractTermVersion.id == v1.id,
               models.ServiceContractTermVersion.status == DRAFT,
               models.ServiceContractTermVersion.terms_hash == terms_hash)
        .values(status=PROPOSED, proposed_at=now, proposed_by=party.actor,
                oem_accepted_by=party.actor, oem_accepted_at=now,
                oem_accepted_hash=terms_hash)
        .execution_options(synchronize_session=False)).rowcount
    if rc != 1 or rv != 1:
        db.rollback()
        raise Refused(409, "The contract changed while you were proposing it")
    _audit(db, party, contract, "contract_proposed", "service_contract", contract.id,
           f"ref={contract.contract_ref} version=1 terms_hash={terms_hash}")
    _publish(db, oem_events.ContractProposed(
        tenant_code=contract.factory_tenant_code, oem_code=contract.oem_code,
        contract_id=contract.id, contract_ref=contract.contract_ref,
        title=contract.title))
    db.commit()
    db.refresh(contract)
    return contract_view(db, party, contract, now)


def withdraw_contract(db, party, contract_id):
    now = utcnow()
    contract = require_contract(db, party, contract_id)
    before = contract.status
    if before not in (DRAFT, PROPOSED):
        raise Refused(409, f"A {before} contract cannot be withdrawn")
    rc = db.execute(
        update(models.ServiceContract)
        .where(models.ServiceContract.id == contract.id,
               models.ServiceContract.status == before)
        .values(status=WITHDRAWN, updated_at=now)
        .execution_options(synchronize_session=False)).rowcount
    if rc != 1:
        db.rollback()
        raise Refused(409, "The contract changed before it could be withdrawn")
    db.execute(
        update(models.ServiceContractTermVersion)
        .where(models.ServiceContractTermVersion.contract_id == contract.id,
               models.ServiceContractTermVersion.version == 1,
               models.ServiceContractTermVersion.status == before)
        .values(status=WITHDRAWN)
        .execution_options(synchronize_session=False))
    # A draft was never shown to the factory, so its withdrawal is not either.
    _audit(db, party, contract, "contract_withdrawn", "service_contract", contract.id,
           f"ref={contract.contract_ref} {before} -> withdrawn",
           sides=(OEM,) if before == DRAFT else SIDES)
    db.commit()
    db.refresh(contract)
    return contract_view(db, party, contract, now)


# ── Accepting or rejecting (the factory) ─────────────────────────────

GRANT_REQUIRED = {
    "field": "grant_downtime_sharing",
    "message": ("Accepting this contract shares downtime with the manufacturer, so "
                "it needs grant_downtime_sharing: true. The manufacturer will see, "
                "for the covered machines only: the per-source machine status "
                "history inside the covered hours, and the downtime reason text "
                "your team logs near each down episode. The grant (SHARE_DOWNTIME) "
                "covers your whole relationship with this manufacturer and can be "
                "withdrawn under Connected Equipment, which withholds statements "
                "from them."),
}


def accept_contract(db, party, contract_id, terms_hash, grant):
    now = utcnow()
    contract = require_contract(db, party, contract_id)
    if grant is not True:
        raise Refused(422, GRANT_REQUIRED)
    if contract.status != PROPOSED:
        raise Refused(409, f"This contract is {contract.status}, not awaiting acceptance")
    v1 = _version(db, contract.id, 1)
    if v1.status != PROPOSED or v1.terms_hash != terms_hash \
            or v1.oem_accepted_hash != terms_hash:
        raise Refused(409, {"message": "These are not the terms on offer; reload and "
                                       "review them",
                            "terms_hash": v1.terms_hash})
    terms = _terms_of(v1)
    installations = _lock_and_check_coverage(db, contract, terms, contract.starts_at,
                                             contract.ends_at)
    # Contract row before version row: the same order as withdraw, so the two
    # cannot deadlock, and the database (not Python) decides which one won.
    rc = db.execute(
        update(models.ServiceContract)
        .where(models.ServiceContract.id == contract.id,
               models.ServiceContract.status == PROPOSED,
               models.ServiceContract.factory_tenant_code == party.tenant_code)
        .values(status=ACCEPTED, factory_accepted_by=party.actor,
                factory_accepted_at=now, updated_at=now)
        .execution_options(synchronize_session=False)).rowcount
    rv = 0
    if rc == 1:
        rv = db.execute(
            update(models.ServiceContractTermVersion)
            .where(models.ServiceContractTermVersion.id == v1.id,
                   models.ServiceContractTermVersion.status == PROPOSED,
                   models.ServiceContractTermVersion.terms_hash == terms_hash,
                   models.ServiceContractTermVersion.oem_accepted_hash == terms_hash)
            .values(status=ACCEPTED, factory_accepted_by=party.actor,
                    factory_accepted_at=now, factory_accepted_hash=terms_hash)
            .execution_options(synchronize_session=False)).rowcount
    if rc != 1 or rv != 1:
        db.rollback()
        raise Refused(409, "The contract changed before it could be accepted")
    _snapshot_coverage(db, v1, terms, installations)
    oem_sharing.widen_grants(db, contract.oem_code, contract.factory_tenant_code,
                             [oem_sharing.SHARE_DOWNTIME], party.actor,
                             context=f"accepting service contract {contract.contract_ref}")
    _audit(db, party, contract, "contract_accepted", "service_contract", contract.id,
           f"ref={contract.contract_ref} version=1 terms_hash={terms_hash} "
           f"installations={len(installations)} grant=SHARE_DOWNTIME")
    _publish(db, oem_events.ContractAccepted(
        tenant_code=contract.factory_tenant_code, oem_code=contract.oem_code,
        contract_id=contract.id, contract_ref=contract.contract_ref,
        terms_hash=terms_hash, installations=len(installations)))
    db.commit()
    db.refresh(contract)
    return contract_view(db, party, contract, now)


def reject_contract(db, party, contract_id, note):
    now = utcnow()
    contract = require_contract(db, party, contract_id)
    if contract.status != PROPOSED:
        raise Refused(409, f"This contract is {contract.status}, not awaiting acceptance")
    rc = db.execute(
        update(models.ServiceContract)
        .where(models.ServiceContract.id == contract.id,
               models.ServiceContract.status == PROPOSED)
        .values(status=REJECTED, updated_at=now)
        .execution_options(synchronize_session=False)).rowcount
    if rc != 1:
        db.rollback()
        raise Refused(409, "The contract changed before it could be rejected")
    db.execute(
        update(models.ServiceContractTermVersion)
        .where(models.ServiceContractTermVersion.contract_id == contract.id,
               models.ServiceContractTermVersion.version == 1,
               models.ServiceContractTermVersion.status == PROPOSED)
        .values(status=REJECTED, decision_note=note or None)
        .execution_options(synchronize_session=False))
    _audit(db, party, contract, "contract_rejected", "service_contract", contract.id,
           f"ref={contract.contract_ref} proposed -> rejected")
    db.commit()
    db.refresh(contract)
    return contract_view(db, party, contract, now)


# ── Termination (either party) ───────────────────────────────────────

def terminate_contract(db, party, contract_id, reason):
    now = utcnow()
    contract = require_contract(db, party, contract_id)
    if contract.status != ACCEPTED:
        raise Refused(409, f"A {contract.status} contract cannot be terminated")
    if now >= contract.ends_at:
        raise Refused(409, "This contract has already ended")
    grid = _grid_terms(db, contract)
    gov = governing_version(accepted_versions(db, contract.id), now)
    notice_days = (_terms_of(gov) if gov is not None else grid).termination_notice_days
    effective = contract_periods.boundary_at_or_after(
        grid, contract.starts_at, now + timedelta(days=notice_days))
    if effective >= contract.ends_at:
        raise Refused(409, {
            "message": (f"With {notice_days} days' notice the termination would take "
                        f"effect at or after the contract's end "
                        f"({_ts(contract.ends_at)}); the contract simply ends then"),
            "earliest_effective": _ts(effective)})
    rc = db.execute(
        update(models.ServiceContract)
        .where(models.ServiceContract.id == contract.id,
               models.ServiceContract.status == ACCEPTED,
               models.ServiceContract.termination_effective_at.is_(None))
        .values(status=TERMINATED, termination_effective_at=effective,
                terminated_by_party=party.side, terminated_by=party.actor,
                termination_reason=reason.strip(), updated_at=now)
        .execution_options(synchronize_session=False)).rowcount
    if rc != 1:
        db.rollback()
        raise Refused(409, "The contract changed before it could be terminated")
    _audit(db, party, contract, "contract_terminated", "service_contract", contract.id,
           f"ref={contract.contract_ref} by={party.side} "
           f"effective={_ts(effective)} notice_days={notice_days}")
    db.commit()
    db.refresh(contract)
    return contract_view(db, party, contract, now)


# ── Periods ──────────────────────────────────────────────────────────

def _statements(db, contract_id):
    return (db.query(models.ContractStatement)
              .filter(models.ContractStatement.contract_id == contract_id)
              .order_by(models.ContractStatement.period_start.asc()).all())


def periods_view(db, party, contract):
    grid = _grid_terms(db, contract)
    versions = accepted_versions(db, contract.id)
    statements = {s.period_start: s for s in _statements(db, contract.id)}
    engine = _engine() if statements else None
    out = []
    for p in contract_periods.periods(grid, contract.starts_at,
                                      contract_periods.contract_effective_end(contract)):
        gov = governing_version(versions, p.start)
        st = statements.get(p.start)
        summary = None
        if st is not None:
            summary = {"id": st.id, "revision": st.revision,
                       "agreed": engine.acceptance_state(db, st)["agreed"]}
        out.append({"start": _ts(p.start), "end": _ts(p.end),
                    "terms_version": gov.version if gov is not None else None,
                    "statement": summary})
    return {"contract_id": contract.id, "timezone": grid.timezone,
            "period_months": grid.period_months, "periods": out}


# ── Amendments (either party drafts; the other accepts) ──────────────

def _agreed_floor(db, contract):
    """The earliest instant an amendment may take effect: the end of the latest
    period whose statement both parties have agreed (else the contract start)."""
    floor = contract.starts_at
    statements = _statements(db, contract.id)
    if not statements:
        return floor
    engine = _engine()
    for st in statements:
        if st.period_end > floor and engine.acceptance_state(db, st)["agreed"]:
            floor = st.period_end
    return floor


def _check_effective_from(db, contract, grid, effective_from, status):
    end = contract_periods.contract_effective_end(contract)
    if not contract_periods.is_boundary(grid, contract.starts_at, effective_from) \
            or effective_from >= end:
        raise Refused(status, {"field": "effective_from",
                               "message": "must be a period boundary of this contract, "
                                          "before its end"})
    floor = _agreed_floor(db, contract)
    if effective_from < floor:
        raise Refused(status, {"field": "effective_from",
                               "message": (f"a statement both parties agreed runs "
                                           f"until {_ts(floor)}; an amendment cannot "
                                           "reach back over it")})


def draft_amendment(db, party, contract_id, body):
    contract = require_contract(db, party, contract_id)
    if contract.status != ACCEPTED:
        raise Refused(409, f"A {contract.status} contract cannot be amended")
    versions = _versions(db, contract.id)
    if any(v.status in (DRAFT, PROPOSED) for v in versions):
        raise Refused(409, "Another amendment is pending; decide or withdraw it first")
    terms = _parse_terms(body.terms)
    grid = _grid_terms(db, contract)
    for f in GRID_FIELDS:
        if getattr(terms, f) != getattr(grid, f):
            raise Refused(422, {"field": f,
                                "message": "cannot be amended: it fixes the period grid "
                                           "every statement is cut on"})
    effective_from = parse_ts(body.effective_from, "effective_from")
    _check_effective_from(db, contract, grid, effective_from, 422)
    _check_owned(db, contract.oem_code, contract.factory_tenant_code, terms, 422)
    number = max(v.version for v in versions) + 1
    version = models.ServiceContractTermVersion(
        contract_id=contract.id, version=number,
        terms_json=contract_terms.terms_json_text(terms),
        terms_hash=contract_terms.terms_hash(terms), effective_from=effective_from,
        status=DRAFT, proposed_by_party=party.side, proposed_by=party.actor)
    db.add(version)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise Refused(409, "Another amendment was drafted at the same time") from None
    _audit(db, party, contract, "contract_amendment_drafted", "service_contract",
           contract.id, f"ref={contract.contract_ref} version={number} "
                        f"effective_from={_ts(effective_from)} "
                        f"terms_hash={version.terms_hash}",
           sides=(party.side,))
    _commit_or_conflict(db, "Another amendment was drafted at the same time")
    return version_view(version)


def _require_version(db, party, contract, number):
    if not _row_id(number):
        raise Refused(404, "Amendment not found")
    version = _version(db, contract.id, number)
    if version is None or version.version == 1 or not _version_visible(version, party):
        raise Refused(404, "Amendment not found")
    return version


def propose_amendment(db, party, contract_id, number, terms_hash):
    now = utcnow()
    contract = require_contract(db, party, contract_id)
    version = _require_version(db, party, contract, number)
    if version.proposed_by_party != party.side:
        raise Refused(403, "Only the party that drafted an amendment proposes it")
    if version.status != DRAFT:
        raise Refused(409, f"This amendment is {version.status}, not a draft")
    if version.terms_hash != terms_hash:
        raise Refused(409, {"message": "These are not the terms you are proposing",
                            "terms_hash": version.terms_hash})
    if contract.status != ACCEPTED:
        raise Refused(409, f"A {contract.status} contract cannot be amended")
    _check_effective_from(db, contract, _grid_terms(db, contract),
                          version.effective_from, 409)
    mine = ({"oem_accepted_by": party.actor, "oem_accepted_at": now,
             "oem_accepted_hash": terms_hash} if party.side == OEM else
            {"factory_accepted_by": party.actor, "factory_accepted_at": now,
             "factory_accepted_hash": terms_hash})
    rv = db.execute(
        update(models.ServiceContractTermVersion)
        .where(models.ServiceContractTermVersion.id == version.id,
               models.ServiceContractTermVersion.status == DRAFT,
               models.ServiceContractTermVersion.terms_hash == terms_hash)
        .values(status=PROPOSED, proposed_at=now, proposed_by=party.actor, **mine)
        .execution_options(synchronize_session=False)).rowcount
    if rv != 1:
        db.rollback()
        raise Refused(409, "The amendment changed before it could be proposed")
    _audit(db, party, contract, "contract_amendment_proposed", "service_contract",
           contract.id, f"ref={contract.contract_ref} version={version.version} "
                        f"by={party.side} effective_from={_ts(version.effective_from)} "
                        f"terms_hash={terms_hash}")
    _publish(db, oem_events.AmendmentProposed(
        tenant_code=contract.factory_tenant_code, oem_code=contract.oem_code,
        contract_id=contract.id, contract_ref=contract.contract_ref,
        version=version.version, proposed_by_party=party.side,
        effective_from=_ts(version.effective_from)))
    db.commit()
    db.refresh(version)
    return version_view(version)


def accept_amendment(db, party, contract_id, number, terms_hash):
    now = utcnow()
    contract = require_contract(db, party, contract_id)
    version = _require_version(db, party, contract, number)
    if version.proposed_by_party == party.side:
        raise Refused(403, "The party that proposed an amendment cannot also accept it")
    if version.status != PROPOSED:
        raise Refused(409, f"This amendment is {version.status}, not awaiting acceptance")
    proposer_hash = (version.oem_accepted_hash if version.proposed_by_party == OEM
                     else version.factory_accepted_hash)
    if version.terms_hash != terms_hash or proposer_hash != terms_hash:
        raise Refused(409, {"message": "These are not the terms on offer; reload and "
                                       "review them",
                            "terms_hash": version.terms_hash})
    if contract.status != ACCEPTED:
        raise Refused(409, f"A {contract.status} contract cannot be amended")
    _check_effective_from(db, contract, _grid_terms(db, contract),
                          version.effective_from, 409)
    terms = _terms_of(version)
    installations = _lock_and_check_coverage(
        db, contract, terms, version.effective_from,
        contract_periods.contract_effective_end(contract))
    rc = db.execute(
        update(models.ServiceContract)
        .where(models.ServiceContract.id == contract.id,
               models.ServiceContract.status == ACCEPTED)
        .values(updated_at=now)
        .execution_options(synchronize_session=False)).rowcount
    proposer_column = (models.ServiceContractTermVersion.oem_accepted_hash
                       if version.proposed_by_party == OEM else
                       models.ServiceContractTermVersion.factory_accepted_hash)
    mine = ({"oem_accepted_by": party.actor, "oem_accepted_at": now,
             "oem_accepted_hash": terms_hash} if party.side == OEM else
            {"factory_accepted_by": party.actor, "factory_accepted_at": now,
             "factory_accepted_hash": terms_hash})
    rv = 0
    if rc == 1:
        rv = db.execute(
            update(models.ServiceContractTermVersion)
            .where(models.ServiceContractTermVersion.id == version.id,
                   models.ServiceContractTermVersion.status == PROPOSED,
                   models.ServiceContractTermVersion.terms_hash == terms_hash,
                   proposer_column == terms_hash)
            .values(status=ACCEPTED, **mine)
            .execution_options(synchronize_session=False)).rowcount
    if rc != 1 or rv != 1:
        db.rollback()
        raise Refused(409, "The amendment changed before it could be accepted")
    _snapshot_coverage(db, version, terms, installations)
    _audit(db, party, contract, "contract_amendment_accepted", "service_contract",
           contract.id, f"ref={contract.contract_ref} version={version.version} "
                        f"by={party.side} effective_from={_ts(version.effective_from)} "
                        f"terms_hash={terms_hash}")
    db.commit()
    db.refresh(version)
    return version_view(version)


def reject_amendment(db, party, contract_id, number, note):
    """The other party rejects a proposal; its author withdraws a draft or proposal."""
    contract = require_contract(db, party, contract_id)
    version = _require_version(db, party, contract, number)
    before = version.status
    authored = version.proposed_by_party == party.side
    if authored and before in (DRAFT, PROPOSED):
        after, action = WITHDRAWN, "contract_amendment_withdrawn"
        sides = (party.side,) if before == DRAFT else SIDES
    elif not authored and before == PROPOSED:
        after, action, sides = REJECTED, "contract_amendment_rejected", SIDES
    else:
        raise Refused(409, f"This amendment is {before}; there is nothing to decide")
    rv = db.execute(
        update(models.ServiceContractTermVersion)
        .where(models.ServiceContractTermVersion.id == version.id,
               models.ServiceContractTermVersion.status == before)
        .values(status=after, decision_note=note or None)
        .execution_options(synchronize_session=False)).rowcount
    if rv != 1:
        db.rollback()
        raise Refused(409, "The amendment changed before it could be decided")
    _audit(db, party, contract, action, "service_contract", contract.id,
           f"ref={contract.contract_ref} version={version.version} "
           f"by={party.side} {before} -> {after}", sides=sides)
    db.commit()
    db.refresh(version)
    return version_view(version)


# ── Statements ───────────────────────────────────────────────────────

def require_statement_access(db, party, contract):
    """CONSENT, for the manufacturer. Read now, never cached: a withdrawal takes
    effect on the next request."""
    if party.side == OEM and not oem_sharing.contract_statement_visible(db, contract):
        raise Refused(403, WITHHELD)


def _require_binding(contract):
    if contract.status not in BINDING_STATUSES:
        raise Refused(409, f"A {contract.status} contract has no statements; nothing "
                           "is measured before the factory accepts it")


def require_statement(db, contract, statement_id):
    if not _row_id(statement_id):
        raise Refused(404, "Statement not found")
    st = (db.query(models.ContractStatement)
            .filter(models.ContractStatement.id == statement_id,
                    models.ContractStatement.contract_id == contract.id).first())
    if st is None:
        raise Refused(404, "Statement not found")
    return st


def _statement_ref(st):
    return {"id": st.id, "period_start": _ts(st.period_start),
            "period_end": _ts(st.period_end), "revision": st.revision,
            "content_hash": st.content_hash}


def _run_compute(db, party, contract, period_start, now, *, allow_expired=False):
    """compute_statement with the factory's evidence readable, errors mapped.

    With `allow_expired`, evidence past retention returns None and rolls
    NOTHING back: the engine refuses before it writes, and the caller's own
    change in this transaction (a dispute transition) must survive. Every other
    refusal rolls the whole transaction back before it is raised."""
    engine = _engine()
    try:
        with oem_sharing.bound_factory_read(contract.factory_tenant_code):
            result = engine.compute_statement(db, contract, period_start,
                                              party=party.for_engine(), now=now)
    except engine.PeriodNotClosed as e:
        db.rollback()
        raise Refused(409, {"message": "This period has not closed yet; statements "
                                       "are computed once late telemetry has settled",
                            "reason": "period_not_closed", "detail": str(e)}) from None
    except engine.EvidenceExpired as e:
        if allow_expired:
            return None
        db.rollback()
        raise Refused(409, {"message": "The evidence for this period has expired "
                                       "under retention; the stored revision stands",
                            "reason": "evidence_expired", "detail": str(e)}) from None
    except engine.NoTermsForPeriod as e:
        db.rollback()
        raise Refused(422, {"message": "No accepted terms govern this period",
                            "detail": str(e)}) from None
    except engine.StatementConflict:
        db.rollback()
        raise Refused(409, "The statement changed while this request was running; "
                           "reload and try again") from None
    # The engine may have revised the row with a conditional UPDATE; read back
    # what is stored rather than trusting a loaded copy.
    db.flush()
    db.refresh(result.statement)
    if result.changed:
        _publish(db, oem_events.StatementComputed(
            tenant_code=contract.factory_tenant_code, oem_code=contract.oem_code,
            contract_id=contract.id, contract_ref=contract.contract_ref,
            statement_id=result.statement.id,
            period_start=_ts(result.statement.period_start),
            revision=result.statement.revision))
    return result


def compute(db, party, contract_id, period_start_text):
    now = utcnow()
    contract = require_contract(db, party, contract_id)
    require_statement_access(db, party, contract)
    _require_binding(contract)
    start = parse_ts(period_start_text, "period_start")
    period = contract_periods.period_starting(contract, _grid_terms(db, contract), start)
    if period is None:
        raise Refused(422, {"field": "period_start",
                            "message": "is not the start of a period of this contract"})
    result = _run_compute(db, party, contract, period.start, now)
    db.commit()
    return {"statement": _statement_ref(result.statement), "changed": result.changed,
            "frozen": result.frozen}


def statement_view(db, party, contract, statement_id):
    require_statement_access(db, party, contract)
    st = require_statement(db, contract, statement_id)
    payload = _engine().statement_payload(db, st)
    payload["disputes"] = [dispute_view(d) for d in _disputes(db, contract.id, st.id)]
    return payload


def preview(db, party, contract):
    require_statement_access(db, party, contract)
    _require_binding(contract)
    engine = _engine()
    with oem_sharing.bound_factory_read(contract.factory_tenant_code):
        out = engine.preview_statement(db, contract, utcnow())
    if out is None:
        raise Refused(404, "No period of this contract is running now")
    return out


def verify(db, party, contract, statement_id):
    require_statement_access(db, party, contract)
    st = require_statement(db, contract, statement_id)
    engine = _engine()
    with oem_sharing.bound_factory_read(contract.factory_tenant_code):
        return engine.verify_statement(db, st, utcnow())


def download(db, party, contract, statement_id):
    """The exact stored bytes. Their SHA-256 is the content hash; each party
    keeps its own copy, which is the only thing that can show a rewrite."""
    require_statement_access(db, party, contract)
    st = require_statement(db, contract, statement_id)
    if st.canonical_json is None:
        raise Refused(410, "The content of this statement was removed when the "
                           "factory was offboarded; its hash and acceptances remain")
    return st, st.canonical_json.encode("utf-8")


def download_response(statement, body):
    """The exact stored bytes, with their hash and revision in headers. The
    filename is built from ids only: a contract reference is free text and has
    no business in a header."""
    return Response(
        content=body, media_type="application/json",
        headers={"X-Content-SHA256": statement.content_hash,
                 "X-Statement-Revision": str(statement.revision),
                 "Content-Disposition": (f'attachment; filename="statement-'
                                         f'{statement.contract_id}-{statement.id}-'
                                         f'r{statement.revision}.json"')})


def accept_statement(db, party, contract_id, statement_id, content_hash, revision):
    now = utcnow()
    contract = require_contract(db, party, contract_id)
    require_statement_access(db, party, contract)
    st = require_statement(db, contract, statement_id)
    engine = _engine()
    # Recompute first: an acceptance of stale evidence is an acceptance of
    # something that is no longer the statement. Past retention the stored
    # revision stands (C11).
    result = _run_compute(db, party, contract, st.period_start, now, allow_expired=True)
    if result is not None:
        st = result.statement
    state = engine.acceptance_state(db, st)
    if state["agreed"]:
        db.commit()
        raise Refused(409, "This statement is already agreed by both parties and is final")
    if content_hash != st.content_hash or revision != st.revision:
        db.commit()     # the recompute is real; persist it, accept nothing
        raise Refused(409, {"message": "The statement is not the one you reviewed; "
                                       "review the current revision",
                            "reason": "hash_mismatch",
                            "content_hash": st.content_hash, "revision": st.revision})
    if st.canonical_json is None:
        db.commit()
        raise Refused(409, "The content of this statement was removed; it cannot be "
                           "accepted")
    live = (db.query(models.ContractDispute)
              .filter(models.ContractDispute.contract_id == contract.id,
                      models.ContractDispute.statement_id == st.id,
                      models.ContractDispute.status.in_(LIVE_DISPUTE_STATUSES)).count())
    if live:
        db.commit()
        raise Refused(409, {"message": "Open disputes must be resolved or withdrawn "
                                       "before this statement can be accepted",
                            "open_disputes": live})
    sla_state = (json.loads(st.canonical_json).get("sla") or {}).get("state")
    if sla_state == "pending_disputes":
        db.commit()
        raise Refused(409, {"message": "Part of this statement is DISPUTED; raise a "
                                       "dispute over that time and resolve it first",
                            "sla_state": sla_state})
    if state.get(party.side) is not None:
        db.commit()
        raise Refused(409, "You have already accepted this revision")
    rc = db.execute(
        update(models.ContractStatement)
        .where(models.ContractStatement.id == st.id,
               models.ContractStatement.content_hash == content_hash,
               models.ContractStatement.revision == revision)
        .values(updated_at=now)
        .execution_options(synchronize_session=False)).rowcount
    if rc != 1:
        db.rollback()
        raise Refused(409, "The statement changed before it could be accepted")
    db.add(models.ContractStatementAcceptance(
        statement_id=st.id, party=party.side, actor=party.actor, accepted_at=now,
        content_hash=content_hash, revision=revision))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise Refused(409, "You have already accepted this revision") from None
    _audit(db, party, contract, "contract_statement_accepted", "contract_statement",
           st.id, f"party={party.side} period_start={_ts(st.period_start)} "
                  f"content_hash={content_hash} revision={revision}")
    db.refresh(st)
    if engine.acceptance_state(db, st)["agreed"]:
        _publish(db, oem_events.StatementAgreed(
            tenant_code=contract.factory_tenant_code, oem_code=contract.oem_code,
            contract_id=contract.id, contract_ref=contract.contract_ref,
            statement_id=st.id, period_start=_ts(st.period_start),
            revision=st.revision, content_hash=st.content_hash))
    _commit_or_conflict(db, "You have already accepted this revision")
    return statement_view(db, party, contract, st.id)


# ── Disputes ─────────────────────────────────────────────────────────

def _disputes(db, contract_id, statement_id=None):
    q = (db.query(models.ContractDispute)
           .filter(models.ContractDispute.contract_id == contract_id))
    if statement_id is not None:
        q = q.filter(models.ContractDispute.statement_id == statement_id)
    return q.order_by(models.ContractDispute.id.asc()).all()


def dispute_view(d):
    """Window, parties, buckets, status and the text each party wrote. No
    usernames and nothing from the statement content."""
    return {
        "id": d.id, "contract_id": d.contract_id, "statement_id": d.statement_id,
        "installation_id": d.installation_id,
        "window_start": _ts(d.window_start), "window_end": _ts(d.window_end),
        "raised_by_party": d.raised_by_party, "raised_at": _ts(d.raised_at),
        "reason": d.reason, "proposed_bucket": d.proposed_bucket, "status": d.status,
        "resolution_bucket": d.resolution_bucket,
        "resolution_note": d.resolution_note,
        "resolution_proposed_by_party": d.resolution_proposed_by_party,
        "resolution_proposed_at": _ts(d.resolution_proposed_at),
        "resolution_accepted_at": _ts(d.resolution_accepted_at),
        "closed_at": _ts(d.closed_at),
    }


def list_disputes(db, party, contract):
    return {"disputes": [dispute_view(d) for d in _disputes(db, contract.id)]}


def _bucket(value, field):
    if value not in contract_terms.RESOLUTION_BUCKETS:
        raise Refused(422, {"field": field,
                            "message": "must be one of "
                                       + ", ".join(contract_terms.RESOLUTION_BUCKETS)
                                       + "; a dispute must settle its window"})
    return value


def require_dispute(db, contract, dispute_id):
    if not _row_id(dispute_id):
        raise Refused(404, "Dispute not found")
    d = (db.query(models.ContractDispute)
           .filter(models.ContractDispute.id == dispute_id,
                   models.ContractDispute.contract_id == contract.id).first())
    if d is None:
        raise Refused(404, "Dispute not found")
    return d


def raise_dispute(db, party, contract_id, statement_id, body):
    now = utcnow()
    contract = require_contract(db, party, contract_id)
    require_statement_access(db, party, contract)
    _bucket(body.proposed_bucket, "proposed_bucket")
    reason = body.reason.strip()
    if not reason:
        raise Refused(422, {"field": "reason", "message": "must not be blank"})
    window_start = parse_ts(body.window_start, "window_start")
    window_end = parse_ts(body.window_end, "window_end")
    if window_end <= window_start:
        raise Refused(422, {"field": "window_end", "message": "must be after window_start"})
    if not _row_id(body.installation_id):
        raise Refused(422, {"field": "installation_id",
                            "message": "is not covered by the terms of this statement"})
    if not _row_id(statement_id):
        raise Refused(404, "Statement not found")
    engine = _engine()
    # Serialise dispute writers on the statement row (a no-op lock on SQLite,
    # whose writes are serialised anyway).
    st = (db.query(models.ContractStatement)
            .filter(models.ContractStatement.id == statement_id,
                    models.ContractStatement.contract_id == contract.id)
            .with_for_update().first())
    if st is None:
        raise Refused(404, "Statement not found")
    if engine.acceptance_state(db, st)["agreed"]:
        raise Refused(409, "An agreed statement is final and cannot be disputed")
    if not (st.period_start <= window_start and window_end <= st.period_end):
        raise Refused(422, {"field": "window_start",
                            "message": "the window must lie inside the statement period"})
    covered = (db.query(models.ServiceContractMachine)
                 .filter(models.ServiceContractMachine.term_version_id == st.term_version_id,
                         models.ServiceContractMachine.installation_id
                         == body.installation_id).first())
    if covered is None:
        raise Refused(422, {"field": "installation_id",
                            "message": "is not covered by the terms of this statement"})
    version = (db.query(models.ServiceContractTermVersion)
                 .filter(models.ServiceContractTermVersion.id == st.term_version_id).one())
    intervals = contract_periods.covered_intervals(
        contract_periods.Period(st.period_start, st.period_end), _terms_of(version),
        st.period_start, st.period_end)
    if not any(s <= window_start and window_end <= e for s, e in intervals):
        raise Refused(422, {"field": "window_start",
                            "message": "the window must lie inside the covered hours"})
    overlap = (db.query(models.ContractDispute)
                 .filter(models.ContractDispute.contract_id == contract.id,
                         models.ContractDispute.installation_id == body.installation_id,
                         models.ContractDispute.status != WITHDRAWN,
                         models.ContractDispute.window_start < window_end,
                         models.ContractDispute.window_end > window_start).first())
    if overlap is not None:
        raise Refused(409, {"message": "This window overlaps another dispute on the same "
                                       "machine",
                            "dispute_id": overlap.id})
    dispute = models.ContractDispute(
        contract_id=contract.id, statement_id=st.id,
        installation_id=body.installation_id, window_start=window_start,
        window_end=window_end, raised_by_party=party.side, raised_by=party.actor,
        raised_at=now, reason=reason, proposed_bucket=body.proposed_bucket,
        status=OPEN)
    db.add(dispute)
    db.flush()
    result = _run_compute(db, party, contract, st.period_start, now)
    if not result.changed:
        # Raising a dispute must move the statement to a new revision (which is
        # what cancels every acceptance). If it did not, refuse rather than
        # leave acceptances standing beside an open dispute.
        db.rollback()
        raise Refused(409, "The statement could not be revised for this dispute")
    _audit(db, party, contract, "contract_dispute_raised", "contract_dispute",
           dispute.id, f"statement={st.id} installation={body.installation_id} "
                       f"window=[{_ts(window_start)},{_ts(window_end)}) "
                       f"by={party.side} proposed={body.proposed_bucket} "
                       f"revision={result.statement.revision}")
    _publish(db, oem_events.DisputeRaised(
        tenant_code=contract.factory_tenant_code, oem_code=contract.oem_code,
        contract_id=contract.id, contract_ref=contract.contract_ref,
        dispute_id=dispute.id, statement_id=st.id, raised_by_party=party.side))
    db.commit()
    db.refresh(dispute)
    return {"dispute": dispute_view(dispute),
            "statement": _statement_ref(result.statement)}


def _recompute_after_dispute(db, party, contract, dispute, now):
    """Bring the statement up to date after a dispute changed state. Past
    retention the statement cannot be recomputed; the dispute's state still
    changes, and acceptance stays blocked or cancelled as the rules say."""
    st = (db.query(models.ContractStatement)
            .filter(models.ContractStatement.id == dispute.statement_id).one())
    result = _run_compute(db, party, contract, st.period_start, now, allow_expired=True)
    return result.statement if result is not None else None


def _dispute_transition(db, dispute, before, values):
    rc = db.execute(
        update(models.ContractDispute)
        .where(models.ContractDispute.id == dispute.id,
               models.ContractDispute.status == before,
               *([models.ContractDispute.resolution_bucket == dispute.resolution_bucket]
                 if before == RESOLUTION_PROPOSED else []))
        .values(**values)
        .execution_options(synchronize_session=False)).rowcount
    if rc != 1:
        db.rollback()
        raise Refused(409, "The dispute changed before this could be recorded")
    # A bulk UPDATE does not touch the loaded object. The engine's recompute
    # reads disputes through this session, and a stale identity-map row would
    # attribute the window by the status it had BEFORE this transition.
    db.expire(dispute)


def _after_dispute(db, party, contract, dispute, now, action, details, event=None):
    """Recompute, audit, publish, commit: the tail every dispute step shares.

    ORDER MATTERS. `_run_compute` rolls the transaction back on a refusal, so the
    audit and event are added only after the recompute has succeeded (or found
    the evidence expired, which rolls nothing back)."""
    db.flush()
    st = _recompute_after_dispute(db, party, contract, dispute, now)
    _audit(db, party, contract, action, "contract_dispute", dispute.id,
           details + (f" revision={st.revision}" if st is not None
                      else " revision=unchanged(evidence_expired)"))
    if event is not None:
        _publish(db, event)
    db.commit()
    db.refresh(dispute)
    ref = _statement_ref(st) if st is not None else None
    if (ref is not None and party.side == OEM
            and not oem_sharing.contract_statement_visible(db, contract)):
        # Withdrawing one's own dispute needs no consent; learning the revised
        # statement's hash does. The revision number is the contract's, the
        # hash is a fingerprint of the factory's data.
        ref = {"id": ref["id"], "revision": ref["revision"], "withheld": True}
    return {"dispute": dispute_view(dispute), "statement": ref}


def propose_resolution(db, party, contract_id, dispute_id, body):
    now = utcnow()
    contract = require_contract(db, party, contract_id)
    require_statement_access(db, party, contract)
    bucket = _bucket(body.resolution_bucket, "resolution_bucket")
    dispute = require_dispute(db, contract, dispute_id)
    before = dispute.status
    if before not in LIVE_DISPUTE_STATUSES:
        raise Refused(409, f"This dispute is {before}")
    _dispute_transition(db, dispute, before, {
        "status": RESOLUTION_PROPOSED, "resolution_bucket": bucket,
        "resolution_note": body.note.strip() or None,
        "resolution_proposed_by_party": party.side,
        "resolution_proposed_by": party.actor, "resolution_proposed_at": now})
    return _after_dispute(
        db, party, contract, dispute, now, "contract_dispute_resolution_proposed",
        f"statement={dispute.statement_id} by={party.side} resolution={bucket}")


def accept_resolution(db, party, contract_id, dispute_id, body):
    now = utcnow()
    contract = require_contract(db, party, contract_id)
    require_statement_access(db, party, contract)
    dispute = require_dispute(db, contract, dispute_id)
    if dispute.status != RESOLUTION_PROPOSED:
        raise Refused(409, f"This dispute is {dispute.status}; no resolution awaits "
                           "acceptance")
    if dispute.resolution_proposed_by_party == party.side:
        raise Refused(403, "The other party must accept a resolution you proposed")
    if body.resolution_bucket != dispute.resolution_bucket:
        raise Refused(409, {"message": "That is not the resolution on offer",
                            "resolution_bucket": dispute.resolution_bucket})
    _dispute_transition(db, dispute, RESOLUTION_PROPOSED, {
        "status": RESOLVED, "resolution_accepted_by": party.actor,
        "resolution_accepted_at": now, "closed_at": now})
    return _after_dispute(
        db, party, contract, dispute, now, "contract_dispute_resolved",
        f"statement={dispute.statement_id} by={party.side} "
        f"resolution={dispute.resolution_bucket}",
        oem_events.DisputeResolved(
            tenant_code=contract.factory_tenant_code, oem_code=contract.oem_code,
            contract_id=contract.id, contract_ref=contract.contract_ref,
            dispute_id=dispute.id, statement_id=dispute.statement_id,
            resolution_bucket=dispute.resolution_bucket))


def withdraw_dispute(db, party, contract_id, dispute_id):
    now = utcnow()
    contract = require_contract(db, party, contract_id)
    dispute = require_dispute(db, contract, dispute_id)
    if dispute.raised_by_party != party.side:
        raise Refused(403, "Only the party that raised a dispute can withdraw it")
    before = dispute.status
    if before not in LIVE_DISPUTE_STATUSES:
        raise Refused(409, f"This dispute is {before}")
    _dispute_transition(db, dispute, before, {"status": WITHDRAWN, "closed_at": now})
    return _after_dispute(
        db, party, contract, dispute, now, "contract_dispute_withdrawn",
        f"statement={dispute.statement_id} by={party.side} {before} -> withdrawn")


# ── History and the reason vocabulary ────────────────────────────────

def history(db, party, contract):
    """This party's audit rows about this contract, bounded at both ends."""
    now = utcnow()
    tenant = party.audit_tenant()
    version_ids = [v.id for v in _versions(db, contract.id)]
    statement_ids = [s.id for s in _statements(db, contract.id)]
    dispute_ids = [d.id for d in _disputes(db, contract.id)]
    entity = [and_(models.AuditLog.entity_type == "service_contract",
                   models.AuditLog.entity_id == contract.id)]
    for kind, ids in (("contract_term_version", version_ids),
                      ("contract_statement", statement_ids),
                      ("contract_dispute", dispute_ids)):
        if ids:
            entity.append(and_(models.AuditLog.entity_type == kind,
                               models.AuditLog.entity_id.in_(ids)))
    rows = (db.query(models.AuditLog)
              .filter(models.AuditLog.tenant_code == tenant,
                      models.AuditLog.entity_type.in_(HISTORY_ENTITY_TYPES),
                      or_(*entity),
                      models.AuditLog.created_at >= contract.created_at,
                      models.AuditLog.created_at < now + timedelta(seconds=1))
              .order_by(models.AuditLog.id.asc()).limit(MAX_HISTORY).all())
    return {"history": [{"at": _ts(r.created_at), "actor": r.actor, "action": r.action,
                         "entity_type": r.entity_type, "entity_id": r.entity_id,
                         "details": r.details} for r in rows]}


def reason_vocabulary(db, party, contract):
    """FACTORY ONLY. Which of this factory's own downtime reasons, logged
    against the covered machines in the last 90 days, the terms on the table
    map, treat as generic, or leave unmapped (and so DISPUTED).

    "The terms on the table": a pending amendment, else the latest accepted
    version, else version 1."""
    if party.side != FACTORY:
        raise Refused(404, NOT_FOUND)
    now = utcnow()
    versions = _versions(db, contract.id)
    version = (next((v for v in reversed(versions) if v.status == PROPOSED
                     and v.proposed_at is not None), None)
               or next((v for v in reversed(versions) if v.status == ACCEPTED), None)
               or versions[0])
    terms = _terms_of(version)
    ids = [c.installation_id for c in terms.covered_installations]
    machine_ids = sorted({
        r.machine_id for r in db.query(models.MachineInstallation)
        .filter(models.MachineInstallation.id.in_(ids),
                models.MachineInstallation.oem_code == contract.oem_code,
                models.MachineInstallation.factory_tenant_code == party.tenant_code,
                models.MachineInstallation.machine_id.isnot(None)).all()})
    since, until = now - timedelta(days=VOCABULARY_LOOKBACK_DAYS), now + timedelta(seconds=1)
    counts = {}
    if machine_ids:
        rows = (db.query(models.DowntimeLog.reason)
                  .filter(models.DowntimeLog.tenant_code == party.tenant_code,
                          models.DowntimeLog.machine_id.in_(machine_ids),
                          models.DowntimeLog.created_at >= since,
                          models.DowntimeLog.created_at < until).all())
        for (reason,) in rows:
            key = contract_terms.reason_key(reason)
            counts[key] = counts.get(key, 0) + 1
    out = []
    for key in sorted(counts):
        if key in terms.generic_reasons:
            status, bucket = "generic", None
        elif key in terms.reason_map:
            status, bucket = "mapped", terms.reason_map[key]
        else:
            status, bucket = "unmapped", contract_terms.DISPUTED
        out.append({"reason_key": key, "count": counts[key], "status": status,
                    "bucket": bucket})
    return {"terms_version": version.version, "since": _ts(since), "until": _ts(until),
            "reasons": out}
