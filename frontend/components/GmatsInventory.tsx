"use client";
import React, { useEffect, useState } from "react";
import { apiGet, apiPost, apiPatch } from "../lib/api";

// ── Types ─────────────────────────────────────────────────────────

interface GItem {
  id: number; item_code: string; item_name: string; category: string; unit: string;
  physical_stock: number; reserved_stock: number; available_stock: number;
  reorder_level: number; purchase_rate: number; location: string | null;
  supplier: string | null; aliases: string[]; reorder_needed: boolean;
}

interface Summary {
  items: number; total_physical: number; total_reserved: number;
  total_available: number; reorder_needed: number; open_proformas: number;
}

interface ProformaLine { item_id: number; item_name: string; qty: number; }
interface Proforma {
  id: number; proforma_no: string; customer_name: string; status: string;
  created_at: string; lines: ProformaLine[];
}
interface Invoice {
  id: number; invoice_no: string; proforma_id: number | null;
  customer_name: string; status: string; created_at: string;
}
interface MIN {
  id: number; min_no: string; customer_name: string; machine_ref: string;
  status: string; created_at: string; lines: ProformaLine[];
}

const TABS = ["Stock", "Proforma (Reserve)", "Tax Invoice", "Free Spares (MIN)", "Reorder Alerts"] as const;
type Tab = typeof TABS[number];

// ── Main ──────────────────────────────────────────────────────────

