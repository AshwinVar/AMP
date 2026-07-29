"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import { parseApiDate } from "../lib/apiDate";

type AtRiskOrder = {
  order_no: string;
  customer: string | null;
  product: string | null;
  due_date: string | null;
  order_quantity: number;
  dispatched_quantity: number;
  state: string;
  days_to_due: number | null;
};

// Mirrors ai.delivery.build_delivery_summary (GET /delivery-summary).
type DeliverySummary = {
  total: number;
  delivered: number;
  on_track: number;
  at_risk: number;
  late: number;
  on_track_rate: number | null;
  resolved: number;
  reliability_rate: number | null;
  fulfillment_rate: number | null;
  units_ordered: number;
  units_dispatched: number;
  units_remaining: number;
  units_at_risk: number;
  by_customer: { customer: string; orders: number; delivered: number; on_track: number; at_risk: number; late: number }[];
  at_risk_orders: AtRiskOrder[];
  upcoming: { date: string; orders: number }[];
};

function dayLabel(iso: string) {
  const d = parseApiDate(iso);
  return d ? d.toLocaleDateString(undefined, { weekday: "narrow" }) : "";
}

// The Orders & Dispatch view's intelligence layer over the order CRUD: are we
// keeping delivery promises? Headline rates, the orders in jeopardy (late
// first), and the due load for the next 7 days. Self-contained; hides on error
// so the CRUD below always renders.
export default function DeliveryIntelCard() {
  const [s, setS] = useState<DeliverySummary | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setS(await apiGet<DeliverySummary>("/delivery-summary"));
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
        Loading delivery intelligence…
      </div>
    );
  }
  if (!s) return null;

  const peakDue = s.upcoming.reduce((m, d) => Math.max(m, d.orders), 0);
  const jeopardy = s.late + s.at_risk;

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4">
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-2">
        <h3 className="text-lg font-semibold">Delivery — the order book</h3>
        <div className={`rounded-xl border px-4 py-2 text-sm ${jeopardy
          ? "border-red-500/40 bg-red-500/10 text-red-300"
          : "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"}`}>
          {jeopardy
            ? `${jeopardy} order${jeopardy === 1 ? "" : "s"} in jeopardy (${s.late} late, ${s.at_risk} at risk) · ${s.units_at_risk.toLocaleString()} units`
            : "Every order delivered or on track."}
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat label="Orders" value={s.total} sub={`${s.delivered} delivered`} />
        <Stat
          label="On-track rate"
          value={s.on_track_rate === null ? "—" : `${s.on_track_rate}%`}
          bad={s.on_track_rate !== null && s.on_track_rate < 90}
        />
        <Stat
          label="Delivery reliability"
          value={s.reliability_rate === null ? "—" : `${s.reliability_rate}%`}
          sub={`${s.resolved} due so far`}
        />
        <Stat
          label="Unit fulfillment"
          value={s.fulfillment_rate === null ? "—" : `${s.fulfillment_rate}%`}
          sub={`${s.units_remaining.toLocaleString()} units to go`}
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Orders in jeopardy */}
        <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
          <h4 className="text-sm font-semibold text-slate-300 mb-3">Chase first (late, then closest due)</h4>
          {s.at_risk_orders.length === 0 ? (
            <p className="text-slate-500 text-sm">Nothing late or at risk.</p>
          ) : (
            <ul className="space-y-2">
              {s.at_risk_orders.slice(0, 6).map((o) => (
                <li key={o.order_no} className="flex items-center justify-between gap-3 text-sm">
                  <span className="min-w-0 truncate">
                    <span className="font-semibold">{o.order_no}</span>
                    <span className="text-slate-400"> · {o.customer || "—"} · {o.product || "—"}</span>
                  </span>
                  <span className={`whitespace-nowrap text-xs ${o.state === "late" ? "text-red-400" : "text-amber-400"}`}>
                    {o.dispatched_quantity}/{o.order_quantity} ·{" "}
                    {o.days_to_due === null ? "no due date"
                      : o.days_to_due < 0 ? `${-o.days_to_due}d late`
                      : `due in ${o.days_to_due}d`}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Due load next 7 days */}
        <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
          <h4 className="text-sm font-semibold text-slate-300 mb-3">Due load — next 7 days</h4>
          {peakDue === 0 ? (
            <p className="text-slate-500 text-sm">No undelivered orders due this week.</p>
          ) : (
            <div className="flex items-end gap-1 h-24">
              {s.upcoming.map((d, i) => {
                const h = peakDue ? Math.max(3, Math.round((d.orders / peakDue) * 84)) : 3;
                return (
                  <div
                    key={d.date}
                    className="flex-1 flex flex-col items-center justify-end gap-1"
                    title={`${d.orders} order${d.orders === 1 ? "" : "s"} due on ${d.date}`}
                  >
                    <span className="text-[10px] text-slate-400">{d.orders || ""}</span>
                    <div className="w-full rounded-t bg-sky-500/50" style={{ height: `${h}px` }} />
                    <span className={`text-[10px] ${i === 0 ? "text-white font-semibold" : "text-slate-500"}`}>
                      {dayLabel(d.date)}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
          {s.by_customer.length > 0 && (
            <p className="text-xs text-slate-500 mt-3">
              Customers: {s.by_customer.slice(0, 4).map((c) =>
                `${c.customer} (${c.orders}${c.late ? `, ${c.late} late` : ""})`
              ).join(" · ")}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}

function Stat({ label, value, sub, bad }: { label: string; value: number | string; sub?: string; bad?: boolean }) {
  return (
    <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
      <p className="text-slate-400 text-xs">{label}</p>
      <h4 className={`text-2xl font-bold mt-1 ${bad ? "text-red-400" : ""}`}>{value}</h4>
      {sub && <p className="text-[11px] text-slate-500 mt-0.5">{sub}</p>}
    </div>
  );
}
