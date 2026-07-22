"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPost } from "../lib/api";

// Mirrors the recovery read-model's "fix this first" fields (ai/recovery.py).
type Component = { key: string; label: string; current: number; target: number; gap_points: number };
type RecoverySummary = {
  has_data: boolean;
  at_world_class: boolean;
  unit_value_gbp: number | null;
  biggest_lever: string | null;
  lever_label: string | null;
  lever_action: string | null;
  lever_recoverable_units_per_year: number;
  lever_recoverable_value_per_year: number | null;
  recoverable_value_per_year: number | null;
  recoverable_units_per_year: number;
  components: Component[];
};

// "Fix this first" — the single highest-value OEE move, quantified. Takes the
// recovery read-model's biggest lever (the component furthest from world-class),
// names it, prices closing just its gap, and routes the owner to act. Self-
// contained; renders nothing until there's a gap worth fixing.
export default function NextBestActionCard({
  onRaised,
}: {
  // Called after the CTA raises (or surfaces) the recovery escalation, with its
  // id so the caller can focus it in the Escalation Center. id is null if it
  // couldn't be created (e.g. the viewer lacks permission) — still route there.
  onRaised?: (escalationId: number | null) => void;
}) {
  const [s, setS] = useState<RecoverySummary | null>(null);
  const [raising, setRaising] = useState(false);

  const load = useCallback(async () => {
    try {
      setS(await apiGet<RecoverySummary>("/recovery-summary"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  const raise = useCallback(async () => {
    setRaising(true);
    try {
      const res = await apiPost<{ created: number; escalation_id: number | null }>(
        "/escalations/generate-oee-recovery",
        {},
      );
      onRaised?.(res.escalation_id ?? null);
    } catch {
      onRaised?.(null); // couldn't create — still take them to the Escalation Center
    } finally {
      setRaising(false);
    }
  }, [onRaised]);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  // Nothing to recommend until there's a lever with a gap to close.
  if (!s || !s.has_data || s.at_world_class || !s.biggest_lever) return null;

  const priced = s.unit_value_gbp != null;
  const comp = s.components.find((c) => c.key === s.biggest_lever);
  const prize = priced
    ? `£${(s.lever_recoverable_value_per_year ?? 0).toLocaleString()} / yr`
    : `+${s.lever_recoverable_units_per_year.toLocaleString()} good units / yr`;

  return (
    <div className="rounded-2xl border border-amber-500/30 bg-gradient-to-br from-amber-500/10 to-slate-900/40 p-6">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="min-w-0">
          <p className="text-xs font-semibold uppercase tracking-wide text-amber-300/90">
            Fix this first
          </p>
          <h3 className="text-2xl font-bold mt-1">
            {s.lever_label} is your biggest lever
          </h3>
          {s.lever_action && (
            <p className="text-slate-300 mt-2 max-w-xl">{s.lever_action}</p>
          )}
          {comp && (
            <p className="text-[11px] text-slate-500 mt-2 tabular-nums">
              {comp.label} {comp.current}% → {comp.target}% world-class
              {" · "}part of {priced
                ? `£${(s.recoverable_value_per_year ?? 0).toLocaleString()}`
                : `${s.recoverable_units_per_year.toLocaleString()} units`} total recovery / yr
            </p>
          )}
        </div>
        <div className="text-right shrink-0">
          <p className="text-3xl font-bold text-amber-300 tabular-nums">{prize}</p>
          <p className="text-[11px] text-slate-500">by closing this gap alone</p>
        </div>
      </div>

      {onRaised && (
        <div className="mt-5">
          <button
            type="button"
            onClick={raise}
            disabled={raising}
            className="rounded-lg bg-amber-500/90 hover:bg-amber-400 disabled:opacity-60 text-slate-900 font-semibold px-4 py-2 text-sm transition"
          >
            {raising ? "Raising…" : "Raise an escalation →"}
          </button>
        </div>
      )}
    </div>
  );
}
