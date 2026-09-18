"use client";

import React from "react";

import type { HistoryRow } from "../../lib/contracts";

/**
 * This party's audit trail of one contract (ADR-0020).
 *
 * Each party reads its OWN copy (the factory's tenant, or the manufacturer's).
 * While a factory has withdrawn SHARE_DOWNTIME, a manufacturer still sees that a
 * statement was computed or accepted, but not its details, which carry the
 * statement's hash.
 */
export default function ContractHistory({ rows, truncated }: { rows: HistoryRow[]; truncated: boolean }) {
  if (rows.length === 0) {
    return <p className="text-xs text-slate-500">No history recorded yet.</p>;
  }
  return (
    <div className="overflow-x-auto">
      {truncated && (
        <p className="mb-2 text-xs text-slate-400">Showing the newest entries only.</p>
      )}
      <table className="w-full text-xs">
        <thead className="text-slate-500 uppercase tracking-wide">
          <tr>
            <th className="p-2 text-left">When (UTC)</th>
            <th className="p-2 text-left">Who</th>
            <th className="p-2 text-left">What</th>
            <th className="p-2 text-left">Details</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.at}-${r.action}-${i}`} className="border-t border-slate-800 align-top">
              <td className="p-2 font-mono">{r.at}</td>
              <td className="p-2">{r.actor}</td>
              <td className="p-2">{r.action.replace(/^contract_/, "").replace(/_/g, " ")}</td>
              <td className="p-2 text-slate-400">
                {r.details_withheld ? "withheld: the factory has withdrawn downtime sharing" : r.details}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
