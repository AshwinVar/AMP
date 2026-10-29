"use client";

import { useCallback, useEffect, useState } from "react";

import { apiGet, errorDetail } from "../lib/api";
import { formatInstant } from "../lib/apiDate";
import { type Cell, downloadCsv, exportFilename, toCsv } from "../lib/chart-export";
import { CURRENCY } from "../lib/money";
import { todayLocalIso } from "../lib/plant-board";
import { useModalFocus } from "../lib/useModalFocus";

/**
 * THE PART-MASTER AUDIT TRAIL — who changed a part's figures, when, from what.
 *
 * The part master now shows one row per part: the spec in force, not its whole
 * history (PartMasterCard). That trim is only safe because nothing is lost —
 * every revision stays in the database, and this is where a person reads it
 * back. A spec change restates the plant's revenue and output, so when a
 * customer disputes an invoice the first question is "who changed the price, and
 * what was it before?". This drawer is the answer, newest first, with the
 * field-level old -> new of each change, and an Export for the spreadsheet the
 * dispute will be argued in.
 *
 * ADMIN ONLY, like the writes it records and like the Price column it exposes —
 * the backend gates /part-spec-audit on Admin, and the control that opens this
 * is shown only to an admin, so a non-admin never calls an endpoint that 403s.
 *
 * Matches the repo drawer pattern (PartRunwayDrawer): a right-hand dialog over a
 * scrim, focus trapped by useModalFocus, Escape to close, self-fetching.
 */

type AuditChange = { old: Cell; new: Cell };
type AuditEntry = {
  part_code: string;
  part_name: string | null;
  actor: string | null;
  at: string | null;
  changes: Record<string, AuditChange>;
};

// Friendly labels for the fields the backend records
// (part_spec_routes._AUDITED_FIELDS). An unknown field falls back to its key, so
// a field added to the spec later still reads, just without a prettier name.
const FIELD_LABEL: Record<string, string> = {
  part_name: "Part name",
  material: "Material",
  part_weight_g: "Weight (g)",
  cavities: "Cavities",
  active_cavities: "Active cavities",
  ideal_cycle_time_s: "Cycle (s)",
  price_per_piece: "Price (" + CURRENCY + ")",
  effective_from: "In force from",
};

const CSV_HEADERS = ["When", "Part", "Part code", "Actor", "Field", "Old", "New"];

function fieldLabel(field: string): string {
  return FIELD_LABEL[field] ?? field;
}

function fmt(value: Cell): string {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

/**
 * A create records every field with old=null; a revise records only the fields
 * that moved. The presence of any non-null `old` is what tells the two apart —
 * the backend stores no separate "action", so the shape of the change set is the
 * signal.
 */
export function isCreate(entry: Pick<AuditEntry, "changes">): boolean {
  const changes = Object.values(entry.changes);
  return changes.length > 0 && changes.every((c) => c.old === null || c.old === undefined);
}

/**
 * The trail flattened one row per changed field, for the CSV. A per-field row is
 * what a spreadsheet wants: "ELE-CLIP, Price, 0.09 -> 0.12" sorts and filters,
 * where a JSON blob in a cell does not. Reuses the board's csv helpers so the
 * formula-injection guard and the UTF-8 BOM (for the rupee sign) come for free.
 */
export function auditCsvRows(entries: AuditEntry[]): Record<string, Cell>[] {
  return entries.flatMap((entry) =>
    Object.entries(entry.changes).map(([field, change]) => ({
      When: formatInstant(entry.at),
      Part: entry.part_name ?? "",
      "Part code": entry.part_code,
      Actor: entry.actor ?? "",
      Field: fieldLabel(field),
      Old: change.old,
      New: change.new,
    })),
  );
}

export default function PartSpecAuditDrawer({ onClose }: { onClose: () => void }) {
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [error, setError] = useState("");
  const [said, setSaid] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const data = await apiGet<{ entries: AuditEntry[] }>("/part-spec-audit");
      setEntries(data.entries);
    } catch (e) {
      setError(errorDetail(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Keep Tab inside the drawer, hand focus back to the opener on close.
  const dialogRef = useModalFocus<HTMLDivElement>();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  function exportCsv() {
    if (!entries || !entries.length) return;
    // A download that silently did not happen is worse than an error, so say.
    const ok = downloadCsv(
      exportFilename("part-master-audit", todayLocalIso()),
      toCsv(CSV_HEADERS, auditCsvRows(entries)),
    );
    setSaid(ok ? "Saved" : "Could not save");
    setTimeout(() => setSaid(""), 2500);
  }

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} aria-hidden="true" />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="Part master change history"
        tabIndex={-1}
        className="relative w-full max-w-xl bg-slate-950 border-l border-slate-800 h-full overflow-y-auto p-6"
      >
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-2xl font-bold">Part master — change history</h2>
            <p className="text-slate-500 text-sm mt-1">
              Every create and revision, newest first: who changed what, and what
              the figure was before.
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {entries && entries.length ? (
              <button
                type="button"
                onClick={exportCsv}
                className="rounded-lg border border-slate-700 px-2.5 py-1 text-xs text-slate-300 hover:border-slate-500 hover:text-slate-100"
                title="Download the whole change history as a CSV"
              >
                {said || "Export"}
              </button>
            ) : null}
            <button
              onClick={onClose}
              className="text-slate-400 hover:text-white text-xl px-2"
              aria-label="Close"
            >
              ✕
            </button>
          </div>
        </div>

        {error ? (
          <div className="mt-4 rounded-xl border border-red-500/40 bg-red-500/10 text-red-300 p-3 text-sm">
            {error}
          </div>
        ) : null}

        {entries === null && !error ? (
          <p className="text-slate-400 mt-6">Loading the change history…</p>
        ) : entries && entries.length === 0 ? (
          <p className="text-slate-500 text-sm mt-6">
            No change has been recorded yet. The trail fills as parts are added
            and revised.
          </p>
        ) : entries ? (
          <ol className="mt-5 space-y-4">
            {entries.map((entry, i) => {
              const created = isCreate(entry);
              return (
                <li key={i} className="border-b border-slate-800/70 pb-4">
                  <div className="flex items-baseline justify-between gap-3">
                    <p className="text-sm font-medium text-slate-100">
                      {entry.part_name ?? entry.part_code}
                      <span className="ml-2 text-xs text-slate-500">{entry.part_code}</span>
                    </p>
                    <span className="shrink-0 text-xs text-slate-600">{formatInstant(entry.at)}</span>
                  </div>
                  <p className="mt-0.5 text-[11px] text-slate-500">
                    {created ? "Created" : "Revised"} by {entry.actor ?? "unknown"}
                  </p>
                  <ul className="mt-2 space-y-1">
                    {Object.entries(entry.changes).map(([field, change]) => (
                      <li key={field} className="text-xs text-slate-300">
                        <span className="text-slate-500">{fieldLabel(field)}: </span>
                        {change.old === null || change.old === undefined ? (
                          <span className="text-slate-200">{fmt(change.new)}</span>
                        ) : (
                          <>
                            <span className="text-slate-500 line-through">{fmt(change.old)}</span>
                            <span className="text-slate-500"> → </span>
                            <span className="text-slate-200">{fmt(change.new)}</span>
                          </>
                        )}
                      </li>
                    ))}
                  </ul>
                </li>
              );
            })}
          </ol>
        ) : null}
      </div>
    </div>
  );
}
