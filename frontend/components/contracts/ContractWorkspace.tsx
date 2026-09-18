"use client";

import React, { useCallback, useEffect, useState } from "react";

import {
  closedPeriod,
  contractError,
  contractStateLabel,
  shortHash,
  type ContractDetail,
  type ContractsApi,
  type ContractSummary,
  type HistoryRow,
  type PeriodsView,
  type PreviewPayload,
  type StatementPayload,
  type Terms,
  type TermVersion,
} from "../../lib/contracts";
import { useInFlight } from "../../lib/useInFlight";
import AcceptancePanel from "./AcceptancePanel";
import ContractHistory from "./ContractHistory";
import ContractTerms from "./ContractTerms";
import DisputePanel from "./DisputePanel";
import StatementView from "./StatementView";

/**
 * One contract workspace for BOTH parties (ADR-0020).
 *
 * The manufacturer's portal and the factory's dashboard render this same
 * component over their own transport (`api`), so the two sides of one contract
 * cannot drift into two readings of it. Each side adds only what is its alone:
 * the manufacturer drafts, proposes and withdraws; the factory reviews a
 * proposal against its own downtime reasons and accepts with explicit consent.
 *
 * `canManage` and `canSign` hide controls a person's role cannot use. They
 * protect nothing: the server enforces every rule again, and its refusal is
 * shown in its own words.
 *
 * WITHHELD IS NOT AN ERROR. When a factory has withdrawn SHARE_DOWNTIME, a
 * manufacturer's statement requests are refused with a consent state. That is
 * rendered as "sharing withdrawn by factory" and carries no numbers at all.
 */

type Bundle = {
  detail: ContractDetail;
  periods: PeriodsView;
  history: { history: HistoryRow[]; truncated: boolean };
  /** When this was read: which periods are closed or still ahead is judged at this instant. */
  loadedAt: number;
};

type Opened =
  | { kind: "statement"; statement: StatementPayload }
  | { kind: "preview"; preview: PreviewPayload }
  | { kind: "withheld"; message: string }
  | { kind: "error"; message: string };

const BINDING = ["accepted", "terminated"];

export function WithheldNotice({ message }: { message?: string }) {
  return (
    <p className="rounded-lg border border-slate-700 bg-slate-950/60 p-3 text-xs text-slate-300"
       data-testid="withheld">
      Sharing withdrawn by factory. Statement content and statement actions are withheld until
      the factory shares downtime with you again; the contract terms stay visible.
      {message ? <span className="block text-slate-500">{message}</span> : null}
    </p>
  );
}

function governing(versions: TermVersion[]): TermVersion | null {
  const accepted = versions.filter((v) => v.status === "accepted");
  return accepted[accepted.length - 1] ?? versions[0] ?? null;
}

