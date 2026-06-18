import React, { useState } from "react";
import type {
  InventoryAnalytics,
  InventoryItem,
  InventoryTransaction,
} from "../lib/phase13-types";

function stockStyle(item: InventoryItem) {
  if (item.current_stock === 0) {
    return "border-red-500/40 bg-red-500/10 text-red-300";
  }

  if (item.current_stock <= item.reorder_level) {
    return "border-yellow-500/40 bg-yellow-500/10 text-yellow-300";
  }

  return "border-green-500/40 bg-green-500/10 text-green-300";
}

function txnStyle(type: string) {
  const t = type.toLowerCase();
  if (t === "in" || t === "receive") return "text-green-400 border-green-500/40 bg-green-500/10";
  if (t === "out" || t === "issue") return "text-red-400 border-red-500/40 bg-red-500/10";
  if (t === "return") return "text-blue-400 border-blue-500/40 bg-blue-500/10";
  return "text-yellow-400 border-yellow-500/40 bg-yellow-500/10"; // adjustment
}

export default function InventorySection({
  items,
  transactions,
  analytics,
  itemForm,
  setItemForm,
  transactionForm,
  setTransactionForm,
  createItem,
  updateItem,
  deleteItem,
  createTransaction,
  generateLowStockEscalations,
}: {
  items: InventoryItem[];
  transactions: InventoryTransaction[];
  analytics: InventoryAnalytics | null;
  itemForm: {
    item_code: string;
    item_name: string;
    category: string;
    supplier: string;
    unit: string;
    current_stock: number;
    reorder_level: number;
    location: string;
  };
  setItemForm: (value: any) => void;
  transactionForm: {
    item_id: string;
    transaction_type: string;
    quantity: number;
    reference: string;
    notes: string;
  };
  setTransactionForm: (value: any) => void;
  createItem: (e: React.FormEvent) => void;
  updateItem: (id: number, currentStock: number, reorderLevel: number) => void;
  deleteItem: (id: number) => void;
  createTransaction: (e: React.FormEvent) => void;
  generateLowStockEscalations: () => void;
}) {
  const [categoryFilter, setCategoryFilter] = React.useState("All");

  const categories = ["All", ...Array.from(new Set(items.map((i) => i.category))).sort()];
  const filteredItems = categoryFilter === "All" ? items : items.filter((i) => i.category === categoryFilter);

  function getItemName(id: number) {
    const item = items.find((row) => row.id === id);
    return item ? `${item.item_code} - ${item.item_name}` : `Item ${id}`;
  }

  return (
    <section className="mt-8 space-y-6">
      <div className="flex flex-col md:flex-row md:items-end md:justify-between gap-4">
        <div>
          <h2 className="text-3xl font-bold">Inventory & Materials</h2>
          <p className="text-slate-400 mt-2">
            Track raw material stock, material issue/return and low-stock escalation.
          </p>
        </div>

        <button
          type="button"
          onClick={generateLowStockEscalations}
          className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3"
        >
          Generate Low Stock Escalations
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <Kpi title="Items" value={analytics?.total_items ?? 0} />
        <Kpi title="Low Stock" value={analytics?.low_stock_items ?? 0} />
        <Kpi title="Stock Units" value={analytics?.total_stock_units ?? 0} />
        <Kpi title="Transactions" value={analytics?.transactions ?? 0} />
      </div>

      <div className="flex flex-wrap gap-2">
        {categories.map((cat) => (
          <button
            key={cat}
            type="button"
            onClick={() => setCategoryFilter(cat)}
            className={`rounded-full px-4 py-1.5 text-sm border transition-colors ${
              categoryFilter === cat
                ? "bg-white text-slate-950 border-white font-semibold"
                : "border-slate-700 text-slate-400 hover:border-slate-500"
            }`}
          >
            {cat}
          </button>
        ))}
      </div>

      <form
        onSubmit={createItem}
        className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-1 md:grid-cols-4 xl:grid-cols-8 gap-4"
      >
        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          placeholder="Item Code"
          value={itemForm.item_code}
          onChange={(e) => setItemForm({ ...itemForm, item_code: e.target.value })}
          required
        />

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          placeholder="Item Name"
          value={itemForm.item_name}
          onChange={(e) => setItemForm({ ...itemForm, item_name: e.target.value })}
          required
        />

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          placeholder="Category"
          value={itemForm.category}
          onChange={(e) => setItemForm({ ...itemForm, category: e.target.value })}
          required
        />

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          placeholder="Supplier"
          value={itemForm.supplier}
          onChange={(e) => setItemForm({ ...itemForm, supplier: e.target.value })}
        />

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          placeholder="Unit"
          value={itemForm.unit}
          onChange={(e) => setItemForm({ ...itemForm, unit: e.target.value })}
          required
        />

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          type="number"
          placeholder="Stock"
          value={itemForm.current_stock}
          onChange={(e) => setItemForm({ ...itemForm, current_stock: Number(e.target.value) })}
        />

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          type="number"
          placeholder="Reorder"
          value={itemForm.reorder_level}
          onChange={(e) => setItemForm({ ...itemForm, reorder_level: Number(e.target.value) })}
        />

        <button
          type="submit"
          className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3"
        >
          Add Item
        </button>
      </form>

      <form
        onSubmit={createTransaction}
        className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-1 md:grid-cols-5 gap-4"
      >
        <select
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          value={transactionForm.item_id}
          onChange={(e) => setTransactionForm({ ...transactionForm, item_id: e.target.value })}
          required
        >
          <option value="">Select Item</option>
          {items.map((item) => (
            <option key={item.id} value={item.id}>
              {item.item_code} - {item.item_name}
            </option>
          ))}
        </select>

        <select
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          value={transactionForm.transaction_type}
          onChange={(e) => setTransactionForm({ ...transactionForm, transaction_type: e.target.value })}
        >
          <option>Receive</option>
          <option>Issue</option>
          <option>Return</option>
          <option>Adjust</option>
        </select>

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          type="number"
          placeholder="Quantity"
          value={transactionForm.quantity}
          onChange={(e) => setTransactionForm({ ...transactionForm, quantity: Number(e.target.value) })}
          required
        />

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          placeholder="Reference"
          value={transactionForm.reference}
          onChange={(e) => setTransactionForm({ ...transactionForm, reference: e.target.value })}
        />

        <button
          type="submit"
          className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3"
        >
          Post Transaction
        </button>
      </form>

      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-2xl font-semibold mb-4">Inventory Master</h3>

        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full min-w-[1050px] text-left text-sm">
            <thead className="text-slate-400 border-b border-slate-800">
              <tr>
                <th className="py-3 px-4">Code</th>
                <th className="py-3 px-4">Item</th>
                <th className="py-3 px-4">Category</th>
                <th className="py-3 px-4">Supplier</th>
                <th className="py-3 px-4">Location</th>
                <th className="py-3 px-4">Stock</th>
                <th className="py-3 px-4">Reorder</th>
                <th className="py-3 px-4">Unit</th>
                <th className="py-3 px-4">Health</th>
                <th className="py-3 px-4">Actions</th>
              </tr>
            </thead>

            <tbody>
              {filteredItems.map((item) => (
                <tr key={item.id} className="border-b border-slate-800">
                  <td className="py-3 px-4 font-semibold">{item.item_code}</td>
                  <td className="py-3 px-4">{item.item_name}</td>
                  <td className="py-3 px-4">{item.category}</td>
                  <td className="py-3 px-4">{item.supplier || "-"}</td>
                  <td className="py-3 px-4 text-slate-400 text-xs">{item.location || "-"}</td>
                  <td className="py-3 px-4">
                    <input
                      className="w-24 bg-slate-950 border border-slate-700 rounded-lg px-2 py-1"
                      type="number"
                      defaultValue={item.current_stock}
                      onBlur={(e) =>
                        updateItem(
                          item.id,
                          Number(e.target.value),
                          item.reorder_level
                        )
                      }
                    />
                  </td>
                  <td className="py-3 px-4">
                    <input
                      className="w-24 bg-slate-950 border border-slate-700 rounded-lg px-2 py-1"
                      type="number"
                      defaultValue={item.reorder_level}
                      onBlur={(e) =>
                        updateItem(
                          item.id,
                          item.current_stock,
                          Number(e.target.value)
                        )
                      }
                    />
                  </td>
                  <td className="py-3 px-4">{item.unit}</td>
                  <td className="py-3 px-4">
                    <span className={`rounded-full px-3 py-1 text-xs border ${stockStyle(item)}`}>
                      {item.current_stock === 0
                        ? "Stockout"
                        : item.current_stock <= item.reorder_level
                        ? "Low Stock"
                        : "Healthy"}
                    </span>
                  </td>
                  <td className="py-3 px-4">
                    <button
                      onClick={() => deleteItem(item.id)}
                      className="text-red-400 border border-red-500/40 rounded-lg px-3 py-1 hover:bg-red-500/10"
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}

              {filteredItems.length === 0 && (
                <tr>
                  <td colSpan={10} className="py-6 px-4 text-slate-400">
                    No inventory items yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-2xl font-semibold mb-4">Recent Material Transactions</h3>

        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full min-w-[760px] text-left text-sm">
            <thead className="text-slate-400 border-b border-slate-800">
              <tr>
                <th className="py-3 px-4">Item</th>
                <th className="py-3 px-4">Type</th>
                <th className="py-3 px-4">Quantity</th>
                <th className="py-3 px-4">Reference</th>
                <th className="py-3 px-4">Notes</th>
              </tr>
            </thead>

            <tbody>
              {transactions.slice(0, 80).map((row) => (
                <tr key={row.id} className="border-b border-slate-800">
                  <td className="py-3 px-4">{getItemName(row.item_id)}</td>
                  <td className="py-3 px-4">
                    <span className={`rounded-full px-3 py-1 text-xs border ${txnStyle(row.transaction_type)}`}>
                      {row.transaction_type}
                    </span>
                  </td>
                  <td className="py-3 px-4">{row.quantity}</td>
                  <td className="py-3 px-4">{row.reference || "-"}</td>
                  <td className="py-3 px-4 text-slate-400">{row.notes || "-"}</td>
                </tr>
              ))}

              {transactions.length === 0 && (
                <tr>
                  <td colSpan={5} className="py-6 px-4 text-slate-400">
                    No inventory transactions yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function Kpi({ title, value }: { title: string; value: string | number }) {
  return (
    <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
      <p className="text-slate-400 text-sm">{title}</p>
      <h3 className="text-2xl font-bold mt-2">{value}</h3>
    </div>
  );
}
