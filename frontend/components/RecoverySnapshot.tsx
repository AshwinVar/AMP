"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPatch } from "../lib/api";

// Mirrors the backend recovery read-model (ai/recovery.py build_recovery_summary).
type Component = { key: string; label: string; current: number; target: number; gap_points: number };
type RecoverySummary = {
  has_data: boolean;
  oee: number;
  world_class: number;
  gap_points: number;
  at_world_class: boolean;
  recoverable_units_window: number;
  recoverable_units_per_year: number;
  unit_value_gbp: number | null;
  recoverable_value_per_year: number | null;
  components: Component[];
  biggest_lever: string | null;
};

// The OEE recovery card: the gap to world-class and what closing it is worth in
// good units. Self-contained — fetches its own summary and refreshes. Renders
// nothing until there's production to measure.
export default function RecoverySnapshot({ isAdmin = false }: { isAdmin?: boolean }) {
  const [s, setS] = useState<RecoverySummary | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      setS(await apiGet<RecoverySummary>("/recovery-summary"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  const saveRate = useCallback(async () => {
    const trimmed = draft.trim();
    const value = trimmed === "" ? null : Number(trimmed);
    if (value !== null && (!Number.isFinite(value) || value < 0)) return; // ignore bad input
    setSaving(true);
    try {
      await apiPatch("/tenant-config", { unit_value_gbp: value });
      await load();
      setEditing(false);
    } catch {
      // Non-admins (403) or a transient error — stay quiet, leave the card as is.
    } finally {
      setSaving(false);
    }
  }, [draft, load]);

  if (!s || !s.has_data) return null;

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">OEE recovery · gap to world-class</h3>
          <p className="text-slate-400 text-sm mt-1">
            {s.at_world_class
              ? `OEE ${s.oee}% — at or above the ${s.world_class}% world-class benchmark`
              : `OEE ${s.oee}% vs ${s.world_class}% world-class — closing the ${s.gap_points}-pt gap is worth more good output`}
          </p>
        </div>
        <div className="text-right">
          {s.recoverable_value_per_year != null ? (
            <>
              <p className="text-3xl font-bold text-emerald-400 tabular-nums">
                {s.at_world_class ? "£0" : `£${s.recoverable_value_per_year.toLocaleString()}`}
              </p>
              <p className="text-[11px] text-slate-500">
                / yr upside{s.at_world_class ? "" : ` · +${s.recoverable_units_per_year.toLocaleString()} good units`}
              </p>
            </>
          ) : (
            <>
              <p className="text-3xl font-bold text-emerald-400 tabular-nums">
                {s.at_world_class ? "0" : `+${s.recoverable_units_per_year.toLocaleString()}`}
              </p>
              <p className="text-[11px] text-slate-500">good units / yr upside</p>
            </>
          )}
        </div>
      </div>

      <div className="mt-5 space-y-3">
        {s.components.map((c) => {
          const isBiggest = c.key === s.biggest_lever;
          const pct = Math.min(100, Math.round((c.current / c.target) * 100));
          return (
            <div key={c.key}>
              <div className="flex items-center justify-between text-sm mb-1">
                <span className={isBiggest ? "text-emerald-300 font-semibold" : "text-slate-300"}>
                  {c.label}
                  {isBiggest ? " · biggest lever" : ""}
                </span>
                <span className="text-slate-400 tabular-nums">
                  {c.current}% <span className="text-slate-600">/ {c.target}%</span>
                </span>
              </div>
              <div className="h-2 rounded-full bg-slate-800 overflow-hidden">
                <div
                  className={`h-full ${c.gap_points === 0 ? "bg-emerald-500" : isBiggest ? "bg-emerald-400" : "bg-emerald-600/60"}`}
                  style={{ width: `${pct}%` }}
                />
              </div>
              <p className="text-[11px] text-slate-500 mt-1">
                {c.gap_points === 0 ? "at world-class" : `${c.gap_points} pts below the ${c.target}% target`}
              </p>
            </div>
          );
        })}
      </div>

      {(s.unit_value_gbp != null || isAdmin) && (
        <div className="mt-4 pt-3 border-t border-slate-800 flex items-center justify-between text-[11px] text-slate-500">
          <span>
            Unit value:{" "}
            {s.unit_value_gbp != null ? (
              <span className="text-slate-300 tabular-nums">£{s.unit_value_gbp.toLocaleString()} / good unit</span>
            ) : (
              <span className="text-slate-500">not set — showing units only</span>
            )}
          </span>
          {isAdmin &&
            (editing ? (
              <span className="flex items-center gap-1">
                <span className="text-slate-500">£</span>
                <input
                  type="number"
                  min="0"
                  step="0.01"
                  autoFocus
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") saveRate();
                    if (e.key === "Escape") setEditing(false);
                  }}
                  className="w-20 rounded bg-slate-800 border border-slate-700 px-2 py-0.5 text-slate-200 tabular-nums"
                />
                <button onClick={saveRate} disabled={saving} className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50">
                  {saving ? "…" : "save"}
                </button>
                <button onClick={() => setEditing(false)} className="text-slate-500 hover:text-slate-400">cancel</button>
              </span>
            ) : (
              <button
                onClick={() => {
                  setDraft(s.unit_value_gbp != null ? String(s.unit_value_gbp) : "");
                  setEditing(true);
                }}
                className="text-emerald-400 hover:text-emerald-300"
              >
                {s.unit_value_gbp != null ? "edit rate" : "set rate"}
              </button>
            ))}
        </div>
      )}
    </div>
  );
}
