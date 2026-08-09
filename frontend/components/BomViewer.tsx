import React, { useEffect, useMemo, useState } from "react";
import { apiGet } from "../lib/api";

/**
 * One COMPONENT line of one bill of materials.
 *
 * The endpoint used to return one row per PART, because the recipe book was a
 * dict with a single raw material per part (ADR-0013). A real bill of materials
 * has many components, so a part now spans several rows — which is why the table
 * groups them rather than keying on `part_number`, a key that is no longer
 * unique and would have collided silently.
 */
interface BomRow {
  id: number;
  part_number: string;
  revision: string;
  active: boolean;
  effective_from: string | null;
  finished_goods_code: string;
  finished_goods_name: string;
  component_id: number | null;
  component_code: string;
  component_name: string;
  quantity_per_unit: number;
  unit: string;
  component_stock: number | null;
  component_reorder_level: number | null;
}

interface BomGroup {
  id: number;
  part_number: string;
  revision: string;
  active: boolean;
  finished_goods_code: string;
  finished_goods_name: string;
  components: BomRow[];
}

function group(rows: BomRow[]): BomGroup[] {
  const byId = new Map<number, BomGroup>();
  for (const row of rows) {
    let g = byId.get(row.id);
    if (!g) {
      g = {
        id: row.id,
        part_number: row.part_number,
        revision: row.revision,
        active: row.active,
        finished_goods_code: row.finished_goods_code,
        finished_goods_name: row.finished_goods_name,
        components: [],
      };
      byId.set(row.id, g);
    }
    // A BOM with no components still arrives as one row with a null
    // component_id — a recipe that only produces is a real thing, and dropping
    // it here would hide it from the only screen that can fix it.
    if (row.component_id !== null) g.components.push(row);
  }
  return [...byId.values()];
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

  const groups = useMemo(() => group(rows), [rows]);

  function stockHealth(row: BomRow) {
    // null means the recipe names an item this workspace does not stock — a
    // different (and more urgent) state than "in stock and healthy".
    if (row.component_stock === null)
      return <span className="rounded-full px-2 py-0.5 text-xs border border-slate-500/40 bg-slate-500/10 text-slate-400">Not stocked</span>;
    if (row.component_stock === 0)
      return <span className="rounded-full px-2 py-0.5 text-xs border border-red-500/40 bg-red-500/10 text-red-400">Stockout</span>;
    if (row.component_reorder_level !== null && row.component_stock <= row.component_reorder_level)
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

      {!loading && !error && groups.length === 0 && (
        <p className="text-slate-400 text-sm">
          No bills of materials defined yet. Until a part has one, completing its
          work order moves no stock.
        </p>
      )}

      {!loading && !error && groups.length > 0 && (
        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full min-w-[900px] text-left text-sm">
            <thead className="text-slate-400 border-b border-slate-800">
              <tr>
                <th className="py-3 px-4">Part Number</th>
                <th className="py-3 px-4">Component</th>
                <th className="py-3 px-4">Qty / Unit</th>
                <th className="py-3 px-4">Current Stock</th>
                <th className="py-3 px-4">Stock Health</th>
                <th className="py-3 px-4">Finished Good Produced</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((g) => {
                const span = Math.max(g.components.length, 1);
                if (g.components.length === 0) {
                  return (
                    <tr key={`bom-${g.id}`} className="border-b border-slate-800">
                      <td className="py-3 px-4 font-semibold text-indigo-300">
                        {g.part_number}
                        <span className="ml-2 text-slate-500 text-xs">rev {g.revision}</span>
                      </td>
                      <td className="py-3 px-4 text-slate-500" colSpan={4}>
                        No components — this part only produces
                      </td>
                      <td className="py-3 px-4">
                        <div className="font-medium">{g.finished_goods_name}</div>
                        <div className="text-slate-500 text-xs">{g.finished_goods_code}</div>
                      </td>
                    </tr>
                  );
                }
                return g.components.map((row, i) => (
                  // Keyed on component_id, which is unique. part_number is not:
                  // a part legitimately appears once per component.
                  <tr key={`component-${row.component_id}`} className="border-b border-slate-800">
                    {i === 0 && (
                      <td className="py-3 px-4 font-semibold text-indigo-300 align-top" rowSpan={span}>
                        {g.part_number}
                        <span className="ml-2 text-slate-500 text-xs">rev {g.revision}</span>
                        {!g.active && (
                          <div className="mt-1 text-xs text-slate-500">inactive</div>
                        )}
                      </td>
                    )}
                    <td className="py-3 px-4">
                      <div className="font-medium">{row.component_name || row.component_code}</div>
                      <div className="text-slate-500 text-xs">{row.component_code}</div>
                    </td>
                    <td className="py-3 px-4">
                      {row.quantity_per_unit > 0
                        ? <span className="font-mono">{row.quantity_per_unit} {row.unit}</span>
                        : <span className="text-slate-500">—</span>}
                    </td>
                    <td className="py-3 px-4 font-mono">
                      {row.component_stock !== null ? row.component_stock : "—"}
                    </td>
                    <td className="py-3 px-4">{stockHealth(row)}</td>
                    {i === 0 && (
                      <td className="py-3 px-4 align-top" rowSpan={span}>
                        <div className="font-medium">{g.finished_goods_name}</div>
                        <div className="text-slate-500 text-xs">{g.finished_goods_code}</div>
                      </td>
                    )}
                  </tr>
                ));
              })}
            </tbody>
          </table>
        </div>
      )}

      <p className="text-slate-600 text-xs mt-3">
        When a Work Order is marked Completed, the system automatically issues every
        component (qty × quantity per unit) and receives the finished good — both posted
        as inventory transactions. Each workspace maintains its own recipes.
      </p>
    </div>
  );
}
