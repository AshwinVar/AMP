/**
 * AMP-native AI on the client (ADR-0020): the shapes the backend sends and the
 * pure decisions about how to say them.
 *
 * The property worth testing is HONESTY, not layout. Every model AMP ships was
 * evaluated on synthetic machines only; two of the three did NOT beat the
 * existing rules and are not adopted. A screen that rounds "experimental" up to
 * "AI-powered", shows a model's number without the baseline's, calls a
 * difference whose interval straddles zero "better", or styles an experimental
 * anomaly score as an alarm, would say something the evaluation does not. So the
 * wording lives here, where it is tested, and the components only render it.
 */
import { parseApiDate } from "./apiDate";

// ── Shapes (backend/amp_ai/registry.py, amp_ai/consent.py, native_ai_routes.py) ──

export type MetricUnit = "percent" | "score";

/** One held-out metric for the model and its baseline, on the SAME data. */
export type HeadlineRow = {
  metric: string;
  dataset: string;
  unit?: MetricUnit;
  higher_is_better: boolean;
  model: number;
  model_lo: number | null;
  model_hi: number | null;
  baseline_name: string;
  baseline: number;
  baseline_lo: number | null;
  baseline_hi: number | null;
  difference: number | null;
  difference_lo: number | null;
  difference_hi: number | null;
};

export type ModelCard = {
  name: string;
  title: string;
  purpose: string;
  baseline: string;
  default_behaviour: string;
  caveat: string;
  available: boolean;
  status: "adopted" | "experimental" | "unavailable";
  adopted: boolean | null;
  reason: string | null;
  reasons: string[];
  headline: HeadlineRow[];
  /** Known weaknesses a reader must see before quoting a number (registry.py builds them from the evaluation). */
  limitations?: string[];
  /** The same comparison on deliberately misspecified synthetic data (stress tests). */
  misspecification_rows?: HeadlineRow[];
  sha256_pinned: string;
  version?: string | null;
  model_name?: string | null;
  created_at?: string | null;
  sha256?: string | null;
  training_data_source?: string | null;
  consent_basis?: string | null;
  generator?: string | null;
  generator_sha256?: string | null;
  seed?: number | null;
};

export type ModelCardsResponse = { models: ModelCard[]; caveat: string };

export type ConsentCapability = {
  capability: string;
  title: string;
  reads: string;
  stored: string;
  used_by: string;
  without: string;
  // ADR-0038: a SCOPED consent names what it was given for (the hosted provider
  // configured when the Admin said yes) and holds only while that is still what
  // is configured. `scope` is what the grant named, `configured` what AMP would
  // use now (null when nothing hosted is configured), `active` whether the grant
  // applies. A learning consent is unscoped: active whenever granted.
  scoped?: boolean;
  scope?: string | null;
  configured?: string | null;
  active?: boolean;
  granted: boolean;
  granted_by: string | null;
  granted_at: string | null;
  revoked_by: string | null;
  revoked_at: string | null;
  updated_at: string | null;
};

export type ConsentPage = {
  tenant: string;
  can_edit: boolean;
  read_only_reason: string | null;
  capabilities: ConsentCapability[];
};

/** The GET /ai/native/anomaly/machines/{id} 200 body, as far as the UI reads it. */
export type AnomalyResult = {
  status: "ok" | "insufficient_history" | "model_unavailable";
  score: number | null;
  reason?: string | null;
  needed?: Record<string, number>;
  have?: Record<string, number>;
  simulated_source?: boolean | null;
  deviating?: { signal: string; direction?: string; z?: number }[];
  evaluation?: {
    adopted: boolean;
    experimental: boolean;
    caveat: string;
    /** The score at or above which the evaluation counted an alarm. */
    alarm_score?: number | null;
    /** How often clean held-out (synthetic) hours reached alarm_score: measured, not nominal. */
    clean_false_alarm_rate?: { estimate: number | null; lo: number | null; hi: number | null } | null;
  };
};

// ── Numbers ──────────────────────────────────────────────────────────────────

function finite(x: number | null | undefined): x is number {
  return typeof x === "number" && Number.isFinite(x);
}

