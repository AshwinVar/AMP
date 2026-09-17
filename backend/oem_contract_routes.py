"""Service contracts, from the MANUFACTURER's side (ADR-0020).

Thin by design: every rule is in service_contracts, keyed on a Party, so the
manufacturer and the factory cannot drift into two versions of one contract.
What this file owes is the gate on each handler:

  * EVERY handler depends on `oem_auth.require_oem(...)`, which re-reads the
    principal from the database and binds nothing wider than the OEM sentinel
    (test_contract_route_guards asserts it, handler by handler, and that it
    found them all).
  * The capability names what the handler does:
        read_contracts     every OEM role
        manage_contracts   OEM_ADMIN, OEM_SERVICE_MANAGER  (draft, amend, compute,
                                                            dispute)
        sign_contracts     OEM_ADMIN only                    (propose, withdraw,
                                                            accept, terminate)
  * `oem_code` comes from the principal, never the path or body. A contract of
    another manufacturer is a 404 with the same message as one that does not
    exist.
  * CONSENT. Statement content and statement actions additionally need the
    factory's SHARE_DOWNTIME grant, read on every request. Without it the reply
    is 403 with detail {"withheld": true, "reason": "sharing withdrawn by
    factory"} and no content. Terms, versions, periods, disputes and history
    stay visible: they are the contract, not the factory's data.

AMP computes statements; it never invoices, charges or moves money.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import oem_auth
import service_contracts as svc
from database import SessionLocal
from schemas_contracts import (AmendmentDraft, ComputeRequest, ContractDraft,
                               DecisionNote, DisputeRaise, ResolutionAcceptance,
                               ResolutionProposal, StatementAcceptance, TermsHash,
                               Termination)

router = APIRouter(prefix="/oem/contracts", tags=["OEM service contracts"])

READ = "read_contracts"
MANAGE = "manage_contracts"
SIGN = "sign_contracts"


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _contract(db, principal, contract_id):
    party = svc.for_oem(principal)
    return party, svc.require_contract(db, party, contract_id)


@router.get("")
def list_contracts(db: Session = Depends(_get_db),
                   principal: dict = Depends(oem_auth.require_oem(READ))):
    return svc.list_contracts(db, svc.for_oem(principal))


@router.post("")
def create_contract(payload: ContractDraft, db: Session = Depends(_get_db),
                    principal: dict = Depends(oem_auth.require_oem(MANAGE))):
    return svc.create_draft(db, svc.for_oem(principal), payload)


@router.get("/{contract_id}")
def get_contract(contract_id: int, db: Session = Depends(_get_db),
                 principal: dict = Depends(oem_auth.require_oem(READ))):
    party, contract = _contract(db, principal, contract_id)
    return svc.contract_view(db, party, contract)


@router.put("/{contract_id}/draft")
def edit_draft(contract_id: int, payload: ContractDraft, db: Session = Depends(_get_db),
               principal: dict = Depends(oem_auth.require_oem(MANAGE))):
    return svc.edit_draft(db, svc.for_oem(principal), contract_id, payload)


@router.post("/{contract_id}/propose")
def propose(contract_id: int, payload: TermsHash, db: Session = Depends(_get_db),
            principal: dict = Depends(oem_auth.require_oem(SIGN))):
    return svc.propose_contract(db, svc.for_oem(principal), contract_id,
                                payload.terms_hash)


@router.post("/{contract_id}/withdraw")
def withdraw(contract_id: int, db: Session = Depends(_get_db),
             principal: dict = Depends(oem_auth.require_oem(SIGN))):
    return svc.withdraw_contract(db, svc.for_oem(principal), contract_id)


@router.post("/{contract_id}/amendments")
def draft_amendment(contract_id: int, payload: AmendmentDraft,
                    db: Session = Depends(_get_db),
                    principal: dict = Depends(oem_auth.require_oem(MANAGE))):
    return svc.draft_amendment(db, svc.for_oem(principal), contract_id, payload)


@router.post("/{contract_id}/amendments/{version}/propose")
def propose_amendment(contract_id: int, version: int, payload: TermsHash,
                      db: Session = Depends(_get_db),
                      principal: dict = Depends(oem_auth.require_oem(SIGN))):
    return svc.propose_amendment(db, svc.for_oem(principal), contract_id, version,
                                 payload.terms_hash)


@router.post("/{contract_id}/amendments/{version}/accept")
def accept_amendment(contract_id: int, version: int, payload: TermsHash,
                     db: Session = Depends(_get_db),
                     principal: dict = Depends(oem_auth.require_oem(SIGN))):
    return svc.accept_amendment(db, svc.for_oem(principal), contract_id, version,
                                payload.terms_hash)


@router.post("/{contract_id}/amendments/{version}/reject")
def reject_amendment(contract_id: int, version: int, payload: DecisionNote,
                     db: Session = Depends(_get_db),
                     principal: dict = Depends(oem_auth.require_oem(SIGN))):
    return svc.reject_amendment(db, svc.for_oem(principal), contract_id, version,
                                payload.note.strip())


@router.post("/{contract_id}/terminate")
def terminate(contract_id: int, payload: Termination, db: Session = Depends(_get_db),
              principal: dict = Depends(oem_auth.require_oem(SIGN))):
    return svc.terminate_contract(db, svc.for_oem(principal), contract_id,
                                  payload.reason)


@router.get("/{contract_id}/periods")
def periods(contract_id: int, db: Session = Depends(_get_db),
            principal: dict = Depends(oem_auth.require_oem(READ))):
    party, contract = _contract(db, principal, contract_id)
    return svc.periods_view(db, party, contract)


@router.get("/{contract_id}/preview")
def preview(contract_id: int, db: Session = Depends(_get_db),
            principal: dict = Depends(oem_auth.require_oem(READ))):
    party, contract = _contract(db, principal, contract_id)
    return svc.preview(db, party, contract)


@router.post("/{contract_id}/statements/compute")
def compute(contract_id: int, payload: ComputeRequest, db: Session = Depends(_get_db),
            principal: dict = Depends(oem_auth.require_oem(MANAGE))):
    return svc.compute(db, svc.for_oem(principal), contract_id, payload.period_start)


@router.get("/{contract_id}/statements/{statement_id}")
def statement(contract_id: int, statement_id: int, db: Session = Depends(_get_db),
              principal: dict = Depends(oem_auth.require_oem(READ))):
    party, contract = _contract(db, principal, contract_id)
    return svc.statement_view(db, party, contract, statement_id)


@router.get("/{contract_id}/statements/{statement_id}/verify")
def verify(contract_id: int, statement_id: int, db: Session = Depends(_get_db),
           principal: dict = Depends(oem_auth.require_oem(READ))):
    party, contract = _contract(db, principal, contract_id)
    return svc.verify(db, party, contract, statement_id)


@router.get("/{contract_id}/statements/{statement_id}/download")
def download(contract_id: int, statement_id: int, db: Session = Depends(_get_db),
             principal: dict = Depends(oem_auth.require_oem(READ))):
    party, contract = _contract(db, principal, contract_id)
    return svc.download_response(*svc.download(db, party, contract, statement_id))


@router.post("/{contract_id}/statements/{statement_id}/accept")
def accept_statement(contract_id: int, statement_id: int, payload: StatementAcceptance,
                     db: Session = Depends(_get_db),
                     principal: dict = Depends(oem_auth.require_oem(SIGN))):
    return svc.accept_statement(db, svc.for_oem(principal), contract_id, statement_id,
                                payload.content_hash, payload.revision)


@router.post("/{contract_id}/statements/{statement_id}/disputes")
def raise_dispute(contract_id: int, statement_id: int, payload: DisputeRaise,
                  db: Session = Depends(_get_db),
                  principal: dict = Depends(oem_auth.require_oem(MANAGE))):
    return svc.raise_dispute(db, svc.for_oem(principal), contract_id, statement_id,
                             payload)


@router.get("/{contract_id}/disputes")
def disputes(contract_id: int, db: Session = Depends(_get_db),
             principal: dict = Depends(oem_auth.require_oem(READ))):
    party, contract = _contract(db, principal, contract_id)
    return svc.list_disputes(db, party, contract)


@router.post("/{contract_id}/disputes/{dispute_id}/propose-resolution")
def propose_resolution(contract_id: int, dispute_id: int, payload: ResolutionProposal,
                       db: Session = Depends(_get_db),
                       principal: dict = Depends(oem_auth.require_oem(MANAGE))):
    return svc.propose_resolution(db, svc.for_oem(principal), contract_id, dispute_id,
                                  payload)


@router.post("/{contract_id}/disputes/{dispute_id}/accept-resolution")
def accept_resolution(contract_id: int, dispute_id: int, payload: ResolutionAcceptance,
                      db: Session = Depends(_get_db),
                      principal: dict = Depends(oem_auth.require_oem(SIGN))):
    return svc.accept_resolution(db, svc.for_oem(principal), contract_id, dispute_id,
                                 payload)


@router.post("/{contract_id}/disputes/{dispute_id}/withdraw")
def withdraw_dispute(contract_id: int, dispute_id: int, db: Session = Depends(_get_db),
                     principal: dict = Depends(oem_auth.require_oem(MANAGE))):
    return svc.withdraw_dispute(db, svc.for_oem(principal), contract_id, dispute_id)


@router.get("/{contract_id}/history")
def history(contract_id: int, db: Session = Depends(_get_db),
            principal: dict = Depends(oem_auth.require_oem(READ))):
    party, contract = _contract(db, principal, contract_id)
    return svc.history(db, party, contract)


def register(app):
    app.include_router(router)
