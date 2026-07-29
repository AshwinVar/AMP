"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import { parseApiDate } from "../lib/apiDate";

type TenantRow = {
  tenant_code: string;
  users: number;
  machines: number;
  production_7d: number;
  orders_7d: number;
  open_escalations: number;
  last_production_at: string | null;
  active: boolean;
};

// Mirrors saas_routes.get_tenant_activity (GET /saas/tenant-activity).
type TenantActivity = {
  days: number;
  tenants: TenantRow[];
  active_count: number;
  quiet_count: number;
};

function ago(iso: string | null) {
  if (!iso) return "never";
  const d = parseApiDate(iso);
  if (!d) return "—";
  const days = Math.floor((Date.now() - d.getTime()) / 86400000);
  return days <= 0 ? "today" : `${days}d ago`;
}

// The founder's adoption panel: which tenants actually USE AMP. Quietest first —
// those are the churn risks to chase. Founder-only endpoint; the card hides
// itself entirely for anyone else (403 -> null).
export default function TenantAdoptionCard() {
  const [d, setD] = useState<TenantActivity | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setD(await apiGet<TenantActivity>("/saas/tenant-activity"));
    } catch {
      setD(null); // not the founder — no card
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (loading && !d) {
    return (
      <div className="rounded-2xl border border-slate-800 bg-slate-900 p-5 text-slate-400 text-sm">
        Loading tenant adoption…
      </div>
    );
  }
  if (!d) return null;

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div>
          <h3 className="text-xl font-bold">Adoption</h3>
          <p className="text-slate-400 mt-1 text-sm">
            Who is actually using AMP — last {d.days} days, quietest first.
          </p>
        </div>
        <div className="flex gap-2 text-xs">
          <span className="rounded-full border border-emerald-500/40 bg-emerald-500/10 px-3 py-1 text-emerald-300">
            {d.active_count} active
          </span>
          <span className={`rounded-full border px-3 py-1 ${d.quiet_count ? "border-amber-500/40 bg-amber-500/10 text-amber-300" : "border-slate-700 text-slate-400"}`}>
            {d.quiet_count} quiet
          </span>
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[720px] text-left text-sm">
          <thead className="text-slate-500 text-xs border-b border-slate-800">
            <tr>
              <th className="py-2 pr-3">Tenant</th>
              <th className="py-2 pr-3">Users</th>
              <th className="py-2 pr-3">Machines</th>
              <th className="py-2 pr-3">Production (7d)</th>
              <th className="py-2 pr-3">Orders (7d)</th>
              <th className="py-2 pr-3">Open escalations</th>
              <th className="py-2 pr-3">Last production</th>
              <th className="py-2">Status</th>
            </tr>
          </thead>
          <tbody>
            {d.tenants.map((t) => (
              <tr key={t.tenant_code} className="border-b border-slate-800/60">
                <td className="py-2 pr-3 font-semibold">{t.tenant_code}</td>
                <td className="py-2 pr-3">{t.users}</td>
                <td className="py-2 pr-3">{t.machines}</td>
                <td className="py-2 pr-3">{t.production_7d}</td>
                <td className="py-2 pr-3">{t.orders_7d}</td>
                <td className={`py-2 pr-3 ${t.open_escalations ? "text-amber-400" : ""}`}>{t.open_escalations}</td>
                <td className="py-2 pr-3 text-slate-400">{ago(t.last_production_at)}</td>
                <td className="py-2">
                  {t.active
                    ? <span className="text-emerald-400 text-xs font-semibold">● active</span>
                    : <span className="text-amber-400 text-xs font-semibold">○ quiet</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
