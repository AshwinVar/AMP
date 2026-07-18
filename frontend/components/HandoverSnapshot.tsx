"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend handover read-model (ai/handover.py build_handover).
type Alert = { key: string; severity: "high" | "medium" | "low"; title: string; detail: string };
type Win = { title: string; detail: string };
type Handover = {
  has_data: boolean;
  oee: number;
  oee_trend: "up" | "down" | "flat";
  produced: { good: number; total: number; good_rate: number; runs: number };
  open_work: { pending_approvals: number; open_escalations: number };
  attention: Alert[];
  wins: Win[];
};

const sevDot: Record<string, string> = { high: "bg-red-500", medium: "bg-amber-400", low: "bg-slate-500" };
const trendGlyph = (t: Handover["oee_trend"]) => (t === "up" ? "↑" : t === "down" ? "↓" : "→");

// The shift-handover card: what was produced, how the plant ran, and what's open
// to carry to the next shift. Self-contained — fetches its own summary and
// refreshes. Renders nothing until there's data.
export default function HandoverSnapshot() {
  const [h, setH] = useState<Handover | null>(null);

  const load = useCallback(async () => {
    try {
      setH(await apiGet<Handover>("/handover"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  if (!h || !h.has_data) return null;

  const tiles = [
    { label: "Good units", value: h.produced.good.toLocaleString(), sub: `${h.produced.good_rate}% good` },
    { label: "Plant OEE", value: `${h.oee}% ${trendGlyph(h.oee_trend)}`, sub: `${h.produced.runs} runs` },
    { label: "Awaiting approval", value: h.open_work.pending_approvals, sub: "agent actions" },
    { label: "Open escalations", value: h.open_work.open_escalations, sub: "to carry over" },
  ];

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Shift handover</h3>
          <p className="text-slate-400 text-sm mt-1">What was made, and what's open to carry to the next shift</p>
        </div>
      </div>

      <div className="mt-4 grid grid-cols-2 md:grid-cols-4 gap-2">
        {tiles.map((t) => (
          <div key={t.label} className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2.5">
            <p className="text-xl font-bold text-slate-100">{t.value}</p>
            <p className="text-[11px] text-slate-400">{t.label}</p>
            <p className="text-[10px] text-slate-600">{t.sub}</p>
          </div>
        ))}
      </div>

      {h.attention.length > 0 && (
        <div className="mt-4">
          <p className="text-xs text-slate-500 mb-2">Carry over — needs attention</p>
          <div className="space-y-1.5">
            {h.attention.map((a) => (
              <div key={a.key} className="flex items-center gap-2.5 text-sm">
                <span className={`h-2 w-2 shrink-0 rounded-full ${sevDot[a.severity]}`} />
                <span className="text-slate-300">{a.title}</span>
                {a.detail && <span className="text-slate-500 text-xs truncate">· {a.detail}</span>}
              </div>
            ))}
          </div>
        </div>
      )}

      {h.wins.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {h.wins.map((w) => (
            <span key={w.title} className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-1 text-xs text-emerald-300" title={w.detail}>
              ✓ {w.title}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
