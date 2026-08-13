"use client";

import React, { useState } from "react";

import { apiGet, apiPost } from "../lib/api";

/**
 * Adding a machine a supplier sent you (ADR-0019).
 *
 * The flow is deliberately three steps, and the middle one is the point:
 *
 *     enter or scan the code  →  SEE WHAT IT IS  →  confirm and choose sharing
 *
 * Opening a link claims nothing. A QR scanned by somebody walking past a crate,
 * or a link forwarded to the wrong person, must not attach equipment to a
 * workspace — so the preview is a read and the commit is a separate, deliberate
 * press.
 *
 * WHAT THE CONFIRMATION SCREEN OWES THE PERSON READING IT: who made this
 * machine, which model, its serial, the warranty their supplier recorded, and
 * exactly what they are about to share. Somebody clicking "add" should not
 * discover afterwards what they agreed to.
 */

type Preview = {
  claim_id: number;
  manufacturer: string;
  manufacturer_name: string;
  support_email: string | null;
  serial_number: string;
  model_code: string | null;
  model_name: string | null;
  family: string | null;
  firmware_version: string | null;
  warranty: { state: string; reason: string; days_remaining: number | null };
  expires_at: string | null;
  available_grants: Array<{ key: string; label: string }>;
  already_granted: string[];
};

