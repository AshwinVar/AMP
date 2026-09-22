"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import { stateNotice } from "../lib/evidence";

// Mirrors ai/brief.py (ADR-0028).
type Section = { key: string; title: string; lines: string[]; state: string; view: string | null };
type BlindSpot = { key: string; state: string; text: string };
type Brief = {
  generated_at: string;
  window: string;
  state: string;
  headline: string;
  sections: Section[];
  blind_spots: BlindSpot[];
  note: string;
};

/** The brief as plain text, for the clipboard: the same words, in the same order. */
export function briefText(b: Brief): string {
  const parts = [`Factory brief — ${b.window}`, "", b.headline, ""];
  for (const s of b.sections) {
    parts.push(s.title.toUpperCase());
    for (const line of s.lines) parts.push(`  ${line}`);
    parts.push("");
  }
  parts.push("WHAT AMP COULD NOT SEE");
  for (const spot of b.blind_spots) parts.push(`  ${spot.text}`);
  parts.push("", b.note);
  return parts.join("\n");
}

/**
 * The Daily Factory Brief (ADR-0028).
 *
 * Four cards already answer the owner's questions; this reads them out in one
 * page, in order, so nobody has to join them up. Two things make it a brief
 * rather than a summary: it always states the window it covers, and it always
 * ends with what AMP could NOT see. A brief that reported only what AMP knows
 * would quietly imply it knows the rest.
 *
 * It is a once-or-twice-a-day read, so it loads on demand rather than polling.
 */
// The sections that ARE the brief: where we are, what is wrong, what to do.
// They open on load; the supporting four (what changed, why, what is likely,
// how the shifts did) stay one tap away.
//
// Every section used to render collapsed, and `open` was a single key, so
// opening one closed another. Measured on the demo plant: 32 lines of content
// across seven sections, and an owner opening their morning brief saw NONE of
// it — seven closed headers and the amber "what AMP could not see" box. That
// box is right to be always visible; it is what stops the rest being read as
// the whole picture. The defect was that it was the only thing on screen, so
// the one thing a brief always showed was its own disclaimer.
const OPEN_BY_DEFAULT = ["position", "problems", "actions"];

export default function DailyBriefSection() {
  const [b, setB] = useState<Brief | null>(null);
  // A set, not one key: several sections are open at once, and closing one
  // must not be the price of reading another.
  const [open, setOpen] = useState<Set<string>>(() => new Set(OPEN_BY_DEFAULT));
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    try {
      setB(await apiGet<Brief>("/daily-brief"));
    } catch {
      // A glanceable card — stay quiet rather than break the page.
    }
  }, []);
  useEffect(() => {
    let alive = true;
    const first = setTimeout(() => { if (alive) load(); }, 0);
    return () => { alive = false; clearTimeout(first); };
  }, [load]);

  const copy = async () => {
    if (!b) return;
    try {
      await navigator.clipboard.writeText(briefText(b));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // clipboard blocked — no-op.
    }
  };

  if (!b) return null;
  const notice = stateNotice(b.state);
  const missing = b.blind_spots.filter((s) => s.state !== "OK");
  return (
    <section className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h2 className="text-xl font-bold">Factory brief</h2>
          {/* The window, in the header, always: a brief with no window can be
              read as "today" whatever it actually covers. */}
          <p className="text-[11px] text-slate-500 mt-0.5">{b.window}</p>
          <p className="text-slate-300 text-sm mt-2">{b.headline}</p>
        </div>
        <button
          type="button"
          onClick={copy}
          className="rounded-lg border border-slate-600 text-slate-300 text-xs px-3 py-1 shrink-0"
        >
          {copied ? "Copied" : "Copy brief"}
        </button>
      </div>
      {notice && <p role="status" className="text-amber-300/90 text-xs mt-2">{b.state} · {notice}</p>}

      <div className="mt-4 space-y-2">
        {b.sections.map((s) => (
          <div key={s.key} className="rounded-xl bg-slate-950/60 border border-slate-800">
            <button
              type="button"
              aria-expanded={open.has(s.key)}
              onClick={() =>
                setOpen((cur) => {
                  const next = new Set(cur);
                  if (!next.delete(s.key)) next.add(s.key);
                  return next;
                })
              }
              className="w-full text-left p-3 flex items-center justify-between gap-2"
            >
              <span className="text-sm font-semibold text-slate-200">{s.title}</span>
              <span className="text-[10px] uppercase tracking-wide text-slate-500 shrink-0">
                {s.state === "OK" ? "" : s.state}
              </span>
            </button>
            {open.has(s.key) && (
              <ul className="px-3 pb-3 space-y-1">
                {s.lines.map((line, i) => (
                  <li key={i} className="text-xs text-slate-400">{line}</li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>

      {/* Never behind a toggle. This is the section that stops the rest being
          read as the whole picture. */}
      <div className="mt-4 rounded-xl border border-amber-500/20 bg-amber-500/5 p-3">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-amber-300/90">
          What AMP could not see{missing.length > 0 ? ` (${missing.length})` : ""}
        </h3>
        <ul className="mt-2 space-y-1">
          {b.blind_spots.map((s) => (
            <li key={s.key} className="text-xs text-slate-400">
              {s.state !== "OK" && (
                <span className="text-[10px] uppercase tracking-wide text-slate-500 mr-1">{s.state}</span>
              )}
              {s.text}
            </li>
          ))}
        </ul>
      </div>

      <p className="text-[11px] text-slate-500 mt-3">{b.note}</p>
    </section>
  );
}
