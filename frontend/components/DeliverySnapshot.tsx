"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet, API_URL, getAuthHeaders } from "../lib/api";

// Mirrors the backend delivery read-model (ai/delivery.py build_delivery_summary).
type ByCustomer = {
  customer: string; orders: number;
  delivered: number; on_track: number; at_risk: number; late: number;
  ordered: number; dispatched: number; fulfillment_rate: number;
};
type ChaseOrder = {
  order_no: string; customer: string; product: string; due_date: string | null;
  order_quantity: number; dispatched_quantity: number; state: "at_risk" | "late"; days_to_due: number | null;
};
type DeliverySummary = {
  total: number;
  delivered: number; on_track: number; at_risk: number; late: number;
  on_track_rate: number;
  fulfillment_rate: number;
  units_ordered: number; units_dispatched: number; units_remaining: number;
  units_at_risk: number;
  by_customer: ByCustomer[];
  at_risk_orders: ChaseOrder[];
  upcoming: { date: string; orders: number }[];
};

function fulfillColor(pct: number) {
  if (pct >= 95) return "text-emerald-400";
  if (pct >= 80) return "text-yellow-400";
  if (pct >= 60) return "text-orange-400";
  return "text-red-400";
}

const dueLabel = (o: ChaseOrder) =>
  o.days_to_due == null ? "no due date"
    : o.state === "late" ? `${Math.abs(o.days_to_due)}d overdue`
    : o.days_to_due === 0 ? "due today"
    : `due in ${o.days_to_due}d`;

// A glanceable order-delivery outlook — unit fulfillment, the state mix, a
// per-customer split, and the orders to chase. Self-contained: fetches its own
// summary and refreshes, so it drops onto any screen. Renders nothing until
// there are orders.
export default function DeliverySnapshot({ onOpen }: { onOpen?: (viewKey: string) => void }) {
  const [d, setD] = useState<DeliverySummary | null>(null);

  const load = useCallback(async () => {
    try {
      setD(await apiGet<DeliverySummary>("/delivery-summary"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  if (!d || d.total === 0) return null;

  const upcomingPeak = Math.max(...d.upcoming.map((u) => u.orders), 1);

  const exportCsv = async () => {
    try {
      const res = await fetch(`${API_URL}/customer-orders/export`, { headers: getAuthHeaders() });
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "order-book.csv";
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      // best-effort export — stay quiet on error.
    }
  };

  const states: { key: keyof DeliverySummary; label: string; cls: string }[] = [
    { key: "delivered", label: "Delivered", cls: "text-emerald-400" },
    { key: "on_track", label: "On track", cls: "text-slate-300" },
    { key: "at_risk", label: "At risk", cls: "text-amber-400" },
    { key: "late", label: "Late", cls: "text-red-400" },
  ];

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Order delivery · outlook</h3>
          <p className="text-slate-400 text-sm mt-1">
            {d.total} order{d.total !== 1 ? "s" : ""} · {d.at_risk + d.late} need attention
            {d.units_at_risk > 0 ? ` · ${d.units_at_risk.toLocaleString()} units at risk` : ""}
          </p>
          <button
            type="button"
            onClick={exportCsv}
            className="mt-2 rounded-md border border-slate-700 px-2.5 py-1 text-xs text-slate-300 hover:border-slate-500 hover:bg-slate-800 transition focus:outline-none focus:ring-2 focus:ring-slate-600"
          >
            Export CSV
          </button>
        </div>
        <div className="text-right">
          <p className={`text-3xl font-bold ${fulfillColor(d.fulfillment_rate)}`}>{d.fulfillment_rate}%</p>
          <p className="text-[11px] text-slate-500">units fulfilled</p>
          <p className="text-[11px] text-slate-500 mt-1">
            <span className={fulfillColor(d.on_track_rate)}>{d.on_track_rate}%</span> on track
          </p>
        </div>
      </div>

      {/* state mix */}
      <div className="mt-4 grid grid-cols-4 gap-2">
        {states.map((s) => (
          <div key={s.key} className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
            <p className={`text-xl font-bold ${s.cls}`}>{d[s.key] as number}</p>
            <p className="text-[11px] text-slate-500">{s.label}</p>
          </div>
        ))}
      </div>

      {d.upcoming.some((u) => u.orders > 0) && (
        <div className="mt-4">
          <p className="text-xs text-slate-500 mb-1.5">Due next 7 days</p>
          <div className="flex items-end gap-1 h-10">
            {d.upcoming.map((u) => (
              <div
                key={u.date}
                className="flex-1 rounded-sm bg-sky-500/60"
                style={{ height: `${Math.max(4, Math.round((u.orders / upcomingPeak) * 100))}%` }}
                title={`${u.date}: ${u.orders} due`}
              />
            ))}
          </div>
        </div>
      )}

      <div className="mt-5 grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* by customer */}
        <div>
          <p className="text-xs text-slate-500 mb-2">By customer</p>
          <div className="space-y-2">
            {d.by_customer.map((c) => (
              <div key={c.customer} className="rounded-lg border border-slate-800 px-3 py-2">
                <div className="flex items-center justify-between text-sm">
                  <span className="text-slate-200 font-medium">{c.customer}</span>
                  <span className={`tabular-nums ${fulfillColor(c.fulfillment_rate)}`}>{c.fulfillment_rate}%</span>
                </div>
                <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-slate-500">
                  <span>{c.orders} order{c.orders !== 1 ? "s" : ""}</span>
                  {c.delivered > 0 && <span className="text-emerald-400/80">{c.delivered} delivered</span>}
                  {c.at_risk > 0 && <span className="text-amber-400/80">{c.at_risk} at risk</span>}
                  {c.late > 0 && <span className="text-red-400/80">{c.late} late</span>}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* chase list */}
        <div>
          <p className="text-xs text-slate-500 mb-2">Orders to chase</p>
          {d.at_risk_orders.length === 0 ? (
            <p className="text-emerald-400 text-sm">Nothing at risk — order book on track.</p>
          ) : (
            <div className="space-y-2">
              {d.at_risk_orders.map((o) => {
                const cls = `flex items-start gap-3 rounded-lg border border-slate-800 border-l-2 ${o.state === "late" ? "border-l-red-500/70" : "border-l-amber-400/70"} bg-slate-900/40 px-3 py-2`;
                const inner = (
                  <>
                    <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${o.state === "late" ? "bg-red-500" : "bg-amber-400"}`} />
                    <div className="min-w-0 flex-1 text-left">
                      <p className="text-sm text-slate-200 truncate">
                        {o.order_no} · <span className="text-slate-400">{o.customer}</span>
                      </p>
                      <p className="text-[11px] text-slate-500 truncate">
                        {o.product} · {o.dispatched_quantity}/{o.order_quantity} shipped
                      </p>
                    </div>
                    <span className={`shrink-0 text-[11px] ${o.state === "late" ? "text-red-400" : "text-amber-400"}`}>
                      {dueLabel(o)}
                    </span>
                  </>
                );
                return onOpen ? (
                  <button
                    key={o.order_no}
                    type="button"
                    onClick={() => onOpen("orders")}
                    title="Open in Orders & Dispatch"
                    className={`${cls} w-full hover:border-slate-600 hover:bg-slate-800/60 transition focus:outline-none focus:ring-2 focus:ring-slate-600`}
                  >
                    {inner}
                  </button>
                ) : (
                  <div key={o.order_no} className={cls}>{inner}</div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
