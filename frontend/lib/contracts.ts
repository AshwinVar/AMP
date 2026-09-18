import { API_URL, getAuthHeaders } from "./api";
import { OemRequestError, refusalText } from "./oem";

/**
 * Agreed downtime attribution for service contracts: the shared client (ADR-0021).
 *
 * A machine maker and a factory already sign service contracts (annual
 * maintenance, warranties, uptime clauses) and argue at month end about whose
 * fault the downtime was. AMP issues a statement that puts every covered second
 * of a covered machine into one of five buckets, each linked to its evidence,
 * and both parties accept the exact statement revision by its SHA-256. The
 * attribution from the factory's own MES reasons is what is new; metering and
 * shared usage ledgers already exist and nothing here claims otherwise. AMP
 * computes the credit and never invoices or moves money.
 *
 * This module is the one place both screens (the manufacturer's portal and the
 * factory's dashboard) take their wording and rules from, so the two parties to
 * one contract never read two versions of it:
 *
 *   * UNMEASURED is "No data". It is never uptime and never downtime.
 *   * Durations are exact integer seconds.
 *   * "Check consistency" hashes the EXACT downloaded bytes with WebCrypto.
 *   * An acceptance counts only for the revision it names.
 *
 * Nothing here decides what anybody may see or do; the server does. This only
 * decides how to say what came back, and disables a button the server would
 * refuse, with the server's reason.
 */

// ── Vocabulary ───────────────────────────────────────────────────────

export type Bucket = "AVAILABLE" | "OEM" | "FACTORY" | "DISPUTED" | "UNMEASURED";
export type Side = "OEM" | "FACTORY";

export const BUCKETS: readonly Bucket[] = ["AVAILABLE", "OEM", "FACTORY", "DISPUTED", "UNMEASURED"];
/** What a dispute may settle a window as. Never DISPUTED: a dispute must settle something. */
export const RESOLUTION_BUCKETS: readonly Bucket[] = ["AVAILABLE", "OEM", "FACTORY", "UNMEASURED"];

const BUCKET_LABEL: Record<Bucket, string> = {
  AVAILABLE: "Available",
  OEM: "OEM (machine fault)",
  FACTORY: "Factory",
  DISPUTED: "Disputed",
  UNMEASURED: "No data",
};

export const BUCKET_TONE: Record<Bucket, string> = {
  AVAILABLE: "bg-emerald-500/70",
  OEM: "bg-red-500/70",
  FACTORY: "bg-sky-500/70",
  DISPUTED: "bg-amber-400/80",
  // Grey and hatched-looking, never green or red: no data is not a verdict.
  UNMEASURED: "bg-slate-600/70",
};

export function bucketLabel(bucket: string): string {
  return BUCKET_LABEL[bucket as Bucket] ?? bucket;
}

// ── Response shapes ──────────────────────────────────────────────────

export type Totals = {
  covered_seconds: number;
  available_seconds: number;
  oem_seconds: number;
  factory_seconds: number;
  disputed_seconds: number;
  unmeasured_seconds: number;
};

const TOTAL_KEY: Record<Bucket, keyof Totals> = {
  AVAILABLE: "available_seconds",
  OEM: "oem_seconds",
  FACTORY: "factory_seconds",
  DISPUTED: "disputed_seconds",
  UNMEASURED: "unmeasured_seconds",
};

export type CoveredInstallation = { installation_id: number; serial_number: string };

export type Terms = {
  schema: 1;
  currency: string;
  period_months: number;
  period_fee: string;
  timezone: string;
  term_months: number;
  coverage:
    | { mode: "24x7" }
    | {
        mode: "weekly";
        windows: Array<{ days: number[]; start: string; end: string }>;
        excluded_dates?: string[];
      };
  sla_target_pct: string;
  credit_tiers: Array<{ below_pct: string; credit_pct: string }>;
  min_measured_pct: string;
  trusted_sources: string[];
  status_defaults: Record<string, string>;
  reason_map: Record<string, string>;
  generic_reasons: string[];
  reason_lead_seconds: number;
  termination_notice_days: number;
  covered_installations: CoveredInstallation[];
};

