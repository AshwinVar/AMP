"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

type Series = { date: string; count: number }[];

// Mirrors the backend ops trends (ai/trends.py build_ops_trends).
type OpsTrends = {
  days: number;
  production: Series;
  downtime: Series;
  quality_failed: Series;
  agent_actions: Series;
};

function wk(iso: string) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString(undefined, { weekday: "short" });
}

export default function TrendsSection() {
  const [t, setT] = useState<OpsTrends | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setT(await apiGet<OpsTrends>("/ops-trends"));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load trends");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  return (
    <section className="mt-8 space-y-6">
      <div className="flex flex-col md:flex-row md:items-end md:justify-between gap-4">
        <div>
          <h2 className="text-3xl font-bold">Trends</h2>
          <p className="text-slate-400 mt-2">The last 7 days across your factory — production, downtime, quality, and the AI fleet.</p>
        </div>
        <button onClick={load} className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3">Refresh</button>
      </div>

      {error && <div className="rounded-xl border border-red-500/40 bg-red-500/10 text-red-300 p-4">{error}</div>}

      {loading && !t ? (
        <p className="text-slate-400">Loading trends…</p>
      ) : t ? (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <BarPanel title="Production" unit="good units" series={t.production} colorClass="bg-emerald-500/60" />
          <BarPanel title="Downtime" unit="events" series={t.downtime} colorClass="bg-red-500/60" />
          <BarPanel title="Quality failures" unit="units" series={t.quality_failed} colorClass="bg-orange-500/60" />
          <BarPanel title="Agent actions" unit="actions" series={t.agent_actions} colorClass="bg-indigo-500/60" />
        </div>
      ) : null}
    </section>
  );
}

function BarPanel({ title, unit, series, colorClass }: { title: string; unit: string; series: Series; colorClass: string }) {
  const peak = series.reduce((m, d) => Math.max(m, d.count), 0);
  const total = series.reduce((s, d) => s + d.count, 0);
  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900 p-6">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold uppercase tracking-wide text-slate-300">{title}</h3>
        <span className="text-xs text-slate-500">{total.toLocaleString()} {unit} · 7d</span>
      </div>
      {total === 0 ? (
        <p className="text-slate-500 text-sm mt-4">Nothing recorded this week.</p>
      ) : (
        <div className="mt-4 flex items-end gap-2 h-28">
          {series.map((d) => {
            const h = peak ? Math.max(4, Math.round((d.count / peak) * 96)) : 4;
            return (
              <div
                key={d.date}
                className="flex-1 flex flex-col items-center justify-end gap-1"
                title={`${d.count} on ${d.date}`}
              >
                <span className="text-[10px] text-slate-400">{d.count || ""}</span>
                <div className={`w-full rounded-t ${colorClass}`} style={{ height: `${h}px` }} />
                <span className="text-[10px] text-slate-500">{wk(d.date)}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