export default function AddConnectedEquipment({
  onAdded,
  initialCode = "",
}: {
  onAdded: () => void;
  /**
   * A code that arrived in the URL, from the QR on the machine.
   *
   * It only fills the box and opens the panel. Everything after that is
   * unchanged — the person still presses "look up" and still presses "confirm",
   * because a scanned link must not be able to accept anything on its own.
   */
  initialCode?: string;
}) {
  // Seeded in the initialiser rather than by an effect: an effect would render
  // the empty form first and then swap, and it would fight anything the person
  // typed in that instant.
  const [open, setOpen] = useState(Boolean(initialCode));
  const [code, setCode] = useState(initialCode);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [chosen, setChosen] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState("");

  function reset() {
    setCode("");
    setPreview(null);
    setChosen([]);
    setError("");
    setDone("");
  }

  async function look(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const p = await apiGet<Preview>(
        `/connected-equipment/claim/${encodeURIComponent(code.trim())}`,
      );
      setPreview(p);
      // Start from what is ALREADY agreed with this manufacturer, not from
      // nothing: accepting can only widen, so showing empty boxes would imply
      // the existing agreement was about to be switched off.
      setChosen(p.already_granted || []);
    } catch (err) {
      // The backend's own sentence. It is deliberately the same for every
      // failure — mistyped, expired, withdrawn, already used — because telling
      // them apart would help somebody guessing codes.
      setError(detailOf(err));
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    if (busy || !preview) return;
    setBusy(true);
    setError("");
    try {
      const result = await apiPost<{ serial_number: string; message: string }>(
        `/connected-equipment/claim/${encodeURIComponent(code.trim())}`,
        { grants: chosen },
      );
      setDone(`${result.serial_number} added.`);
      setPreview(null);
      setCode("");
      onAdded();
    } catch (err) {
      setError(detailOf(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-lg font-semibold text-white">
            Add connected equipment
          </h3>
          <p className="text-slate-400 text-sm mt-1 max-w-2xl">
            When a supplier sends you a machine, it comes with a claim code —
            printed on it, or on the paperwork. Enter it here to add the machine
            and decide what its manufacturer may see.
          </p>
        </div>
        {!open && (
          <button
            type="button"
            onClick={() => {
              reset();
              setOpen(true);
            }}
            className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white"
          >
            Add equipment
          </button>
        )}
      </div>

      {done && <p className="mt-4 text-sm text-green-400">{done}</p>}

      {open && (
        <div className="mt-5 border-t border-slate-800 pt-5">
          {!preview ? (
            <form onSubmit={look} className="space-y-3">
              <label className="block text-sm text-slate-300" htmlFor="claim-code">
                Claim code
              </label>
              <input
                id="claim-code"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                placeholder="AMP-XXXXX-XXXXX-XXXXX"
                autoComplete="off"
                className="w-full max-w-md rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm text-white"
              />
              <p className="text-xs text-slate-500">
                Scanning the QR on the machine opens this page with the code
                filled in. Dashes and capitals do not matter.
              </p>
              {error && (
                <p role="alert" className="text-sm text-red-400">
                  {error}
                </p>
              )}
              <div className="flex gap-3">
                <button
                  type="submit"
                  disabled={busy || !code.trim()}
                  className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
                >
                  {busy ? "Checking…" : "Look up machine"}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setOpen(false);
                    reset();
                  }}
                  className="rounded-lg border border-slate-700 px-4 py-2 text-sm text-slate-300"
                >
                  Cancel
                </button>
              </div>
            </form>
          ) : (
            <div className="space-y-5">
              {/* STEP 2. What this actually is, before anything is committed. */}
              <div className="rounded-xl border border-slate-800 bg-slate-950/60 p-4">
                <p className="text-xs uppercase tracking-wide text-slate-500">
                  You are about to add
                </p>
                <p className="mt-1 text-lg font-semibold text-white font-mono">
                  {preview.serial_number}
                </p>
                <dl className="mt-3 grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
                  <div>
                    <dt className="inline text-slate-500">Manufacturer: </dt>
                    <dd className="inline text-slate-200">
                      {preview.manufacturer_name}
                    </dd>
                  </div>
                  <div>
                    <dt className="inline text-slate-500">Model: </dt>
                    <dd className="inline text-slate-200">
                      {preview.model_name ?? preview.model_code ?? "—"}
                    </dd>
                  </div>
                  <div>
                    <dt className="inline text-slate-500">Warranty: </dt>
                    <dd className="inline text-slate-200">
                      {preview.warranty?.reason ?? "not recorded"}
                    </dd>
                  </div>
                  {preview.support_email && (
                    <div>
                      <dt className="inline text-slate-500">Support: </dt>
                      <dd className="inline text-slate-200">
                        {preview.support_email}
                      </dd>
                    </div>
                  )}
                </dl>
                <p className="mt-3 text-xs text-slate-500">
                  If this is not the machine in front of you, do not add it — ask
                  your supplier to check which code they sent.
                </p>
              </div>

              {/* STEP 3. Consent, stated before the commit rather than after. */}
              <div>
                <h4 className="text-sm font-semibold text-white">
                  What {preview.manufacturer_name} may see
                </h4>
                <p className="text-xs text-slate-500 mt-1">
                  Nothing is shared unless you tick it. You can change this at any
                  time, and adding a machine never turns anything off.
                </p>
                <div className="mt-3 grid gap-2 sm:grid-cols-2">
                  {preview.available_grants.map((g) => (
                    <label
                      key={g.key}
                      className={`flex items-start gap-3 rounded-lg border p-3 text-sm cursor-pointer ${
                        chosen.includes(g.key)
                          ? "border-blue-500/40 bg-blue-500/5 text-slate-200"
                          : "border-slate-800 bg-slate-900 text-slate-400"
                      }`}
                    >
                      <input
                        type="checkbox"
                        className="mt-0.5"
                        checked={chosen.includes(g.key)}
                        onChange={() =>
                          setChosen((prev) =>
                            prev.includes(g.key)
                              ? prev.filter((k) => k !== g.key)
                              : [...prev, g.key],
                          )
                        }
                      />
                      <span>
                        {g.label}
                        {preview.already_granted?.includes(g.key) && (
                          <span className="block text-[11px] text-slate-500">
                            already shared with this manufacturer
                          </span>
                        )}
                      </span>
                    </label>
                  ))}
                </div>
                <p className="mt-3 text-xs text-slate-500">
                  {chosen.length === 0
                    ? "You are sharing nothing. The machine will still be added."
                    : `Sharing ${chosen.length} of ${preview.available_grants.length}.`}{" "}
                  No setting here can reveal work orders, production quantities,
                  recipes, inventory, costs or operators.
                </p>
              </div>

              {error && (
                <p role="alert" className="text-sm text-red-400">
                  {error}
                </p>
              )}

              <div className="flex flex-wrap gap-3">
                <button
                  type="button"
                  onClick={confirm}
                  disabled={busy}
                  className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
                >
                  {busy ? "Adding…" : "Confirm and add machine"}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setPreview(null);
                    setError("");
                  }}
                  className="rounded-lg border border-slate-700 px-4 py-2 text-sm text-slate-300"
                >
                  Back
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * The backend's own sentence, not a generic one.
 *
 * "That claim code is not valid…" and "Unknown sharing grants: X" call for
 * different reactions, and a swallowed message makes both look like an outage.
 */
function detailOf(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err);
  try {
    const parsed = JSON.parse(raw);
    if (parsed?.detail) return String(parsed.detail);
  } catch {
    /* a non-JSON error body is still an error; keep the text */
  }
  // apiGet formats failures as "Failed request: <path> | <status> | <body>".
  const tail = raw.split("|").pop()?.trim() ?? raw;
  try {
    const parsed = JSON.parse(tail);
    if (parsed?.detail) return String(parsed.detail);
  } catch {
    /* fall through */
  }
  return tail || "That did not work.";
}