export type TermVersion = {
  version: number;
  status: string;
  terms_hash: string;
  terms: Terms;
  effective_from: string | null;
  proposed_by_party: Side | null;
  proposed_by: string | null;
  proposed_at: string | null;
  oem_accepted_by: string | null;
  oem_accepted_at: string | null;
  oem_accepted_hash: string | null;
  factory_accepted_by: string | null;
  factory_accepted_at: string | null;
  factory_accepted_hash: string | null;
  decision_note: string | null;
};

export type ContractSummary = {
  id: number;
  contract_ref: string;
  title: string;
  contract_type: string;
  oem_code: string;
  factory_tenant_code: string;
  status: string;
  state: string;
  starts_at: string;
  ends_at: string;
  effective_end: string;
  termination_effective_at: string | null;
};

export type ContractDetail = ContractSummary & {
  created_by: string;
  created_at: string;
  proposed_at: string | null;
  factory_accepted_by: string | null;
  factory_accepted_at: string | null;
  terminated_by_party: string | null;
  terminated_by: string | null;
  termination_reason: string | null;
  updated_at: string | null;
  downtime_shared: boolean;
  versions: TermVersion[];
};

export type PeriodRow = {
  start: string;
  end: string;
  terms_version: number | null;
  statement: { id: number; revision: number; agreed: boolean } | null;
};

export type PeriodsView = {
  contract_id: number;
  timezone: string;
  period_months: number;
  periods: PeriodRow[];
};

export type Interval = {
  start: string;
  end: string;
  seconds: number;
  bucket: Bucket;
  cause: string;
  evidence: Record<string, unknown>;
};

export type MachineStatement = {
  installation_id: number;
  serial_number: string;
  coverage_end: string | null;
  totals: Totals;
  intervals: Interval[];
};

export type Sla = {
  target_pct: string;
  min_measured_pct: string;
  measured_pct: string | null;
  base_seconds: number;
  availability_pct: string | null;
  availability_if_disputes_oem: string | null;
  availability_if_disputes_factory: string | null;
  availability_if_disputes_available: string | null;
  state: string | null;
  not_evaluable_reason: string | null;
};

export type Credit = {
  currency: string;
  period_fee: string;
  tier_below_pct: string | null;
  credit_pct: string | null;
  amount: string | null;
  range_min: string | null;
  range_max: string | null;
};

export type StatementContent = {
  schema: string;
  contract: {
    id: number;
    ref: string;
    oem_code: string;
    factory_tenant_code: string;
    terms_version: number;
    terms_hash: string;
  };
  period: { start: string; end: string; timezone: string };
  machines: MachineStatement[];
  totals: Totals;
  sla: Sla;
  credit: Credit;
};

export type Acceptance = {
  party: Side;
  actor: string;
  accepted_at: string;
  content_hash: string;
  revision: number;
  valid: boolean;
};

export type Dispute = {
  id: number;
  contract_id: number;
  statement_id: number;
  installation_id: number;
  window_start: string;
  window_end: string;
  raised_by_party: Side;
  raised_at: string;
  reason: string;
  proposed_bucket: string;
  status: string;
  resolution_bucket: string | null;
  resolution_note: string | null;
  resolution_proposed_by_party: Side | null;
  resolution_proposed_at: string | null;
  resolution_accepted_at: string | null;
  closed_at: string | null;
};

export type StatementPayload = {
  id: number;
  contract_id: number;
  term_version_id: number;
  period_start: string;
  period_end: string;
  revision: number;
  content_hash: string;
  computed_at: string;
  computed_by_party: Side;
  computed_by: string;
  content: StatementContent | null;
  content_purged: boolean;
  acceptances: Acceptance[];
  agreed: boolean;
  disputes: Dispute[];
};

