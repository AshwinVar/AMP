import React, { useEffect, useState } from "react";
import { apiGet } from "../lib/api";

interface BomRow {
  part_number: string;
  raw_material_code: string;
  raw_material_name: string;
  consume_per_unit: number;
  raw_unit: string;
  finished_goods_code: string;
  finished_goods_name: string;
  raw_current_stock: number | null;
  raw_reorder_level: number | null;
}

export default function BomViewer() {
  const [rows, setRows] = useState<BomRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    apiGet<BomRow[]>("/bom")
      .then(setRows)
      .catch(() => setError("Failed to load BOM — Admin access required."))
      .finally(() => setLoading(false));
  }, []);

  function stockHealth(row: BomRow) {
    if (row.raw_current_stock === null) return null;
    if (row.raw_current_stock === 0)
      return <span className="rounded-full px-2 py-0.5 text-xs border border-red-500/40 bg-red-500/10 text-red-400">Stockout</span>;
    if (row.raw_reorder_level !== null && row.raw_current_stock <= row.raw_reorder_level)
      return <span className="rounded-full px-2 py-0.5 text-xs border border-yellow-500/40 bg-yellow-500/10 text-yellow-400">Low Stock</span>;
    return <span className="rounded-full px-2 py-0.5 text-xs border border-green-500/40 bg-green-500/10 text-green-400">Healthy</span>;
  }

  return (
    <div className="rounded-2xl bg-slate-900 border border-indigo-500/30 p-5 mt-6">
      <div className="flex items-center gap-3 mb-4">
        <span className="rounded-lg bg-indigo-500/20 border border-indigo-500/40 px-3 py-1 text-xs text-indigo-400 font-semibold tracking-wider">
          ADMIN ONLY
        </span>
        <h3 className="text-xl font-semibold">Bill of Materials (BOM)</h3>
        <span className="text-slate-500 text-sm">— defines what each part consumes and produces</span>
      </div>

      {loading && <p className="text-slate-400 text-sm">Loading BOM...</p>}
      {error   && <p className="text-red-400 text-sm">{error}</p>}

      {!loading && !error && (
        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full min-w-[900px] text-left text-sm">
            <thead className="text-slate-400 border-b border-slate-800">
              <tr>
                <th className="py-3 px-4">Part Number</th>
                <th className="py-3 px-4">Raw Material</th>
                <th className="py-3 px-4">Qty / Unit</th>
                <th className="py-3 px-4">Current Stock</th>
                <th className="py-3 px-4">Stock Health</th>
                <th className="py-3 px-4">Finished Good Produced</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.part_number} className="border-b border-slate-800">
                  <td className="py-3 px-4 font-semibold text-indigo-300">{row.part_number}</td>
                  <td className="py-3 px-4">
                    <div className="font-medium">{row.raw_material_name}</div>
                    <div className="text-slate-500 text-xs">{row.raw_material_code}</div>
                  </td>
                  <td className="py-3 px-4">
                    {row.consume_per_unit > 0
                      ? <span className="font-mono">{row.consume_per_unit} {row.raw_unit}</span>
                      : <span className="text-slate-500">—</span>}
                  </td>
                  <td className="py-3 px-4 font-mono">
                    {row.raw_current_stock !== null ? row.raw_current_stock : "—"}
                  </td>
                  <td className="py-3 px-4">{stockHealth(row)}</td>
                  <td className="py-3 px-4">
                    <div className="font-medium">{row.finished_goods_name}</div>
                    <div className="text-slate-500 text-xs">{row.finished_goods_code}</div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="text-slate-600 text-xs mt-3">
        When a Work Order is marked Completed, the system automatically issues the raw material quantity
        (qty × consume_per_unit) and receives the finished good — both posted as inventory transactions.
      </p>
    </div>
  );
}
