"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPatch } from "../lib/api";

type TenantConfig = {
  brand_name: string | null;
  brand_color: string | null;
  brand_logo_url: string | null;
};

// White-label settings for the workspace: name, accent colour, logo URL. Drives
// PATCH /tenant-config, which lets a tenant Admin re-brand their OWN workspace
// (and the founder, while previewing a client, re-brand the previewed tenant).
// Licence fields are server-gated — this card only ever touches branding.
export default function BrandingSettingsCard({ onSaved }: { onSaved?: () => void }) {
  const [name, setName] = useState("");
  const [color, setColor] = useState("#6366f1");
  const [logo, setLogo] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState(false);

  const load = useCallback(async () => {
    try {
      const cfg = await apiGet<TenantConfig>("/tenant-config");
      setName(cfg.brand_name || "");
      setColor(cfg.brand_color || "#6366f1");
      setLogo(cfg.brand_logo_url || "");
      setLoaded(true);
    } catch {
      // leave the card hidden if the config can't be read
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (!loaded) return null;

  const save = async () => {
    setBusy(true);
    setNotice(null);
    setError(false);
    try {
      await apiPatch("/tenant-config", {
        brand_name: name.trim() || "AMP",
        brand_color: color,
        brand_logo_url: logo.trim() || null,
      });
      setNotice("Branding saved — it now fronts this workspace.");
      onSaved?.();
    } catch {
      setError(true);
      setNotice("Could not save branding — check your permissions.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4">
      <div>
        <h3 className="text-lg font-semibold">Workspace branding</h3>
        <p className="text-slate-400 mt-1 text-sm">
          White-label this workspace — the name and colour front the whole dashboard.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <label className="block">
          <span className="text-xs text-slate-400">Brand name</span>
          <input
            className="mt-1 w-full bg-slate-950 border border-slate-700 rounded-xl px-4 py-2.5 text-sm"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Acme Manufacturing"
          />
        </label>
        <label className="block">
          <span className="text-xs text-slate-400">Accent colour</span>
          <span className="mt-1 flex items-center gap-3">
            <input
              type="color"
              className="h-10 w-14 rounded-lg border border-slate-700 bg-slate-950 p-1"
              value={color}
              onChange={(e) => setColor(e.target.value)}
              aria-label="Accent colour"
            />
            <code className="text-xs text-slate-400">{color}</code>
          </span>
        </label>
        <label className="block">
          <span className="text-xs text-slate-400">Logo URL (optional)</span>
          <input
            className="mt-1 w-full bg-slate-950 border border-slate-700 rounded-xl px-4 py-2.5 text-sm"
            value={logo}
            onChange={(e) => setLogo(e.target.value)}
            placeholder="https://…/logo.png"
          />
        </label>
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <button
          type="button"
          onClick={save}
          disabled={busy}
          className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-2 text-sm disabled:opacity-50"
        >
          {busy ? "Saving…" : "Save branding"}
        </button>
        {/* live preview of how the sidebar header will read */}
        <span className="inline-flex items-center gap-2 rounded-xl border border-slate-800 bg-slate-950 px-3 py-2">
          <span className="inline-block h-3 w-3 rounded-full" style={{ backgroundColor: color }} />
          <span className="text-sm font-semibold">{name.trim() || "AMP"}</span>
        </span>
        {notice && (
          <span className={`text-xs ${error ? "text-red-400" : "text-emerald-400"}`}>{notice}</span>
        )}
      </div>
    </section>
  );
}
