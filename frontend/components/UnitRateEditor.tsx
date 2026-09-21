"use client";

import { useCallback, useState } from "react";
import { apiPatch, errorDetail } from "../lib/api";
import { CURRENCY } from "../lib/money";

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
  // Why the last save did not happen, in the server's words. This is the rate
  // every loss figure is priced from, and a save that did nothing used to say
  // nothing — the editor just stayed open, which reads as "still typing".
  const [failed, setFailed] = useState<string | null>(null);

  const save = useCallback(async () => {
    const trimmed = draft.trim();
    const value = trimmed === "" ? null : Number(trimmed);
    if (value !== null && (!Number.isFinite(value) || value < 0)) {
      setFailed("Enter a rate of 0 or more.");
      return;
    }
    setSaving(true);
    setFailed(null);
    try {
      await apiPatch("/tenant-config", { unit_value_gbp: value });
      await onSaved();
      setEditing(false);
    } catch (e) {
      setFailed(`Not saved: ${errorDetail(e)}`);
    } finally {
      setSaving(false);
    }
  }, [draft, onSaved]);

  if (!isAdmin) return null;

  if (editing) {
    return (
      <span className="inline-flex items-center gap-1">
        <span className="text-slate-500">{CURRENCY}</span>
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
        <button onClick={() => { setEditing(false); setFailed(null); }} className="text-slate-500 hover:text-slate-400">cancel</button>
        {failed && <span role="alert" className="text-xs text-red-400">{failed}</span>}
      </span>
    );
  }

  return (
    <button
      onClick={() => {
        setDraft(rate != null ? String(rate) : "");
        setFailed(null);
        setEditing(true);
      }}
      className="text-emerald-400 hover:text-emerald-300"
    >
      {rate != null ? "edit rate" : "set rate"}
    </button>
  );
}
