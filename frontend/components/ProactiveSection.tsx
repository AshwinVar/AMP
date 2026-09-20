"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPost, getUserRole } from "../lib/api";

// Mirrors ai/proactive.py (ADR-0031).
type Item = {
  signature: string;
  kind: string;
  severity: string;
  title: string;
  message: string;
  why: string;
  view: string;
  suppressed_by?: string;
};
type Plan = {
  state: string;
  headline: string;
  considered: number;
  qualified: Item[];
  suppressed: Item[];
  cooldown_hours: number;
  max_per_run: number;
  bar: string;
};

const SEVERITY_CLASS: Record<string, string> = {
  Critical: "text-red-300 border-red-500/40",
  Warning: "text-amber-300 border-amber-500/40",
  Info: "text-slate-400 border-slate-600",
};

/** Group the held-back items by the reason they were held back. */
export function byReason(items: Item[]): [string, Item[]][] {
  const groups = new Map<string, Item[]>();
  for (const i of items) {
    const key = i.suppressed_by ?? "HELD BACK";
    groups.set(key, [...(groups.get(key) ?? []), i]);
  }
  return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
}

/**
 * What AMP would tell you, and what it is holding back (ADR-0031).
 *
 * A proactive feature that showed only what it sent could not be argued with.
 * This card leads with the restraint: the bar is printed on it, the held-back
 * items are grouped by the reason they were held back, and the counts
 * reconcile with what was considered.
 *
 * Sending is a separate, explicit act — the button, for an Admin or Supervisor.
 * Opening this page never notifies anyone.
 */
export default function ProactiveSection() {
  const [p, setP] = useState<Plan | null>(null);
  const [open, setOpen] = useState(false);
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState<string | null>(null);
  const role = getUserRole();
  const canSend = role === "Admin" || role === "Supervisor";

  const load = useCallback(async () => {
    try {
      setP(await apiGet<Plan>("/proactive"));
    } catch {
      // A glanceable card — stay quiet rather than break the page.
    }
  }, []);
  useEffect(() => {
    let alive = true;
    const first = setTimeout(() => { if (alive) load(); }, 0);
    return () => { alive = false; clearTimeout(first); };
  }, [load]);

  const send = async () => {
    if (sending) return;
    setSending(true);
    try {
      const r = await apiPost<{ sent: number; held_back: number }>("/proactive/send", {});
      setSent(`Sent ${r.sent}, held back ${r.held_back}.`);
      await load();
    } catch {
      setSent("Could not send just now.");
    }
    setSending(false);
  };

  if (!p) return null;
  return (
    <section className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h2 className="text-xl font-bold">What AMP would tell you</h2>
          <p className="text-slate-300 text-sm mt-1">{p.headline}</p>
        </div>
        {canSend && p.qualified.length > 0 && (
          <button
            type="button"
            onClick={send}
            disabled={sending}
            className="rounded-lg border border-slate-600 text-slate-300 text-xs px-3 py-1 shrink-0 disabled:opacity-50"
          >
            {sending ? "Sending…" : `Notify (${p.qualified.length})`}
          </button>
        )}
      </div>
      {sent && <p role="status" className="text-xs text-slate-400 mt-2">{sent}</p>}

      {/* The bar, printed. A threshold nobody can see is a threshold nobody can
          argue with. */}
      <p className="text-[11px] text-slate-500 mt-3">{p.bar}</p>

      {p.qualified.length > 0 && (
        <ul className="mt-3 space-y-2">
          {p.qualified.map((q) => (
            <li key={q.signature} className="rounded-xl bg-slate-950/60 border border-slate-800 p-3">
              <div className="flex items-start justify-between gap-2 flex-wrap">
                <div>
                  <p className="text-sm font-semibold">{q.title}</p>
                  <p className="text-xs text-slate-400">{q.message}</p>
                  <p className="text-[11px] text-slate-500 mt-1">Raised because {q.why}.</p>
                </div>
                <span className={`text-[10px] uppercase tracking-wide rounded-full px-2 py-0.5 border shrink-0 ${SEVERITY_CLASS[q.severity] ?? SEVERITY_CLASS.Info}`}>
                  {q.severity}
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}

      {p.suppressed.length > 0 && (
        <div className="mt-4">
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen(!open)}
            className="text-xs text-slate-400 hover:text-white"
          >
            {open ? "Hide" : "Show"} the {p.suppressed.length} AMP is holding back
          </button>
          {open && (
            <div className="mt-2 space-y-3">
              {byReason(p.suppressed).map(([reason, items]) => (
                <div key={reason}>
                  <h3 className="text-[10px] uppercase tracking-wide text-slate-500">
                    {reason} ({items.length})
                  </h3>
                  <ul className="mt-1 space-y-1">
                    {items.map((i) => (
                      <li key={i.signature} className="text-xs text-slate-400">
                        {i.title}
                        <span className="block text-[11px] text-slate-600">{i.why}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      <p className="text-[11px] text-slate-600 mt-3">
        {p.considered} considered · the same thing is not raised twice within {p.cooldown_hours} hours ·
        at most {p.max_per_run} in one go
      </p>
    </section>
  );
}