export type PreviewPayload = {
  preview: true;
  as_of: string;
  period_start: string;
  period_end: string;
  content: StatementContent;
};

export type VerifyReport = {
  statement_id: number;
  revision: number;
  stored_hash: string;
  blob_hash: string | null;
  records_hash: string | null;
  consistency: "consistent" | "blob_mismatch" | "records_diverged" | "content_purged";
  acceptances: Acceptance[];
  agreed: boolean;
  live: { hash: string | null; matches: boolean | null; reason: string | null };
};

export type HistoryRow = {
  at: string;
  actor: string;
  action: string;
  entity_type: string;
  entity_id: number;
  details: string | null;
  details_withheld: boolean;
};

export type ReasonVocabulary = {
  terms_version: number;
  since: string;
  until: string;
  reasons: Array<{ reason_key: string; count: number; status: "mapped" | "generic" | "unmapped";
                   bucket: string | null }>;
};

export type StatementRef = {
  id: number;
  period_start?: string;
  period_end?: string;
  revision: number;
  content_hash?: string;
  withheld?: boolean;
};

export type ComputeResult = { statement: StatementRef; changed: boolean; frozen: boolean };

export type DisputeBody = {
  installation_id: number;
  window_start: string;
  window_end: string;
  reason: string;
  proposed_bucket: string;
};

/**
 * The operations both parties have on a contract. Each screen supplies its own
 * transport (the manufacturer's /oem/contracts, the factory's /service-contracts)
 * and the shared workspace drives either through this one interface.
 */
export interface ContractsApi {
  side: Side;
  list(): Promise<{ contracts: ContractSummary[]; truncated: boolean }>;
  detail(id: number): Promise<ContractDetail>;
  periods(id: number): Promise<PeriodsView>;
  preview(id: number): Promise<PreviewPayload>;
  compute(id: number, periodStart: string): Promise<ComputeResult>;
  statement(id: number, statementId: number): Promise<StatementPayload>;
  verify(id: number, statementId: number): Promise<VerifyReport>;
  downloadPath(id: number, statementId: number): string;
  acceptStatement(id: number, statementId: number, contentHash: string,
                  revision: number): Promise<StatementPayload>;
  raiseDispute(id: number, statementId: number, body: DisputeBody): Promise<unknown>;
  proposeResolution(id: number, disputeId: number, bucket: string, note: string): Promise<unknown>;
  acceptResolution(id: number, disputeId: number, bucket: string): Promise<unknown>;
  withdrawDispute(id: number, disputeId: number): Promise<unknown>;
  history(id: number): Promise<{ history: HistoryRow[]; truncated: boolean }>;
  terminate(id: number, reason: string): Promise<ContractDetail>;
  draftAmendment(id: number, terms: Terms, effectiveFrom: string): Promise<TermVersion>;
  proposeAmendment(id: number, version: number, termsHash: string): Promise<TermVersion>;
  acceptAmendment(id: number, version: number, termsHash: string): Promise<TermVersion>;
  rejectAmendment(id: number, version: number, note: string): Promise<TermVersion>;
}

// ── Formatting ───────────────────────────────────────────────────────

const pad2 = (n: number) => String(n).padStart(2, "0");

/** Exact integer seconds as "1 h 02 min 05 s". Anything else is "—", never a guess. */
export function formatDuration(seconds: number): string {
  if (!Number.isSafeInteger(seconds) || seconds < 0) return "—";
  if (seconds === 0) return "0 s";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const parts: string[] = [];
  if (h > 0) parts.push(`${h} h`);
  if (h > 0 || m > 0) parts.push(`${h > 0 ? pad2(m) : m} min`);
  if (s > 0) parts.push(`${h > 0 || m > 0 ? pad2(s) : s} s`);
  return parts.join(" ");
}

