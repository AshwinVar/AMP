"""Request bodies for the service-contract routes (ADR-0020).

Every body refuses fields it does not name (`extra="forbid"`). A contract body
that carried an `oem_code` must be refused rather than ignored: the manufacturer
comes from the authenticated principal, and a request that tries to name one is
either a bug or a probe, and neither should look like it worked.

Strict types throughout. `grant_downtime_sharing` is a StrictBool because the
STRING "true" is not consent, and a statement `revision` is a StrictInt because
"1" is not the revision a party saw. Terms are a plain JSON object here and are
validated field by field, by name, in contract_terms.parse.
"""
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

NOTE_MAX = 1000


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContractDraft(_Body):
    contract_ref: StrictStr = Field(min_length=1, max_length=64)
    title: StrictStr = Field(min_length=1, max_length=200)
    contract_type: StrictStr
    factory_tenant_code: StrictStr = Field(min_length=1, max_length=64)
    # "YYYY-MM": the contract starts at local midnight on the 1st of that month
    # in the terms' timezone. A month, not an instant, so a start can never fall
    # inside a period.
    start_month: StrictStr
    terms: dict


class TermsHash(_Body):
    terms_hash: StrictStr


class FactoryAcceptance(_Body):
    terms_hash: StrictStr
    # Optional in the SCHEMA so that a missing flag reaches the handler and is
    # refused with an explanation of what the grant discloses, rather than a
    # bare "field required".
    grant_downtime_sharing: Optional[StrictBool] = None


class DecisionNote(_Body):
    note: StrictStr = Field(default="", max_length=NOTE_MAX)


class Termination(_Body):
    reason: StrictStr = Field(min_length=1, max_length=NOTE_MAX)


class AmendmentDraft(_Body):
    terms: dict
    effective_from: StrictStr


class ComputeRequest(_Body):
    period_start: StrictStr


class StatementAcceptance(_Body):
    content_hash: StrictStr
    revision: StrictInt


class DisputeRaise(_Body):
    installation_id: StrictInt
    window_start: StrictStr
    window_end: StrictStr
    reason: StrictStr = Field(min_length=1, max_length=NOTE_MAX)
    proposed_bucket: StrictStr


class ResolutionProposal(_Body):
    resolution_bucket: StrictStr
    note: StrictStr = Field(default="", max_length=NOTE_MAX)


class ResolutionAcceptance(_Body):
    # The bucket the accepting party saw. Either party may counter-propose while
    # a resolution is pending, so accepting "the current proposal" without
    # naming it could accept a proposal made a second earlier by the other side.
    resolution_bucket: StrictStr
