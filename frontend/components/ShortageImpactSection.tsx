"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import { stateNotice } from "../lib/evidence";
import { money } from "../lib/money";

// Mirrors ai/shortage.py (ADR-0030).
type OrderRow = {
  work_order_no: string;
  part_number: string;
  outstanding: number;
  per_unit: number;
  component_unit: string;
  units_at_risk: number;
  planned_end: string | null;
};
type Shortage = {
  item_code: string;
  item_name: string;
  unit: string;
  on_hand: number;
  required_units: number;
  shortfall_units: number;
  units_at_risk: number;
  money_at_risk: number | null;
  currency: string | null;
  orders: OrderRow[];
  orders_affected: number;
  suggested_order_units: number;
  basis: string;
};
type Unlinked = { item_code: string; item_name: string; on_hand: number; unit: string; why: string };
type Impact = {
  state: string;
  headline: string;
  units_at_risk: number;
  money_at_risk: number | null;
  priced: boolean;
  shortages: Shortage[];
  unlinked: Unlinked[];
  note: string;
};

/** What one shortage would cost, as this card may state it. */
export function shortageSize(s: Pick<Shortage, "money_at_risk" | "units_at_risk">): string {
  if (s.money_at_risk != null) return money(s.money_at_risk);
  return `${s.units_at_risk.toLocaleString()} units`;
}

/**
 * What a shortage will actually stop (ADR-0030).
 *
 * ADR-0026 refused to put a number on a stock-out, in writing: AMP had no
 * measured link from a shortage to the units not made. This card is that link,
 * so the thing it must never do is blur the two cases. An item in a bill of
 * materials gets arithmetic — orders, quantity per unit, what the stock reaches.
 * An item in no recipe gets no number at all, and is listed underneath with the
 * reason, where a buyer can still see it.
 */
export default function ShortageImpactSection() {
  const [s, setS] = useState<Impact | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setS(await apiGet<Impact>("/shortage-impact"));
    } catch {
      // A glanceable card — stay quiet rather than break the page.
    }
  }, []);
  useEffect(() => {
    let alive = true;
    const first = setTimeout(() => { if (alive) load(); }, 0);
    const id = setInterval(() => { if (alive) load(); }, 120000);
    return () => { alive = false; clearTimeout(first); clearInterval(id); };
  }, [load]);

  if (!s) return null;
  const notice = stateNotice(s.state);
  return (
    <section className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
      <h2 className="text-xl font-bold">What the shortages will stop</h2>
      <p className="text-slate-300 text-sm mt-1">{s.headline}</p>
      {notice && <p role="status" className="text-amber-300/90 text-xs mt-2">{s.state} · {notice}</p>}

      {s.shortages.length > 0 && (
        <ul className="mt-4 space-y-2">
          {s.shortages.map((row) => (
            <li key={row.item_code} className="rounded-xl bg-slate-950/60 border border-slate-800 p-3">
              <div className="flex items-start justify-between gap-2 flex-wrap">
                <div>
                  <p className="text-sm font-semibold">{row.item_name}</p>
                  <p className="text-xs text-slate-400 tabular-nums">
                    {row.on_hand.toLocaleString()} {row.unit} on hand · the open orders need{" "}
                    {row.required_units.toLocaleString()} {row.unit}
                    {row.shortfall_units > 0 && (
                      <> · short by {row.shortfall_units.toLocaleString()} {row.unit}</>
                    )}
                  </p>
                </div>
                <div className="text-right shrink-0">
                  <p className="text-sm font-semibold tabular-nums">{shortageSize(row)}</p>
                  <p className="text-[10px] text-slate-500">
                    {row.orders_affected} open order{row.orders_affected === 1 ? "" : "s"}
                  </p>
                </div>
              </div>
              <div className="flex gap-3 mt-2">
                <button
                  type="button"
                  aria-expanded={open === row.item_code}
                  onClick={() => setOpen(open === row.item_code ? null : row.item_code)}
                  className="text-xs text-slate-400 hover:text-white"
                >
                  {open === row.item_code ? "Hide" : "Show"} the orders ({row.orders.length})
                </button>
                <span className="text-xs text-slate-500">
                  Suggested order: {row.suggested_order_units.toLocaleString()} {row.unit}
                </span>
              </div>
              {open === row.item_code && (
                <>
                  <ul className="mt-2 space-y-1">
                    {row.orders.map((o) => (
                      <li key={o.work_order_no} className="text-xs flex flex-wrap items-baseline gap-x-2">
                        <span className="text-slate-300">{o.work_order_no}</span>
                        <span className="text-slate-500">
                          {o.part_number} · {o.outstanding.toLocaleString()} outstanding ·{" "}
                          {o.per_unit} {o.component_unit} each
                        </span>
                        <span className={o.units_at_risk > 0 ? "text-orange-400" : "text-emerald-400/80"}>
                          {o.units_at_risk > 0
                            ? `${o.units_at_risk.toLocaleString()} cannot be made`
                            : "covered"}
                        </span>
                      </li>
                    ))}
                  </ul>
                  {/* The allocation rule, wherever the allocation is shown. */}
                  <p className="text-[11px] text-slate-500 mt-2">{row.basis}</p>
                </>
              )}
            </li>
          ))}
        </ul>
      )}

      {s.unlinked.length > 0 && (
        <div className="mt-4 rounded-xl border border-slate-800 bg-slate-950/40 p-3">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
            Short, but AMP cannot say what it would stop ({s.unlinked.length})
          </h3>
          <ul className="mt-2 space-y-1">
            {s.unlinked.map((u) => (
              <li key={u.item_code} className="text-xs text-slate-400">
                <span className="text-slate-300">{u.item_name}</span>
                <span className="tabular-nums"> · {u.on_hand.toLocaleString()} {u.unit} on hand</span>
                <span className="block text-[11px] text-slate-500">{u.why}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {s.shortages.length === 0 && s.unlinked.length === 0 && (
        <p className="text-slate-400 text-sm mt-3">Nothing is at or below its reorder level.</p>
      )}
      {!s.priced && s.shortages.length > 0 && (
        <p className="text-[11px] text-slate-500 mt-3">
          No unit value is set, so the risk is given in units of production, not money.
        </p>
      )}
    </section>
  );
}
