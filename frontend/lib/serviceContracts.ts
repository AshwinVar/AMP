import { apiGet, apiPost } from "./api";
import type {
  ComputeResult,
  ContractDetail,
  ContractsApi,
  ContractSummary,
  DisputeBody,
  HistoryRow,
  PeriodsView,
  PreviewPayload,
  ReasonVocabulary,
  StatementPayload,
  TermVersion,
  VerifyReport,
} from "./contracts";

/**
 * Service contracts from the FACTORY's side: /service-contracts (ADR-0021).
 *
 * Transport only, over the dashboard's own apiGet/apiPost (so the founder's
 * company preview header and session refresh behave as on every other screen).
 * Two things only a factory has: accepting or rejecting a proposed contract,
 * and its own downtime-reason vocabulary.
 *
 * ACCEPT CARRIES CONSENT. The body always names `grant_downtime_sharing`; the
 * screen sends true only when the administrator ticked the disclosure, and the
 * server refuses anything else.
 */

const base = (id: number) => `/service-contracts/${id}`;

export const serviceContractsApi: ContractsApi & {
  accept(id: number, termsHash: string, grantDowntimeSharing: boolean): Promise<ContractDetail>;
  reject(id: number, note: string): Promise<ContractDetail>;
  reasonVocabulary(id: number): Promise<ReasonVocabulary>;
} = {
  side: "FACTORY",
  list: () => apiGet<{ contracts: ContractSummary[]; truncated: boolean }>("/service-contracts"),
  detail: (id) => apiGet<ContractDetail>(base(id)),
  periods: (id) => apiGet<PeriodsView>(`${base(id)}/periods`),
  preview: (id) => apiGet<PreviewPayload>(`${base(id)}/preview`),
  compute: (id, periodStart) =>
    apiPost<ComputeResult>(`${base(id)}/statements/compute`, { period_start: periodStart }),
  statement: (id, sid) => apiGet<StatementPayload>(`${base(id)}/statements/${sid}`),
  verify: (id, sid) => apiGet<VerifyReport>(`${base(id)}/statements/${sid}/verify`),
  downloadPath: (id, sid) => `${base(id)}/statements/${sid}/download`,
  acceptStatement: (id, sid, contentHash, revision) =>
    apiPost<StatementPayload>(`${base(id)}/statements/${sid}/accept`,
                              { content_hash: contentHash, revision }),
  raiseDispute: (id, sid, body: DisputeBody) =>
    apiPost(`${base(id)}/statements/${sid}/disputes`, body),
  proposeResolution: (id, did, bucket, note) =>
    apiPost(`${base(id)}/disputes/${did}/propose-resolution`, { resolution_bucket: bucket, note }),
  acceptResolution: (id, did, bucket) =>
    apiPost(`${base(id)}/disputes/${did}/accept-resolution`, { resolution_bucket: bucket }),
  withdrawDispute: (id, did) => apiPost(`${base(id)}/disputes/${did}/withdraw`, {}),
  history: (id) => apiGet<{ history: HistoryRow[]; truncated: boolean }>(`${base(id)}/history`),
  terminate: (id, reason) => apiPost<ContractDetail>(`${base(id)}/terminate`, { reason }),
  draftAmendment: (id, terms, effectiveFrom) =>
    apiPost<TermVersion>(`${base(id)}/amendments`, { terms, effective_from: effectiveFrom }),
  proposeAmendment: (id, version, termsHash) =>
    apiPost<TermVersion>(`${base(id)}/amendments/${version}/propose`, { terms_hash: termsHash }),
  acceptAmendment: (id, version, termsHash) =>
    apiPost<TermVersion>(`${base(id)}/amendments/${version}/accept`, { terms_hash: termsHash }),
  rejectAmendment: (id, version, note) =>
    apiPost<TermVersion>(`${base(id)}/amendments/${version}/reject`, { note }),
  accept: (id, termsHash, grantDowntimeSharing) =>
    apiPost<ContractDetail>(`${base(id)}/accept`,
                            { terms_hash: termsHash, grant_downtime_sharing: grantDowntimeSharing }),
  reject: (id, note) => apiPost<ContractDetail>(`${base(id)}/reject`, { note }),
  reasonVocabulary: (id) => apiGet<ReasonVocabulary>(`${base(id)}/reason-vocabulary`),
};