/** Each bucket's seconds and share of the covered time, in bucket order. */
export function bucketShares(totals: Totals): Array<{ bucket: Bucket; seconds: number; share: number }> {
  const covered = totals.covered_seconds;
  return BUCKETS.map((bucket) => {
    const seconds = totals[TOTAL_KEY[bucket]];
    return { bucket, seconds, share: covered > 0 ? seconds / covered : 0 };
  });
}

/** True when the five buckets add up to exactly the covered seconds. */
export function sharesTileCovered(totals: Totals): boolean {
  return BUCKETS.reduce((sum, b) => sum + totals[TOTAL_KEY[b]], 0) === totals.covered_seconds;
}

const SLA_STATE_LABEL: Record<string, string> = {
  met: "SLA met",
  breached: "SLA breached",
  pending_disputes: "Pending disputes",
  not_evaluable: "Not evaluable",
};

export function slaStateLabel(state: string | null): string {
  if (state === null) return SLA_STATE_LABEL.not_evaluable;
  return SLA_STATE_LABEL[state] ?? state;
}

const CONTRACT_STATE_LABEL: Record<string, string> = {
  draft: "Draft",
  proposed: "Awaiting factory acceptance",
  accepted: "Accepted",
  rejected: "Rejected",
  withdrawn: "Withdrawn",
  terminated: "Terminated",
  not_started: "Not started",
  active: "Active",
  ended: "Ended",
};

export function contractStateLabel(state: string): string {
  return CONTRACT_STATE_LABEL[state] ?? state;
}

/** A cause the engine wrote, in words. An unknown cause is shown as written. */
export function describeCause(cause: string): string {
  const [kind, rest] = cause.includes(":")
    ? [cause.slice(0, cause.indexOf(":")), cause.slice(cause.indexOf(":") + 1)]
    : [cause, ""];
  switch (kind) {
    case "status":
      return `Reported ${rest}`;
    case "status_default":
      return `${rest} with no explicit reason (the agreed default)`;
    case "reason":
      return `Factory reason: ${rest}`;
    case "open_dispute":
      return `Under dispute ${rest}`;
    case "agreed_override":
      return `Agreed resolution of dispute ${rest}`;
    case "no_telemetry":
      return "No trusted telemetry received";
    case "conflicting_status":
      return "Trusted sources disagreed";
    case "unrecognised_status":
      return "A status the terms do not recognise";
    case "unmapped_reason":
      return "A reason the terms do not map";
    case "conflicting_reasons":
      return "Reasons for both parties";
    case "installation_unlinked":
      return "Installation unlinked from the accepted machine";
    default:
      return cause;
  }
}

type Rec = Record<string, unknown>;
const list = (value: unknown): Rec[] => (Array.isArray(value) ? (value as Rec[]) : []);

/** The evidence of an interval as short lines: only what the server put there. */
export function evidenceLines(evidence: Rec | null | undefined): string[] {
  if (!evidence) return [];
  const out: string[] = [];
  if ("telemetry_spans" in evidence) {
    const spans = list(evidence.telemetry_spans);
    if (spans.length === 0) out.push("no telemetry span held this time");
    for (const s of spans) {
      out.push(`${s.source} span #${s.id}: ${s.status} ${s.start} to ${s.end}`);
    }
  }
  for (const l of list(evidence.downtime_logs)) {
    out.push(`downtime log #${l.id} at ${l.at}: “${l.reason}”`);
  }
  for (const d of list(evidence.disputes)) {
    out.push(`dispute #${d.id}: ${d.status}`
      + (d.resolution_bucket ? ` as ${bucketLabel(String(d.resolution_bucket))}` : ""));
  }
  if (evidence.linkage && typeof evidence.linkage === "object") {
    const link = evidence.linkage as Rec;
    out.push(`coverage ended ${link.coverage_ended_at} (${link.reason})`);
  }
  return out;
}

