"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend part drill-down (ai/coverage.py build_part_runway).
type RunwayState = "out" | "critical" | "watch" | "ok";
type PoState = "received" | "on_track" | "at_risk" | "late";
type Verdict = "covered" | "late_cover" | "no_inbound" | "not_at_risk";

type InboundPo = {
  po_no: string; supplier: string;
  order_quantity: number; received_quantity: number; outstanding: number; unit: string;
  expected_delivery_date: string | null; days_to_due: number | null;
  state: PoState; arrives_before_stockout: boolean | null;
};

type Movement = {
  id: number; transaction_type: string; quantity: number;
  direction: "in" | "out" | "adjust"; reference: string | null; at: string | null;
};

type PartRunway = {
  found: boolean;
  item_code: string; item_name: string;
  category: string | null; supplier: string | null; unit: string; location: string | null;
  current_stock: number; reorder_level: number;
  window_days: number; critical_days: number;
  daily_burn: number; days_of_cover: number | null; stockout_date: string | null;
  state: RunwayState;
  consumed_units: number; received_units: number;
  daily: { date: string; out: number; in: number }[];
  inbound: InboundPo[]; inbound_units: number; next_arrival: string | null;
  cover_verdict: Verdict; days_uncovered: number | null;
  recent: Movement[];
};

const stateCls: Record<RunwayState, string> = {
  out: "text-red-400",
  critical: "text-orange-400",
  watch: "text-yellow-400",
  ok: "text-emerald-400",
};

const PO_STATE_LABEL: Record<PoState, string> = {
  received: "received", on_track: "on track", at_risk: "at risk", late: "late",
};
const PO_STATE_CLS: Record<PoState, string> = {
  received: "text-emerald-400", on_track: "text-slate-400",
  at_risk: "text-amber-400", late: "text-red-400",
};

