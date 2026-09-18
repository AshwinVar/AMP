/**
 * Fixtures for the service-contract component tests (ADR-0020). Shapes follow
 * backend/service_contracts.py and backend/contract_statements.py exactly.
 */
import type {
  Acceptance,
  ContractDetail,
  ContractsApi,
  Dispute,
  PeriodsView,
  StatementContent,
  StatementPayload,
  TermVersion,
} from "../../lib/contracts";
import { termsTemplate } from "../../lib/contracts";

export const HASH = "3ef65c41cac109befb92d5ad6ab272074b1c06b0e78fb1958ba42bac66e69aa3";

export function content(over: Partial<StatementContent> = {}): StatementContent {
  return {
    schema: "amp.downtime-attribution-statement/1",
    contract: { id: 7, ref: "AMC-7", oem_code: "OEM_ALPHA", factory_tenant_code: "FACTORY_A",
                terms_version: 1, terms_hash: "t".repeat(64) },
    period: { start: "2026-04-30T18:30:00Z", end: "2026-05-31T18:30:00Z", timezone: "Asia/Kolkata" },
    machines: [{
      installation_id: 12,
      serial_number: "AER-0042",
      coverage_end: null,
      totals: { covered_seconds: 2_678_400, available_seconds: 2_600_000, oem_seconds: 7_200,
                factory_seconds: 36_000, disputed_seconds: 3_600, unmeasured_seconds: 31_600 },
      intervals: [
        { start: "2026-05-02T08:00:00Z", end: "2026-05-02T10:00:00Z", seconds: 7_200,
          bucket: "OEM", cause: "status_default:Breakdown",
          evidence: { telemetry_spans: [{ id: 3, source: "mqtt", status: "Breakdown",
                                          start: "2026-05-02T08:00:00Z", end: "2026-05-02T10:00:00Z" }],
                      downtime_logs: [] } },
        { start: "2026-05-03T00:00:00Z", end: "2026-05-03T08:46:40Z", seconds: 31_600,
          bucket: "UNMEASURED", cause: "no_telemetry", evidence: { telemetry_spans: [] } },
      ],
    }],
    totals: { covered_seconds: 2_678_400, available_seconds: 2_600_000, oem_seconds: 7_200,
              factory_seconds: 36_000, disputed_seconds: 3_600, unmeasured_seconds: 31_600 },
    sla: { target_pct: "97.00", min_measured_pct: "90.00", measured_pct: "98.82",
           base_seconds: 2_610_800, availability_pct: "96.27",
           availability_if_disputes_oem: null, availability_if_disputes_factory: null,
           availability_if_disputes_available: null, state: "breached", not_evaluable_reason: null },
    credit: { currency: "INR", period_fee: "1234567.00", tier_below_pct: "97.00",
              credit_pct: "5.00", amount: "61728.35", range_min: null, range_max: null },
    ...over,
  };
}

export function acceptance(over: Partial<Acceptance> = {}): Acceptance {
  return { party: "OEM", actor: "oem:OEM_ALPHA:alpha_admin", accepted_at: "2026-06-02T10:00:00Z",
           content_hash: HASH, revision: 2, valid: true, ...over };
}

export function dispute(over: Partial<Dispute> = {}): Dispute {
  return { id: 4, contract_id: 7, statement_id: 5, installation_id: 12,
           window_start: "2026-05-02T08:00:00Z", window_end: "2026-05-02T09:00:00Z",
           raised_by_party: "FACTORY", raised_at: "2026-06-02T00:00:00Z",
           reason: "planned stop for a changeover", proposed_bucket: "FACTORY", status: "open",
           resolution_bucket: null, resolution_note: null, resolution_proposed_by_party: null,
           resolution_proposed_at: null, resolution_accepted_at: null, closed_at: null, ...over };
}

export function statement(over: Partial<StatementPayload> = {}): StatementPayload {
  return { id: 5, contract_id: 7, term_version_id: 1, period_start: "2026-04-30T18:30:00Z",
           period_end: "2026-05-31T18:30:00Z", revision: 2, content_hash: HASH,
           computed_at: "2026-06-01T00:00:00Z", computed_by_party: "OEM",
           computed_by: "oem:OEM_ALPHA:alpha_admin", content: content(), content_purged: false,
           acceptances: [], agreed: false, disputes: [], ...over };
}

export function version(over: Partial<TermVersion> = {}): TermVersion {
  return { version: 1, status: "proposed", terms_hash: "h".repeat(64),
           terms: termsTemplate([{ installation_id: 12, serial_number: "AER-0042" }]),
           effective_from: "2026-04-30T18:30:00Z", proposed_by_party: "OEM",
           proposed_by: "alpha_admin", proposed_at: "2026-04-20T00:00:00Z",
           oem_accepted_by: "alpha_admin", oem_accepted_at: "2026-04-20T00:00:00Z",
           oem_accepted_hash: "h".repeat(64), factory_accepted_by: null,
           factory_accepted_at: null, factory_accepted_hash: null, decision_note: null, ...over };
}

export function detail(over: Partial<ContractDetail> = {}): ContractDetail {
  return { id: 7, contract_ref: "AMC-7", title: "Annual maintenance", contract_type: "AMC",
           oem_code: "OEM_ALPHA", factory_tenant_code: "FACTORY_A", status: "proposed",
           state: "proposed", starts_at: "2026-04-30T18:30:00Z", ends_at: "2027-04-30T18:30:00Z",
           effective_end: "2027-04-30T18:30:00Z", termination_effective_at: null,
           created_by: "alpha_admin", created_at: "2026-04-19T00:00:00Z",
           proposed_at: "2026-04-20T00:00:00Z", factory_accepted_by: null, factory_accepted_at: null,
           terminated_by_party: null, terminated_by: null, termination_reason: null,
           updated_at: null, downtime_shared: true, versions: [version()], ...over };
}

export function periods(over: Partial<PeriodsView> = {}): PeriodsView {
  return { contract_id: 7, timezone: "Asia/Kolkata", period_months: 1,
           periods: [{ start: "2026-04-30T18:30:00Z", end: "2026-05-31T18:30:00Z",
                       terms_version: 1, statement: { id: 5, revision: 2, agreed: false } }],
           ...over };
}

/** A ContractsApi whose every method is a vi.fn() the test configures. */
export function fakeApi(side: "OEM" | "FACTORY", make: (name: string) => unknown): ContractsApi {
  const names = ["list", "detail", "periods", "preview", "compute", "statement", "verify",
                 "acceptStatement", "raiseDispute", "proposeResolution", "acceptResolution",
                 "withdrawDispute", "history", "terminate", "draftAmendment", "proposeAmendment",
                 "acceptAmendment", "rejectAmendment"];
  const api: Record<string, unknown> = { side, downloadPath: (id: number, sid: number) =>
    `/${side === "OEM" ? "oem/contracts" : "service-contracts"}/${id}/statements/${sid}/download` };
  for (const n of names) api[n] = make(n);
  return api as unknown as ContractsApi;
}
