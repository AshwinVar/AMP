// The Copilot's evidence vocabulary (ADR-0022), mirrored from backend/ai/evidence.py.
//
// Every figure the Copilot shows arrives as a Fact that says how AMP knows it. The
// strings below are the backend's, character for character, and
// backend/test_copilot_evidence_vocabulary.py fails the build if the two lists drift:
// a provenance the UI does not know would render as a blank chip, and a state it
// does not know would hide "NOT CONFIGURED" behind a normal-looking answer.
import { CURRENCY, money } from "./money";

export const PROVENANCE = [
  "MEASURED FACT",
  "DERIVED METRIC",
  "CORRELATION",
  "RULE-BASED ASSESSMENT",
  "MODEL ESTIMATE",
  "UNKNOWN",
] as const;
export type Provenance = (typeof PROVENANCE)[number];

export const DATA_STATES = [
  "OK",
  "NO DATA",
  "PARTIAL DATA",
  "NOT MEASURED",
  "NOT CONFIGURED",
  "INSUFFICIENT HISTORY",
  "MODEL NOT VALIDATED",
] as const;

export const REFUSALS = ["NOT PERMITTED", "NOT LICENSED", "NOT FOUND", "INVALID ARGUMENTS", "FAILED"] as const;

// Words, not probabilities: AMP has no calibrated forecast, and a number would
// imply one. Each risk states the rule that earned its word (ADR-0026).
export const LIKELIHOOD = ["LIKELY", "POSSIBLE", "WATCH"] as const;

export const CAUSE_LABELS = [
  "CAUSE CONFIRMED",
  "LIKELY CONTRIBUTOR",
  "CORRELATED EVENT",
  "INSUFFICIENT EVIDENCE",
] as const;

// What a Copilot answer may offer to PROPOSE (ADR-0039). A closed set, because
// each kind names a path AMP can actually carry out — a draft AMP cannot
// execute is a promise it cannot keep. Nothing here is a write: raising the
// draft is a separate authenticated call, and approving it is another.
export const PROPOSABLE_KINDS = ["maintenance_task"] as const;
export type ProposableKind = (typeof PROPOSABLE_KINDS)[number];

export type Fact = {
  id: string;
  key: string;
  label: string;
  value: number | string | null;
  unit: string;
  provenance: string;
  source: string;
  window: string;
  detail: string;
};

export type ToolRun = {
  tool: string;
  state: string;
  summary: string;
  notes?: string[];
  elapsed_ms?: number | null;
};

export type Grounding = { passed: boolean; numbers_checked: number; reasons: string[] } | null;

/**
 * An action a Copilot answer drafted, ready for a person to raise (ADR-0039).
 *
 * It is a description, not a record: nothing exists in AMP when this arrives.
 * Raising it sends only `kind` and `machine_id` — the server re-derives the
 * priority, the task type and the wording from the machine itself, so a field
 * edited here can never become what is written.
 */
export type Proposal = {
  kind: ProposableKind;
  machine_id: number;
  machine: string;
  priority: string;
  task_type: string;
  summary: string;
  reason: string;
  label: string;
};

/** What each provenance means, in one line a plant manager can read. */
export const PROVENANCE_MEANING: Record<Provenance, string> = {
  "MEASURED FACT": "Read directly from recorded data.",
  "DERIVED METRIC": "Calculated by a fixed formula from recorded data.",
  CORRELATION: "Two things moved together. Not proof that one caused the other.",
  "RULE-BASED ASSESSMENT": "A fixed rule or threshold. Not machine learning.",
  "MODEL ESTIMATE": "A trained model's estimate, with its validation status.",
  UNKNOWN: "AMP does not have this figure, and says so instead of guessing.",
};

export type Tone = "measured" | "derived" | "correlation" | "rule" | "model" | "unknown";

/** The chip colour family for a provenance; an unrecognised one reads as unknown. */
export function provenanceTone(p: string): Tone {
  switch (p) {
    case "MEASURED FACT":
      return "measured";
    case "DERIVED METRIC":
      return "derived";
    case "CORRELATION":
      return "correlation";
    case "RULE-BASED ASSESSMENT":
      return "rule";
    case "MODEL ESTIMATE":
      return "model";
    default:
      return "unknown";
  }
}

/**
 * A fact's value as it should read. Money only when the unit IS the currency the
 * backend priced it in; "unknown" for a missing value, never a zero or a blank.
 */
export function formatFactValue(f: Pick<Fact, "value" | "unit">): string {
  if (f.value === null || f.value === undefined) return "unknown";
  if (typeof f.value === "string") return f.value;
  if (!Number.isFinite(f.value)) return "unknown";
  if (f.unit === CURRENCY) return money(f.value);
  const n = f.value.toLocaleString();
  if (!f.unit) return n;
  if (f.unit === "%") return `${n}%`;
  if (f.unit.startsWith("/")) return `${n}${f.unit}`;
  return `${n} ${f.unit}`;
}

export function isRefusal(state: string | null | undefined): boolean {
  return !!state && (REFUSALS as readonly string[]).includes(state);
}

/**
 * The sentence that goes above an answer whose data is not complete. Null for OK,
 * so a normal answer carries no banner at all.
 */
export function stateNotice(state: string | null | undefined): string | null {
  switch (state) {
    case "NO DATA":
      return "No data recorded for this yet, so there is nothing to measure.";
    case "PARTIAL DATA":
      return "Partial data: part of the plant did not report, so this covers only what did.";
    case "NOT MEASURED":
      return "Not measured: AMP has no reading for this.";
    case "NOT CONFIGURED":
      return "Not configured: this needs a setting (a plan, a target or a unit value) before AMP can answer it.";
    case "INSUFFICIENT HISTORY":
      return "Not enough history yet to answer this fairly.";
    case "MODEL NOT VALIDATED":
      return "Model not validated on a real plant: treat this as an experimental estimate.";
    case "NOT PERMITTED":
      return "Your role can't see this.";
    case "NOT LICENSED":
      return "This is in a module pack your workspace doesn't include.";
    case "NOT FOUND":
      return "AMP couldn't find that in your workspace.";
    case "INVALID ARGUMENTS":
      return "AMP couldn't run that request as asked.";
    case "FAILED":
      return "A read failed, so nothing was answered from it.";
    default:
      return null;
  }
}
