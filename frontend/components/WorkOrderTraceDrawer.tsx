"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend work-order traceability read-model (ai/trace.py build_work_order_trace).
type PlanRow = {
  plan_no: string; plan_date: string | null; shift_name: string | null;
  machine: string; planned: number; actual: number; shortfall: number; status: string | null;
};
type InspectionRow = {
  inspection_no: string; machine: string; inspector: string;
  inspected: number; passed: number; failed: number; rework: number; scrap: number;
  defect_category: string | null; status: string | null; at: string | null;
};
type MaterialRow = {
  item_code: string; item_name: string; category: string | null; unit: string | null;
  supplier: string | null; consumed: number; received: number; movements: number;
  last_at: string | null;
};
type DowntimeRow = {
  reason: string; duration: string; minutes: number; notes: string | null; at: string | null;
};
type TimelineEntry = {
  kind: "plan" | "inspection" | "material" | "downtime";
  at: string | null; label: string; detail: string;
};
type Gap = { severity: "high" | "medium"; message: string };

type WorkOrderTrace = {
  found: boolean;
  work_order_no: string;
  part_number: string | null;
  batch_number: string | null;
  machine_id?: number;
  machine: string | null;
  line?: string;
  status: string | null;
  closed?: boolean;
  material_state?: string | null;
  target?: number;
  actual?: number;
  shortfall?: number;
  progress_rate?: number;
  created_at?: string | null;
  planned_start?: string | null;
  planned_end?: string | null;
  days_late?: number;
  shifts?: string[];
  plans: { count: number; planned: number; actual: number; attainment_rate: number; rows: PlanRow[] };
  quality: {
    inspections: number; inspected: number; passed: number; failed: number;
    rework: number; scrap: number; first_pass_yield: number; fail_rate: number;
    defects: { category: string; count: number }[]; rows: InspectionRow[];
  };
  materials: { consumed: number; received: number; rows: MaterialRow[] };
  downtime: { events: number; minutes: number; rows: DowntimeRow[] };
  timeline: TimelineEntry[];
  gaps: Gap[];
};

const KIND: Record<TimelineEntry["kind"], { label: string; dot: string }> = {
  plan: { label: "Planned", dot: "bg-sky-400" },
  material: { label: "Material", dot: "bg-violet-400" },
  inspection: { label: "Quality", dot: "bg-emerald-400" },
  downtime: { label: "Stoppage", dot: "bg-orange-400" },
};

function yieldColor(pct: number) {
  if (pct >= 98) return "text-emerald-400";
  if (pct >= 95) return "text-yellow-400";
  return "text-red-400";
}

const minutes = (m: number) => (m < 60 ? `${m}m` : `${(m / 60).toFixed(1)}h`);