/** 0.1937 -> "0.194" (a score) or "19.4%" (a proportion). Missing -> "—", never 0. */
export function formatMetricValue(value: number | null | undefined, unit: MetricUnit = "score"): string {
  if (!finite(value)) return "—";
  return unit === "percent" ? `${(value * 100).toFixed(1)}%` : value.toFixed(3);
}

/** A signed difference: "+0.099", "−0.103", "+3.1 pts". */
export function formatDifference(value: number | null | undefined, unit: MetricUnit = "score"): string {
  if (!finite(value)) return "—";
  const sign = value > 0 ? "+" : value < 0 ? "−" : "±";
  const magnitude = Math.abs(value);
  return unit === "percent" ? `${sign}${(magnitude * 100).toFixed(1)} pts` : `${sign}${magnitude.toFixed(3)}`;
}

/** "[0.103, 0.306]" for a 95% interval; "" when either end is missing (never a made-up bound). */
export function formatInterval(
  lo: number | null | undefined,
  hi: number | null | undefined,
  unit: MetricUnit = "score",
  signed = false,
): string {
  if (!finite(lo) || !finite(hi)) return "";
  const f = signed ? formatDifference : formatMetricValue;
  return `[${f(lo, unit)}, ${f(hi, unit)}]`;
}

export type Comparison = {
  verdict: "better" | "worse" | "unclear" | "equal";
  /** True when the verdict comes from a paired 95% interval, false for point estimates only. */
  fromInterval: boolean;
  text: string;
};

/**
 * How the model compares with its baseline on one row.
 *
 * "better" or "worse" is claimed from a paired interval only when the interval
 * EXCLUDES zero. Without an interval the point estimates are compared and the
 * text says that is all it is.
 */
export function compareToBaseline(row: HeadlineRow): Comparison {
  const sign = row.higher_is_better ? 1 : -1;
  if (finite(row.difference_lo) && finite(row.difference_hi)) {
    if (row.difference_lo > 0 || row.difference_hi < 0) {
      const up = row.difference_lo > 0;
      const better = (up ? 1 : -1) * sign > 0;
      return {
        verdict: better ? "better" : "worse",
        fromInterval: true,
        text: `${better ? "Better" : "Worse"} than the ${row.baseline_name} (95% interval excludes zero)`,
      };
    }
    return {
      verdict: "unclear",
      fromInterval: true,
      text: `No clear difference from the ${row.baseline_name} (95% interval includes zero)`,
    };
  }
  if (!finite(row.model) || !finite(row.baseline)) {
    return { verdict: "unclear", fromInterval: false, text: "Not enough data to compare" };
  }
  if (row.model === row.baseline) {
    return { verdict: "equal", fromInterval: false, text: `Equal to the ${row.baseline_name}` };
  }
  const better = (row.model > row.baseline ? 1 : -1) * sign > 0;
  return {
    verdict: better ? "better" : "worse",
    fromInterval: false,
    text: `${better ? "Better" : "Worse"} than the ${row.baseline_name} on the point estimate only (no interval)`,
  };
}

// ── Verdicts and labels ────────────────────────────────────────────────────────

export type VerdictBadge = { label: string; tone: "adopted" | "experimental" | "unavailable" };

export function verdictBadge(card: Pick<ModelCard, "available" | "adopted">): VerdictBadge {
  if (!card.available) return { label: "Unavailable", tone: "unavailable" };
  if (card.adopted === true) return { label: "Adopted", tone: "adopted" };
  return { label: "Experimental · not adopted", tone: "experimental" };
}

/**
 * What a model was trained and evaluated on, per model, because it differs. The
 * anomaly check trains nothing ahead of time: its EVALUATION is synthetic, but
 * every request fits a baseline from the machine's own telemetry (with consent),
 * so "trained on synthetic data only" would be false for it. A model this
 * function does not know gets no claim at all.
 */
