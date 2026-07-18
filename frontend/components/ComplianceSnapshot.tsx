"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend compliance read-model (ai/compliance.py build_compliance_summary).
type Doc = {
  document_no: string; title: string; type: string; department: string;
  owner: string; status: string; review_due_date: string | null; overdue: boolean;
};
type ComplianceSummary = {
  total: number;
  overdue: number;
  due_soon: number;
  pending_approval: number;
  by_status: { status: string; count: number }[];
  documents: Doc[];
};

const statusChip: Record<string, string> = {
  Approved: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  "In Review": "border-amber-500/40 bg-amber-500/10 text-amber-300",
  Draft: "border-slate-700 bg-slate-800 text-slate-300",
};

// A glanceable compliance read-out — controlled documents overdue for review, due
// soon, and awaiting approval, plus the docs to review next. Self-contained:
// fetches its own summary and refreshes. Renders nothing when there are no docs.
export default function ComplianceSnapshot({ onOpen }: { onOpen?: (viewKey: string) => void }) {
  const [s, setS] = useState<ComplianceSummary | null>(null);

  const load = useCallback(async () => {
    try {
      setS(await apiGet<ComplianceSummary>("/compliance-summary"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  if (!s || s.total === 0) return null;

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Compliance</h3>
          <p className="text-slate-400 text-sm mt-1">
            {s.total} controlled document{s.total !== 1 ? "s" : ""}
            {s.overdue > 0 ? ` · ${s.overdue} review${s.overdue !== 1 ? "s" : ""} overdue` : ""}
          </p>
        </div>
        {onOpen && (
          <button
            type="button"
            onClick={() => onOpen("documents")}
            className="rounded-md border border-slate-700 px-2.5 py-1 text-xs text-slate-300 hover:border-slate-500 hover:bg-slate-800 transition focus:outline-none focus:ring-2 focus:ring-slate-600"
          >
            Open Documents →
          </button>
        )}
      </div>

      <div className="mt-4 grid grid-cols-3 gap-2">
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2.5 text-center">
          <p className={`text-xl font-bold ${s.overdue > 0 ? "text-red-400" : "text-slate-100"}`}>{s.overdue}</p>
          <p className="text-[11px] text-slate-500">overdue</p>
        </div>
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2.5 text-center">
          <p className={`text-xl font-bold ${s.due_soon > 0 ? "text-amber-400" : "text-slate-100"}`}>{s.due_soon}</p>
          <p className="text-[11px] text-slate-500">due soon</p>
        </div>
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2.5 text-center">
          <p className={`text-xl font-bold ${s.pending_approval > 0 ? "text-amber-400" : "text-slate-100"}`}>{s.pending_approval}</p>
          <p className="text-[11px] text-slate-500">unapproved</p>
        </div>
      </div>

      {s.by_status.length > 0 && (
        <div className="mt-4 flex flex-wrap gap-2">
          {s.by_status.map((b) => (
            <span key={b.status} className={`rounded-md border px-2.5 py-1 text-xs font-medium ${statusChip[b.status] ?? statusChip.Draft}`}>
              {b.status} <span className="opacity-70">· {b.count}</span>
            </span>
          ))}
        </div>
      )}

      <div className="mt-4">
        <p className="text-xs text-slate-500 mb-2">To review</p>
        <div className="space-y-2">
          {s.documents.map((d) => (
            <div
              key={d.document_no}
              className={`flex items-start gap-3 rounded-lg border border-slate-800 border-l-2 ${d.overdue ? "border-l-red-500/70" : "border-l-slate-600"} bg-slate-900/40 px-3 py-2`}
            >
              <div className="min-w-0 flex-1">
                <p className="text-sm text-slate-200 truncate">
                  {d.title} <span className="text-slate-500">· {d.type}</span>
                </p>
                <p className="text-[11px] text-slate-500 truncate">
                  {d.document_no} · {d.owner}{d.review_due_date ? ` · review ${d.review_due_date}` : ""}
                </p>
              </div>
              {d.overdue && (
                <span className="shrink-0 rounded bg-red-500/15 px-1.5 py-0.5 text-[10px] font-medium text-red-300">overdue</span>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
