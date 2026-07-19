"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPost } from "../lib/api";

// Mirrors the backend report read-model (ai/report.py build_weekly_report).
type WeeklyReport = { has_data: boolean; generated_at: string; markdown: string };
type AiReport = { report: string; source?: string; model?: string | null; note?: string };

// The weekly plant report: the whole week composed into one Markdown page an
// owner can copy into an email or download and file. Self-contained — fetches its
// own report and refreshes. Renders nothing until there's data. When an LLM is
// connected, one tap rewrites it as an AI narrative (honestly badged).
export default function WeeklyReportSnapshot() {
  const [r, setR] = useState<WeeklyReport | null>(null);
  const [copied, setCopied] = useState(false);
  const [aiEnabled, setAiEnabled] = useState(false);
  const [aiReport, setAiReport] = useState<AiReport | null>(null);
  const [aiLoading, setAiLoading] = useState(false);

  const load = useCallback(async () => {
    try {
      setR(await apiGet<WeeklyReport>("/weekly-report"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    apiGet<{ enabled: boolean }>("/ai/status").then((s) => setAiEnabled(!!s.enabled)).catch(() => {});
  }, []);

  const aiNarrative = async () => {
    if (aiLoading) return;
    setAiLoading(true);
    try {
      setAiReport(await apiPost<AiReport>("/ai/report", {}));
    } catch {
      // The server itself falls back to rules; a request failure here just
      // leaves the standard report showing.
    }
    setAiLoading(false);
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 60000);
    return () => clearInterval(id);
  }, [load]);

  if (!r || !r.has_data) return null;

  // Copy/download act on whatever is on screen — the AI narrative when shown,
  // otherwise the standard report.
  const shownText = aiReport ? aiReport.report : r.markdown;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(shownText);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // clipboard blocked — no-op.
    }
  };

  const download = () => {
    const blob = new Blob([shownText], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `weekly-plant-report-${r.generated_at.slice(0, 10)}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Weekly report</h3>
          <p className="text-slate-400 text-sm mt-1">The week on one page — ready to send</p>
        </div>
        <div className="flex items-center gap-2">
          {aiEnabled && !aiReport && (
            <button
              type="button"
              onClick={aiNarrative}
              disabled={aiLoading}
              className="rounded-md border border-emerald-500/40 px-2.5 py-1 text-xs text-emerald-300 hover:bg-emerald-500/10 transition disabled:opacity-50"
            >
              {aiLoading ? "Writing…" : "✦ AI narrative"}
            </button>
          )}
          {aiReport && (
            <button
              type="button"
              onClick={() => setAiReport(null)}
              className="rounded-md border border-slate-700 px-2.5 py-1 text-xs text-slate-300 hover:border-slate-500 transition"
            >
              Show standard
            </button>
          )}
          <button
            type="button"
            onClick={copy}
            className="rounded-md border border-slate-700 px-2.5 py-1 text-xs text-slate-300 hover:border-slate-500 hover:bg-slate-800 transition focus:outline-none focus:ring-2 focus:ring-slate-600"
          >
            {copied ? "Copied ✓" : "Copy"}
          </button>
          <button
            type="button"
            onClick={download}
            className="rounded-md border border-slate-700 px-2.5 py-1 text-xs text-slate-300 hover:border-slate-500 hover:bg-slate-800 transition focus:outline-none focus:ring-2 focus:ring-slate-600"
          >
            Download .md
          </button>
        </div>
      </div>

      {aiReport && (
        <div className="mt-3 flex items-center gap-2">
          <span className={`text-[10px] uppercase tracking-wide rounded-full px-2 py-0.5 border ${aiReport.source === "llm" ? "text-emerald-300 border-emerald-500/40" : "text-slate-400 border-slate-700"}`}>
            {aiReport.source === "llm" ? `✦ AI · ${aiReport.model || "model"}` : "rules"}
          </span>
          {aiReport.note && <span className="text-amber-300/80 text-xs">{aiReport.note}</span>}
        </div>
      )}
      <pre className="mt-4 max-h-64 overflow-auto rounded-lg border border-slate-800 bg-slate-950/60 p-4 text-xs leading-relaxed text-slate-300 whitespace-pre-wrap">
        {shownText}
      </pre>
    </div>
  );
}