export default function ContractWorkspace({
  api,
  canManage,
  canSign,
  reloadToken = 0,
  renderReview,
  renderOfferActions,
}: {
  api: ContractsApi;
  canManage: boolean;
  canSign: boolean;
  reloadToken?: number;
  /** The factory's review of a proposed contract. */
  renderReview?: (detail: ContractDetail, reload: () => void) => React.ReactNode;
  /** The manufacturer's propose / withdraw controls for a draft or proposal. */
  renderOfferActions?: (detail: ContractDetail, reload: () => void) => React.ReactNode;
}) {
  const { run, busy } = useInFlight();
  const [contracts, setContracts] = useState<ContractSummary[] | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [listError, setListError] = useState("");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [bundle, setBundle] = useState<Bundle | null>(null);
  const [detailError, setDetailError] = useState("");
  const [opened, setOpened] = useState<Opened | null>(null);
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [terminateReason, setTerminateReason] = useState("");
  const [amending, setAmending] = useState(false);
  const [amendText, setAmendText] = useState("");
  const [amendFrom, setAmendFrom] = useState("");
  const [rejectNote, setRejectNote] = useState("");

  const counterpartyOf = (c: ContractSummary) =>
    api.side === "OEM" ? c.factory_tenant_code : c.oem_code;

  const loadList = useCallback(
    () =>
      api.list()
        .then((r) => {
          setContracts(Array.isArray(r?.contracts) ? r.contracts : []);
          setTruncated(Boolean(r?.truncated));
          setListError("");
        })
        .catch((e) => setListError(contractError(e).message)),
    [api],
  );

  const loadContract = useCallback(
    (id: number) =>
      Promise.all([api.detail(id), api.periods(id), api.history(id)])
        .then(([detail, periods, history]) => {
          setBundle({ detail, periods, history, loadedAt: Date.now() });
          setDetailError("");
        })
        .catch((e) => setDetailError(contractError(e).message)),
    [api],
  );

  const openStatement = useCallback(
    (id: number, statementId: number) =>
      api.statement(id, statementId)
        .then((statement) => setOpened({ kind: "statement", statement }))
        .catch((e) => {
          const err = contractError(e);
          setOpened(err.withheld ? { kind: "withheld", message: "" }
            : { kind: "error", message: err.message });
        }),
    [api],
  );

  useEffect(() => {
    loadList();
  }, [loadList, reloadToken]);

  useEffect(() => {
    if (selectedId !== null) loadContract(selectedId);
  }, [selectedId, loadContract]);

  const reload = useCallback(() => {
    loadList();
    if (selectedId === null) return;
    loadContract(selectedId);
    if (opened?.kind === "statement") openStatement(selectedId, opened.statement.id);
  }, [loadList, loadContract, openStatement, selectedId, opened]);

  function select(id: number) {
    setSelectedId(id);
    setBundle(null);
    setOpened(null);
    setActionError("");
    setNotice("");
    setAmending(false);
  }

  async function act(key: string, fn: () => Promise<unknown>, done?: string) {
    await run(key, async () => {
      setActionError("");
      setNotice("");
      try {
        await fn();
        if (done) setNotice(done);
        reload();
      } catch (e) {
        setActionError(contractError(e).message);
        reload();
      }
    });
  }

  const detail = bundle?.detail ?? null;
  const binding = detail ? BINDING.includes(detail.status) : false;
  const withheld = Boolean(detail && api.side === "OEM" && binding && !detail.downtime_shared);
  const pendingAmendment = detail?.versions.find(
    (v) => v.version > 1 && (v.status === "draft" || v.status === "proposed")) ?? null;
  const futureStarts = (bundle?.periods.periods ?? [])
    .filter((p) => bundle !== null && Date.parse(p.start) > bundle.loadedAt && !p.statement)
    .map((p) => p.start);

  function startAmending() {
    const current = detail ? governing(detail.versions) : null;
    setAmendText(JSON.stringify(current?.terms ?? {}, null, 2));
    setAmendFrom(futureStarts[0] ?? "");
    setAmending(true);
  }

  function submitAmendment() {
    if (!detail) return;
    let terms: Terms;
    try {
      terms = JSON.parse(amendText) as Terms;
    } catch {
      setActionError("The amended terms are not valid JSON.");
      return;
    }
    void act("amend", async () => {
      await api.draftAmendment(detail.id, terms, amendFrom);
      setAmending(false);
    }, "Amendment drafted. Propose it so the other party can accept it.");
  }

  return (
    <div className="space-y-6">
      <section>
        {listError && (
          <p role="alert" className="text-sm text-red-400">{listError}</p>
        )}
        {contracts !== null && contracts.length === 0 && !listError && (
          <p className="text-sm text-slate-500">No service contracts yet.</p>
        )}
        {truncated && (
          <p className="text-xs text-slate-400">Showing the newest 500 contracts.</p>
        )}
        {contracts !== null && contracts.length > 0 && (
          <div className="overflow-x-auto rounded-xl border border-slate-800 bg-slate-900/60">
            <table className="w-full text-sm">
              <thead className="text-slate-500 text-xs uppercase tracking-wide">
                <tr>
                  <th className="p-3 text-left">Reference</th>
                  <th className="p-3 text-left">Title</th>
                  <th className="p-3 text-left">{api.side === "OEM" ? "Factory" : "Manufacturer"}</th>
                  <th className="p-3 text-left">Status</th>
                  <th className="p-3 text-left">Term</th>
                </tr>
              </thead>
              <tbody>
                {contracts.map((c) => (
                  <tr key={c.id} onClick={() => select(c.id)}
                      className={`border-t border-slate-800 cursor-pointer hover:bg-slate-800/40 ${
                        selectedId === c.id ? "bg-slate-800/40" : ""}`}>
                    <td className="p-3 font-mono text-xs">{c.contract_ref}</td>
                    <td className="p-3">{c.title}</td>
                    <td className="p-3">{counterpartyOf(c)}</td>
                    <td className="p-3">{contractStateLabel(c.state)}</td>
                    <td className="p-3 text-xs text-slate-400">{c.starts_at} to {c.effective_end}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {detailError && <p role="alert" className="text-sm text-red-400">{detailError}</p>}

      {detail && bundle && (
        <section className="space-y-5 rounded-xl border border-slate-800 bg-slate-900/40 p-5"
                 data-testid="contract-detail">
          <header>
            <h3 className="text-lg font-semibold text-white">
              <span className="font-mono">{detail.contract_ref}</span> · {detail.title}
            </h3>
            <p className="text-xs text-slate-400">
              {detail.contract_type} · {api.side === "OEM" ? "factory" : "manufacturer"}{" "}
              {counterpartyOf(detail)} · {contractStateLabel(detail.status)}
              {detail.state !== detail.status ? ` (${contractStateLabel(detail.state)})` : ""} ·{" "}
              {detail.starts_at} to {detail.effective_end}
            </p>
            {detail.termination_effective_at && (
              <p className="text-xs text-slate-400">
                Terminated by {detail.terminated_by_party} effective {detail.termination_effective_at}
                {detail.termination_reason ? `: ${detail.termination_reason}` : ""}
              </p>
            )}
          </header>

          {notice && <p className="text-xs text-emerald-400">{notice}</p>}
          {actionError && <p role="alert" className="text-xs text-red-400">{actionError}</p>}

          {renderReview && detail.status === "proposed" && renderReview(detail, reload)}
          {renderOfferActions && ["draft", "proposed"].includes(detail.status)
            && renderOfferActions(detail, reload)}
          {withheld && <WithheldNotice />}

          <div>
            <h4 className="text-sm font-medium text-white">Terms</h4>
            <div className="mt-2 space-y-3">
              {detail.versions.map((v) => (
                <details key={v.version} open={v === governing(detail.versions)}
                         className="rounded-lg border border-slate-800 p-3">
                  <summary className="cursor-pointer text-xs text-slate-300">
                    Version {v.version} · {v.status}
                    {v.proposed_by_party ? ` · proposed by ${v.proposed_by_party}` : ""} ·{" "}
                    <span className="font-mono">{shortHash(v.terms_hash)}</span>
                  </summary>
                  <div className="mt-2">
                    <ContractTerms version={v} />
                  </div>
                </details>
              ))}
            </div>
          </div>

          {binding && detail.status === "accepted" && (
            <div className="space-y-2 text-xs">
              <h4 className="text-sm font-medium text-white">Amendments</h4>
              {pendingAmendment ? (
                <div className="flex flex-wrap items-center gap-2" data-testid="pending-amendment">
                  <span className="text-slate-300">
                    Version {pendingAmendment.version} is {pendingAmendment.status}, from{" "}
                    {pendingAmendment.effective_from}.
                  </span>
                  {canSign && pendingAmendment.proposed_by_party === api.side
                    && pendingAmendment.status === "draft" && (
                    <button type="button" disabled={busy("amend-propose")}
                            onClick={() => act("amend-propose", () => api.proposeAmendment(
                              detail.id, pendingAmendment.version, pendingAmendment.terms_hash),
                              "Amendment proposed.")}
                            className="rounded bg-blue-600 px-2 py-1 text-white">Propose it</button>
                  )}
                  {canSign && pendingAmendment.proposed_by_party !== api.side
                    && pendingAmendment.status === "proposed" && (
                    <button type="button" disabled={busy("amend-accept")}
                            onClick={() => act("amend-accept", () => api.acceptAmendment(
                              detail.id, pendingAmendment.version, pendingAmendment.terms_hash),
                              "Amendment accepted.")}
                            className="rounded bg-emerald-600 px-2 py-1 text-white">
                      Accept version {pendingAmendment.version}
                    </button>
                  )}
                  {canSign && (
                    <>
                      <input aria-label="Amendment decision note" placeholder="note (optional)"
                             value={rejectNote} onChange={(e) => setRejectNote(e.target.value)}
                             className="rounded bg-slate-800 px-2 py-1" />
                      <button type="button" disabled={busy("amend-reject")}
                              onClick={() => act("amend-reject", () => api.rejectAmendment(
                                detail.id, pendingAmendment.version, rejectNote))}
                              className="rounded border border-slate-700 px-2 py-1 text-slate-300">
                        {pendingAmendment.proposed_by_party === api.side ? "Withdraw it" : "Reject it"}
                      </button>
                    </>
                  )}
                </div>
              ) : canManage && !amending ? (
                <button type="button" onClick={startAmending}
                        className="rounded border border-slate-700 px-2 py-1 text-slate-300">
                  Draft an amendment
                </button>
              ) : null}
              {amending && (
                <div className="space-y-2">
                  <p className="text-slate-500">
                    Edit the terms. The period grid (currency, period length, timezone, term) cannot
                    change. Both parties must accept the exact hash before it applies.
                  </p>
                  <textarea aria-label="Amended terms" value={amendText} rows={12}
                            onChange={(e) => setAmendText(e.target.value)}
                            className="w-full rounded bg-slate-950 p-2 font-mono" />
                  <label className="flex items-center gap-2">
                    Effective from
                    <select value={amendFrom} onChange={(e) => setAmendFrom(e.target.value)}
                            className="rounded bg-slate-800 px-2 py-1">
                      {futureStarts.map((s) => <option key={s} value={s}>{s}</option>)}
                    </select>
                  </label>
                  <div className="flex gap-2">
                    <button type="button" onClick={submitAmendment} disabled={busy("amend")}
                            className="rounded bg-blue-600 px-2 py-1 text-white">Save draft amendment</button>
                    <button type="button" onClick={() => setAmending(false)}
                            className="rounded border border-slate-700 px-2 py-1 text-slate-300">Cancel</button>
                  </div>
                </div>
              )}
            </div>
          )}

          {canSign && detail.status === "accepted" && (
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <input aria-label="Termination reason" placeholder="reason for terminating"
                     value={terminateReason} onChange={(e) => setTerminateReason(e.target.value)}
                     className="rounded bg-slate-800 px-2 py-1" />
              <button type="button" disabled={!terminateReason.trim() || busy("terminate")}
                      onClick={() => act("terminate", () => api.terminate(detail.id, terminateReason),
                                         "Termination recorded; it takes effect at a period boundary "
                                         + "after the notice period.")}
                      className="rounded border border-red-500/50 px-2 py-1 text-red-300 disabled:opacity-40">
                Terminate
              </button>
            </div>
          )}

          {binding && (
            <div>
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <h4 className="text-sm font-medium text-white">Periods and statements</h4>
                {!withheld && (
                  <button type="button" disabled={busy("preview")}
                          onClick={() => run("preview", () => api.preview(detail.id)
                            .then((preview) => setOpened({ kind: "preview", preview }))
                            .catch((e) => setActionError(contractError(e).message)))}
                          className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-300">
                    Preview the running period
                  </button>
                )}
              </div>
              <table className="mt-2 w-full text-xs">
                <thead className="text-slate-500 uppercase tracking-wide">
                  <tr>
                    <th className="p-2 text-left">Period (UTC)</th>
                    <th className="p-2 text-left">Terms</th>
                    <th className="p-2 text-left">Statement</th>
                    <th className="p-2 text-left" />
                  </tr>
                </thead>
                <tbody>
                  {bundle.periods.periods.map((p) => (
                    <tr key={p.start} className="border-t border-slate-800">
                      <td className="p-2 font-mono">{p.start} to {p.end}</td>
                      <td className="p-2">{p.terms_version ?? "—"}</td>
                      <td className="p-2">
                        {p.statement
                          ? `revision ${p.statement.revision}${p.statement.agreed ? ", agreed" : ""}`
                          : "none"}
                      </td>
                      <td className="p-2 space-x-2 text-right">
                        {p.statement && (
                          <button type="button"
                                  onClick={() => openStatement(detail.id, p.statement!.id)}
                                  className="rounded border border-slate-700 px-2 py-1 text-slate-200">
                            Open
                          </button>
                        )}
                        {canManage && !withheld && closedPeriod(p, bundle.loadedAt) && p.terms_version !== null
                          && !p.statement?.agreed && (
                          <button type="button" disabled={busy(`compute:${p.start}`)}
                                  onClick={() => act(`compute:${p.start}`, async () => {
                                    const r = await api.compute(detail.id, p.start);
                                    await openStatement(detail.id, r.statement.id);
                                  })}
                                  className="rounded bg-blue-600 px-2 py-1 text-white">
                            {p.statement ? "Recompute" : "Compute"}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {opened?.kind === "withheld" && <WithheldNotice message={opened.message} />}
          {opened?.kind === "error" && (
            <p role="alert" className="text-xs text-red-400">{opened.message}</p>
          )}
          {opened?.kind === "preview" && <StatementView content={opened.preview.content} preview />}
          {opened?.kind === "statement" && (
            <div className="space-y-4">
              {opened.statement.content ? (
                <StatementView content={opened.statement.content} />
              ) : (
                <p className="text-xs text-slate-400">
                  The content of this statement was removed when the factory was offboarded. Its
                  hash and acceptances remain.
                </p>
              )}
              <AcceptancePanel api={api} contractId={detail.id} statement={opened.statement}
                               canSign={canSign} onChanged={reload} />
              <DisputePanel api={api} contractId={detail.id} statement={opened.statement}
                            canManage={canManage} canSign={canSign} onChanged={reload} />
            </div>
          )}

          <div>
            <h4 className="text-sm font-medium text-white">History</h4>
            <div className="mt-2">
              <ContractHistory rows={bundle.history.history} truncated={bundle.history.truncated} />
            </div>
          </div>
        </section>
      )}
    </div>
  );
}
