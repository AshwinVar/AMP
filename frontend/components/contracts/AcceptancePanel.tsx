"use client";

import React, { useState } from "react";

import {
  acceptBlockers,
  acceptanceByRevision,
  contractError,
  fetchStatementBytes,
  shortHash,
  verifyCanonical,
  type ContractsApi,
  type StatementPayload,
  type VerifyReport,
} from "../../lib/contracts";
import { useInFlight } from "../../lib/useInFlight";

/**
 * Accepting one exact statement revision, and checking what was accepted (ADR-0021).
 *
 * An acceptance names a content hash AND a revision. Any later change to the
 * statement bumps the revision and cancels every earlier acceptance, even when
 * the bytes come back identical (a dispute raised and withdrawn). So acceptances
 * are listed PER REVISION, valid or cancelled as the server says, and Accept
 * sends the hash and revision this screen is showing.
 *
 * "Check consistency" does two things and says which is which: the server's own
 * report (stored bytes, hash and records agree), and a re-hash IN THIS BROWSER of
 * the exact bytes downloaded. Neither proves nobody with full database access
 * rewrote everything together; a party's own saved copy is what shows that.
 */

type Check = {
  report: VerifyReport;
  local: { hash: string; matches: boolean };
  bytes: ArrayBuffer;
};

const CONSISTENCY_TEXT: Record<string, string> = {
  consistent: "Consistent: the stored bytes, their hash and the attribution records agree.",
  blob_mismatch: "NOT consistent: the stored bytes do not hash to the statement's hash.",
  records_diverged: "NOT consistent: the attribution records disagree with the stored bytes.",
  content_purged: "The content was removed when the factory was offboarded; the hash and "
    + "acceptances remain.",
};

export default function AcceptancePanel({
  api,
  contractId,
  statement,
  canSign,
  onChanged,
}: {
  api: ContractsApi;
  contractId: number;
  statement: StatementPayload;
  canSign: boolean;
  onChanged: () => void;
}) {
  const { run, busy } = useInFlight();
  const [error, setError] = useState("");
  const [check, setCheck] = useState<Check | null>(null);

  const blockers = acceptBlockers(statement, api.side);
  const groups = acceptanceByRevision(statement.acceptances);

  async function accept() {
    await run("accept", async () => {
      setError("");
      try {
        await api.acceptStatement(contractId, statement.id, statement.content_hash,
                                  statement.revision);
        onChanged();
      } catch (e) {
        setError(contractError(e).message);
        onChanged();
      }
    });
  }

  async function checkConsistency() {
    await run("check", async () => {
      setError("");
      try {
        const [report, download] = await Promise.all([
          api.verify(contractId, statement.id),
          fetchStatementBytes(api.downloadPath(contractId, statement.id)),
        ]);
        const local = await verifyCanonical(download.bytes, statement.content_hash);
        setCheck({ report, local, bytes: download.bytes });
      } catch (e) {
        setError(contractError(e).message);
      }
    });
  }

  function save() {
    if (!check || typeof URL.createObjectURL !== "function") return;
    const url = URL.createObjectURL(new Blob([check.bytes], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `statement-${contractId}-${statement.id}-r${statement.revision}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-4 text-sm">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h4 className="text-white font-medium">
          Revision {statement.revision}{" "}
          <span className="font-mono text-xs text-slate-500" title={statement.content_hash}>
            {shortHash(statement.content_hash)}
          </span>
        </h4>
        <span className={statement.agreed ? "text-emerald-400 text-xs" : "text-slate-400 text-xs"}
              data-testid="agreed">
          {statement.agreed ? "Agreed by both parties (final)" : "Not agreed yet"}
        </span>
      </div>

      {groups.length === 0 ? (
        <p className="mt-2 text-xs text-slate-500">No party has accepted any revision yet.</p>
      ) : (
        <ul className="mt-2 space-y-2 text-xs">
          {groups.map((g) => (
            <li key={g.revision} data-testid={`acceptances-r${g.revision}`}>
              <p className="text-slate-400">
                Revision {g.revision}{g.revision === statement.revision ? " (current)" : ""}
              </p>
              {g.rows.map((a) => (
                <p key={`${a.party}-${a.revision}`} className="ml-3">
                  {a.party} accepted by {a.actor} at {a.accepted_at}:{" "}
                  {a.valid ? (
                    <span className="text-emerald-400">valid</span>
                  ) : (
                    <span className="text-amber-300">
                      cancelled (the statement changed after this acceptance)
                    </span>
                  )}
                </p>
              ))}
            </li>
          ))}
        </ul>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        {canSign && (
          <button
            type="button"
            onClick={accept}
            disabled={blockers.length > 0 || busy("accept")}
            className="rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
          >
            Accept revision {statement.revision}
          </button>
        )}
        <button
          type="button"
          onClick={checkConsistency}
          disabled={busy("check") || statement.content_purged}
          className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs text-slate-200 disabled:opacity-40"
        >
          Check consistency
        </button>
        {check && (
          <button type="button" onClick={save}
                  className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs text-slate-200">
            Save this copy
          </button>
        )}
      </div>
      {canSign && blockers.length > 0 && (
        <ul className="mt-2 text-xs text-slate-400" data-testid="accept-blockers">
          {blockers.map((b) => (
            <li key={b}>{b}</li>
          ))}
        </ul>
      )}
      {error && (
        <p role="alert" className="mt-2 text-xs text-red-400">
          {error}
        </p>
      )}
      {check && (
        <div className="mt-3 space-y-1 text-xs" data-testid="consistency">
          <p className={check.report.consistency === "consistent" ? "text-emerald-400" : "text-amber-300"}>
            Server: {CONSISTENCY_TEXT[check.report.consistency] ?? check.report.consistency}
          </p>
          <p className={check.local.matches ? "text-emerald-400" : "text-red-400"}>
            This browser: the downloaded bytes hash to{" "}
            <span className="font-mono" title={check.local.hash}>{shortHash(check.local.hash)}</span>
            {check.local.matches ? ", which matches revision " + statement.revision
              : ", which does NOT match the revision shown"}.
          </p>
          <p className="text-slate-400">
            Live recompute:{" "}
            {check.report.live.matches === null
              ? `not possible (${check.report.live.reason})`
              : check.report.live.matches ? "current evidence gives the same hash"
                : "current evidence would give a different hash"}
          </p>
          <p className="text-slate-500">
            Consistent does not prove that nobody with full database access rewrote the bytes,
            hash, records and acceptances together. Keep your own saved copy.
          </p>
        </div>
      )}
    </section>
  );
}
