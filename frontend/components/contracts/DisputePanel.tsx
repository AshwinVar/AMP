"use client";

import React, { useState } from "react";

import {
  RESOLUTION_BUCKETS,
  bucketLabel,
  contractError,
  liveDisputes,
  parseUtcInput,
  type ContractsApi,
  type Dispute,
  type StatementPayload,
} from "../../lib/contracts";
import { useInFlight } from "../../lib/useInFlight";

/**
 * Disputing a window of a statement, and settling it (ADR-0020).
 *
 * A dispute covers one machine and one UTC window inside the statement period.
 * Raising or withdrawing one revises the statement, which cancels every
 * acceptance. A resolution proposed by one party is accepted by the OTHER, and
 * settles the window as Available, OEM, Factory or No data — never Disputed.
 * Time the rules themselves made Disputed (an unmapped reason, conflicting
 * sources) is cleared the same way: raise a dispute over it.
 */

const LIVE = (d: Dispute) => liveDisputes([d]).length === 1;

export default function DisputePanel({
  api,
  contractId,
  statement,
  canManage,
  canSign,
  onChanged,
}: {
  api: ContractsApi;
  contractId: number;
  statement: StatementPayload;
  canManage: boolean;
  canSign: boolean;
  onChanged: () => void;
}) {
  const { run, busy } = useInFlight();
  const [error, setError] = useState("");
  const machines = statement.content?.machines ?? [];
  const [installation, setInstallation] = useState<number | "">(machines[0]?.installation_id ?? "");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [bucket, setBucket] = useState<string>("FACTORY");
  const [reason, setReason] = useState("");
  const [resolution, setResolution] = useState<Record<number, string>>({});
  const [note, setNote] = useState<Record<number, string>>({});

  const serial = (id: number) =>
    machines.find((m) => m.installation_id === id)?.serial_number ?? `installation ${id}`;

  async function act(key: string, fn: () => Promise<unknown>) {
    await run(key, async () => {
      setError("");
      try {
        await fn();
        onChanged();
      } catch (e) {
        setError(contractError(e).message);
      }
    });
  }

  function raise(e: React.FormEvent) {
    e.preventDefault();
    const windowStart = parseUtcInput(start);
    const windowEnd = parseUtcInput(end);
    if (installation === "" || !windowStart || !windowEnd) {
      setError("Choose a machine and give the window as UTC date and time (YYYY-MM-DDTHH:MM).");
      return;
    }
    if (!reason.trim()) {
      setError("Say why this window is disputed.");
      return;
    }
    void act("raise", async () => {
      await api.raiseDispute(contractId, statement.id, {
        installation_id: Number(installation), window_start: windowStart, window_end: windowEnd,
        reason: reason.trim(), proposed_bucket: bucket,
      });
      setReason("");
    });
  }

  const canRaise = canManage && !statement.agreed && statement.content !== null;

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-4 text-sm">
      <h4 className="text-white font-medium">Disputes</h4>
      {statement.disputes.length === 0 ? (
        <p className="mt-1 text-xs text-slate-500">No disputes on this statement.</p>
      ) : (
        <ul className="mt-2 space-y-3 text-xs">
          {statement.disputes.map((d) => (
            <li key={d.id} data-testid={`dispute-${d.id}`}
                className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
              <p className="text-slate-200">
                #{d.id} · {serial(d.installation_id)} · {d.window_start} to {d.window_end} ·{" "}
                <span className="text-amber-300">{d.status.replace(/_/g, " ")}</span>
              </p>
              <p className="text-slate-400">
                Raised by {d.raised_by_party}, proposing {bucketLabel(d.proposed_bucket)}:{" "}
                &ldquo;{d.reason}&rdquo;
              </p>
              {d.resolution_bucket && (
                <p className="text-slate-400">
                  Resolution {d.status === "resolved" ? "agreed" : "proposed"}
                  {d.resolution_proposed_by_party ? ` by ${d.resolution_proposed_by_party}` : ""}:{" "}
                  {bucketLabel(d.resolution_bucket)}
                  {d.resolution_note ? ` (“${d.resolution_note}”)` : ""}
                </p>
              )}
              {LIVE(d) && (
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  {canManage && (
                    <>
                      <select
                        aria-label={`Resolution for dispute ${d.id}`}
                        value={resolution[d.id] ?? "FACTORY"}
                        onChange={(e) => setResolution({ ...resolution, [d.id]: e.target.value })}
                        className="rounded bg-slate-800 px-2 py-1"
                      >
                        {RESOLUTION_BUCKETS.map((b) => (
                          <option key={b} value={b}>{bucketLabel(b)}</option>
                        ))}
                      </select>
                      <input
                        aria-label={`Resolution note for dispute ${d.id}`}
                        placeholder="note (optional)"
                        value={note[d.id] ?? ""}
                        onChange={(e) => setNote({ ...note, [d.id]: e.target.value })}
                        className="rounded bg-slate-800 px-2 py-1"
                      />
                      <button
                        type="button"
                        disabled={busy(`propose:${d.id}`)}
                        onClick={() => act(`propose:${d.id}`, () => api.proposeResolution(
                          contractId, d.id, resolution[d.id] ?? "FACTORY", note[d.id] ?? ""))}
                        className="rounded border border-slate-700 px-2 py-1 text-slate-200"
                      >
                        Propose resolution
                      </button>
                    </>
                  )}
                  {canSign && d.status === "resolution_proposed"
                    && d.resolution_proposed_by_party !== api.side && d.resolution_bucket && (
                    <button
                      type="button"
                      disabled={busy(`accept:${d.id}`)}
                      onClick={() => act(`accept:${d.id}`, () => api.acceptResolution(
                        contractId, d.id, d.resolution_bucket as string))}
                      className="rounded bg-emerald-600 px-2 py-1 text-white"
                    >
                      Accept resolution: {bucketLabel(d.resolution_bucket)}
                    </button>
                  )}
                  {canManage && d.raised_by_party === api.side && (
                    <button
                      type="button"
                      disabled={busy(`withdraw:${d.id}`)}
                      onClick={() => act(`withdraw:${d.id}`, () => api.withdrawDispute(contractId, d.id))}
                      className="rounded border border-slate-700 px-2 py-1 text-slate-300"
                    >
                      Withdraw
                    </button>
                  )}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {canRaise && (
        <form onSubmit={raise} className="mt-3 grid gap-2 text-xs sm:grid-cols-2">
          <label className="flex flex-col gap-1">
            Machine
            <select value={installation}
                    onChange={(e) => setInstallation(e.target.value === "" ? "" : Number(e.target.value))}
                    className="rounded bg-slate-800 px-2 py-1">
              {machines.map((m) => (
                <option key={m.installation_id} value={m.installation_id}>{m.serial_number}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            The window should be
            <select value={bucket} onChange={(e) => setBucket(e.target.value)}
                    className="rounded bg-slate-800 px-2 py-1">
              {RESOLUTION_BUCKETS.map((b) => (
                <option key={b} value={b}>{bucketLabel(b)}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            From (UTC, YYYY-MM-DDTHH:MM)
            <input value={start} onChange={(e) => setStart(e.target.value)}
                   className="rounded bg-slate-800 px-2 py-1 font-mono" />
          </label>
          <label className="flex flex-col gap-1">
            To (UTC, YYYY-MM-DDTHH:MM)
            <input value={end} onChange={(e) => setEnd(e.target.value)}
                   className="rounded bg-slate-800 px-2 py-1 font-mono" />
          </label>
          <label className="flex flex-col gap-1 sm:col-span-2">
            Why
            <textarea value={reason} onChange={(e) => setReason(e.target.value)} maxLength={1000}
                      className="rounded bg-slate-800 px-2 py-1" rows={2} />
          </label>
          <div className="sm:col-span-2">
            <button type="submit" disabled={busy("raise")}
                    className="rounded-lg bg-amber-600 px-3 py-1.5 font-medium text-white disabled:opacity-40">
              Raise dispute
            </button>
            <span className="ml-2 text-slate-500">
              Raising a dispute revises the statement and cancels every acceptance.
            </span>
          </div>
        </form>
      )}
      {error && (
        <p role="alert" className="mt-2 text-xs text-red-400">
          {error}
        </p>
      )}
    </section>
  );
}
