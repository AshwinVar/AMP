"use client";
import React, { useEffect, useState, useRef } from "react";
import { apiGet, apiPost, apiPatch, API_URL, getAuthHeaders } from "../lib/api";

// ── Types ─────────────────────────────────────────────────────────

interface InventoryItem {
  id: number; item_code: string; item_name: string; category: string;
  unit: string; current_stock: number; reorder_level: number;
  location?: string | null; supplier?: string | null;
}

interface Remnant {
  id: number; tag_no: string; item_id: number; item_code: string; item_name: string;
  source_reference: string; original_qty: number; remaining_qty: number;
  unit: string; location: string; status: string; notes: string;
}

interface IssueSlip {
  id: number; slip_no: string; item_id: number; item_code: string; item_name: string;
  remnant_id: number | null; work_order_ref: string; requested_qty: number;
  issued_qty: number; requested_by: string; approved_by: string | null;
  status: string; notes: string; created_at: string;
}

interface GRN {
  id: number; grn_no: string; purchase_order_ref: string; supplier_name: string;
  received_by: string; status: string; notes: string; created_at: string;
  items: GRNItem[];
}

interface GRNItem {
  id: number; item_id: number; item_code: string; item_name: string;
  lot_no: string; ordered_qty: number; received_qty: number;
  accepted_qty: number; rejected_qty: number; inspection_status: string;
}

interface CycleCount {
  id: number; count_no: string; counted_by: string; status: string;
  notes: string; created_at: string;
  items: CycleCountItem[];
}

interface CycleCountItem {
  id: number; item_id: number; item_code: string; item_name: string;
  book_qty: number; physical_qty: number; variance: number;
}

interface VarianceRow {
  item_id: number; item_code: string; item_name: string; category: string;
  unit: string; book_stock: number; total_received: number; total_issued: number;
  last_physical_count: number | null; last_variance: number | null; status: string;
}

// ── Helpers ───────────────────────────────────────────────────────

