"use client";

import React, { useCallback, useEffect, useState } from "react";

import {
  createClaim,
  fetchClaims,
  registerMachine,
  revokeClaim,
  type OemClaim,
} from "../lib/oem";

/**
 * A manufacturer's stock, and the invitations it has issued (ADR-0019).
 *
 * Two things happen here and they are deliberately separate steps:
 *
 *     register a machine     it exists, and belongs to NOBODY
 *     issue an invitation    a code that a customer can turn into an installation
 *
 * Registering attaches nothing. That is the whole point of the design: a
 * manufacturer cannot put a row on a customer's screen, it can only send a code
 * with the machine and wait for them to accept it.
 *
 * THE CODE IS SHOWN EXACTLY ONCE. AMP stores a SHA-256 and the last four
 * characters, so this screen cannot show it again and does not pretend it can —
 * the copy box stays open until dismissed, and the list afterwards shows only
 * the hint.
 */

type Model = { id: number; model_code: string; name: string };

const STATUS_TONE: Record<string, string> = {
  Pending: "text-amber-300 border-amber-500/40 bg-amber-500/10",
  Claimed: "text-green-400 border-green-500/40 bg-green-500/10",
  Revoked: "text-slate-400 border-slate-600/50 bg-slate-700/20",
  Expired: "text-slate-400 border-slate-600/50 bg-slate-700/20",
};

