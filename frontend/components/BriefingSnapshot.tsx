"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPost, getUserRole } from "../lib/api";

// Mirrors the backend briefing read-model (ai/briefing.py build_briefing).
type Alert = { key: string; severity: "high" | "medium" | "low"; title: string; detail: string; module: string; escalated: boolean };
type Win = { title: string; detail: string };
type Briefing = {
  has_data: boolean;
  oee: number;
  oee_trend: "up" | "down" | "flat";
  headline: string;
  alerts: Alert[];
  wins: Win[];
};

// Severity → the dot/rail accent for each alert row.
const sevDot: Record<string, string> = {
  high: "bg-red-500",
  medium: "bg-amber-400",
  low: "bg-slate-500",
};
const sevRail: Record<string, string> = {
  high: "border-l-red-500/70",
  medium: "border-l-amber-400/70",
  low: "border-l-slate-600",
};

// Each alert's module → the dashboard view that drills into it. Views the user's
// role or plan can't open are gated gracefully by the dashboard's renderSection.
const MODULE_TO_VIEW: Record<string, string> = {
  machines: "machines",
  inventory: "inventory",
  quality: "quality",
  downtime: "downtime",
  oee: "executive",
};

// OEE trend arrow — up is good (green), down is bad (red), flat is neutral.
function trendMark(t: Briefing["oee_trend"]) {
  if (t === "up") return { glyph: "↑", cls: "text-emerald-400", label: "trending up" };
  if (t === "down") return { glyph: "↓", cls: "text-red-400", label: "trending down" };
  return { glyph: "→", cls: "text-slate-400", label: "steady" };
}

// The morning briefing: the plant's "what needs attention right now" hero for the
// top of the Overview home. Self-contained — fetches its own digest and refreshes,
// and renders nothing until there's production to brief on.
export default function BriefingSnapshot({ onOpen }: { onOpen?: (viewKey: string) => void }) {
  const [b, setB] = useState<Briefing | null>(null);
  const [role, setRole] = useState("");
  const [escalating, setEscalating] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setB(await apiGet<Briefing>("/briefing"));
    } catch {
      // A glanceable hero — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    setRole(getUserRole());
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  // Hand the briefing's most urgent alert to the Escalation agent, which files it
  // into the approval queue. The agent already does this on its own; this is the
  // "act now" shortcut for a supervisor watching the board.
  const escalate = useCallback(async () => {
    setEscalating(true);
    setNote(null);
    try {
      const r = await apiPost<{ escalated: boolean; reason?: string }>("/briefing/escalate", {});
      setNote(
        r.escalated ? "Escalated to the approval queue"
          : r.reason === "already_open" ? "Already escalated"
          : "Nothing urgent to escalate",
      );
      await load();
    } catch {
      setNote("Couldn't escalate — try again");
    } finally {
      setEscalating(false);
    }
  }, [load]);

  if (!b || !b.has_data) return null;

  const tm = trendMark(b.oee_trend);
  const canEscalate = role === "Admin" || role === "Supervisor";
  const nextHigh = b.alerts.find((a) => a.severity === "high" && !a.escalated);
  const showEscalate = canEscalate && Boolean(nextHigh);

  return (
    <div className="rounded-2xl border border-slate-800 bg-gradient-to-br from-slate-900 to-slate-900/40 p-6">
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Morning briefing</h3>
          <p className="text-slate-400 text-sm mt-1">{b.headline}</p>
          {(showEscalate || note) && (
            <div className="mt-2 flex items-center gap-3">
              {showEscalate && (
                <button
                  type="button"
                  onClick={escalate}
                  disabled={escalating}
                  className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2.5 py-1 text-xs font-medium text-amber-300 hover:bg-amber-500/20 transition focus:outline-none focus:ring-2 focus:ring-amber-500/40 disabled:opacity-50"
                  title="Hand the top alert to the Escalation agent"
                >
                  {escalating ? "Escalating…" : "⚡ Escalate top alert"}
                </button>
              )}
              {note && <span className="text-xs text-slate-400">{note}</span>}
            </div>
          )}
        </div>
        <div className="text-right">
          <p className="text-3xl font-bold text-slate-100 flex items-center gap-1.5 justify-end">
            {b.oee}%
            <span className={tm.cls} title={tm.label} aria-label={tm.label}>{tm.glyph}</span>
          </p>
          <p className="text-[11px] text-slate-500">plant OEE · 7 days</p>
        </div>
      </div>

      {b.alerts.length > 0 ? (
        <div className="mt-5 space-y-2">
          {b.alerts.map((a) => {
            const view = MODULE_TO_VIEW[a.module];
            const clickable = Boolean(onOpen && view);
            const base = `group flex items-start gap-3 rounded-lg border border-slate-800 border-l-2 ${sevRail[a.severity]} bg-slate-900/60 px-3 py-2.5`;
            const inner = (
              <>
                <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${sevDot[a.severity]}`} />
                <div className="min-w-0 flex-1">
                  <p className="text-sm text-slate-200">{a.title}</p>
                  {a.detail && <p className="text-xs text-slate-500 mt-0.5 truncate">{a.detail}</p>}
                </div>
                {a.escalated && (
                  <span
                    className="shrink-0 rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-300"
                    title="Escalated by the agent — awaiting approval in Agent Activity"
                  >
                    ⚡ escalated
                  </span>
                )}
                <span className="shrink-0 rounded bg-slate-800 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-slate-400">
                  {a.module}
                </span>
                {clickable && (
                  <span className="shrink-0 text-slate-600 group-hover:text-slate-200 transition mt-0.5" aria-hidden>→</span>
                )}
              </>
            );
            return clickable ? (
              <button
                key={a.key}
                type="button"
                onClick={() => onOpen!(view)}
                className={`${base} w-full text-left hover:border-slate-600 hover:bg-slate-800/60 transition focus:outline-none focus:ring-2 focus:ring-slate-600`}
                title={`Open ${a.module}`}
              >
                {inner}
              </button>
            ) : (
              <div key={a.key} className={base}>
                {inner}
              </div>
            );
          })}
        </div>
      ) : (
        <p className="mt-5 text-sm text-emerald-400">Nothing needs attention — plant is running clean.</p>
      )}

      {b.wins.length > 0 && (
        <div className="mt-4 flex flex-wrap gap-2">
          {b.wins.map((w) => (
            <span
              key={w.title}
              className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-1 text-xs text-emerald-300"
              title={w.detail}
            >
              ✓ {w.title}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
