"""Frozen contracts between the AMP-native AI capabilities and the integration layer.

These types are fixed in Phase 0 so the failure-risk, telemetry-anomaly and
native-copilot builders and the integration builder can work in parallel.
Change them only with every consumer in view.

THE AUTHORIZATION CHAIN THEY SERVE
----------------------------------
USER -> AUTHENTICATION -> RBAC -> TENANT / OEM / CONSENT -> AMP TOOL -> DATA -> MODEL.
A model never decides who may see what. ``ConsentGate`` is how a capability
asks "may this tenant's own data be learned from, or sent outside AMP, for this
purpose?" and gets an answer with a REASON it can show; ``RouteDecision`` is the
only thing the native copilot model produces - a proposed intent, which the
existing tenant-scoped tool functions then act on.
"""
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

__all__ = [
    "ConsentDecision", "ConsentGate", "ConsentRequired", "RouteDecision",
    "CAPABILITY_TELEMETRY_BASELINE", "CAPABILITY_EXTERNAL_MODEL", "LEARNING_CAPABILITIES",
    "CONSENT_CAPABILITIES", "SCOPED_CAPABILITIES",
]

# Learning a per-machine telemetry baseline from the tenant's OWN readings.
CAPABILITY_TELEMETRY_BASELINE = "telemetry_baseline"
# Every capability that learns from tenant data. AMP-native models never learn
# from a tenant's data except through one of these.
LEARNING_CAPABILITIES = (CAPABILITY_TELEMETRY_BASELINE,)
# Sending the tenant's Copilot questions and the evidence behind their answers
# to a HOSTED language model (Anthropic, Gemini): the data leaves infrastructure
# AMP runs (ADR-0037). Not learning - AMP fits nothing to it - but the same
# decision shape: the company's, explicit, audited, revocable.
CAPABILITY_EXTERNAL_MODEL = "external_model"
# Every capability a company must consent to before AMP uses it: learning from
# its data, or letting its data leave AMP. Anything not listed here has no
# consent that could be granted, so a gate must refuse it.
CONSENT_CAPABILITIES = LEARNING_CAPABILITIES + (CAPABILITY_EXTERNAL_MODEL,)
# The capabilities whose consent names WHAT it was given for (ADR-0038): the
# hosted provider configured when the Admin said yes. Such a consent holds only
# while that is still the provider configured; a learning capability names no
# third party and has no scope.
SCOPED_CAPABILITIES = (CAPABILITY_EXTERNAL_MODEL,)


@dataclass(frozen=True)
class ConsentDecision:
    granted: bool
    capability: str
    reason: str
    granted_by: str | None
    granted_at: datetime | None
    # What a granted, scoped consent was given for (the provider name); None
    # for a refusal or an unscoped capability.
    scope: str | None = None

    def __post_init__(self):
        if type(self.granted) is not bool:
            raise TypeError("granted must be a bool")
        if not isinstance(self.capability, str) or not self.capability:
            raise ValueError("capability must be a non-empty string")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("a consent decision must carry a human-readable reason")
        if self.granted_by is not None and not isinstance(self.granted_by, str):
            raise TypeError("granted_by must be a string or None")
        if self.granted_at is not None and not isinstance(self.granted_at, datetime):
            raise TypeError("granted_at must be a datetime or None")
        if self.scope is not None and (not isinstance(self.scope, str) or not self.scope):
            raise TypeError("scope must be a non-empty string or None")


@runtime_checkable
class ConsentGate(Protocol):
    def check(self, db, tenant: str, capability: str) -> ConsentDecision: ...


class ConsentRequired(Exception):
    """Raised when a capability needs a learning consent the tenant has not granted."""

    def __init__(self, decision: ConsentDecision):
        if not isinstance(decision, ConsentDecision):
            raise TypeError("ConsentRequired needs the ConsentDecision that refused")
        if decision.granted:
            raise ValueError("ConsentRequired raised with a GRANTED decision")
        super().__init__(decision.reason)
        self.decision = decision


@dataclass(frozen=True)
class RouteDecision:
    route: str | None
    confidence: float
    top: list[tuple[str, float]]
    model_version: str

    def __post_init__(self):
        if self.route is not None and (not isinstance(self.route, str) or not self.route):
            raise ValueError("route must be a non-empty string or None")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)) \
                or not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be a number in [0, 1]")
        if not isinstance(self.model_version, str) or not self.model_version:
            raise ValueError("model_version must be a non-empty string")
