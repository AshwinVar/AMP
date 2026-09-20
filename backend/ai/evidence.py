"""Evidence: what an AMP answer is made of, and how sure AMP is of it (ADR-0022).

Every figure the Copilot, the brief or an explorer shows is a `Fact`. A fact
carries its value AND the answer to "how do you know?", taken from one fixed
vocabulary, so the answer has the same meaning on every screen and in every
test:

    MEASURED FACT          read from recorded data: a count, a sum, a status
    DERIVED METRIC         a fixed formula over measured facts: OEE, a rate,
                           money from the tenant's own configured unit value
    CORRELATION            two measured things moved together; NOT a cause
    RULE-BASED ASSESSMENT  a deterministic threshold or hand-weighted score
                           (health bands, risk points). Never called ML.
    MODEL ESTIMATE         a trained model's output, with its validation status
    UNKNOWN                AMP does not know, and says so instead of guessing

A whole result has a data state, the honest-data vocabulary of the sprint:
NO DATA, PARTIAL DATA, NOT MEASURED, NOT CONFIGURED, INSUFFICIENT HISTORY and
MODEL NOT VALIDATED. None of them is ever turned into a zero. "No production
recorded" and "0% OEE" are different claims, and only one of them is true when
a gateway is down (ADR-0014).

The tool layer adds its refusals (NOT PERMITTED, NOT LICENSED, NOT FOUND,
INVALID ARGUMENTS, FAILED) so a refusal is a result the UI can render and the
evaluation can count, never an exception a model can talk its way around.

The root-cause labels live here too, so there is one list:
CAUSE CONFIRMED / LIKELY CONTRIBUTOR / CORRELATED EVENT / INSUFFICIENT EVIDENCE.

`frontend/lib/evidence.ts` mirrors these strings;
test_copilot_evidence_vocabulary.py fails the build if the two drift.
"""
import math
from dataclasses import dataclass, field

# ── Provenance: how a figure was obtained ───────────────────────────
MEASURED = "MEASURED FACT"
DERIVED = "DERIVED METRIC"
CORRELATION = "CORRELATION"
RULE = "RULE-BASED ASSESSMENT"
MODEL = "MODEL ESTIMATE"
UNKNOWN = "UNKNOWN"
PROVENANCE = (MEASURED, DERIVED, CORRELATION, RULE, MODEL, UNKNOWN)

# ── Data states: how complete a result is ───────────────────────────
OK = "OK"
NO_DATA = "NO DATA"
PARTIAL_DATA = "PARTIAL DATA"
NOT_MEASURED = "NOT MEASURED"
NOT_CONFIGURED = "NOT CONFIGURED"
INSUFFICIENT_HISTORY = "INSUFFICIENT HISTORY"
MODEL_NOT_VALIDATED = "MODEL NOT VALIDATED"
DATA_STATES = (OK, NO_DATA, PARTIAL_DATA, NOT_MEASURED, NOT_CONFIGURED,
               INSUFFICIENT_HISTORY, MODEL_NOT_VALIDATED)

# ── Refusals: the tool layer would not, or could not, answer ────────
NOT_PERMITTED = "NOT PERMITTED"
NOT_LICENSED = "NOT LICENSED"
NOT_FOUND = "NOT FOUND"
INVALID_ARGUMENTS = "INVALID ARGUMENTS"
FAILED = "FAILED"
REFUSALS = (NOT_PERMITTED, NOT_LICENSED, NOT_FOUND, INVALID_ARGUMENTS, FAILED)

# ── Likelihood: how a RULE rates something that has not happened yet ─
#
# Words, not probabilities. AMP has no calibrated forecast of a stoppage or a
# missed date, and a number would imply one; each risk states the rule and the
# threshold that earned its word (ai/risk_radar.py).
LIKELY = "LIKELY"
POSSIBLE = "POSSIBLE"
WATCH = "WATCH"
LIKELIHOOD = (LIKELY, POSSIBLE, WATCH)

# ── Root-cause labels (the Root-Cause Explorer) ─────────────────────
CAUSE_CONFIRMED = "CAUSE CONFIRMED"
LIKELY_CONTRIBUTOR = "LIKELY CONTRIBUTOR"
CORRELATED_EVENT = "CORRELATED EVENT"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT EVIDENCE"
CAUSE_LABELS = (CAUSE_CONFIRMED, LIKELY_CONTRIBUTOR, CORRELATED_EVENT, INSUFFICIENT_EVIDENCE)


@dataclass(frozen=True)
class Fact:
    """One figure with its provenance.

    `key` is stable and machine-readable ("downtime.minutes"); `label` is what a
    person reads. `value` is a number, a string (a machine name, a reason) or
    None, and None is allowed ONLY with provenance UNKNOWN: a missing figure is
    stated as unknown, never shipped as a blank a reader fills in. A number must
    be finite, because NaN and infinity print as figures and mean nothing.
    """
    key: str
    label: str
    value: object
    provenance: str
    unit: str = ""
    source: str = ""
    window: str = ""
    detail: str = ""

    def __post_init__(self):
        if self.provenance not in PROVENANCE:
            raise ValueError(f"{self.key}: unknown provenance {self.provenance!r}")
        if self.value is None and self.provenance != UNKNOWN:
            raise ValueError(f"{self.key}: a missing value must be labelled {UNKNOWN}")
        if isinstance(self.value, bool):
            raise ValueError(f"{self.key}: a fact is a figure or a name, not a flag")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError(f"{self.key}: {self.value!r} is not a figure")
        if not isinstance(self.value, (int, float, str, type(None))):
            raise ValueError(f"{self.key}: unsupported value type {type(self.value).__name__}")

    def to_dict(self, fact_id: str) -> dict:
        return {"id": fact_id, "key": self.key, "label": self.label, "value": self.value,
                "unit": self.unit, "provenance": self.provenance, "source": self.source,
                "window": self.window, "detail": self.detail}


@dataclass
class ToolResult:
    """What one AMP tool returned: a state, a sentence AMP wrote from the facts,
    the facts themselves and the screen that owns the detail."""
    tool: str
    state: str
    summary: str
    facts: list = field(default_factory=list)
    view: str = None
    notes: list = field(default_factory=list)
    elapsed_ms: int = None

    def __post_init__(self):
        if self.state not in DATA_STATES and self.state not in REFUSALS:
            raise ValueError(f"{self.tool}: unknown state {self.state!r}")

    @property
    def refused(self) -> bool:
        return self.state in REFUSALS

    def to_dict(self, first_id: int = 1) -> dict:
        return {"tool": self.tool, "state": self.state, "summary": self.summary,
                "view": self.view, "notes": list(self.notes), "elapsed_ms": self.elapsed_ms,
                "facts": [f.to_dict(f"F{first_id + i}") for i, f in enumerate(self.facts)]}


def refusal(tool: str, state: str, why: str) -> ToolResult:
    """A refusal as a result: no facts, one plain sentence saying why."""
    if state not in REFUSALS:
        raise ValueError(f"{state!r} is not a refusal")
    return ToolResult(tool=str(tool)[:80], state=state, summary=why)
