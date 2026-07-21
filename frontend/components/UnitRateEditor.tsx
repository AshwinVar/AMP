"use client";

import { useCallback, useState } from "react";
import { apiPatch } from "../lib/api";

// Shared inline editor for the tenant's £/good-unit rate
// (TenantConfig.unit_value_gbp). Admin-only; PATCHes /tenant-config and calls
// onSaved so the caller can refresh. Used by the recovery card and the Executive
// OEE money-story panel so the control lives in one place.
export default function UnitRateEditor({
  rate,
  isAdmin = false,
  onSaved,
}: {
  rate: number | null;
  isAdmin?: boolean;
  onSaved: () => void | Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);

  const save = useCallback(async () => {
    const trimmed = draft.trim();
    const value = trimmed === "" ? null : Number(trimmed);
    if (value !== null && (!Number.isFinite(value) || value < 0)) return; // ignore bad input
    setSaving(true);
    try {
      await apiPatch("/tenant-config", { unit_value_gbp: value });
      await onSaved();
      setEditing(false);
    } catch {
      // Non-admin (403) or a transient error — stay quiet, leave things as they are.
    } finally {
      setSaving(false);
    }
  }, [draft, onSaved]);

  if (!isAdmin) return null;

  if (editing) {
    return (
      <span className="inline-flex items-center gap-1">
        <span className="text-slate-500">£</span>
        <input
          type="number"
          min="0"
          step="0.01"
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") save();
            if (e.key === "Escape") setEditing(false);
          }}
          className="w-20 rounded bg-slate-800 border border-slate-700 px-2 py-0.5 text-slate-200 tabular-nums"
        />
        <button onClick={save} disabled={saving} className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50">
          {saving ? "…" : "save"}
        </button>
        <button onClick={() => setEditing(false)} className="text-slate-500 hover:text-slate-400">cancel</button>
      </span>
    );
  }

  return (
    <button
      onClick={() => {
        setDraft(rate != null ? String(rate) : "");
        setEditing(true);
      }}
      className="text-emerald-400 hover:text-emerald-300"
    >
      {rate != null ? "edit rate" : "set rate"}
    </button>
  );
}