export function shortHash(hash: string | null): string {
  return hash ? `${hash.slice(0, 12)}…` : "—";
}

// ── Acceptance ───────────────────────────────────────────────────────

/** Acceptances grouped by the revision they name, newest revision first. */
export function acceptanceByRevision(acceptances: Acceptance[]): Array<{ revision: number; rows: Acceptance[] }> {
  const groups = new Map<number, Acceptance[]>();
  for (const a of acceptances) {
    groups.set(a.revision, [...(groups.get(a.revision) ?? []), a]);
  }
  return [...groups.entries()]
    .sort((a, b) => b[0] - a[0])
    .map(([revision, rows]) => ({ revision, rows }));
}

/** This side's VALID acceptance (the server's verdict), or null. */
export function currentAcceptance(acceptances: Acceptance[], side: Side): Acceptance | null {
  return acceptances.find((a) => a.party === side && a.valid) ?? null;
}

const LIVE_DISPUTE_STATUSES = ["open", "resolution_proposed"];

export function liveDisputes(disputes: Dispute[]): Dispute[] {
  return disputes.filter((d) => LIVE_DISPUTE_STATUSES.includes(d.status));
}

/**
 * Why this side cannot accept this statement now, in the server's own terms.
 * Empty when it can. The server re-checks all of it; this exists so a button is
 * never offered only to be refused.
 */
export function acceptBlockers(statement: StatementPayload, side: Side): string[] {
  if (statement.agreed) return ["Both parties accepted this revision; it is final"];
  if (statement.content_purged || statement.content === null) {
    return ["The content of this statement was removed; it cannot be accepted"];
  }
  const mine = currentAcceptance(statement.acceptances, side);
  if (mine) return [`You have already accepted revision ${mine.revision}`];
  const out: string[] = [];
  const live = liveDisputes(statement.disputes).length;
  if (live > 0) {
    out.push(`${live} open dispute${live === 1 ? "" : "s"} must be resolved or withdrawn first`);
  }
  if (statement.content.sla.state === "pending_disputes" && live === 0) {
    out.push("Part of this statement is Disputed: raise a dispute over that time and "
      + "resolve it first");
  }
  return out;
}

// ── Consistency check ────────────────────────────────────────────────

function hex(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer), (b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * SHA-256 of the EXACT bytes a party downloaded, compared with a hash.
 *
 * Never a re-serialisation: parsed-and-restringified JSON is the same data in
 * different bytes, and the hash both parties accepted is over the bytes.
 */
export async function verifyCanonical(bytes: ArrayBuffer | Uint8Array,
                                      expectedHash: string): Promise<{ hash: string; matches: boolean }> {
  const data = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  const hash = hex(await globalThis.crypto.subtle.digest("SHA-256", data as BufferSource));
  return { hash, matches: hash === expectedHash };
}

/** The stored statement bytes, with the hash and revision the server sent beside them. */
export async function fetchStatementBytes(path: string): Promise<{
  bytes: ArrayBuffer; hash: string | null; revision: string | null;
}> {
  const response = await fetch(`${API_URL}${path}`, { headers: getAuthHeaders() });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`Failed request: ${path} | ${response.status} | ${text}`);
  }
  return {
    bytes: await response.arrayBuffer(),
    hash: response.headers.get("X-Content-SHA256"),
    revision: response.headers.get("X-Statement-Revision"),
  };
}

// ── Refusals ─────────────────────────────────────────────────────────

function parseBody(text: string): unknown {
  try {
    const body = JSON.parse(text);
    return body && typeof body === "object" && "detail" in body ? body.detail : text;
  } catch {
    return text;
  }
}

/**
 * A refusal in any of the shapes it arrives in: the OEM client's
 * OemRequestError, apiGet's "Failed request: path | status | body", or
 * apiPost's raw body. `withheld` is the server saying the factory has withdrawn
 * SHARE_DOWNTIME — a consent state to render as such, never as an error.
 */