export default function OemMachineRegistry({ models }: { models: Model[] }) {
  const [claims, setClaims] = useState<OemClaim[]>([]);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);

  const [serial, setSerial] = useState("");
  const [modelId, setModelId] = useState<number | "">("");
  const [issued, setIssued] = useState<{ code: string; url: string; serial: string } | null>(null);

  const load = useCallback(() => {
    fetchClaims()
      .then((r) => {
        setClaims(r.claims);
        setError("");
      })
      // A failed load must never render as "no invitations" — an OEM would
      // reissue codes for machines that already have live ones.
      .catch((e) => setError(e instanceof Error ? e.message : "Could not load claims"))
      .finally(() => setLoaded(true));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function registerAndOffer(e: React.FormEvent) {
    e.preventDefault();
    if (busy || !serial.trim() || modelId === "") return;
    setBusy(true);
    setError("");
    try {
      const machine = await registerMachine({
        serial_number: serial.trim(),
        model_id: Number(modelId),
      });
      const claim = await createClaim(machine.installation_id);
      setIssued({
        code: claim.claim_code,
        url: claim.claim_url,
        serial: machine.serial_number,
      });
      setSerial("");
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not register the machine");
    } finally {
      setBusy(false);
    }
  }

  async function withdraw(claim: OemClaim) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await revokeClaim(claim.id);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not withdraw it");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="mt-8">
      <h2 className="text-lg font-semibold text-white">Machines and installations</h2>
      <p className="text-slate-500 text-xs mt-1 max-w-3xl">
        Register a machine you have built, then issue an installation code to send
        with it. Registering attaches the machine to nobody — a customer becomes
        its site only by accepting the code themselves.
      </p>

      {models.length === 0 ? (
        <p className="mt-4 rounded-xl border border-slate-800 bg-slate-900/60 p-4 text-sm text-slate-400">
          Your catalogue has no models yet, so there is nothing to register a
          machine against. Ask your AMP contact to add one.
        </p>
      ) : (
        <form
          onSubmit={registerAndOffer}
          className="mt-4 flex flex-wrap items-end gap-3 rounded-xl border border-slate-800 bg-slate-900/60 p-4"
        >
          <div>
            <label className="block text-xs text-slate-500" htmlFor="oem-serial">
              Serial number
            </label>
            <input
              id="oem-serial"
              value={serial}
              onChange={(e) => setSerial(e.target.value)}
              placeholder="SN-0001"
              autoComplete="off"
              className="mt-1 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm text-white"
            />
          </div>
          <div>
            <label className="block text-xs text-slate-500" htmlFor="oem-model">
              Model
            </label>
            <select
              id="oem-model"
              value={modelId}
              onChange={(e) =>
                setModelId(e.target.value === "" ? "" : Number(e.target.value))
              }
              className="mt-1 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white"
            >
              <option value="">Choose…</option>
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.model_code} — {m.name}
                </option>
              ))}
            </select>
          </div>
          <button
            type="submit"
            disabled={busy || !serial.trim() || modelId === ""}
            className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
          >
            {busy ? "Working…" : "Register and create code"}
          </button>
        </form>
      )}

      {error && (
        <p role="alert" className="mt-3 text-sm text-red-400">
          {error}
        </p>
      )}

      {/* SHOWN ONCE. Deliberately a blocking panel rather than a toast: a code
          that scrolls away is a machine nobody can install. */}
      {issued && (
        <div className="mt-4 rounded-xl border border-blue-500/40 bg-blue-500/5 p-5">
          <h3 className="text-white font-semibold">
            Installation code for {issued.serial}
          </h3>
          <p className="mt-1 text-xs text-slate-400">
            Print this with the machine or send it to the customer. AMP stores
            only a hash — <strong>this is the only time it can be shown</strong>.
          </p>
          <p className="mt-3 select-all rounded-lg border border-slate-700 bg-slate-950 px-4 py-3 font-mono text-lg tracking-wider text-white">
            {issued.code}
          </p>
          <p className="mt-2 break-all text-xs text-slate-500">
            QR target: {issued.url}
          </p>
          <button
            type="button"
            onClick={() => setIssued(null)}
            className="mt-3 rounded-lg border border-slate-700 px-4 py-2 text-sm text-slate-300"
          >
            I have saved it
          </button>
        </div>
      )}

      <div className="mt-5 overflow-x-auto rounded-xl border border-slate-800 bg-slate-900/60">
        <table className="w-full text-sm">
          <thead className="text-slate-500 text-xs uppercase tracking-wide">
            <tr>
              <th className="text-left p-3">Serial</th>
              <th className="text-left p-3">Model</th>
              <th className="text-left p-3">Status</th>
              <th className="text-left p-3">Code</th>
              <th className="text-left p-3">Accepted by</th>
              <th className="text-left p-3">Expires</th>
              <th className="text-left p-3"></th>
            </tr>
          </thead>
          <tbody>
            {claims.map((c) => (
              <tr key={c.id} className="border-t border-slate-800">
                <td className="p-3 font-mono text-xs">{c.serial_number ?? "—"}</td>
                <td className="p-3">{c.model_code ?? "—"}</td>
                <td className="p-3">
                  <span
                    className={`inline-block rounded border px-2 py-0.5 text-xs ${
                      STATUS_TONE[c.status] ?? STATUS_TONE.Expired
                    }`}
                  >
                    {c.status}
                  </span>
                </td>
                {/* Only the hint. The code itself is unrecoverable by design. */}
                <td className="p-3 font-mono text-xs text-slate-500">
                  …{c.code_hint}
                </td>
                <td className="p-3 text-slate-300">
                  {c.claimed_by_customer ?? (
                    <span className="text-slate-500">not yet</span>
                  )}
                </td>
                <td className="p-3 text-slate-400 text-xs">
                  {c.expires_at ? c.expires_at.slice(0, 10) : "—"}
                </td>
                <td className="p-3">
                  {c.status === "Pending" && (
                    <button
                      type="button"
                      onClick={() => withdraw(c)}
                      disabled={busy}
                      className="rounded border border-slate-700 px-3 py-1 text-xs text-slate-300 disabled:opacity-40"
                    >
                      Withdraw
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {loaded && claims.length === 0 && !error && (
          <p className="p-6 text-sm text-slate-500">
            No installation codes issued yet. Register a machine above to create
            one.
          </p>
        )}
        {!loaded && !error && (
          <p className="p-6 text-sm text-slate-500">Loading…</p>
        )}
      </div>
    </section>
  );
}