export function dataLabel(card: Pick<ModelCard, "name">): string {
  switch (card.name) {
    case "failure_risk":
      return "Trained and evaluated on synthetic data only";
    case "telemetry_anomaly":
      return "Evaluated on synthetic data only · each check fits a baseline from this machine's own telemetry, with consent";
    case "copilot_intent":
      return "Trained and evaluated on AMP-authored questions only";
    default:
      return "See Provenance for what this model was trained and evaluated on";
  }
}

/** "failure_risk@v1 · sha256 ad30970f…" — what a support conversation needs to identify a model. */
export function modelIdentity(card: Pick<ModelCard, "model_name" | "version" | "sha256" | "sha256_pinned">): string {
  const name = card.model_name && card.version ? `${card.model_name}@${card.version}` : null;
  const hash = card.sha256 || card.sha256_pinned;
  const short = hash ? `sha256 ${hash.slice(0, 8)}…` : null;
  return [name, short].filter(Boolean).join(" · ");
}

// ── The copilot's answer badge ─────────────────────────────────────────────────

export type CopilotTurnSource = {
  source?: string;
  route_source?: string;
  confidence?: number | null;
  model?: string | null;
};

export type CopilotBadge = { text: string; tone: "llm" | "native" | "rules" };

/**
 * Which engine answered: an LLM, AMP's own intent model, or the keyword rules.
 *
 * The model's confidence is NOT shown as a percentage: it is a raw softmax value,
 * not calibrated, and its routing threshold was set so that confident answers were
 * right about 90% of the time on validation - a "59%" badge would understate that
 * and a "91%" one would claim a calibration nobody measured.
 */
export function copilotBadge(turn: CopilotTurnSource): CopilotBadge {
  if (turn.source === "llm") return { text: `✦ AI · ${turn.model || "model"}`, tone: "llm" };
  if (turn.route_source === "model" && finite(turn.confidence)) {
    return { text: "AMP native · model-routed", tone: "native" };
  }
  return { text: "instant · rules", tone: "rules" };
}

// ── Consent ────────────────────────────────────────────────────────────────────

function day(iso: string | null): string | null {
  const d = parseApiDate(iso);
  return d ? d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : null;
}

/** A provider's name as the card says it. */
export function providerLabel(name: string | null | undefined): string {
  if (!name) return "";
  const known: Record<string, string> = { anthropic: "Anthropic", gemini: "Google Gemini", local: "the self-hosted model" };
  return known[name] ?? name;
}

/**
 * "On · turned on by ta-admin on 17 Sept 2026" / "Off · turned off by …" / "Off · never turned on".
 * A scoped grant says what it was given for; one given for a provider that is
 * no longer the configured one says so and is NOT active (ADR-0038).
 */
export function consentStateText(cap: ConsentCapability): string {
  if (cap.granted) {
    const when = day(cap.granted_at);
    const by = `turned on by ${cap.granted_by || "an Admin"}${when ? ` on ${when}` : ""}`;
    if (cap.scoped) {
      const given = cap.scope ? `for ${providerLabel(cap.scope)}` : "before AMP recorded which provider a consent is for";
      if (cap.active) return `On ${given} · ${by}`;
      if (!cap.configured) return `Not active · given ${given}, ${by} · no hosted AI provider is configured now`;
      return `Not active · given ${given}, ${by} · ${providerLabel(cap.configured)} is configured now — turn it on again to allow ${providerLabel(cap.configured)}`;
    }
    return `On · ${by}`;
  }
  if (cap.revoked_at || cap.revoked_by) {
    const when = day(cap.revoked_at);
    return `Off · turned off by ${cap.revoked_by || "an Admin"}${when ? ` on ${when}` : ""}`;
  }
  if (cap.scoped && !cap.configured) return "Off · no hosted AI provider is configured, so there is nothing to consent to";
  return "Off · never turned on";
}

/** Whether the switch can be turned ON: a scoped consent needs something configured to be given for. */
export function consentCanTurnOn(cap: ConsentCapability): boolean {
  return !cap.scoped || !!cap.configured;
}

// ── The anomaly check ─────────────────────────────────────────────────────────────