function statusBadge(status: string) {
  const map: Record<string, string> = {
    Available: "text-green-400 border-green-500/40 bg-green-500/10",
    Consumed:  "text-slate-400 border-slate-600/40 bg-slate-600/10",
    Scrapped:  "text-red-400 border-red-500/40 bg-red-500/10",
    "In Use":  "text-yellow-400 border-yellow-500/40 bg-yellow-500/10",
    Pending:   "text-yellow-400 border-yellow-500/40 bg-yellow-500/10",
    Approved:  "text-blue-400 border-blue-500/40 bg-blue-500/10",
    Issued:    "text-green-400 border-green-500/40 bg-green-500/10",
    Rejected:  "text-red-400 border-red-500/40 bg-red-500/10",
    Draft:     "text-slate-400 border-slate-600/40 bg-slate-600/10",
    Accepted:  "text-green-400 border-green-500/40 bg-green-500/10",
    Partial:   "text-yellow-400 border-yellow-500/40 bg-yellow-500/10",
    OK:        "text-green-400 border-green-500/40 bg-green-500/10",
    Low:       "text-yellow-400 border-yellow-500/40 bg-yellow-500/10",
    Stockout:  "text-red-400 border-red-500/40 bg-red-500/10",
  };
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs border ${map[status] ?? "text-slate-400 border-slate-700"}`}>
      {status}
    </span>
  );
}

const TABS = ["Remnants", "Issue Slips", "GRN", "Cycle Count", "Variance Report", "Import CSV"] as const;
type Tab = typeof TABS[number];

// ── Main component ────────────────────────────────────────────────

export default function EnterpriseInventory({ items }: { items: InventoryItem[] }) {
  const [tab, setTab] = useState<Tab>("Remnants");

  return (
    <div className="mt-8 space-y-6">
      <div>
        <h2 className="text-2xl font-bold">Enterprise Inventory</h2>
        <p className="text-slate-400 text-sm mt-1">
          Remnant tracking · Material issue slips · GRN · Cycle counts · Variance reporting · CSV import
        </p>
      </div>

      <div className="flex flex-wrap gap-2 border-b border-slate-800 pb-3">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-colors ${
              tab === t
                ? "bg-white text-slate-950"
                : "text-slate-400 hover:text-white hover:bg-slate-800"
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      {tab === "Remnants"        && <RemnantsTab items={items} />}
      {tab === "Issue Slips"     && <IssueSlipsTab items={items} />}
      {tab === "GRN"             && <GRNTab items={items} />}
      {tab === "Cycle Count"     && <CycleCountTab items={items} />}
      {tab === "Variance Report" && <VarianceReportTab />}
      {tab === "Import CSV"      && <ImportCSVTab />}
    </div>
  );
}

// ── Remnants ──────────────────────────────────────────────────────

function RemnantsTab({ items }: { items: InventoryItem[] }) {
  const [rows, setRows] = useState<Remnant[]>([]);
  const [form, setForm] = useState({ item_id: "", original_qty: "", remaining_qty: "", unit: "m", location: "", source_reference: "", notes: "" });
  const [loading, setLoading] = useState(false);

  const load = () => apiGet<Remnant[]>("/remnants").then(setRows).catch(() => {});
  useEffect(() => { load(); }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    await apiPost("/remnants", { ...form, item_id: Number(form.item_id), original_qty: Number(form.original_qty), remaining_qty: Number(form.remaining_qty) });
    setForm({ item_id: "", original_qty: "", remaining_qty: "", unit: "m", location: "", source_reference: "", notes: "" });
    await load();
    setLoading(false);
  }

  async function updateStatus(id: number, status: string, remaining_qty: number) {
    await apiPatch(`/remnants/${id}/status`, { status, remaining_qty });
    load();
  }

  return (
    <div className="space-y-5">
      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-lg font-semibold mb-1">What is a remnant?</h3>
        <p className="text-slate-400 text-sm">
          When you cut 40m from a 100m pipe, the leftover 60m is a <strong className="text-white">remnant</strong>.
          Log it here with a tag number and location so any future usage is formally booked against a job — eliminating stock mismatches.
        </p>
      </div>

      <form onSubmit={submit} className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-2 md:grid-cols-4 xl:grid-cols-8 gap-3">
        <select className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm col-span-2" value={form.item_id} onChange={e => setForm({ ...form, item_id: e.target.value })} required>
          <option value="">Select item</option>
          {items.map(i => <option key={i.id} value={i.id}>{i.item_code} – {i.item_name}</option>)}
        </select>
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" placeholder="Original qty" type="number" value={form.original_qty} onChange={e => setForm({ ...form, original_qty: e.target.value })} required />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" placeholder="Remaining qty" type="number" value={form.remaining_qty} onChange={e => setForm({ ...form, remaining_qty: e.target.value })} required />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" placeholder="Unit (m/kg/pcs)" value={form.unit} onChange={e => setForm({ ...form, unit: e.target.value })} />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" placeholder="Location (e.g. Rack B3)" value={form.location} onChange={e => setForm({ ...form, location: e.target.value })} />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" placeholder="Source (WO/PO ref)" value={form.source_reference} onChange={e => setForm({ ...form, source_reference: e.target.value })} />
        <button type="submit" disabled={loading} className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-2 text-sm">
          {loading ? "Logging…" : "Log Remnant"}
        </button>
      </form>

      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-lg font-semibold mb-4">Remnant Register <span className="text-slate-500 text-sm font-normal">({rows.length} entries)</span></h3>
        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full min-w-[900px] text-left text-sm">
            <thead className="text-slate-400 border-b border-slate-800">
              <tr>
                {["Tag", "Item", "Source", "Original", "Remaining", "Unit", "Location", "Status", "Actions"].map(h => (
                  <th key={h} className="py-3 px-4">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 && <tr><td colSpan={9} className="py-6 px-4 text-slate-400">No remnants logged yet.</td></tr>}
              {rows.map(r => (
                <tr key={r.id} className="border-b border-slate-800">
                  <td className="py-3 px-4 font-mono text-indigo-300">{r.tag_no}</td>
                  <td className="py-3 px-4">
                    <div className="font-medium">{r.item_name}</div>
                    <div className="text-slate-500 text-xs">{r.item_code}</div>
                  </td>
                  <td className="py-3 px-4 text-slate-400 text-xs">{r.source_reference || "—"}</td>
                  <td className="py-3 px-4">{r.original_qty}</td>
                  <td className="py-3 px-4 font-semibold">{r.remaining_qty}</td>
                  <td className="py-3 px-4">{r.unit}</td>
                  <td className="py-3 px-4 text-slate-400 text-xs">{r.location || "—"}</td>
                  <td className="py-3 px-4">{statusBadge(r.status)}</td>
                  <td className="py-3 px-4 flex gap-2">
                    {r.status === "Available" && (
                      <button onClick={() => updateStatus(r.id, "Scrapped", r.remaining_qty)} className="text-xs text-red-400 border border-red-500/30 rounded-lg px-2 py-1 hover:bg-red-500/10">Scrap</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// ── Issue Slips ───────────────────────────────────────────────────

function IssueSlipsTab({ items }: { items: InventoryItem[] }) {
  const [rows, setRows] = useState<IssueSlip[]>([]);
  const [form, setForm] = useState({ item_id: "", requested_qty: "", work_order_ref: "", requested_by: "", notes: "" });
  const [loading, setLoading] = useState(false);

  const load = () => apiGet<IssueSlip[]>("/issue-slips").then(setRows).catch(() => {});
  useEffect(() => { load(); }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    await apiPost("/issue-slips", { ...form, item_id: Number(form.item_id), requested_qty: Number(form.requested_qty) });
    setForm({ item_id: "", requested_qty: "", work_order_ref: "", requested_by: "", notes: "" });
    await load();
    setLoading(false);
  }

  const slipAction = async (id: number, action: "approve" | "issue" | "reject") => {
    await apiPatch(`/issue-slips/${id}/${action}`, {});
    load();
  };

  const slipColors: Record<string, string> = {
    Pending: "text-yellow-400", Approved: "text-blue-400",
    Issued: "text-green-400", Rejected: "text-red-400",
  };

  return (
    <div className="space-y-5">
      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-lg font-semibold mb-1">Material Issue Slip workflow</h3>
        <p className="text-slate-400 text-sm">
          Every material that leaves the store must have a signed-off slip linked to a work order.
          Flow: <span className="text-yellow-400">Pending</span> → <span className="text-blue-400">Approved</span> → <span className="text-green-400">Issued</span>.
          Stock deducts only when Issued — eliminating informal takes.
        </p>
      </div>

      <form onSubmit={submit} className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
        <select className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm col-span-2" value={form.item_id} onChange={e => setForm({ ...form, item_id: e.target.value })} required>
          <option value="">Select item</option>
          {items.map(i => <option key={i.id} value={i.id}>{i.item_code} – {i.item_name} (stock: {i.current_stock} {i.unit})</option>)}
        </select>
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" placeholder="Qty requested" type="number" value={form.requested_qty} onChange={e => setForm({ ...form, requested_qty: e.target.value })} required />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" placeholder="Work Order / Job ref" value={form.work_order_ref} onChange={e => setForm({ ...form, work_order_ref: e.target.value })} />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" placeholder="Requested by" value={form.requested_by} onChange={e => setForm({ ...form, requested_by: e.target.value })} required />
        <button type="submit" disabled={loading} className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-2 text-sm">
          {loading ? "Creating…" : "Raise Slip"}
        </button>
      </form>

      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-lg font-semibold mb-4">Issue Slip Register <span className="text-slate-500 text-sm font-normal">({rows.length} slips)</span></h3>
        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full min-w-[900px] text-left text-sm">
            <thead className="text-slate-400 border-b border-slate-800">
              <tr>
                {["Slip No", "Item", "WO Ref", "Requested Qty", "Requested By", "Approved By", "Status", "Actions"].map(h => (
                  <th key={h} className="py-3 px-4">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 && <tr><td colSpan={8} className="py-6 px-4 text-slate-400">No issue slips yet.</td></tr>}
              {rows.map(s => (
                <tr key={s.id} className="border-b border-slate-800">
                  <td className={`py-3 px-4 font-mono font-semibold ${slipColors[s.status] ?? ""}`}>{s.slip_no}</td>
                  <td className="py-3 px-4">
                    <div className="font-medium">{s.item_name}</div>
                    <div className="text-slate-500 text-xs">{s.item_code}</div>
                  </td>
                  <td className="py-3 px-4 text-slate-400 text-xs">{s.work_order_ref || "—"}</td>
                  <td className="py-3 px-4 font-semibold">{s.requested_qty}</td>
                  <td className="py-3 px-4">{s.requested_by}</td>
                  <td className="py-3 px-4 text-slate-400">{s.approved_by || "—"}</td>
                  <td className="py-3 px-4">{statusBadge(s.status)}</td>
                  <td className="py-3 px-4 flex gap-2 flex-wrap">
                    {s.status === "Pending" && (
                      <>
                        <button onClick={() => slipAction(s.id, "approve")} className="text-xs text-blue-400 border border-blue-500/30 rounded-lg px-2 py-1 hover:bg-blue-500/10">Approve</button>
                        <button onClick={() => slipAction(s.id, "reject")} className="text-xs text-red-400 border border-red-500/30 rounded-lg px-2 py-1 hover:bg-red-500/10">Reject</button>
                      </>
                    )}
                    {s.status === "Approved" && (
                      <button onClick={() => slipAction(s.id, "issue")} className="text-xs text-green-400 border border-green-500/30 rounded-lg px-2 py-1 hover:bg-green-500/10">Issue & Deduct</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// ── GRN ──────────────────────────────────────────────────────────

function GRNTab({ items }: { items: InventoryItem[] }) {
  const [rows, setRows] = useState<GRN[]>([]);
  const [supplier, setSupplier] = useState("");
  const [poRef, setPoRef] = useState("");
  const [grnItems, setGrnItems] = useState([{ item_id: "", received_qty: "", accepted_qty: "", rejected_qty: "0", lot_no: "", inspection_status: "Accepted" }]);
  const [loading, setLoading] = useState(false);

  const load = () => apiGet<GRN[]>("/grns").then(setRows).catch(() => {});
  useEffect(() => { load(); }, []);

  const addLine = () => setGrnItems(prev => [...prev, { item_id: "", received_qty: "", accepted_qty: "", rejected_qty: "0", lot_no: "", inspection_status: "Accepted" }]);
  const updateLine = (i: number, field: string, val: string) => setGrnItems(prev => prev.map((row, idx) => idx === i ? { ...row, [field]: val } : row));

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    await apiPost("/grns", {
      supplier_name: supplier, purchase_order_ref: poRef,
      items: grnItems.map(l => ({ ...l, item_id: Number(l.item_id), received_qty: Number(l.received_qty), accepted_qty: Number(l.accepted_qty), rejected_qty: Number(l.rejected_qty) })),
    });
    setSupplier(""); setPoRef("");
    setGrnItems([{ item_id: "", received_qty: "", accepted_qty: "", rejected_qty: "0", lot_no: "", inspection_status: "Accepted" }]);
    await load();
    setLoading(false);
  }

  return (
    <div className="space-y-5">
      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-lg font-semibold mb-1">Goods Receipt Note (GRN)</h3>
        <p className="text-slate-400 text-sm">
          When a delivery arrives, raise a GRN. Enter received and accepted quantities per item. Clicking <strong className="text-white">Accept GRN</strong> posts the accepted quantities into stock and creates receipt transactions automatically.
        </p>
      </div>

      <form onSubmit={submit} className="rounded-2xl bg-slate-900 border border-slate-800 p-5 space-y-4">
        <div className="grid grid-cols-2 gap-3">
          <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" placeholder="Supplier name" value={supplier} onChange={e => setSupplier(e.target.value)} required />
          <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" placeholder="PO Reference (optional)" value={poRef} onChange={e => setPoRef(e.target.value)} />
        </div>
        <div className="space-y-2">
          {grnItems.map((line, i) => (
            <div key={i} className="grid grid-cols-2 md:grid-cols-6 gap-2">
              <select className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm col-span-2" value={line.item_id} onChange={e => updateLine(i, "item_id", e.target.value)} required>
                <option value="">Select item</option>
                {items.map(it => <option key={it.id} value={it.id}>{it.item_code} – {it.item_name}</option>)}
              </select>
              <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" placeholder="Received qty" type="number" value={line.received_qty} onChange={e => updateLine(i, "received_qty", e.target.value)} required />
              <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" placeholder="Accepted qty" type="number" value={line.accepted_qty} onChange={e => updateLine(i, "accepted_qty", e.target.value)} required />
              <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" placeholder="Lot no." value={line.lot_no} onChange={e => updateLine(i, "lot_no", e.target.value)} />
              <select className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" value={line.inspection_status} onChange={e => updateLine(i, "inspection_status", e.target.value)}>
                <option>Accepted</option><option>Rejected</option><option>Partial</option>
              </select>
            </div>
          ))}
        </div>
        <div className="flex gap-3">
          <button type="button" onClick={addLine} className="text-sm text-slate-400 border border-slate-700 rounded-xl px-4 py-2 hover:border-slate-500">+ Add Line</button>
          <button type="submit" disabled={loading} className="rounded-xl bg-white text-slate-950 font-semibold px-5 py-2 text-sm">{loading ? "Creating…" : "Create GRN"}</button>
        </div>
      </form>

      <div className="space-y-4">
        {rows.length === 0 && <p className="text-slate-400 text-sm">No GRNs yet.</p>}
        {rows.map(g => (
          <div key={g.id} className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-3">
                <span className="font-mono font-semibold text-indigo-300">{g.grn_no}</span>
                {statusBadge(g.status)}
                <span className="text-slate-400 text-sm">{g.supplier_name}</span>
                {g.purchase_order_ref && <span className="text-slate-500 text-xs">PO: {g.purchase_order_ref}</span>}
              </div>
              {g.status === "Draft" && (
                <button onClick={async () => { await apiPatch(`/grns/${g.id}/accept`, {}); load(); }} className="text-sm text-green-400 border border-green-500/30 rounded-xl px-4 py-1.5 hover:bg-green-500/10 font-semibold">
                  Accept GRN → Post to Stock
                </button>
              )}
            </div>
            <div className="overflow-x-auto rounded-xl border border-slate-800">
              <table className="w-full text-left text-sm">
                <thead className="text-slate-400 border-b border-slate-800">
                  <tr>{["Item", "Lot No", "Received", "Accepted", "Rejected", "Inspection"].map(h => <th key={h} className="py-2 px-3">{h}</th>)}</tr>
                </thead>
                <tbody>
                  {g.items.map(li => (
                    <tr key={li.id} className="border-b border-slate-800">
                      <td className="py-2 px-3"><div className="font-medium">{li.item_name}</div><div className="text-slate-500 text-xs">{li.item_code}</div></td>
                      <td className="py-2 px-3 text-slate-400 text-xs">{li.lot_no || "—"}</td>
                      <td className="py-2 px-3">{li.received_qty}</td>
                      <td className="py-2 px-3 text-green-400 font-semibold">{li.accepted_qty}</td>
                      <td className="py-2 px-3 text-red-400">{li.rejected_qty}</td>
                      <td className="py-2 px-3">{statusBadge(li.inspection_status)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Cycle Count ───────────────────────────────────────────────────

function CycleCountTab({ items }: { items: InventoryItem[] }) {
  const [rows, setRows] = useState<CycleCount[]>([]);
  const [countedBy, setCountedBy] = useState("");
  const [physicals, setPhysicals] = useState<Record<number, string>>({});
  const [selectedItems, setSelectedItems] = useState<number[]>([]);
  const [loading, setLoading] = useState(false);

  const load = () => apiGet<CycleCount[]>("/cycle-counts").then(setRows).catch(() => {});
  useEffect(() => { load(); }, []);

  const toggleItem = (id: number) => setSelectedItems(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (selectedItems.length === 0) return;
    setLoading(true);
    await apiPost("/cycle-counts", {
      counted_by: countedBy,
      items: selectedItems.map(id => ({ item_id: id, physical_qty: Number(physicals[id] ?? 0) })),
    });
    setCountedBy(""); setPhysicals({}); setSelectedItems([]);
    await load();
    setLoading(false);
  }

  return (
    <div className="space-y-5">
      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-lg font-semibold mb-1">Physical Stock Count (Cycle Count)</h3>
        <p className="text-slate-400 text-sm">
          Physically count items and enter the actual quantity. The system calculates the variance against book stock.
          When an Admin approves the count, stock is adjusted automatically and a variance transaction is posted.
        </p>
      </div>

      <form onSubmit={submit} className="rounded-2xl bg-slate-900 border border-slate-800 p-5 space-y-4">
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm w-64" placeholder="Counted by (name)" value={countedBy} onChange={e => setCountedBy(e.target.value)} required />
        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full text-left text-sm">
            <thead className="text-slate-400 border-b border-slate-800">
              <tr>
                <th className="py-2 px-3 w-8"><input type="checkbox" onChange={e => setSelectedItems(e.target.checked ? items.map(i => i.id) : [])} /></th>
                <th className="py-2 px-3">Item</th>
                <th className="py-2 px-3">Category</th>
                <th className="py-2 px-3">Book Stock</th>
                <th className="py-2 px-3">Physical Count</th>
                <th className="py-2 px-3">Variance (preview)</th>
              </tr>
            </thead>
            <tbody>
              {items.map(item => {
                const physical = physicals[item.id] !== undefined ? Number(physicals[item.id]) : null;
                const variance = physical !== null ? physical - item.current_stock : null;
                return (
                  <tr key={item.id} className={`border-b border-slate-800 ${selectedItems.includes(item.id) ? "bg-slate-800/30" : ""}`}>
                    <td className="py-2 px-3"><input type="checkbox" checked={selectedItems.includes(item.id)} onChange={() => toggleItem(item.id)} /></td>
                    <td className="py-2 px-3">
                      <div className="font-medium">{item.item_name}</div>
                      <div className="text-slate-500 text-xs">{item.item_code}</div>
                    </td>
                    <td className="py-2 px-3 text-slate-400 text-xs">{item.category}</td>
                    <td className="py-2 px-3 font-mono">{item.current_stock} {item.unit}</td>
                    <td className="py-2 px-3">
                      {selectedItems.includes(item.id) && (
                        <input type="number" className="bg-slate-950 border border-slate-700 rounded-lg px-2 py-1 w-24 text-sm" placeholder="Enter qty" value={physicals[item.id] ?? ""} onChange={e => setPhysicals(p => ({ ...p, [item.id]: e.target.value }))} />
                      )}
                    </td>
                    <td className="py-2 px-3">
                      {variance !== null && (
                        <span className={`font-mono font-semibold ${variance < 0 ? "text-red-400" : variance > 0 ? "text-green-400" : "text-slate-400"}`}>
                          {variance > 0 ? "+" : ""}{variance}
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <button type="submit" disabled={loading || selectedItems.length === 0} className="rounded-xl bg-white text-slate-950 font-semibold px-5 py-2 text-sm disabled:opacity-50">
          {loading ? "Submitting…" : `Submit Count (${selectedItems.length} items selected)`}
        </button>
      </form>

      <div className="space-y-4">
        {rows.map(c => (
          <div key={c.id} className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-3">
                <span className="font-mono font-semibold text-indigo-300">{c.count_no}</span>
                {statusBadge(c.status)}
                <span className="text-slate-400 text-sm">by {c.counted_by}</span>
              </div>
              {c.status === "Draft" && (
                <button onClick={async () => { await apiPatch(`/cycle-counts/${c.id}/approve`, {}); load(); }} className="text-sm text-green-400 border border-green-500/30 rounded-xl px-4 py-1.5 hover:bg-green-500/10 font-semibold">
                  Approve & Adjust Stock
                </button>
              )}
            </div>
            <div className="overflow-x-auto rounded-xl border border-slate-800">
              <table className="w-full text-left text-sm">
                <thead className="text-slate-400 border-b border-slate-800">
                  <tr>{["Item", "Book Qty", "Physical Qty", "Variance"].map(h => <th key={h} className="py-2 px-3">{h}</th>)}</tr>
                </thead>
                <tbody>
                  {c.items.map(ci => (
                    <tr key={ci.id} className="border-b border-slate-800">
                      <td className="py-2 px-3"><div className="font-medium">{ci.item_name}</div><div className="text-slate-500 text-xs">{ci.item_code}</div></td>
                      <td className="py-2 px-3 font-mono">{ci.book_qty}</td>
                      <td className="py-2 px-3 font-mono">{ci.physical_qty}</td>
                      <td className={`py-2 px-3 font-mono font-semibold ${ci.variance < 0 ? "text-red-400" : ci.variance > 0 ? "text-green-400" : "text-slate-400"}`}>
                        {ci.variance > 0 ? "+" : ""}{ci.variance}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Variance Report ───────────────────────────────────────────────

function VarianceReportTab() {
  const [rows, setRows] = useState<VarianceRow[]>([]);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    const data = await apiGet<VarianceRow[]>("/inventory/variance-report").catch(() => []);
    setRows(data);
    setLoading(false);
  };
  useEffect(() => { load(); }, []);

  const mismatches = rows.filter(r => r.last_variance !== null && r.last_variance !== 0);

  return (
    <div className="space-y-5">
      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-lg font-semibold mb-1">Stock Variance Report</h3>
        <p className="text-slate-400 text-sm">
          Shows book stock vs physical count vs total movement for every item.
          Items with a non-zero variance (like the pipe offcut scenario) appear highlighted in red.
          Run a cycle count and approve it to resolve mismatches.
        </p>
      </div>

      {mismatches.length > 0 && (
        <div className="rounded-2xl bg-red-500/10 border border-red-500/30 p-4">
          <p className="text-red-400 font-semibold text-sm">{mismatches.length} item{mismatches.length > 1 ? "s" : ""} with unresolved variance — review and run a cycle count to correct.</p>
        </div>
      )}

      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold">All Items</h3>
          <button onClick={load} disabled={loading} className="text-sm text-slate-400 border border-slate-700 rounded-xl px-4 py-1.5 hover:border-slate-500">
            {loading ? "Loading…" : "Refresh"}
          </button>
        </div>
        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full min-w-[900px] text-left text-sm">
            <thead className="text-slate-400 border-b border-slate-800">
              <tr>{["Item", "Category", "Book Stock", "Total In", "Total Out", "Last Count", "Variance", "Status"].map(h => <th key={h} className="py-3 px-4">{h}</th>)}</tr>
            </thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.item_id} className={`border-b border-slate-800 ${r.last_variance !== null && r.last_variance !== 0 ? "bg-red-500/5" : ""}`}>
                  <td className="py-3 px-4"><div className="font-medium">{r.item_name}</div><div className="text-slate-500 text-xs">{r.item_code}</div></td>
                  <td className="py-3 px-4 text-slate-400 text-xs">{r.category}</td>
                  <td className="py-3 px-4 font-mono font-semibold">{r.book_stock} {r.unit}</td>
                  <td className="py-3 px-4 font-mono text-green-400">+{r.total_received}</td>
                  <td className="py-3 px-4 font-mono text-red-400">-{r.total_issued}</td>
                  <td className="py-3 px-4 font-mono">{r.last_physical_count ?? "—"}</td>
                  <td className={`py-3 px-4 font-mono font-semibold ${(r.last_variance ?? 0) < 0 ? "text-red-400" : (r.last_variance ?? 0) > 0 ? "text-green-400" : "text-slate-400"}`}>
                    {r.last_variance !== null ? `${r.last_variance > 0 ? "+" : ""}${r.last_variance}` : "—"}
                  </td>
                  <td className="py-3 px-4">{statusBadge(r.status)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// ── CSV Import ────────────────────────────────────────────────────

function ImportCSVTab() {
  const [result, setResult] = useState<{ created: number; updated: number; skipped: number; errors: string[] } | null>(null);
  const [loading, setLoading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  async function handleUpload(e: React.FormEvent) {
    e.preventDefault();
    const file = fileRef.current?.files?.[0];
    if (!file) return;
    setLoading(true);
    const fd = new FormData();
    fd.append("file", file);
    try {
      const res = await fetch(`${API_URL}/inventory/import-csv`, {
        method: "POST",
        headers: { Authorization: `Bearer ${localStorage.getItem("token")}` },
        body: fd,
      });
      const data = await res.json();
      setResult(data);
    } catch {
      setResult({ created: 0, updated: 0, skipped: 0, errors: ["Upload failed — check connection"] });
    }
    setLoading(false);
  }

  return (
    <div className="space-y-5">
      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-lg font-semibold mb-1">Import from Tally / Excel CSV</h3>
        <p className="text-slate-400 text-sm mb-3">
          Export your stock ledger or item master from Tally as CSV and upload here. FlowMES accepts both Tally column names and generic names.
          Existing items are updated; new ones are created.
        </p>
        <div className="bg-slate-950 rounded-xl border border-slate-800 p-4 text-xs font-mono text-slate-400">
          <p className="text-slate-300 mb-2">Accepted column names (case-insensitive):</p>
          <p>item_code / Item Code &nbsp;·&nbsp; item_name / Item Name &nbsp;·&nbsp; category / Category</p>
          <p>unit / Unit &nbsp;·&nbsp; current_stock / Opening Stock / Stock &nbsp;·&nbsp; reorder_level / Reorder Level</p>
          <p>supplier / Supplier &nbsp;·&nbsp; location / Location</p>
        </div>
      </div>

      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-base font-semibold mb-1">Sample CSV format</h3>
        <div className="overflow-x-auto rounded-xl border border-slate-800 mt-2">
          <table className="w-full text-xs font-mono text-slate-400">
            <thead className="border-b border-slate-800 text-slate-300">
              <tr>{["item_code","item_name","category","unit","current_stock","reorder_level","supplier","location"].map(h => <th key={h} className="py-2 px-3 text-left">{h}</th>)}</tr>
            </thead>
            <tbody>
              <tr className="border-b border-slate-800"><td className="py-1.5 px-3">PIPE-P1-100</td><td className="py-1.5 px-3">GI Pipe 1 inch</td><td className="py-1.5 px-3">Raw Material</td><td className="py-1.5 px-3">m</td><td className="py-1.5 px-3">100</td><td className="py-1.5 px-3">20</td><td className="py-1.5 px-3">Tata Steel</td><td className="py-1.5 px-3">RM Store A</td></tr>
              <tr><td className="py-1.5 px-3">COMP-AIR-46</td><td className="py-1.5 px-3">Compressor Oil 46</td><td className="py-1.5 px-3">Consumables</td><td className="py-1.5 px-3">L</td><td className="py-1.5 px-3">200</td><td className="py-1.5 px-3">50</td><td className="py-1.5 px-3">Castrol India</td><td className="py-1.5 px-3">Lube Store</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <form onSubmit={handleUpload} className="rounded-2xl bg-slate-900 border border-slate-800 p-5 flex items-center gap-4">
        <input ref={fileRef} type="file" accept=".csv" className="text-sm text-slate-300 file:mr-3 file:rounded-lg file:border-0 file:bg-slate-800 file:text-slate-300 file:px-4 file:py-2 file:text-sm hover:file:bg-slate-700" />
        <button type="submit" disabled={loading} className="rounded-xl bg-white text-slate-950 font-semibold px-5 py-2 text-sm whitespace-nowrap">
          {loading ? "Importing…" : "Upload & Import"}
        </button>
      </form>

      {result && (
        <div className={`rounded-2xl border p-5 ${result.errors.length > 0 ? "border-yellow-500/30 bg-yellow-500/5" : "border-green-500/30 bg-green-500/5"}`}>
          <p className="font-semibold text-sm mb-2">{result.errors.length === 0 ? "Import complete" : "Import completed with warnings"}</p>
          <div className="flex gap-6 text-sm">
            <span className="text-green-400">Created: {result.created}</span>
            <span className="text-blue-400">Updated: {result.updated}</span>
            <span className="text-slate-400">Skipped: {result.skipped}</span>
          </div>
          {result.errors.length > 0 && (
            <div className="mt-3 space-y-1">
              {result.errors.map((err, i) => <p key={i} className="text-yellow-400 text-xs font-mono">{err}</p>)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