export function contractError(e: unknown): { status: number | null; detail: unknown;
                                             message: string; withheld: boolean } {
  let status: number | null = null;
  let detail: unknown;
  if (e instanceof OemRequestError) {
    status = e.status;
    detail = e.detail;
  } else if (e instanceof Error) {
    const failed = /^Failed request: .*? \| (\d{3}) \| ([\s\S]*)$/.exec(e.message);
    if (failed) {
      status = Number(failed[1]);
      detail = parseBody(failed[2]);
    } else {
      detail = parseBody(e.message);
    }
  } else {
    detail = String(e);
  }
  const withheld = Boolean(detail && typeof detail === "object"
    && (detail as Rec).withheld === true);
  const message = refusalText(detail) ?? (e instanceof Error ? e.message : String(e));
  return { status, detail, message, withheld };
}

// ── Time ─────────────────────────────────────────────────────────────

/** A period can be computed once it has ended (the server adds its settling time). */
export function closedPeriod(period: { end: string }, now: number = Date.now()): boolean {
  return Date.parse(period.end) <= now;
}

const UTC_INPUT = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?Z?$/;

/** "2026-05-02T08:30" (UTC) -> "2026-05-02T08:30:00Z", or null for anything else. */
export function parseUtcInput(text: string): string | null {
  const m = UTC_INPUT.exec(text.trim());
  if (!m) return null;
  const [year, month, day, hour, minute, second] =
    [m[1], m[2], m[3], m[4], m[5], m[6] ?? "00"].map(Number);
  const date = new Date(Date.UTC(year, month - 1, day, hour, minute, second));
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1
      || date.getUTCDate() !== day || date.getUTCHours() !== hour
      || date.getUTCMinutes() !== minute || date.getUTCSeconds() !== second) {
    return null;
  }
  return `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${pad2(second)}Z`;
}

/**
 * The "YYYY-MM" a contract starts in: the month of `startsAt` in the contract's
 * own timezone. The server stores a draft's start_month as local midnight on the
 * 1st, in UTC (contract_periods.month_start_utc), so this is its exact inverse.
 * Reading the month off the UTC string is wrong east of UTC: an Asia/Kolkata May
 * starts at 2026-04-30T18:30:00Z.
 */
export function startMonthOf(startsAt: string, timeZone: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit" })
    .formatToParts(new Date(startsAt));
  const part = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}`;
}

// ── Drafting ─────────────────────────────────────────────────────────

/**
 * The terms a new contract starts from: the plan's conservative defaults.
 * Only MQTT is trusted (every other status source is posted by the factory),
 * Offline is disputed rather than assigned, and MQTT's automatic "Breakdown"
 * reason counts as no reason. The manufacturer edits all of it before
 * proposing, and the factory sees every line before accepting.
 */
export function termsTemplate(installations: CoveredInstallation[]): Terms {
  return {
    schema: 1,
    currency: "INR",
    period_months: 1,
    period_fee: "40000.00",
    timezone: "Asia/Kolkata",
    term_months: 12,
    coverage: { mode: "24x7" },
    sla_target_pct: "97.00",
    credit_tiers: [
      { below_pct: "97.00", credit_pct: "5.00" },
      { below_pct: "95.00", credit_pct: "10.00" },
    ],
    min_measured_pct: "90.00",
    trusted_sources: ["mqtt"],
    status_defaults: { Breakdown: "OEM", Maintenance: "FACTORY", Offline: "DISPUTED" },
    reason_map: { "no material": "FACTORY", "no operator": "FACTORY", "power failure": "FACTORY",
                  "planned stop": "FACTORY", "changeover": "FACTORY" },
    generic_reasons: ["breakdown", "unknown"],
    reason_lead_seconds: 600,
    termination_notice_days: 30,
    covered_installations: installations,
  };
}
