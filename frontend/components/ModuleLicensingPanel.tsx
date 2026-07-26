"use client";

import { useEffect, useState } from "react";
import { apiGet, apiPatch, apiPost } from "../lib/api";
import type { ModulePack, ModulesResponse } from "../lib/modules";

// The licence the plan-gate + nav obey (TenantConfig), distinct from the SaaS
// CRM record (CompanyTenant) the section above manages.
type TenantConfig = {
  tenant_code: string;
  plan: string;
  enabled_modules: string[];
  brand_name?: string;
};

// Founder-only panel: assign a tenant a plan bundle (POST apply-plan) or toggle
// an individual module pack (PATCH /tenant-configs/{code}). Both drive the same
// TenantConfig.enabled_modules that the server plan-gate and the dashboard nav
// read, so a change here is what makes a module appear or disappear in that
// tenant's AMP. Fetches its own data and hides itself if the caller isn't the
// platform owner (the endpoints are DEFAULT-tenant only).
export default function ModuleLicensingPanel() {
  const [configs, setConfigs] = useState<TenantConfig[] | null>(null);
  const [packs, setPacks] = useState<ModulePack[]>([]);
  const [bundles, setBundles] = useState<Record<string, string[]>>({});
  const [denied, setDenied] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function load() {
    try {
      const [cfgs, mods] = await Promise.all([
        apiGet<TenantConfig[]>("/tenant-configs"),
        apiGet<ModulesResponse>("/modules"),
      ]);
      setConfigs(cfgs);
      setPacks(mods.packs || []);
      setBundles(mods.plan_bundles || {});
    } catch {
      // /tenant-configs is platform-owner only — hide the panel for everyone else.
      setDenied(true);
    }
  }
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function applyPlan(code: string, plan: string) {
    setBusy(code);
    setError(null);
    setNotice(null);
    try {
      await apiPost(`/tenant-configs/${code}/apply-plan`, { plan });
      setNotice(`${code}: applied the ${plan} plan.`);
      await load();
    } catch (e) {
      setError(`${code}: ${(e as Error)?.message || "could not apply the plan"}`);
    } finally {
      setBusy(null);
    }
  }

  async function togglePack(cfg: TenantConfig, packId: string, on: boolean) {
    setBusy(cfg.tenant_code);
    setError(null);
    setNotice(null);
    const next = on
      ? Array.from(new Set([...cfg.enabled_modules, packId]))
      : cfg.enabled_modules.filter((m) => m !== packId);
    try {
      await apiPatch(`/tenant-configs/${cfg.tenant_code}`, { enabled_modules: next });
      setNotice(`${cfg.tenant_code}: ${on ? "enabled" : "disabled"} the ${packId} pack.`);
      await load();
    } catch (e) {
      setError(`${cfg.tenant_code}: ${(e as Error)?.message || "could not update modules"}`);
    } finally {
      setBusy(null);
    }
  }

  if (denied) return null; // not the platform owner — nothing to show
  if (!configs) {
    return (
      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5 text-slate-400 text-sm">
        Loading module licensing…
      </div>
    );
  }

  const planNames = Object.keys(bundles);

  return (
    <section className="space-y-4">
      <div>
        <h3 className="text-xl font-bold">Module Licensing</h3>
        <p className="text-slate-400 mt-1 text-sm">
          Assign a plan or toggle individual modules per tenant. This drives what
          appears in each customer&apos;s AMP — the same licence the server enforces.
        </p>
      </div>

      {notice && (
        <div className="rounded-xl border border-emerald-500/40 bg-emerald-500/10 px-4 py-2 text-sm text-emerald-300">
          {notice}
        </div>
      )}
      {error && (
        <div className="rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-2 text-sm text-red-300">
          {error}
        </div>
      )}

      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5 overflow-x-auto">
        <table className="w-full min-w-[820px] text-left text-sm">
          <thead className="text-slate-400 border-b border-slate-800">
            <tr>
              <th className="py-3 px-4">Tenant</th>
              <th className="py-3 px-4">Plan</th>
              <th className="py-3 px-4">Modules</th>
              <th className="py-3 px-4">Apply plan</th>
            </tr>
          </thead>
          <tbody>
            {configs.map((cfg) => {
              const enabled = new Set(cfg.enabled_modules);
              const rowBusy = busy === cfg.tenant_code;
              return (
                <tr key={cfg.tenant_code} className="border-b border-slate-800 align-top">
                  <td className="py-3 px-4 font-semibold whitespace-nowrap">
                    {cfg.tenant_code}
                    {cfg.brand_name && cfg.brand_name !== cfg.tenant_code && (
                      <div className="text-xs text-slate-500 font-normal">{cfg.brand_name}</div>
                    )}
                  </td>
                  <td className="py-3 px-4">
                    <span className="inline-block rounded-full border border-slate-700 bg-slate-950 px-3 py-1 text-xs capitalize">
                      {cfg.plan || "—"}
                    </span>
                  </td>
                  <td className="py-3 px-4">
                    <div className="flex flex-wrap gap-2">
                      {packs.map((p) => {
                        const on = enabled.has(p.id) || !p.gated;
                        const locked = !p.gated; // core + admin are always on
                        return (
                          <button
                            key={p.id}
                            type="button"
                            disabled={locked || rowBusy}
                            onClick={() => togglePack(cfg, p.id, !enabled.has(p.id))}
                            title={
                              locked
                                ? `${p.label} is always included`
                                : on
                                  ? `Disable ${p.label}`
                                  : `Enable ${p.label}`
                            }
                            className={`rounded-lg px-2.5 py-1 text-xs border transition ${
                              on
                                ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-300"
                                : "border-slate-700 bg-slate-950 text-slate-500"
                            } ${locked ? "opacity-70 cursor-default" : "cursor-pointer hover:border-emerald-400"}`}
                          >
                            {on ? "●" : "○"} {p.label}
                          </button>
                        );
                      })}
                    </div>
                  </td>
                  <td className="py-3 px-4 whitespace-nowrap">
                    <select
                      aria-label={`Apply plan to ${cfg.tenant_code}`}
                      className="bg-slate-950 border border-slate-700 rounded-lg px-2 py-1"
                      defaultValue=""
                      disabled={rowBusy}
                      onChange={(e) => {
                        const plan = e.target.value;
                        e.target.value = "";
                        if (plan) applyPlan(cfg.tenant_code, plan);
                      }}
                    >
                      <option value="" disabled>
                        Apply plan…
                      </option>
                      {planNames.map((plan) => (
                        <option key={plan} value={plan} className="capitalize">
                          {plan}
                        </option>
                      ))}
                    </select>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
