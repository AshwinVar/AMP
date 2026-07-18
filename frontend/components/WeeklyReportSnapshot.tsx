"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend report read-model (ai/report.py build_weekly_report).
type WeeklyReport = { has_data: boolean; generated_at: string; markdown: string };

// The weekly plant report: the whole week composed into one Markdown page an
// owner can copy into an email or download and file. Self-contained — fetches its
// own report and refreshes. Renders nothing until there's data.
export default function WeeklyReportSnapshot() {
  const [r, setR] = useState<WeeklyReport | null>(null);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    try {
      setR(await apiGet<WeeklyReport>("/weekly-report"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 60000);
    return () => clearInterval(id);
  }, [load]);

  if (!r || !r.has_data) return null;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(r.markdown);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // clipboard blocked — no-op.
    }
  };

  const download = () => {
    const blob = new Blob([r.markdown], { type: "text/markdown" });
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

      <pre className="mt-4 max-h-64 overflow-auto rounded-lg border border-slate-800 bg-slate-950/60 p-4 text-xs leading-relaxed text-slate-300 whitespace-pre-wrap">
        {r.markdown}
      </pre>
    </div>
  );
}
