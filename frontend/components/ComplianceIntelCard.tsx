"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors ai.compliance.build_compliance_summary (GET /compliance-summary).
type ComplianceSummary = {
  total: number;
  overdue: number;
  due_soon: number;
  pending_approval: number;
  by_status: { status: string; count: number }[];
  documents: {
    document_no: string;
    title: string;
    type: string;
    department: string;
    owner: string;
    status: string;
    review_due_date: string | null;
    overdue: boolean;
  }[];
};

// The Documents view's compliance layer over the document CRUD: is the
// controlled-document system healthy? Review debt (overdue/due-soon), unapproved
// documents, and the reviews to run next. Self-contained; hides on error so the
// CRUD below always renders.
export default function ComplianceIntelCard() {
  const [s, setS] = useState<ComplianceSummary | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setS(await apiGet<ComplianceSummary>("/compliance-summary"));
    } catch {
      setS(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (loading && !s) {
    return (
      <div className="rounded-2xl border border-slate-800 bg-slate-900 p-5 text-slate-400 text-sm">
        Loading compliance…
      </div>
    );
  }
  if (!s || s.total === 0) return null;   // no controlled documents -> no card

  const debt = s.overdue + s.due_soon;

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4">
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-2">
        <h3 className="text-lg font-semibold">Compliance — controlled documents</h3>
        <div className={`rounded-xl border px-4 py-2 text-sm ${s.overdue
          ? "border-red-500/40 bg-red-500/10 text-red-300"
          : debt || s.pending_approval
            ? "border-amber-500/40 bg-amber-500/10 text-amber-300"
            : "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"}`}>
          {s.overdue
            ? `${s.overdue} review${s.overdue === 1 ? "" : "s"} overdue.`
            : debt || s.pending_approval
              ? `${s.due_soon} due soon · ${s.pending_approval} unapproved.`
              : "Document system healthy — nothing overdue or unapproved."}
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat label="Documents" value={s.total} />
        <Stat label="Overdue for review" value={s.overdue} bad={s.overdue > 0} />
        <Stat label="Due soon" value={s.due_soon} warn={s.due_soon > 0} />
        <Stat label="Unapproved" value={s.pending_approval} warn={s.pending_approval > 0} />
      </div>

      {s.documents.length > 0 && (
        <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
          <h4 className="text-sm font-semibold text-slate-300 mb-3">Review next</h4>
          <ul className="space-y-2">
            {s.documents.slice(0, 6).map((d) => (
              <li key={d.document_no} className="flex items-center justify-between gap-3 text-sm">
                <span className="min-w-0 truncate">
                  <span className="font-semibold">{d.document_no}</span>
                  <span className="text-slate-400"> · {d.title} · {d.department}</span>
                </span>
                <span className={`whitespace-nowrap text-xs ${d.overdue ? "text-red-400" : "text-slate-400"}`}>
                  {d.review_due_date || "no due date"}{d.overdue ? " · OVERDUE" : ""}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function Stat({ label, value, bad, warn }: { label: string; value: number | string; bad?: boolean; warn?: boolean }) {
  return (
    <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
      <p className="text-slate-400 text-xs">{label}</p>
      <h4 className={`text-2xl font-bold mt-1 ${bad ? "text-red-400" : warn ? "text-amber-400" : ""}`}>{value}</h4>
    </div>
  );
}
