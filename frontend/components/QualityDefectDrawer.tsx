"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend defect drill-down (ai/quality.py build_defect_detail).
type Inspection = {
  id: number;
  inspection_no: string;
  machine_id: number | null;
  machine: string;
  inspector: string;
  inspected: number;
  failed: number;
  at: string | null;
};

type DefectDetail = {
  category: string;
  inspections: number;
  failed: number;
  rework: number;
  scrap: number;
  by_machine: { machine_id: number; name: string; failed: number; inspections: number }[];
  recent: Inspection[];
};

function fmt(iso: string | null) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

export default function QualityDefectDrawer({ category, onClose }: { category: string; onClose: () => void }) {
  const [detail, setDetail] = useState<DefectDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setDetail(await apiGet<DefectDetail>(`/quality-defect?category=${encodeURIComponent(category)}`));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load defect detail");
    } finally {
      setLoading(false);
    }
  }, [category]);

  useEffect(() => {
    load();
  }, [load]);

  // Close on Escape for keyboard users.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} aria-hidden="true" />
      <div
        role="dialog"
        aria-modal="true"
        className="relative w-full max-w-xl bg-slate-950 border-l border-slate-800 h-full overflow-y-auto p-6"
      >
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-2xl font-bold">Defect — {category}</h2>
            <p className="text-slate-500 text-sm mt-1">Where this defect is coming from</p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-white text-xl px-2" aria-label="Close">
            ✕
          </button>
        </div>

        {error && (
          <div className="mt-4 rounded-xl border border-red-500/40 bg-red-500/10 text-red-300 p-3 text-sm">{error}</div>
        )}

        {loading && !detail ? (
          <p className="text-slate-400 mt-6">Loading defect detail…</p>
        ) : detail ? (
          <div className="mt-5 space-y-6">
            {/* Totals */}
            <div className="grid grid-cols-3 gap-3">
              <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
                <p className="text-3xl font-bold text-orange-400">{detail.failed}</p>
                <p className="text-xs text-slate-500 mt-1">units failed</p>
              </div>
              <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
                <p className="text-3xl font-bold text-yellow-300">{detail.rework}</p>
                <p className="text-xs text-slate-500 mt-1">rework</p>
              </div>
              <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
                <p className="text-3xl font-bold text-red-400">{detail.scrap}</p>
                <p className="text-xs text-slate-500 mt-1">scrap</p>
              </div>
            </div>

            {detail.inspections === 0 ? (
              <p className="text-slate-500 text-sm">No “{category}” failures recorded.</p>
            ) : (
              <>
                {/* Machines producing it */}
                <div>
                  <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Machines producing it</h3>
                  <div className="mt-3 space-y-2">
                    {detail.by_machine.map((m) => (
                      <div
                        key={m.machine_id}
                        className="flex items-center justify-between rounded-lg border border-slate-800 bg-slate-900 px-3 py-2 text-sm"
                      >
                        <span className="text-slate-300">{m.name}</span>
                        <span className="text-slate-500">
                          {m.failed} failed · {m.inspections} inspection{m.inspections !== 1 ? "s" : ""}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>

                {/* Recent inspections */}
                <div>
                  <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">
                    Recent inspections · {detail.inspections}
                  </h3>
                  <ol className="mt-3 space-y-3">
                    {detail.recent.map((i) => (
                      <li key={i.id} className="border-b border-slate-800/70 pb-3">
                        <div className="flex items-center justify-between gap-2">
                          <p className="text-sm font-medium">
                            {i.inspection_no} <span className="text-slate-500">· {i.machine}</span>
                          </p>
                          <span className="text-xs text-orange-300">
                            {i.failed}/{i.inspected} failed
                          </span>
                        </div>
                        <p className="text-xs text-slate-600 mt-0.5">
                          {i.inspector} · {fmt(i.at)}
                        </p>
                      </li>
                    ))}
                  </ol>
                </div>
              </>
            )}
          </div>
        ) : null}
      </div>
    </div>
  );
}
