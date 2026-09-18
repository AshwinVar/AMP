"use client";

import { useCallback, useEffect, useState } from "react";

import { consentStateText, type ConsentCapability, type ConsentPage } from "../lib/aiModels";
import { apiGet, apiPut, getUserRole } from "../lib/api";
import { describeActionFailure } from "../lib/useActionError";
import { LoadError, useLoadError } from "../lib/useLoadError";

/**
 * AI learning consent (ADR-0020): whether AMP-native AI may learn from THIS
 * company's own data.
 *
 * AMP's models ship trained on synthetic data. The one capability that fits
 * anything to a company's own records (a machine's normal telemetry range, for
 * the anomaly check) runs only if an Admin of that company turned it on here,
 * and stops on the next request after they turn it off. The backend enforces
 * all of it: Admin only, never from a founder preview, and the change and its
 * audit record commit together. This card only says so plainly and asks before
 * turning something ON.
 *
 * Operators are not shown the card (the endpoint refuses them). Supervisors see
 * it read-only, with the server's reason.
 */
export default function AILearningConsentCard() {
  const role = getUserRole();
  const canSee = role === "Admin" || role === "Supervisor";
  const [page, setPage] = useState<ConsentPage | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [writeError, setWriteError] = useState<string | null>(null);
  const { error, track } = useLoadError();

  const load = useCallback(() => {
    track(apiGet<ConsentPage>("/ai-consent"), setPage, "AI learning consent");
  }, [track]);

  useEffect(() => {
    if (canSee) load();
  }, [canSee, load]);

  const save = useCallback(async (cap: ConsentCapability, granted: boolean) => {
    setSaving(cap.capability);
    setWriteError(null);
    try {
      setPage(await apiPut<ConsentPage>(`/ai-consent/${encodeURIComponent(cap.capability)}`, { granted }));
      setConfirming(null);
    } catch (e) {
      setWriteError(describeActionFailure(e, granted ? "turn this on" : "turn this off"));
    } finally {
      setSaving(null);
    }
  }, []);

  if (!canSee) return null;
  if (!page) return error ? <LoadError message={error} /> : null;

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">AI learning consent</h3>
          <p className="text-slate-400 text-sm mt-1">
            AMP&apos;s models are trained on synthetic data. Nothing learns from {page.tenant}&apos;s own data unless an
            Admin of {page.tenant} turns it on here.
          </p>
        </div>
        {!page.can_edit && <span className="text-[11px] text-slate-500 mt-1">Read only</span>}
      </div>

      {page.read_only_reason && <p className="text-xs text-slate-500 mt-2">{page.read_only_reason}</p>}
      {writeError && (
        <div role="alert" className="mt-3 rounded-xl border border-red-500/40 bg-red-500/10 text-red-300 p-3 text-sm">
          {writeError}
        </div>
      )}

      <div className="mt-4 space-y-3">
        {page.capabilities.map((cap) => (
          <div key={cap.capability} className="rounded-xl border border-slate-800 bg-slate-900 p-4">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-sm text-slate-200 font-medium">{cap.title}</p>
                <p className="text-xs text-slate-500 mt-1">{consentStateText(cap)}</p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={cap.granted}
                aria-label={cap.title}
                disabled={!page.can_edit || saving === cap.capability}
                onClick={() => (cap.granted ? save(cap, false) : setConfirming(cap.capability))}
                title={cap.granted ? "On — click to turn off" : "Off — click to review and turn on"}
                className={`shrink-0 relative inline-flex h-6 w-11 items-center rounded-full transition disabled:opacity-50 disabled:cursor-not-allowed ${
                  cap.granted ? "bg-emerald-500" : "bg-slate-700"
                }`}
              >
                <span
                  className={`inline-block h-4 w-4 transform rounded-full bg-white transition ${
                    cap.granted ? "translate-x-6" : "translate-x-1"
                  }`}
                />
              </button>
            </div>
            <dl className="mt-3 grid grid-cols-1 md:grid-cols-2 gap-2 text-xs">
              <div>
                <dt className="text-slate-500">What it reads</dt>
                <dd className="text-slate-300">{cap.reads}</dd>
              </div>
              <div>
                <dt className="text-slate-500">What is kept</dt>
                <dd className="text-slate-300">{cap.stored}</dd>
              </div>
            </dl>
            {confirming === cap.capability && (
              <div role="dialog" aria-label={`Turn on ${cap.title}`} className="mt-3 rounded-xl border border-amber-500/40 bg-amber-500/10 p-3">
                <p className="text-sm text-amber-200">Turn on &ldquo;{cap.title}&rdquo; for {page.tenant}?</p>
                <p className="text-xs text-amber-200/80 mt-1">
                  {cap.reads} {cap.stored} It is recorded in the audit log under your name, and any Admin can turn it off
                  again.
                </p>
                <div className="mt-3 flex gap-2">
                  <button
                    type="button"
                    onClick={() => save(cap, true)}
                    disabled={saving === cap.capability}
                    className="rounded-lg bg-white text-slate-950 text-xs font-semibold px-3 py-1.5 disabled:opacity-50"
                  >
                    Turn on
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirming(null)}
                    className="rounded-lg border border-slate-600 text-slate-300 text-xs px-3 py-1.5"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