const REQUIREMENT_LABELS: Record<string, string> = {
  fit_buckets_per_signal: "five-minute readings in the machine's current state",
  distinct_days: "days of telemetry",
  calibration_buckets: "recent five-minute readings to calibrate against",
  scorable_signals: "signals with enough history",
};

export type AnomalyView = {
  kind: "consent_required" | "preview_not_allowed" | "insufficient_history" | "unavailable" | "scored" | "error";
  /** Experimental scores are NEVER an alert; only an adopted evaluation could make one. */
  alert: false;
  text: string;
  detail: string | null;
};

/** Read what lib/api.ts threw for GET: "Failed request: <path> | <status> | <body>". */
function refusalBody(message: string): { status: number | null; body: Record<string, unknown> | null } {
  const parts = message.split(" | ");
  const status = parts.length >= 3 ? Number(parts[1]) : NaN;
  let body: Record<string, unknown> | null = null;
  try {
    const parsed = JSON.parse(parts.length >= 3 ? parts.slice(2).join(" | ") : message);
    body = parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null;
  } catch {
    body = null;
  }
  return { status: Number.isFinite(status) ? status : null, body };
}

export function describeAnomalyError(error: unknown): AnomalyView {
  const message = error instanceof Error ? error.message : String(error ?? "");
  const { status, body } = refusalBody(message);
  if (status === 403 && body?.code === "learning_consent_required") {
    return {
      kind: "consent_required",
      alert: false,
      text: "Learning from telemetry is off for this company. An Admin can turn it on under AI consent.",
      detail: typeof body.reason === "string" ? body.reason : null,
    };
  }
  if (status === 403 && body?.code === "learning_not_from_preview") {
    // A founder previewing a customer: the customer's consent covers its own
    // Admins and Supervisors, so the check does not run from a preview at all.
    return {
      kind: "preview_not_allowed",
      alert: false,
      text: "The anomaly check learns from this company's telemetry, so it does not run from a platform preview.",
      detail: typeof body.reason === "string" ? body.reason : null,
    };
  }
  if (status === 404) {
    return { kind: "error", alert: false, text: "That machine was not found.", detail: null };
  }
  return { kind: "error", alert: false, text: "The anomaly check could not be run. Please try again.", detail: null };
}

export function describeAnomaly(result: AnomalyResult): AnomalyView {
  if (result.status === "insufficient_history") {
    const needed = result.needed || {};
    const have = result.have || {};
    const short = Object.keys(REQUIREMENT_LABELS).find(
      (k) => finite(needed[k]) && finite(have[k]) && have[k] < needed[k],
    );
    const text = short
      ? `Not enough history yet (have ${have[short]} of ${needed[short]} ${REQUIREMENT_LABELS[short]}).`
      : "Not enough history yet.";
    return { kind: "insufficient_history", alert: false, text, detail: null };
  }
  if (result.status !== "ok" || !finite(result.score)) {
    return {
      kind: "unavailable",
      alert: false,
      text: "The anomaly check is unavailable right now.",
      detail: result.reason || null,
    };
  }
  const experimental = result.evaluation?.adopted !== true;
  const signals = (result.deviating || []).map((d) => d.signal);
  const simulated = result.simulated_source === true ? " The telemetry source is a simulator." : "";
  // The score is a mid-rank against this machine's own recent hours (baseline.py), not a probability and
  // not a mark out of 100; the only honest reading of a high one is the false-alarm rate MEASURED at the alarm level.
  const alarm = result.evaluation?.alarm_score;
  const rate = result.evaluation?.clean_false_alarm_rate?.estimate;
  const measured =
    finite(alarm) && finite(rate)
      ? `In its synthetic evaluation, ${(rate * 100).toFixed(1)}% of clean hours were rarer than ${Math.round(alarm * 100)}% of their machine's recent hours (false alarms at that level).`
      : null;
  const parts = [signals.length ? `Furthest from normal: ${signals.join(", ")}` : null, measured].filter(Boolean);
  return {
    kind: "scored",
    alert: false,
    text:
      `${experimental ? "Experimental: the" : "The"} last hour is rarer than ${Math.floor(result.score * 100)}% of` +
      ` this machine's recent hours (its own previous 14 days). A rank, not a probability of a fault.${simulated}`,
    detail: parts.length ? parts.join(" ") : null,
  };
}