export default function GmatsInventory({ tenant = "GMATS" }: { tenant?: string }) {
  const [tab, setTab] = useState<Tab>("Stock");
  const [items, setItems] = useState<GItem[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);

  const loadItems = () => {
    apiGet<GItem[]>(`/gmats/items?tenant=${tenant}`).then(setItems).catch(() => {});
    apiGet<Summary>(`/gmats/summary?tenant=${tenant}`).then(setSummary).catch(() => {});
  };
  useEffect(() => { loadItems(); }, [tenant]);

  return (
    <section className="mt-8 space-y-6">
      <div>
        <div className="flex items-center gap-3">
          <h2 className="text-3xl font-bold">GMATS Compressors — Inventory</h2>
          <span className="rounded-lg bg-indigo-500/20 border border-indigo-500/40 px-3 py-1 text-xs text-indigo-300 font-semibold tracking-wider">
            COMPANY: GMATS
          </span>
        </div>
        <p className="text-slate-400 mt-2 text-sm">
          4-bucket stock (Physical · Reserved · Available) · item aliases · Proforma reservation · Tax-invoice deduction · free-spares issue
        </p>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-6 gap-3">
        <Kpi title="Items" value={summary?.items ?? 0} />
        <Kpi title="Physical" value={summary?.total_physical ?? 0} />
        <Kpi title="Reserved" value={summary?.total_reserved ?? 0} accent="yellow" />
        <Kpi title="Available" value={summary?.total_available ?? 0} accent="green" />
        <Kpi title="Reorder Needed" value={summary?.reorder_needed ?? 0} accent="red" />
        <Kpi title="Open Proformas" value={summary?.open_proformas ?? 0} accent="blue" />
      </div>

      <div className="flex flex-wrap gap-2 border-b border-slate-800 pb-3">
        {TABS.map((t) => (
          <button key={t} onClick={() => setTab(t)}
            className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-colors ${
              tab === t ? "bg-white text-slate-950" : "text-slate-400 hover:text-white hover:bg-slate-800"
            }`}>
            {t}
          </button>
        ))}
      </div>

      {tab === "Stock"              && <StockTab tenant={tenant} items={items} reload={loadItems} />}
      {tab === "Proforma (Reserve)" && <ProformaTab tenant={tenant} items={items} reload={loadItems} />}
      {tab === "Tax Invoice"        && <InvoiceTab tenant={tenant} />}
      {tab === "Free Spares (MIN)"  && <MinTab tenant={tenant} items={items} reload={loadItems} />}
      {tab === "Reorder Alerts"     && <ReorderTab items={items} />}
    </section>
  );
}

function Kpi({ title, value, accent }: { title: string; value: number; accent?: string }) {
  const color =
    accent === "yellow" ? "text-yellow-400" :
    accent === "green"  ? "text-green-400"  :
    accent === "red"    ? "text-red-400"    :
    accent === "blue"   ? "text-blue-400"   : "text-white";
  return (
    <div className="rounded-2xl bg-slate-900 border border-slate-800 p-4">
      <p className="text-slate-400 text-xs">{title}</p>
      <h3 className={`text-2xl font-bold mt-1 ${color}`}>{value}</h3>
    </div>
  );
}

// ── Stock tab ─────────────────────────────────────────────────────

function StockTab({ tenant, items, reload }: { tenant: string; items: GItem[]; reload: () => void }) {
  const [resolveInput, setResolveInput] = useState("");
  const [resolveResult, setResolveResult] = useState<string | null>(null);
  const [stockInId, setStockInId] = useState<number | null>(null);
  const [stockInQty, setStockInQty] = useState("");

  async function resolve() {
    if (!resolveInput.trim()) return;
    try {
      const res = await apiGet<any>(`/gmats/resolve?tenant=${tenant}&name=${encodeURIComponent(resolveInput)}`);
      if (res.matched) {
        setResolveResult(`✓ "${resolveInput}" → ${res.item.item_name} (${res.item.item_code}) · matched via ${res.via} · available ${res.item.available_stock} ${res.item.unit}`);
      } else {
        setResolveResult(`✗ "${resolveInput}" — no matching item or alias found`);
      }
    } catch { setResolveResult("Lookup failed"); }
  }

  async function doStockIn(id: number) {
    if (!stockInQty) return;
    await apiPost(`/gmats/items/${id}/stock-in`, { qty: Number(stockInQty) });
    setStockInId(null); setStockInQty("");
    reload();
  }

  return (
    <div className="space-y-5">
      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-lg font-semibold mb-1">Alias lookup — many names, one item</h3>
        <p className="text-slate-400 text-sm mb-3">
          Different people call the same part different names. Type any alias, code, or name (e.g. <span className="text-indigo-300">GI Coupler 1"</span>, <span className="text-indigo-300">Screw Oil 46</span>) — the system maps it to one master stock item.
        </p>
        <div className="flex gap-2">
          <input
            className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm flex-1"
            placeholder='Try "GI Coupler 1\"" or "Comp Oil 46"'
            value={resolveInput}
            onChange={(e) => setResolveInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && resolve()}
          />
          <button onClick={resolve} className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-2 text-sm">Resolve</button>
        </div>
        {resolveResult && (
          <p className={`mt-3 text-sm font-mono ${resolveResult.startsWith("✓") ? "text-green-400" : "text-red-400"}`}>{resolveResult}</p>
        )}
      </div>

      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-lg font-semibold mb-4">Stock Master <span className="text-slate-500 text-sm font-normal">— Physical · Reserved · Available</span></h3>
        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full min-w-[1000px] text-left text-sm">
            <thead className="text-slate-400 border-b border-slate-800">
              <tr>
                {["Code", "Item & Aliases", "Category", "Physical", "Reserved", "Available", "Reorder", "Status", "Stock In"].map(h => (
                  <th key={h} className="py-3 px-4">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.length === 0 && <tr><td colSpan={9} className="py-6 px-4 text-slate-400">No GMATS items yet.</td></tr>}
              {items.map((it) => (
                <tr key={it.id} className="border-b border-slate-800 align-top">
                  <td className="py-3 px-4 font-mono text-indigo-300">{it.item_code}</td>
                  <td className="py-3 px-4">
                    <div className="font-medium">{it.item_name}</div>
                    {it.aliases.length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-1">
                        {it.aliases.map((a) => (
                          <span key={a} className="text-[10px] text-slate-400 bg-slate-800 border border-slate-700 rounded px-1.5 py-0.5">{a}</span>
                        ))}
                      </div>
                    )}
                  </td>
                  <td className="py-3 px-4 text-slate-400 text-xs">{it.category}</td>
                  <td className="py-3 px-4 font-mono">{it.physical_stock} {it.unit}</td>
                  <td className="py-3 px-4 font-mono text-yellow-400">{it.reserved_stock}</td>
                  <td className="py-3 px-4 font-mono font-semibold text-green-400">{it.available_stock}</td>
                  <td className="py-3 px-4 font-mono text-slate-400">{it.reorder_level}</td>
                  <td className="py-3 px-4">
                    {it.reorder_needed
                      ? <span className="rounded-full px-2 py-0.5 text-xs border border-red-500/40 bg-red-500/10 text-red-400">Purchase Req.</span>
                      : <span className="rounded-full px-2 py-0.5 text-xs border border-green-500/40 bg-green-500/10 text-green-400">Healthy</span>}
                  </td>
                  <td className="py-3 px-4">
                    {stockInId === it.id ? (
                      <div className="flex gap-1">
                        <input className="w-16 bg-slate-950 border border-slate-700 rounded-lg px-2 py-1 text-sm" type="number" placeholder="Qty" value={stockInQty} onChange={(e) => setStockInQty(e.target.value)} autoFocus />
                        <button onClick={() => doStockIn(it.id)} className="text-xs text-green-400 border border-green-500/30 rounded-lg px-2 py-1 hover:bg-green-500/10">Add</button>
                      </div>
                    ) : (
                      <button onClick={() => { setStockInId(it.id); setStockInQty(""); }} className="text-xs text-slate-300 border border-slate-700 rounded-lg px-2 py-1 hover:border-slate-500">+ Stock</button>
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

// ── Proforma tab ──────────────────────────────────────────────────

function ProformaTab({ tenant, items, reload }: { tenant: string; items: GItem[]; reload: () => void }) {
  const [rows, setRows] = useState<Proforma[]>([]);
  const [customer, setCustomer] = useState("");
  const [lines, setLines] = useState([{ item_id: "", qty: "" }]);
  const [err, setErr] = useState("");

  const load = () => apiGet<Proforma[]>(`/gmats/proformas?tenant=${tenant}`).then(setRows).catch(() => {});
  useEffect(() => { load(); }, [tenant]);

  const addLine = () => setLines((p) => [...p, { item_id: "", qty: "" }]);
  const setLine = (i: number, f: string, v: string) => setLines((p) => p.map((r, idx) => idx === i ? { ...r, [f]: v } : r));

  async function submit(e: React.FormEvent) {
    e.preventDefault(); setErr("");
    try {
      await apiPost(`/gmats/proformas`, {
        tenant, customer_name: customer,
        lines: lines.filter(l => l.item_id && l.qty).map(l => ({ item_id: Number(l.item_id), qty: Number(l.qty) })),
      });
      setCustomer(""); setLines([{ item_id: "", qty: "" }]);
      load(); reload();
    } catch (e: any) { setErr(e.message || "Failed to create proforma"); }
  }

  async function generateInvoice(pid: number) {
    try { await apiPost(`/gmats/proformas/${pid}/invoice`, {}); load(); reload(); }
    catch (e: any) { setErr(e.message); }
  }
  async function cancel(pid: number) {
    try { await apiPatch(`/gmats/proformas/${pid}/cancel`, {}); load(); reload(); }
    catch (e: any) { setErr(e.message); }
  }

  const statusBadge = (s: string) => {
    const m: Record<string, string> = {
      Open: "text-yellow-400 border-yellow-500/40 bg-yellow-500/10",
      Invoiced: "text-green-400 border-green-500/40 bg-green-500/10",
      Cancelled: "text-slate-400 border-slate-600/40 bg-slate-600/10",
    };
    return <span className={`rounded-full px-2 py-0.5 text-xs border ${m[s]}`}>{s}</span>;
  };

  return (
    <div className="space-y-5">
      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-lg font-semibold mb-1">Proforma Invoice → reserves stock (prevents double-selling)</h3>
        <p className="text-slate-400 text-sm">
          When Sales raises a Proforma, the quantity moves from <span className="text-green-400">Available</span> into <span className="text-yellow-400">Reserved</span>. Physical stock is untouched until the Tax Invoice is generated.
        </p>
      </div>

      <form onSubmit={submit} className="rounded-2xl bg-slate-900 border border-slate-800 p-5 space-y-4">
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm w-full md:w-80" placeholder="Customer name" value={customer} onChange={(e) => setCustomer(e.target.value)} required />
        <div className="space-y-2">
          {lines.map((l, i) => (
            <div key={i} className="grid grid-cols-3 gap-2">
              <select className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm col-span-2" value={l.item_id} onChange={(e) => setLine(i, "item_id", e.target.value)}>
                <option value="">Select item</option>
                {items.map((it) => <option key={it.id} value={it.id}>{it.item_code} – {it.item_name} (avail {it.available_stock})</option>)}
              </select>
              <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" type="number" placeholder="Qty" value={l.qty} onChange={(e) => setLine(i, "qty", e.target.value)} />
            </div>
          ))}
        </div>
        <div className="flex gap-3">
          <button type="button" onClick={addLine} className="text-sm text-slate-400 border border-slate-700 rounded-xl px-4 py-2 hover:border-slate-500">+ Add Line</button>
          <button type="submit" className="rounded-xl bg-white text-slate-950 font-semibold px-5 py-2 text-sm">Create Proforma (Reserve)</button>
        </div>
        {err && <p className="text-red-400 text-sm">{err}</p>}
      </form>

      <div className="space-y-3">
        {rows.map((p) => (
          <div key={p.id} className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-3">
                <span className="font-mono font-semibold text-indigo-300">{p.proforma_no}</span>
                {statusBadge(p.status)}
                <span className="text-slate-400 text-sm">{p.customer_name}</span>
              </div>
              {p.status === "Open" && (
                <div className="flex gap-2">
                  <button onClick={() => generateInvoice(p.id)} className="text-sm text-green-400 border border-green-500/30 rounded-xl px-4 py-1.5 hover:bg-green-500/10 font-semibold">Generate Tax Invoice →</button>
                  <button onClick={() => cancel(p.id)} className="text-sm text-red-400 border border-red-500/30 rounded-xl px-3 py-1.5 hover:bg-red-500/10">Cancel</button>
                </div>
              )}
            </div>
            <div className="text-sm text-slate-400">
              {p.lines.map((l, i) => <span key={i} className="mr-3">{l.item_name} × {l.qty}</span>)}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Tax Invoice tab ───────────────────────────────────────────────

function InvoiceTab({ tenant }: { tenant: string }) {
  const [rows, setRows] = useState<Invoice[]>([]);
  useEffect(() => { apiGet<Invoice[]>(`/gmats/invoices?tenant=${tenant}`).then(setRows).catch(() => {}); }, [tenant]);

  return (
    <div className="space-y-5">
      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-lg font-semibold mb-1">Tax Invoice → final stock deduction</h3>
        <p className="text-slate-400 text-sm">
          Generating the Tax Invoice (from the Proforma tab) deducts the reserved quantity from <span className="text-white">Physical</span> stock and clears the reservation. This is the only step that reduces real inventory on a sale.
        </p>
      </div>
      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-lg font-semibold mb-4">Generated Invoices</h3>
        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full text-left text-sm">
            <thead className="text-slate-400 border-b border-slate-800">
              <tr>{["Invoice No", "From Proforma", "Customer", "Status", "Date"].map(h => <th key={h} className="py-3 px-4">{h}</th>)}</tr>
            </thead>
            <tbody>
              {rows.length === 0 && <tr><td colSpan={5} className="py-6 px-4 text-slate-400">No invoices yet — generate one from the Proforma tab.</td></tr>}
              {rows.map((v) => (
                <tr key={v.id} className="border-b border-slate-800">
                  <td className="py-3 px-4 font-mono font-semibold text-green-400">{v.invoice_no}</td>
                  <td className="py-3 px-4 font-mono text-slate-400">PI #{v.proforma_id ?? "-"}</td>
                  <td className="py-3 px-4">{v.customer_name}</td>
                  <td className="py-3 px-4"><span className="rounded-full px-2 py-0.5 text-xs border border-green-500/40 bg-green-500/10 text-green-400">{v.status}</span></td>
                  <td className="py-3 px-4 text-slate-400 text-xs">{new Date(v.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// ── Material Issue Note (free spares) ─────────────────────────────

function MinTab({ tenant, items, reload }: { tenant: string; items: GItem[]; reload: () => void }) {
  const [rows, setRows] = useState<MIN[]>([]);
  const [customer, setCustomer] = useState("");
  const [machine, setMachine] = useState("");
  const [lines, setLines] = useState([{ item_id: "", qty: "" }]);
  const [err, setErr] = useState("");

  const load = () => apiGet<MIN[]>(`/gmats/min?tenant=${tenant}`).then(setRows).catch(() => {});
  useEffect(() => { load(); }, [tenant]);

  const addLine = () => setLines((p) => [...p, { item_id: "", qty: "" }]);
  const setLine = (i: number, f: string, v: string) => setLines((p) => p.map((r, idx) => idx === i ? { ...r, [f]: v } : r));

  function prefillExample() {
    setCustomer("ABC Pvt Ltd");
    setMachine("20 HP Screw Compressor");
    const af = items.find(i => i.item_code === "AF-001");
    const of = items.find(i => i.item_code === "OF-001");
    const oil = items.find(i => i.item_code === "OIL-46");
    setLines([
      { item_id: af ? String(af.id) : "", qty: "1" },
      { item_id: of ? String(of.id) : "", qty: "1" },
      { item_id: oil ? String(oil.id) : "", qty: "5" },
    ]);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault(); setErr("");
    try {
      await apiPost(`/gmats/min`, {
        tenant, customer_name: customer, machine_ref: machine,
        lines: lines.filter(l => l.item_id && l.qty).map(l => ({ item_id: Number(l.item_id), qty: Number(l.qty) })),
      });
      setCustomer(""); setMachine(""); setLines([{ item_id: "", qty: "" }]);
      load(); reload();
    } catch (e: any) { setErr(e.message || "Failed to create MIN"); }
  }

  return (
    <div className="space-y-5">
      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <h3 className="text-lg font-semibold mb-1">Material Issue Note → free spares with a machine</h3>
            <p className="text-slate-400 text-sm">
              When a compressor ships with free Air Filter, Oil Filter and Oil, those spares still leave the store. A MIN deducts them from Physical stock <strong className="text-white">even though they're not billed</strong> — solving the missing-stock problem.
            </p>
          </div>
          <button onClick={prefillExample} className="text-sm text-indigo-300 border border-indigo-500/30 rounded-xl px-4 py-2 hover:bg-indigo-500/10 whitespace-nowrap">
            Fill GMATS example
          </button>
        </div>
      </div>

      <form onSubmit={submit} className="rounded-2xl bg-slate-900 border border-slate-800 p-5 space-y-4">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" placeholder="Customer name" value={customer} onChange={(e) => setCustomer(e.target.value)} required />
          <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" placeholder="Machine sold (e.g. 20 HP Screw Compressor)" value={machine} onChange={(e) => setMachine(e.target.value)} />
        </div>
        <div className="space-y-2">
          {lines.map((l, i) => (
            <div key={i} className="grid grid-cols-3 gap-2">
              <select className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm col-span-2" value={l.item_id} onChange={(e) => setLine(i, "item_id", e.target.value)}>
                <option value="">Select free spare</option>
                {items.map((it) => <option key={it.id} value={it.id}>{it.item_code} – {it.item_name} (phys {it.physical_stock})</option>)}
              </select>
              <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" type="number" placeholder="Qty" value={l.qty} onChange={(e) => setLine(i, "qty", e.target.value)} />
            </div>
          ))}
        </div>
        <div className="flex gap-3">
          <button type="button" onClick={addLine} className="text-sm text-slate-400 border border-slate-700 rounded-xl px-4 py-2 hover:border-slate-500">+ Add Spare</button>
          <button type="submit" className="rounded-xl bg-white text-slate-950 font-semibold px-5 py-2 text-sm">Issue Free Spares (Deduct Stock)</button>
        </div>
        {err && <p className="text-red-400 text-sm">{err}</p>}
      </form>

      <div className="space-y-3">
        {rows.map((m) => (
          <div key={m.id} className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
            <div className="flex items-center gap-3 mb-2">
              <span className="font-mono font-semibold text-indigo-300">{m.min_no}</span>
              <span className="rounded-full px-2 py-0.5 text-xs border border-green-500/40 bg-green-500/10 text-green-400">{m.status}</span>
              <span className="text-slate-400 text-sm">{m.customer_name}</span>
              {m.machine_ref && <span className="text-slate-500 text-xs">with {m.machine_ref}</span>}
            </div>
            <div className="text-sm text-slate-400">
              {m.lines.map((l, i) => <span key={i} className="mr-3">{l.item_name} × {l.qty}</span>)}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Reorder Alerts ────────────────────────────────────────────────

function ReorderTab({ items }: { items: GItem[] }) {
  const flagged = items.filter((i) => i.reorder_needed);
  return (
    <div className="space-y-5">
      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-lg font-semibold mb-1">Reorder Alerts</h3>
        <p className="text-slate-400 text-sm">
          Any item whose <span className="text-green-400">Available</span> stock has fallen to or below its minimum (reorder) level is flagged <span className="text-red-400">Purchase Required</span>.
        </p>
      </div>
      {flagged.length === 0 ? (
        <p className="text-slate-400 text-sm">All items above reorder level. Nothing to purchase.</p>
      ) : (
        <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
          <div className="overflow-x-auto rounded-xl border border-slate-800">
            <table className="w-full text-left text-sm">
              <thead className="text-slate-400 border-b border-slate-800">
                <tr>{["Item", "Available", "Reorder Level", "Suggested Supplier", "Action"].map(h => <th key={h} className="py-3 px-4">{h}</th>)}</tr>
              </thead>
              <tbody>
                {flagged.map((it) => (
                  <tr key={it.id} className="border-b border-slate-800 bg-red-500/5">
                    <td className="py-3 px-4"><div className="font-medium">{it.item_name}</div><div className="text-slate-500 text-xs">{it.item_code}</div></td>
                    <td className="py-3 px-4 font-mono text-red-400 font-semibold">{it.available_stock} {it.unit}</td>
                    <td className="py-3 px-4 font-mono text-slate-400">{it.reorder_level}</td>
                    <td className="py-3 px-4 text-slate-400">{it.supplier || "—"}</td>
                    <td className="py-3 px-4"><span className="rounded-full px-3 py-1 text-xs border border-red-500/40 bg-red-500/10 text-red-400">⚠ Purchase Required</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