function fmtDate(iso: string | null) {
  if (!iso) return null;
  return new Date(iso + "T00:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function coverLabel(p: PartRunway) {
  if (p.state === "out") return "out of stock";
  if (p.days_of_cover == null) return "no recent burn";
  const d = p.days_of_cover;
  return `${d % 1 === 0 ? d : d.toFixed(1)} day${d === 1 ? "" : "s"} of cover`;
}

// The verdict is the whole point of the drawer: does what's on order land before
// we run dry? Each one gets its own colour and a plain-English line.
function verdictBanner(p: PartRunway) {
  switch (p.cover_verdict) {
    case "not_at_risk":
      return {
        cls: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
        title: "Not at risk",
        body: p.days_of_cover == null
          ? `Nothing has consumed ${p.item_name} in the last ${p.window_days} days.`
          : `At the current burn there are ${coverLabel(p)} left.`,
      };
    case "covered":
      return {
        cls: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
        title: "Covered by inbound",
        body: `${p.inbound_units} ${p.unit} on order${p.next_arrival ? `, first landing ${fmtDate(p.next_arrival)}` : ""}${p.stockout_date ? ` — before the ${fmtDate(p.stockout_date)} stockout` : ""}.`,
      };
    case "late_cover":
      return {
        cls: "border-orange-500/40 bg-orange-500/10 text-orange-300",
        title: `Inbound arrives ${p.days_uncovered}d too late`,
        body: `Runs dry ${fmtDate(p.stockout_date)}; the next PO lands ${fmtDate(p.next_arrival)}. Expedite it or pull stock forward.`,
      };
    default:
      return {
        cls: "border-red-500/40 bg-red-500/10 text-red-300",
        title: "Nothing on order",
        body: `Runs dry ${fmtDate(p.stockout_date) ?? "soon"} with no open purchase order for this part.`,
      };
  }
}

const dueLabel = (o: InboundPo) =>
  o.days_to_due == null ? "no due date"
    : o.days_to_due < 0 ? `${Math.abs(o.days_to_due)}d overdue`
    : o.days_to_due === 0 ? "due today"
    : `due in ${o.days_to_due}d`;

// A right-hand drawer that drills into one stocked part: the burn rate and days
// of cover behind its Runway row, the daily in/out movement, the open POs and
// whether they land in time, and its recent stock movements. Self-fetches from
// /inventory-part; closes on Escape.
export default function PartRunwayDrawer({ itemCode, onClose }: { itemCode: string; onClose: () => void }) {
  const [detail, setDetail] = useState<PartRunway | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setDetail(await apiGet<PartRunway>(`/inventory-part?item_code=${encodeURIComponent(itemCode)}`));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load part detail");
    } finally {
      setLoading(false);
    }
  }, [itemCode]);

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

  // Scale the movement bars against the busiest day either way.
  const peak = detail
    ? Math.max(1, ...detail.daily.map((d) => Math.max(d.out, d.in)))
    : 1;
  const banner = detail && detail.found ? verdictBanner(detail) : null;

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
            <h2 className="text-2xl font-bold">{detail?.item_name ?? itemCode}</h2>
            <p className="text-slate-500 text-sm mt-1">
              {itemCode}
              {detail?.category ? ` · ${detail.category}` : ""}
              {detail?.supplier ? ` · ${detail.supplier}` : ""}
              {detail?.location ? ` · ${detail.location}` : ""}
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
          <p className="text-slate-400 mt-6">Loading part detail…</p>
        ) : detail ? (
          !detail.found ? (
            <p className="text-slate-500 text-sm mt-6">No item {itemCode} in the item master.</p>
          ) : (
            <div className="mt-5 space-y-6">
              {/* The verdict: does what's on order land before we run dry? */}
              {banner && (
                <div className={`rounded-xl border p-4 ${banner.cls}`}>
                  <p className="text-sm font-semibold">{banner.title}</p>
                  <p className="text-xs mt-1 opacity-90">{banner.body}</p>
                </div>
              )}

              {/* Headline runway numbers */}
              <div className="grid grid-cols-3 gap-3">
                <div className="rounded-2xl bg-slate-900 border border-slate-800 p-4">
                  <p className={`text-2xl font-bold ${stateCls[detail.state]}`}>{detail.current_stock}</p>
                  <p className="text-xs text-slate-500 mt-1">{detail.unit} in stock</p>
                </div>
                <div className="rounded-2xl bg-slate-900 border border-slate-800 p-4">
                  <p className="text-2xl font-bold text-slate-200">{detail.daily_burn}</p>
                  <p className="text-xs text-slate-500 mt-1">{detail.unit}/day burn</p>
                </div>
                <div className="rounded-2xl bg-slate-900 border border-slate-800 p-4">
                  <p className={`text-2xl font-bold ${stateCls[detail.state]}`}>
                    {detail.days_of_cover ?? "—"}
                  </p>
                  <p className="text-xs text-slate-500 mt-1">
                    days of cover
                    {detail.stockout_date ? ` · dry ${fmtDate(detail.stockout_date)}` : ""}
                  </p>
                </div>
              </div>

              {/* Daily movement over the burn window */}
              <div>
                <p className="text-xs text-slate-500 mb-1.5">
                  Movement · last {detail.window_days} days ({detail.consumed_units} out, {detail.received_units} in)
                </p>
                <div className="flex items-end gap-1 h-14">
                  {detail.daily.map((d) => (
                    <div key={d.date} className="flex-1 flex flex-col justify-end gap-px" title={`${d.date}: ${d.out} out, ${d.in} in`}>
                      <div className="rounded-sm bg-emerald-500/50" style={{ height: `${Math.round((d.in / peak) * 45)}%` }} />
                      <div className="rounded-sm bg-orange-500/70" style={{ height: `${Math.round((d.out / peak) * 45)}%` }} />
                    </div>
                  ))}
                </div>
              </div>

              {/* Open POs on this part */}
              <div>
                <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">
                  On order{detail.inbound_units > 0 ? ` · ${detail.inbound_units} ${detail.unit}` : ""}
                </h3>
                {detail.inbound.length === 0 ? (
                  <p className="text-slate-500 text-sm mt-3">No open purchase orders for this part.</p>
                ) : (
                  <div className="mt-3 space-y-2">
                    {detail.inbound.map((o) => (
                      <div
                        key={o.po_no}
                        className={`flex items-start gap-3 rounded-lg border border-slate-800 border-l-2 ${o.arrives_before_stockout === false ? "border-l-orange-500/70" : "border-l-emerald-500/60"} bg-slate-900/40 px-3 py-2`}
                      >
                        <div className="min-w-0 flex-1">
                          <p className="text-sm text-slate-200 truncate">
                            {o.po_no} <span className="text-slate-500">· {o.supplier}</span>
                          </p>
                          <p className="text-[11px] text-slate-500 truncate">
                            {o.outstanding} {o.unit} outstanding · {o.received_quantity}/{o.order_quantity} in
                          </p>
                        </div>
                        <div className="shrink-0 text-right">
                          <p className={`text-[11px] ${PO_STATE_CLS[o.state]}`}>{PO_STATE_LABEL[o.state]}</p>
                          <p className="text-[11px] text-slate-600">{dueLabel(o)}</p>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* Recent stock movements */}
              <div>
                <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Recent movements</h3>
                {detail.recent.length === 0 ? (
                  <p className="text-slate-500 text-sm mt-3">
                    No stock movement in the last {detail.window_days} days.
                  </p>
                ) : (
                  <ol className="mt-3 space-y-3">
                    {detail.recent.map((m) => (
                      <li key={m.id} className="border-b border-slate-800/70 pb-3 flex items-center justify-between gap-2">
                        <div className="min-w-0">
                          <p className="text-sm font-medium truncate">
                            <span className={m.direction === "out" ? "text-orange-400" : m.direction === "in" ? "text-emerald-400" : "text-slate-400"}>
                              {m.direction === "out" ? "−" : m.direction === "in" ? "+" : "±"}{m.quantity} {detail.unit}
                            </span>
                            <span className="text-slate-500"> · {m.transaction_type}</span>
                          </p>
                          {m.reference && <p className="text-xs text-slate-600 mt-0.5 truncate">{m.reference}</p>}
                        </div>
                        <span className="text-xs text-slate-600 shrink-0">
                          {m.at ? new Date(m.at).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : ""}
                        </span>
                      </li>
                    ))}
                  </ol>
                )}
              </div>
            </div>
          )
        ) : null}
      </div>
    </div>
  );
}
