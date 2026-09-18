"use client";

import React, { useEffect, useState } from "react";

import { getUserRole } from "../lib/api";
import {
  bucketLabel,
  contractError,
  type ContractDetail,
  type ReasonVocabulary,
} from "../lib/contracts";
import { serviceContractsApi } from "../lib/serviceContracts";
import { useInFlight } from "../lib/useInFlight";
import ContractWorkspace from "./contracts/ContractWorkspace";

/**
 * Service contracts in the factory's dashboard (ADR-0021).
 *
 * The factory's half: review what a machine's manufacturer proposes, see which
 * of your OWN downtime reasons its terms would attribute and which would come
 * out Disputed, and accept only with explicit consent to share downtime. From
 * then on, compute, accept or dispute each month's statement.
 *
 * CONSENT IS A TICKED BOX, NEVER A SIDE EFFECT. Accepting grants the
 * manufacturer SHARE_DOWNTIME. The disclosure says exactly what that shows
 * them, the Accept button stays disabled until the box is ticked, and the
 * request carries `grant_downtime_sharing` as the box says. The server refuses
 * an acceptance without it.
 *
 * Admins take binding actions; Supervisors read. Operators do not reach this
 * screen (lib/modules). None of that is the security boundary; the server is.
 */

const VOCAB_TONE: Record<string, string> = {
  mapped: "text-emerald-300",
  generic: "text-slate-400",
  unmapped: "text-amber-300",
};

export function ContractReview({ detail, canSign, reload }: {
  detail: ContractDetail;
  canSign: boolean;
  reload: () => void;
}) {
  const { run, busy } = useInFlight();
  const [vocabulary, setVocabulary] = useState<ReasonVocabulary | null>(null);
  const [vocabularyError, setVocabularyError] = useState("");
  const [consent, setConsent] = useState(false);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const v1 = detail.versions.find((v) => v.version === 1);

  useEffect(() => {
    serviceContractsApi.reasonVocabulary(detail.id)
      .then((v) => {
        setVocabulary(v);
        setVocabularyError("");
      })
      .catch((e) => setVocabularyError(contractError(e).message));
  }, [detail.id]);

  async function act(key: string, fn: () => Promise<unknown>) {
    await run(key, async () => {
      setError("");
      try {
        await fn();
      } catch (e) {
        setError(contractError(e).message);
      }
      reload();
    });
  }

  return (
    <section className="space-y-3 rounded-xl border border-blue-500/30 bg-blue-500/5 p-4 text-xs"
             data-testid="contract-review">
      <h4 className="text-sm font-medium text-white">
        {detail.oem_code} proposes this contract. Nothing is measured until you accept it.
      </h4>

      <div>
        <p className="text-slate-300">
          How the proposed terms treat the downtime reasons your team logged on these machines in
          the last 90 days:
        </p>
        {vocabularyError && <p role="alert" className="text-red-400">{vocabularyError}</p>}
        {vocabulary && vocabulary.reasons.length === 0 && (
          <p className="text-slate-500">No downtime reasons logged on these machines in that time.</p>
        )}
        {vocabulary && vocabulary.reasons.length > 0 && (
          <table className="mt-1 text-xs" data-testid="reason-vocabulary">
            <tbody>
              {vocabulary.reasons.map((r) => (
                <tr key={r.reason_key}>
                  <td className="pr-3 text-slate-200">&ldquo;{r.reason_key}&rdquo;</td>
                  <td className="pr-3 text-slate-500">{r.count} logged</td>
                  <td className={VOCAB_TONE[r.status] ?? "text-slate-400"}>
                    {r.status === "mapped" ? `attributed to ${bucketLabel(r.bucket ?? "")}`
                      : r.status === "generic" ? "treated as no reason (the status default applies)"
                        : "not in the terms: that downtime would be Disputed"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-3 text-slate-300"
           data-testid="consent-disclosure">
        <p className="font-medium text-white">What accepting shares with {detail.oem_code}</p>
        <ul className="mt-1 list-disc pl-5">
          <li>For the covered machines only, from the contract&apos;s first month (which may be
            before today): the status history reported by each trusted source, inside the covered
            hours.</li>
          <li>The downtime reason text your team logs near each stop on those machines.</li>
          <li>Never production counts, work orders, customers, notes, operators or machine names.</li>
        </ul>
        <p className="mt-2 text-slate-400">
          This grant (SHARE_DOWNTIME) covers your whole relationship with {detail.oem_code}, not
          only these machines. You can withdraw it under Connected Equipment; statements are then
          withheld from them.
        </p>
        {canSign && (
          <label className="mt-2 flex items-start gap-2">
            <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
            <span>I agree to share downtime with {detail.oem_code} as described above.</span>
          </label>
        )}
      </div>

      {canSign ? (
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" disabled={!consent || !v1 || busy("accept")}
                  onClick={() => act("accept", () =>
                    serviceContractsApi.accept(detail.id, v1!.terms_hash, consent))}
                  className="rounded-lg bg-emerald-600 px-3 py-1.5 font-medium text-white disabled:opacity-40">
            Accept contract
          </button>
          <input aria-label="Rejection note" placeholder="reason for rejecting (optional)"
                 value={note} onChange={(e) => setNote(e.target.value)}
                 className="rounded bg-slate-800 px-2 py-1" />
          <button type="button" disabled={busy("reject")}
                  onClick={() => act("reject", () => serviceContractsApi.reject(detail.id, note))}
                  className="rounded-lg border border-slate-700 px-3 py-1.5 text-slate-300">
            Reject
          </button>
        </div>
      ) : (
        <p className="text-slate-500">Only an Admin can accept or reject this contract.</p>
      )}
      {error && <p role="alert" className="text-red-400">{error}</p>}
    </section>
  );
}

export default function ServiceContracts() {
  const canSign = getUserRole() === "Admin";
  return (
    <div className="space-y-4" data-testid="service-contracts">
      <div>
        <h2 className="text-xl font-semibold text-white">Service contracts</h2>
        <p className="mt-1 max-w-3xl text-sm text-slate-400">
          Contracts your machines&apos; manufacturers propose: annual maintenance, warranties and
          uptime clauses. Each month AMP attributes every covered minute of downtime to the
          manufacturer, to your own stops (from your downtime reasons), to disputed time, or to No
          data, and both sides accept the exact statement. AMP computes any credit; it never
          invoices or moves money.
        </p>
      </div>
      <ContractWorkspace
        api={serviceContractsApi}
        canManage={canSign}
        canSign={canSign}
        renderReview={(detail, reload) => (
          <ContractReview detail={detail} canSign={canSign} reload={reload} />
        )}
      />
    </div>
  );
}
