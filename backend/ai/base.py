"""AI platform contracts (ADR-0003).

The stable types every AI capability shares. Consumers depend on these — not on
any concrete engine — so a rule-based implementation can give way to ML or an LLM
behind the same interface without changing a single caller.
"""
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class Recommendation:
    """An AI-produced suggestion, decoupled from storage and transport.

    Services return this; a thin mapper persists it (see ``ai.recommendations``).
    Keeping the value object separate from the ORM row means the reasoning and
    the storage can evolve independently.
    """
    recommendation_type: str
    title: str
    message: str
    severity: str = "Medium"
    confidence: int = 75
    related_machine_id: Optional[int] = None


@runtime_checkable
class AIService(Protocol):
    """Marker for an AI capability (Prediction, Recommendations, Copilot, ...).

    Deliberately small — concrete services expose their own domain methods. The
    contract that matters is that consumers import ``ai.<service>`` and depend on
    this package's types, never on an engine module directly.
    """
    name: str
