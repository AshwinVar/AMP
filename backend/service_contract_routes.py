"""Service contracts, from the FACTORY's side (ADR-0021).

The factory's half of the contract a manufacturer proposes: review the terms,
see which of its OWN downtime reasons they map, accept explicitly (which grants
SHARE_DOWNTIME) or reject, propose or accept amendments, compute, accept or
dispute monthly statements, and terminate.

Thin by design: every rule is in service_contracts. What this file owes:

  * EVERY handler authenticates with `get_current_user` through `require_roles`
    (test_contract_route_guards asserts it, handler by handler). An OEM token is
    refused there (ADR-0017), so a manufacturer cannot act as the factory.
  * Admin and Supervisor may READ. Only Admin takes a binding or
    statement-changing action: accept, reject, amend, terminate, compute,
    accept a statement, dispute. Operators see nothing.
  * The tenant is the request's (tenancy.request_tenant), never a parameter. A
    contract of another factory, or one never proposed, is a 404.
  * The reason vocabulary is FACTORY ONLY: it lists the factory's own free-text
    reasons, which the manufacturer never sees outside covered episodes.

AMP computes statements; it never invoices, charges or moves money.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import service_contracts as svc
from auth import require_roles
from database import SessionLocal
from schemas_contracts import (AmendmentDraft, ComputeRequest, DecisionNote,
                               DisputeRaise, FactoryAcceptance, ResolutionAcceptance,
                               ResolutionProposal, StatementAcceptance, TermsHash,
                               Termination)

router = APIRouter(prefix="/service-contracts", tags=["Service contracts"])

READERS = ["Admin", "Supervisor"]
SIGNERS = ["Admin"]


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _contract(db, current_user, contract_id):
    party = svc.for_factory(current_user)
    return party, svc.require_contract(db, party, contract_id)


@router.get("")
def list_contracts(db: Session = Depends(_get_db),
                   current_user: dict = Depends(require_roles(READERS))):
    return svc.list_contracts(db, svc.for_factory(current_user))


@router.get("/{contract_id}")
def get_contract(contract_id: int, db: Session = Depends(_get_db),
                 current_user: dict = Depends(require_roles(READERS))):
    party, contract = _contract(db, current_user, contract_id)
    return svc.contract_view(db, party, contract)


@router.post("/{contract_id}/accept")
def accept(contract_id: int, payload: FactoryAcceptance, db: Session = Depends(_get_db),
           current_user: dict = Depends(require_roles(SIGNERS))):
    return svc.accept_contract(db, svc.for_factory(current_user), contract_id,
                               payload.terms_hash, payload.grant_downtime_sharing)


@router.post("/{contract_id}/reject")
def reject(contract_id: int, payload: DecisionNote, db: Session = Depends(_get_db),
           current_user: dict = Depends(require_roles(SIGNERS))):
    return svc.reject_contract(db, svc.for_factory(current_user), contract_id,
                               payload.note.strip())


@router.get("/{contract_id}/reason-vocabulary")
def reason_vocabulary(contract_id: int, db: Session = Depends(_get_db),
                      current_user: dict = Depends(require_roles(READERS))):
    party, contract = _contract(db, current_user, contract_id)
    return svc.reason_vocabulary(db, party, contract)


@router.post("/{contract_id}/amendments")
def draft_amendment(contract_id: int, payload: AmendmentDraft,
                    db: Session = Depends(_get_db),
                    current_user: dict = Depends(require_roles(SIGNERS))):
    return svc.draft_amendment(db, svc.for_factory(current_user), contract_id, payload)


@router.post("/{contract_id}/amendments/{version}/propose")
def propose_amendment(contract_id: int, version: int, payload: TermsHash,
                      db: Session = Depends(_get_db),
                      current_user: dict = Depends(require_roles(SIGNERS))):
    return svc.propose_amendment(db, svc.for_factory(current_user), contract_id,
                                 version, payload.terms_hash)


@router.post("/{contract_id}/amendments/{version}/accept")
def accept_amendment(contract_id: int, version: int, payload: TermsHash,
                     db: Session = Depends(_get_db),
                     current_user: dict = Depends(require_roles(SIGNERS))):
    return svc.accept_amendment(db, svc.for_factory(current_user), contract_id,
                                version, payload.terms_hash)


@router.post("/{contract_id}/amendments/{version}/reject")
def reject_amendment(contract_id: int, version: int, payload: DecisionNote,
                     db: Session = Depends(_get_db),
                     current_user: dict = Depends(require_roles(SIGNERS))):
    return svc.reject_amendment(db, svc.for_factory(current_user), contract_id,
                                version, payload.note.strip())


@router.post("/{contract_id}/terminate")
def terminate(contract_id: int, payload: Termination, db: Session = Depends(_get_db),
              current_user: dict = Depends(require_roles(SIGNERS))):
    return svc.terminate_contract(db, svc.for_factory(current_user), contract_id,
                                  payload.reason)


@router.get("/{contract_id}/periods")
def periods(contract_id: int, db: Session = Depends(_get_db),
            current_user: dict = Depends(require_roles(READERS))):
    party, contract = _contract(db, current_user, contract_id)
    return svc.periods_view(db, party, contract)


@router.get("/{contract_id}/preview")
def preview(contract_id: int, db: Session = Depends(_get_db),
            current_user: dict = Depends(require_roles(READERS))):
    party, contract = _contract(db, current_user, contract_id)
    return svc.preview(db, party, contract)


@router.post("/{contract_id}/statements/compute")
def compute(contract_id: int, payload: ComputeRequest, db: Session = Depends(_get_db),
            current_user: dict = Depends(require_roles(SIGNERS))):
    return svc.compute(db, svc.for_factory(current_user), contract_id,
                       payload.period_start)


@router.get("/{contract_id}/statements/{statement_id}")
def statement(contract_id: int, statement_id: int, db: Session = Depends(_get_db),
              current_user: dict = Depends(require_roles(READERS))):
    party, contract = _contract(db, current_user, contract_id)
    return svc.statement_view(db, party, contract, statement_id)


@router.get("/{contract_id}/statements/{statement_id}/verify")
def verify(contract_id: int, statement_id: int, db: Session = Depends(_get_db),
           current_user: dict = Depends(require_roles(READERS))):
    party, contract = _contract(db, current_user, contract_id)
    return svc.verify(db, party, contract, statement_id)


@router.get("/{contract_id}/statements/{statement_id}/download")
def download(contract_id: int, statement_id: int, db: Session = Depends(_get_db),
             current_user: dict = Depends(require_roles(READERS))):
    party, contract = _contract(db, current_user, contract_id)
    return svc.download_response(*svc.download(db, party, contract, statement_id))


@router.post("/{contract_id}/statements/{statement_id}/accept")
def accept_statement(contract_id: int, statement_id: int, payload: StatementAcceptance,
                     db: Session = Depends(_get_db),
                     current_user: dict = Depends(require_roles(SIGNERS))):
    return svc.accept_statement(db, svc.for_factory(current_user), contract_id,
                                statement_id, payload.content_hash, payload.revision)


@router.post("/{contract_id}/statements/{statement_id}/disputes")
def raise_dispute(contract_id: int, statement_id: int, payload: DisputeRaise,
                  db: Session = Depends(_get_db),
                  current_user: dict = Depends(require_roles(SIGNERS))):
    return svc.raise_dispute(db, svc.for_factory(current_user), contract_id,
                             statement_id, payload)


@router.get("/{contract_id}/disputes")
def disputes(contract_id: int, db: Session = Depends(_get_db),
             current_user: dict = Depends(require_roles(READERS))):
    party, contract = _contract(db, current_user, contract_id)
    return svc.list_disputes(db, party, contract)


@router.post("/{contract_id}/disputes/{dispute_id}/propose-resolution")
def propose_resolution(contract_id: int, dispute_id: int, payload: ResolutionProposal,
                       db: Session = Depends(_get_db),
                       current_user: dict = Depends(require_roles(SIGNERS))):
    return svc.propose_resolution(db, svc.for_factory(current_user), contract_id,
                                  dispute_id, payload)


@router.post("/{contract_id}/disputes/{dispute_id}/accept-resolution")
def accept_resolution(contract_id: int, dispute_id: int, payload: ResolutionAcceptance,
                      db: Session = Depends(_get_db),
                      current_user: dict = Depends(require_roles(SIGNERS))):
    return svc.accept_resolution(db, svc.for_factory(current_user), contract_id,
                                 dispute_id, payload)


@router.post("/{contract_id}/disputes/{dispute_id}/withdraw")
def withdraw_dispute(contract_id: int, dispute_id: int, db: Session = Depends(_get_db),
                     current_user: dict = Depends(require_roles(SIGNERS))):
    return svc.withdraw_dispute(db, svc.for_factory(current_user), contract_id,
                                dispute_id)


@router.get("/{contract_id}/history")
def history(contract_id: int, db: Session = Depends(_get_db),
            current_user: dict = Depends(require_roles(READERS))):
    party, contract = _contract(db, current_user, contract_id)
    return svc.history(db, party, contract)


def register(app):
    app.include_router(router)
