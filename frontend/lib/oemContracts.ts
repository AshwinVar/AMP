import type {
  ComputeResult,
  ContractDetail,
  ContractsApi,
  ContractSummary,
  DisputeBody,
  HistoryRow,
  PeriodsView,
  PreviewPayload,
  StatementPayload,
  Terms,
  TermVersion,
  VerifyReport,
} from "./contracts";
import { get, post } from "./oem";

/**
 * Service contracts from the MANUFACTURER's side: /oem/contracts (ADR-0021).
 *
 * Transport only, over the OEM client's own get/post (so a refusal keeps its
 * status and structured detail). What a manufacturer may do is decided by the
 * server from the principal's capabilities and the factory's SHARE_DOWNTIME
 * grant; this module decides nothing.
 */

const base = (id: number) => `/oem/contracts/${id}`;

export type ContractDraftBody = {
  contract_ref: string;
  title: string;
  contract_type: string;
  factory_tenant_code: string;
  start_month: string;
  terms: Terms;
};

export const oemContractsApi: ContractsApi & {
  create(body: ContractDraftBody): Promise<ContractDetail>;
  propose(id: number, termsHash: string): Promise<ContractDetail>;
  withdraw(id: number): Promise<ContractDetail>;
} = {
  side: "OEM",
  list: () => get<{ contracts: ContractSummary[]; truncated: boolean }>("/oem/contracts"),
  detail: (id) => get<ContractDetail>(base(id)),
  periods: (id) => get<PeriodsView>(`${base(id)}/periods`),
  preview: (id) => get<PreviewPayload>(`${base(id)}/preview`),
  compute: (id, periodStart) =>
    post<ComputeResult>(`${base(id)}/statements/compute`, { period_start: periodStart }),
  statement: (id, sid) => get<StatementPayload>(`${base(id)}/statements/${sid}`),
  verify: (id, sid) => get<VerifyReport>(`${base(id)}/statements/${sid}/verify`),
  downloadPath: (id, sid) => `${base(id)}/statements/${sid}/download`,
  acceptStatement: (id, sid, contentHash, revision) =>
    post<StatementPayload>(`${base(id)}/statements/${sid}/accept`,
                           { content_hash: contentHash, revision }),
  raiseDispute: (id, sid, body: DisputeBody) =>
    post(`${base(id)}/statements/${sid}/disputes`, body),
  proposeResolution: (id, did, bucket, note) =>
    post(`${base(id)}/disputes/${did}/propose-resolution`, { resolution_bucket: bucket, note }),
  acceptResolution: (id, did, bucket) =>
    post(`${base(id)}/disputes/${did}/accept-resolution`, { resolution_bucket: bucket }),
  withdrawDispute: (id, did) => post(`${base(id)}/disputes/${did}/withdraw`, {}),
  history: (id) => get<{ history: HistoryRow[]; truncated: boolean }>(`${base(id)}/history`),
  terminate: (id, reason) => post<ContractDetail>(`${base(id)}/terminate`, { reason }),
  draftAmendment: (id, terms, effectiveFrom) =>
    post<TermVersion>(`${base(id)}/amendments`, { terms, effective_from: effectiveFrom }),
  proposeAmendment: (id, version, termsHash) =>
    post<TermVersion>(`${base(id)}/amendments/${version}/propose`, { terms_hash: termsHash }),
  acceptAmendment: (id, version, termsHash) =>
    post<TermVersion>(`${base(id)}/amendments/${version}/accept`, { terms_hash: termsHash }),
  rejectAmendment: (id, version, note) =>
    post<TermVersion>(`${base(id)}/amendments/${version}/reject`, { note }),
  create: (body) => post<ContractDetail>("/oem/contracts", body),
  propose: (id, termsHash) => post<ContractDetail>(`${base(id)}/propose`, { terms_hash: termsHash }),
  withdraw: (id) => post<ContractDetail>(`${base(id)}/withdraw`, {}),
};