// ── The failure-risk model's own estimates (ADR-0027) ─────────────────────────

/** One machine as /ai/native/failure-risk returns it. */
export type FailureRiskRow = {
  machine_id: number;
  name: string;
  probability: number | null;
  band: string | null;
  rule_score: number;
  rule_level: string;
  excluded_reason: string | null;
};

export type FailureRiskResponse = {
  status: string;
  model_version: string | null;
  adopted: boolean | null;
  caveat: string;
  horizon_days?: number;
  machines: FailureRiskRow[];
};

export type FailureRiskView = {
  unavailable: string | null;
  headline: string;
  rows: { id: number; name: string; estimate: string; band: string; rule: string }[];
  excluded: string | null;
};

/**
 * How the failure-risk model's numbers may be said on a screen.
 *
 * Three rules, and they are the reason this lives here rather than in the
 * component: the estimate is never shown without the rule score beside it, the
 * word "estimate" is never dropped, and a machine the model refused to score
 * says so instead of showing a blank that reads as zero. The model is adopted
 * but evaluated on SYNTHETIC machines only, so nothing here may be worded as a
 * measurement, a forecast or an instruction.
 */
export function describeFailureRisk(res: FailureRiskResponse | null): FailureRiskView {
  const horizon = res?.horizon_days ? `next ${res.horizon_days} days` : "coming days";
  const headline =
    `Estimated chance each machine STARTS a new breakdown in the ${horizon}, beside the rule score ` +
    `for the same machine. An estimate, not a measurement.`;
  if (!res || res.status !== "ok") {
    return {
      unavailable:
        "AMP's failure-risk model is not available right now, so there is no estimate to show. " +
        "The rule-based health score is unaffected.",
      headline,
      rows: [],
      excluded: null,
    };
  }
  const scored = res.machines.filter((m) => m.probability != null);
  const skipped = res.machines.filter((m) => m.excluded_reason === "already_in_breakdown");
  scored.sort((a, b) => (b.probability as number) - (a.probability as number));
  return {
    unavailable: null,
    headline,
    rows: scored.map((m) => ({
      id: m.machine_id,
      name: m.name,
      estimate: `${((m.probability as number) * 100).toFixed(1)}%`,
      band: m.band ?? "—",
      rule: `${Math.round(m.rule_score)}/100 · ${m.rule_level}`,
    })),
    excluded: skipped.length
      ? `${skipped.length} machine${skipped.length === 1 ? "" : "s"} already in breakdown ` +
        `${skipped.length === 1 ? "was" : "were"} not scored: the model estimates a NEW breakdown starting.`
      : null,
  };
}

// ── The anomaly check across the whole fleet (ADR-0032) ──────────────────────

export type AnomalyRow = {
  machine_id: number;
  name: string;
  line: string;
  score: number | null;
  state: string;
  reason: string | null;
};

export type AnomalySweep = {
  state: string;
  headline: string;
  machines: AnomalyRow[];
  scored: number;
  not_scored: number;
  consent: boolean;
  note: string;
};

/** One row of the fleet sweep, as this screen may say it. */
export type AnomalySweepRow = { id: number; name: string; value: string; muted: boolean };

/**
 * How a fleet sweep may be read out.
 *
 * The rule that matters: a machine AMP could not score shows its REASON, never
 * a blank and never a zero. A missing number and a number of zero are different
 * claims, and on this screen the difference is "we have not looked at that one"
 * against "we looked and it is quiet".
 *
 * `muted` is true for a row with no score, so the component can render it
 * quietly WITHOUT dropping it — the caller may not filter these out.
 */
export function describeSweepRow(row: AnomalyRow): AnomalySweepRow {
  if (row.score === null) {
    return { id: row.machine_id, name: row.name, muted: true,
             value: row.reason ? `not scored — ${row.reason}` : "not scored" };
  }
  return { id: row.machine_id, name: row.name, muted: false, value: row.score.toFixed(2) };
}
