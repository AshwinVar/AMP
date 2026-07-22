"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend supplier drill-down (ai/supply.py build_supplier_detail).
type State = "received" | "on_track" | "at_risk" | "late";

type ChasePo = {
  po_no: string; item_name: string; expected_delivery_date: string | null;
  order_quantity: number; received_quantity: number; unit: string;
  state: "at_risk" | "late"; days_to_due: number | null;
};

type RecentPo = {
  po_no: string; item_name: string;
  order_quantity: number; received_quantity: number; unit: string;
  expected_delivery_date: string | null; state: State;
};

type SupplierDetail = {
  supplier: string;
  category: string | null;
  supplier_status: string | null;
  total: number;
  received: number; on_track: number; at_risk: number; late: number;
  receipt_rate: number;
  reliability_rate: number;
  ordered_units: number; received_units: number; overdue_units: number;
  chase: ChasePo[];
  upcoming: { date: string; pos: number }[];
  recent: RecentPo[];
};

function rateColor(pct: number) {
  if (pct >= 95) return "text-emerald-400";
  if (pct >= 80) return "text-yellow-400";
  if (pct >= 60) return "text-orange-400";
  return "text-red-400";
}

const STATE_LABEL: Record<State, string> = {
  received: "received",
  on_track: "on track",
  at_risk: "at risk",
  late: "late",
};
const STATE_CLS: Record<State, string> = {
  received: "text-emerald-400",
  on_track: "text-slate-400",
  at_risk: "text-amber-400",
  late: "text-red-400",
};

const dueLabel = (o: ChasePo) =>
  o.days_to_due == null ? "no due date"
    : o.state === "late" ? `${Math.abs(o.days_to_due)}d overdue`
    : o.days_to_due === 0 ? "due today"
    : `due in ${o.days_to_due}d`;

// A right-hand drawer that drills into one supplier: its unit receipt rate and
// delivery reliability, the PO state mix, an inbound timeline, the POs to chase,
// and its recent POs. Self-fetches from /supply-supplier; closes on Escape.
export default function SupplierDrawer({ supplier, onClose }: { supplier: string; onClose: () => void }) {
  const [detail, setDetail] = useState<SupplierDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setDetail(await apiGet<SupplierDetail>(`/supply-supplier?supplier=${encodeURIComponent(supplier)}`));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load supplier detail");
    } finally {
      setLoading(false);
    }
  }, [supplier]);

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

  const upcomingPeak = detail ? Math.max(...detail.upcoming.map((u) => u.pos), 1) : 1;

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
            <h2 className="text-2xl font-bold">{supplier}</h2>
            <p className="text-slate-500 text-sm mt-1">
              Inbound reliability
              {detail?.category ? ` · ${detail.category}` : ""}
              {detail?.supplier_status ? ` · ${detail.supplier_status}` : ""}
            </p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-white text-xl px-2" aria-label="Close">
            ✕
          </button>
        </div>

        {error && (
          <div className="mt-4 rounded-xl border border-red-500/40 bg-red-500/10 text-red-300 p-3 text-sm">{error}</div>
        )}

        {loading && !detail ? (
          <p className="text-slate-400 mt-6">Loading supplier detail…</p>
        ) : detail ? (
          detail.total === 0 ? (
            <p className="text-slate-500 text-sm mt-6">No purchase orders on record for {supplier}.</p>
          ) : (
            <div className="mt-5 space-y-6">
              {/* Headline rates */}
              <div className="grid grid-cols-2 gap-3">
                <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
                  <p className={`text-3xl font-bold ${rateColor(detail.receipt_rate)}`}>{detail.receipt_rate}%</p>
                  <p className="text-xs text-slate-500 mt-1">
                    units received · {detail.received_units}/{detail.ordered_units}
                  </p>
                </div>
                <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
                  <p className={`text-3xl font-bold ${rateColor(detail.reliability_rate)}`}>{detail.reliability_rate}%</p>
                  <p className="text-xs text-slate-500 mt-1">
                    received · of POs already due
                  </p>
                </div>
              </div>

              {/* State mix */}
              <div className="grid grid-cols-4 gap-2">
                {(["received", "on_track", "at_risk", "late"] as State[]).map((k) => (
                  <div key={k} className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
                    <p className={`text-xl font-bold ${STATE_CLS[k]}`}>{detail[k]}</p>
                    <p className="text-[11px] text-slate-500 capitalize">{STATE_LABEL[k]}</p>
                  </div>
                ))}
              </div>

              {detail.overdue_units > 0 && (
                <p className="text-sm text-red-300">
                  {detail.overdue_units} unit{detail.overdue_units !== 1 ? "s" : ""} still owed on overdue POs.
                </p>
              )}

              {/* Inbound timeline */}
              {detail.upcoming.some((u) => u.pos > 0) && (
                <div>
                  <p className="text-xs text-slate-500 mb-1.5">Expected next 7 days</p>
                  <div className="flex items-end gap-1 h-10">
                    {detail.upcoming.map((u) => (
                      <div
                        key={u.date}
                        className="flex-1 rounded-sm bg-sky-500/60"
                        style={{ height: `${Math.max(4, Math.round((u.pos / upcomingPeak) * 100))}%` }}
                        title={`${u.date}: ${u.pos} due`}
                      />
                    ))}
                  </div>
                </div>
              )}

              {/* POs to chase */}
              <div>
                <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">POs to chase</h3>
                {detail.chase.length === 0 ? (
                  <p className="text-emerald-400 text-sm mt-3">Nothing at risk — this supplier is on track.</p>
                ) : (
                  <div className="mt-3 space-y-2">
                    {detail.chase.map((o) => (
                      <div
                        key={o.po_no}
                        className={`flex items-start gap-3 rounded-lg border border-slate-800 border-l-2 ${o.state === "late" ? "border-l-red-500/70" : "border-l-amber-400/70"} bg-slate-900/40 px-3 py-2`}
                      >
                        <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${o.state === "late" ? "bg-red-500" : "bg-amber-400"}`} />
                        <div className="min-w-0 flex-1">
                          <p className="text-sm text-slate-200 truncate">{o.po_no}</p>
                          <p className="text-[11px] text-slate-500 truncate">
                            {o.item_name} · {o.received_quantity}/{o.order_quantity} {o.unit} in
                          </p>
                        </div>
                        <span className={`shrink-0 text-[11px] ${o.state === "late" ? "text-red-400" : "text-amber-400"}`}>
                          {dueLabel(o)}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* Recent POs */}
              <div>
                <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">
                  Recent POs · {detail.total}
                </h3>
                <ol className="mt-3 space-y-3">
                  {detail.recent.map((p) => (
                    <li key={p.po_no} className="border-b border-slate-800/70 pb-3">
                      <div className="flex items-center justify-between gap-2">
                        <p className="text-sm font-medium truncate">
                          {p.po_no} <span className="text-slate-500">· {p.item_name}</span>
                        </p>
                        <span className={`text-xs shrink-0 ${STATE_CLS[p.state]}`}>{STATE_LABEL[p.state]}</span>
                      </div>
                      <p className="text-xs text-slate-600 mt-0.5">
                        {p.received_quantity}/{p.order_quantity} {p.unit} in
                        {p.expected_delivery_date ? ` · due ${p.expected_delivery_date}` : ""}
                      </p>
                    </li>
                  ))}
                </ol>
              </div>
            </div>
          )
        ) : null}
      </div>
    </div>
  );
}
