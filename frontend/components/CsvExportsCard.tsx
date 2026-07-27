"use client";

import { useState } from "react";
import { API_URL, getDownloadHeaders } from "../lib/api";

// Every CSV the backend serves (/reports/*.csv, Admin/Supervisor-gated,
// tenant-scoped). Label → path → saved filename.
const EXPORTS: { key: string; label: string; path: string; filename: string }[] = [
  { key: "workorders", label: "Work orders", path: "/reports/work-orders.csv", filename: "work_orders.csv" },
  { key: "maintenance", label: "Maintenance", path: "/reports/maintenance.csv", filename: "maintenance_tasks.csv" },
  { key: "pos", label: "Purchase orders", path: "/reports/purchase-orders.csv", filename: "purchase_orders.csv" },
  { key: "inventory", label: "Inventory", path: "/reports/inventory.csv", filename: "inventory_items.csv" },
  { key: "quality", label: "Quality", path: "/reports/quality.csv", filename: "quality_inspections.csv" },
  { key: "escalations", label: "Escalations", path: "/reports/escalations.csv", filename: "escalations.csv" },
  { key: "downtime", label: "Downtime", path: "/reports/downtime.csv", filename: "downtime_report.csv" },
  { key: "shifts", label: "Shifts", path: "/reports/shifts.csv", filename: "shift_report.csv" },
  { key: "oee", label: "OEE records", path: "/reports/oee.csv", filename: "oee_report.csv" },
];

// One-click Excel handoff: every operational table as a tenant-scoped CSV.
// Downloads go through fetch with the auth (+ founder-preview tenant) headers,
// then save as a blob — a plain <a href> couldn't carry the bearer token.
export default function CsvExportsCard() {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const download = async (exp: (typeof EXPORTS)[number]) => {
    if (busy) return;
    setBusy(exp.key);
    setError(null);
    try {
      const res = await fetch(`${API_URL}${exp.path}`, { headers: getDownloadHeaders(), cache: "no-store" });
      if (!res.ok) throw new Error(`${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = exp.filename;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      setError(`Could not export ${exp.label} — check your permissions and try again.`);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Exports</h3>
      <p className="text-slate-400 text-sm mt-1">
        Every operational table as a CSV — scoped to this workspace, ready for Excel.
      </p>
      {error && (
        <div className="mt-3 rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-2 text-sm text-red-300">
          {error}
        </div>
      )}
      <div className="mt-4 flex flex-wrap gap-2">
        {EXPORTS.map((exp) => (
          <button
            key={exp.key}
            type="button"
            onClick={() => download(exp)}
            disabled={busy !== null}
            className="rounded-md border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:border-slate-500 hover:bg-slate-800 transition disabled:opacity-50"
          >
            {busy === exp.key ? "Exporting…" : `↓ ${exp.label}`}
          </button>
        ))}
      </div>
    </div>
  );
}