// A right-hand drawer that drills into one work order's genealogy: what was
// planned and made, the shifts and machine it ran on, the materials issued
// against it and the goods received from it, what quality found, the downtime
// while it was live, a merged timeline, and — the traceability question — where
// the record is silent. Self-fetches from /work-order-trace; closes on Escape.
export default function WorkOrderTraceDrawer({
  workOrderNo,
  onClose,
}: {
  workOrderNo: string;
  onClose: () => void;
}) {
  const [trace, setTrace] = useState<WorkOrderTrace | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setTrace(
        await apiGet<WorkOrderTrace>(`/work-order-trace?work_order_no=${encodeURIComponent(workOrderNo)}`)
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load work-order trace");
    } finally {
      setLoading(false);
    }
  }, [workOrderNo]);

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
            <h2 className="text-2xl font-bold">{workOrderNo}</h2>
            <p className="text-slate-500 text-sm mt-1">
              Traceability
              {trace?.part_number ? ` · ${trace.part_number}` : ""}
              {trace?.batch_number ? ` · batch ${trace.batch_number}` : ""}
              {trace?.machine ? ` · ${trace.machine}` : ""}
            </p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-white text-xl px-2" aria-label="Close">
            ✕
          </button>
        </div>

        {error && (
          <div className="mt-4 rounded-xl border border-red-500/40 bg-red-500/10 text-red-300 p-3 text-sm">{error}</div>
        )}

        {loading && !trace ? (
          <p className="text-slate-400 mt-6">Loading trace…</p>
        ) : trace ? (
          !trace.found ? (
            <p className="text-slate-500 text-sm mt-6">No work order on record for {workOrderNo}.</p>
          ) : (
            <div className="mt-5 space-y-6">
              {/* Headline: how far the job got, and how clean it came out */}
              <div className="grid grid-cols-2 gap-3">
                <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
                  <p className="text-3xl font-bold text-slate-100">
                    {trace.actual ?? 0}
                    <span className="text-base text-slate-500 font-normal">/{trace.target ?? 0}</span>
                  </p>
                  <p className="text-xs text-slate-500 mt-1">
                    {trace.progress_rate ?? 0}% made · {trace.status}
                    {trace.days_late ? ` · ${trace.days_late}d late` : ""}
                  </p>
                </div>
                <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
                  <p className={`text-3xl font-bold ${yieldColor(trace.quality.first_pass_yield)}`}>
                    {trace.quality.inspections ? `${trace.quality.first_pass_yield}%` : "—"}
                  </p>
                  <p className="text-xs text-slate-500 mt-1">
                    {trace.quality.inspections
                      ? `first-pass yield · ${trace.quality.inspected} inspected`
                      : "never inspected"}
                  </p>
                </div>
              </div>

              {/* The four record sources, as counts */}
              <div className="grid grid-cols-4 gap-2">
                <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
                  <p className="text-xl font-bold text-sky-300">{trace.plans.count}</p>
                  <p className="text-[11px] text-slate-500">plans</p>
                </div>
                <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
                  <p className="text-xl font-bold text-violet-300">{trace.materials.consumed}</p>
                  <p className="text-[11px] text-slate-500">units issued</p>
                </div>
                <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
                  <p className={`text-xl font-bold ${trace.quality.scrap ? "text-red-300" : "text-slate-200"}`}>
                    {trace.quality.scrap}
                  </p>
                  <p className="text-[11px] text-slate-500">scrapped</p>
                </div>
                <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
                  <p className={`text-xl font-bold ${trace.downtime.minutes ? "text-orange-300" : "text-slate-200"}`}>
                    {minutes(trace.downtime.minutes)}
                  </p>
                  <p className="text-[11px] text-slate-500">stopped</p>
                </div>
              </div>

              {/* Where the record is silent */}
              <div>
                <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">
                  What the record doesn&apos;t show
                </h3>
                {trace.gaps.length === 0 ? (
                  <p className="text-emerald-400 text-sm mt-3">
                    This job is fully traced — plans, materials, inspections and stoppages are all on record.
                  </p>
                ) : (
                  <div className="mt-3 space-y-2">
                    {trace.gaps.map((g, i) => (
                      <div
                        key={i}
                        className={`rounded-lg border border-slate-800 border-l-2 ${g.severity === "high" ? "border-l-red-500/70" : "border-l-amber-400/70"} bg-slate-900/40 px-3 py-2`}
                      >
                        <p className={`text-sm ${g.severity === "high" ? "text-slate-200" : "text-slate-300"}`}>
                          {g.message}
                        </p>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* Material genealogy — what it was built from */}
              <div>
                <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">
                  Material genealogy
                </h3>
                {trace.materials.rows.length === 0 ? (
                  <p className="text-slate-500 text-sm mt-3">
                    No stock movement references this work order — there is no record of what it consumed.
                  </p>
                ) : (
                  <div className="mt-3 space-y-2">
                    {trace.materials.rows.map((m) => (
                      <div
                        key={m.item_code}
                        className="flex items-center justify-between rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-sm"
                      >
                        <div className="min-w-0 flex-1">
                          <p className="text-slate-200 font-medium truncate">
                            {m.item_code} <span className="text-slate-500 font-normal">· {m.item_name}</span>
                          </p>
                          <p className="text-[11px] text-slate-500 truncate">
                            {m.movements} movement{m.movements !== 1 ? "s" : ""}
                            {m.category ? ` · ${m.category}` : ""}
                            {m.supplier ? ` · ${m.supplier}` : ""}
                          </p>
                        </div>
                        <span className="tabular-nums shrink-0 text-[11px]">
                          {m.consumed > 0 && (
                            <span className="text-violet-300">
                              −{m.consumed}
                              {m.unit ? ` ${m.unit}` : ""}
                            </span>
                          )}
                          {m.consumed > 0 && m.received > 0 && <span className="text-slate-600"> · </span>}
                          {m.received > 0 && (
                            <span className="text-emerald-300">
                              +{m.received}
                              {m.unit ? ` ${m.unit}` : ""}
                            </span>
                          )}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* Scheduled: the plans it ran under */}
              {trace.plans.rows.length > 0 && (
                <div>
                  <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">
                    Scheduled · {trace.plans.attainment_rate}% attained
                  </h3>
                  <div className="mt-3 space-y-2">
                    {trace.plans.rows.map((p) => (
                      <div
                        key={p.plan_no}
                        className={`flex items-center justify-between rounded-lg border border-slate-800 ${p.shortfall ? "border-l-2 border-l-amber-400/70" : ""} px-3 py-2 text-sm`}
                      >
                        <div className="min-w-0 flex-1">
                          <p className="text-slate-200 font-medium truncate">
                            {p.plan_no} <span className="text-slate-500 font-normal">· {p.shift_name ?? "—"}</span>
                          </p>
                          <p className="text-[11px] text-slate-500">
                            {p.plan_date ?? "—"} · {p.machine}
                          </p>
                        </div>
                        <span className="tabular-nums shrink-0 text-[11px] text-slate-400">
                          {p.actual}/{p.planned}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Quality: what was found, and the defect Pareto */}
              {trace.quality.inspections > 0 && (
                <div>
                  <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">
                    Quality · {trace.quality.failed} failed of {trace.quality.inspected}
                  </h3>
                  {trace.quality.defects.length > 0 && (
                    <div className="mt-3 flex flex-wrap gap-2">
                      {trace.quality.defects.map((d) => (
                        <span
                          key={d.category}
                          className="rounded-full border border-red-500/30 bg-red-500/10 text-red-300 px-2.5 py-0.5 text-xs"
                        >
                          {d.category} · {d.count}
                        </span>
                      ))}
                    </div>
                  )}
                  <div className="mt-3 space-y-2">
                    {trace.quality.rows.map((i) => (
                      <div
                        key={i.inspection_no}
                        className="flex items-center justify-between rounded-lg border border-slate-800 px-3 py-2 text-sm"
                      >
                        <div className="min-w-0 flex-1">
                          <p className="text-slate-200 font-medium truncate">
                            {i.inspection_no}{" "}
                            <span className="text-slate-500 font-normal">· {i.defect_category ?? "no defect logged"}</span>
                          </p>
                          <p className="text-[11px] text-slate-500 truncate">
                            {i.machine} · {i.inspector}
                            {i.scrap ? ` · ${i.scrap} scrapped` : ""}
                            {i.rework ? ` · ${i.rework} reworked` : ""}
                          </p>
                        </div>
                        <span className="tabular-nums shrink-0 text-[11px] text-slate-400">
                          {i.passed}/{i.inspected}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Stoppages while the job was live */}
              {trace.downtime.rows.length > 0 && (
                <div>
                  <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">
                    Stopped while running · {minutes(trace.downtime.minutes)}
                  </h3>
                  <div className="mt-3 space-y-2">
                    {trace.downtime.rows.map((d, i) => (
                      <div
                        key={`${d.at}-${i}`}
                        className="flex items-center justify-between rounded-lg border border-slate-800 px-3 py-2 text-sm"
                      >
                        <div className="min-w-0 flex-1">
                          <p className="text-slate-200 font-medium truncate">{d.reason}</p>
                          <p className="text-[11px] text-slate-500 truncate">
                            {d.at ? new Date(d.at).toLocaleString() : "—"}
                            {d.notes ? ` · ${d.notes}` : ""}
                          </p>
                        </div>
                        <span className="tabular-nums shrink-0 text-[11px] text-orange-300">{minutes(d.minutes)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* The merged record */}
              {trace.timeline.length > 0 && (
                <div>
                  <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Job history</h3>
                  <ol className="mt-3 space-y-3">
                    {trace.timeline.map((e, i) => (
                      <li key={`${e.at}-${i}`} className="border-b border-slate-800/70 pb-3">
                        <div className="flex items-center gap-2">
                          <span className={`h-2 w-2 rounded-full shrink-0 ${KIND[e.kind].dot}`} />
                          <p className="text-sm font-medium truncate">{e.label}</p>
                        </div>
                        <p className="text-xs text-slate-600 mt-0.5 pl-4 truncate">
                          {KIND[e.kind].label}
                          {e.at ? ` · ${new Date(e.at).toLocaleString()}` : ""}
                          {e.detail ? ` · ${e.detail}` : ""}
                        </p>
                      </li>
                    ))}
                  </ol>
                </div>
              )}
            </div>
          )
        ) : null}
      </div>
    </div>
  );
}
