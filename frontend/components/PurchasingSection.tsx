import type { InventoryItem } from "../lib/phase13-types";
import type { PurchaseOrder, PurchasingAnalytics, Supplier } from "../lib/phase18-types";

function statusStyle(status: string) {
  switch (status) {
    case "Received":
      return "border-green-500/40 bg-green-500/10 text-green-300";
    case "Partial":
      return "border-blue-500/40 bg-blue-500/10 text-blue-300";
    case "Cancelled":
      return "border-slate-500/40 bg-slate-500/10 text-slate-300";
    default:
      return "border-yellow-500/40 bg-yellow-500/10 text-yellow-300";
  }
}

export default function PurchasingSection({
  suppliers,
  purchaseOrders,
  inventoryItems,
  analytics,
  supplierForm,
  setSupplierForm,
  poForm,
  setPoForm,
  createSupplier,
  updateSupplier,
  deleteSupplier,
  createPurchaseOrder,
  updatePurchaseOrder,
  deletePurchaseOrder,
  generateOverdueEscalations,
}: {
  suppliers: Supplier[];
  purchaseOrders: PurchaseOrder[];
  inventoryItems: InventoryItem[];
  analytics: PurchasingAnalytics | null;
  supplierForm: {
    supplier_code: string;
    supplier_name: string;
    contact_person: string;
    email: string;
    phone: string;
    category: string;
    status: string;
  };
  setSupplierForm: (value: any) => void;
  poForm: {
    po_no: string;
    supplier_id: string;
    item_id: string;
    item_name: string;
    order_quantity: number;
    received_quantity: number;
    unit: string;
    expected_delivery_date: string;
    status: string;
    notes: string;
  };
  setPoForm: (value: any) => void;
  createSupplier: (e: React.FormEvent) => void;
  updateSupplier: (id: number, status: string) => void;
  deleteSupplier?: (id: number) => void;
  createPurchaseOrder: (e: React.FormEvent) => void;
  updatePurchaseOrder: (id: number, receivedQty: number, status?: string) => void;
  deletePurchaseOrder?: (id: number) => void;
  generateOverdueEscalations: () => void;
}) {
  function getSupplierName(id: number) {
    return suppliers.find((row) => row.id === id)?.supplier_name || `Supplier ${id}`;
  }

  return (
    <section className="mt-8 space-y-6">
      <div className="flex flex-col md:flex-row md:items-end md:justify-between gap-4">
        <div>
          <h2 className="text-3xl font-bold">Purchasing & Suppliers</h2>
          <p className="text-slate-400 mt-2">
            Manage suppliers, purchase orders, expected delivery and material receiving.
          </p>
        </div>

        <button
          type="button"
          onClick={generateOverdueEscalations}
          className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3"
        >
          Generate Overdue PO Escalations
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 xl:grid-cols-9 gap-4">
        <Kpi title="Suppliers" value={analytics?.suppliers ?? 0} />
        <Kpi title="POs" value={analytics?.purchase_orders ?? 0} />
        <Kpi title="Open" value={analytics?.open ?? 0} />
        <Kpi title="Partial" value={analytics?.partial ?? 0} />
        <Kpi title="Received" value={analytics?.received ?? 0} />
        <Kpi title="Overdue" value={analytics?.overdue ?? 0} />
        <Kpi title="Ordered" value={analytics?.ordered_qty ?? 0} />
        <Kpi title="Received Qty" value={analytics?.received_qty ?? 0} />
        <Kpi title="Receipt Rate" value={`${analytics?.receipt_rate ?? 0}%`} />
      </div>

      <form
        onSubmit={createSupplier}
        className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-1 md:grid-cols-4 xl:grid-cols-8 gap-4"
      >
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Supplier Code" value={supplierForm.supplier_code} onChange={(e) => setSupplierForm({ ...supplierForm, supplier_code: e.target.value })} required />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Supplier Name" value={supplierForm.supplier_name} onChange={(e) => setSupplierForm({ ...supplierForm, supplier_name: e.target.value })} required />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Contact" value={supplierForm.contact_person} onChange={(e) => setSupplierForm({ ...supplierForm, contact_person: e.target.value })} />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Email" value={supplierForm.email} onChange={(e) => setSupplierForm({ ...supplierForm, email: e.target.value })} />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Phone" value={supplierForm.phone} onChange={(e) => setSupplierForm({ ...supplierForm, phone: e.target.value })} />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Category" value={supplierForm.category} onChange={(e) => setSupplierForm({ ...supplierForm, category: e.target.value })} />
        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={supplierForm.status} onChange={(e) => setSupplierForm({ ...supplierForm, status: e.target.value })}>
          <option>Active</option>
          <option>On Hold</option>
          <option>Inactive</option>
        </select>
        <button type="submit" className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3">Add Supplier</button>
      </form>

      <form
        onSubmit={createPurchaseOrder}
        className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-1 md:grid-cols-4 xl:grid-cols-9 gap-4"
      >
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="PO No" value={poForm.po_no} onChange={(e) => setPoForm({ ...poForm, po_no: e.target.value })} required />
        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={poForm.supplier_id} onChange={(e) => setPoForm({ ...poForm, supplier_id: e.target.value })} required>
          <option value="">Supplier</option>
          {suppliers.map((supplier) => <option key={supplier.id} value={supplier.id}>{supplier.supplier_name}</option>)}
        </select>
        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={poForm.item_id} onChange={(e) => {
          const item = inventoryItems.find((row) => String(row.id) === e.target.value);
          setPoForm({ ...poForm, item_id: e.target.value, item_name: item?.item_name || poForm.item_name, unit: item?.unit || poForm.unit });
        }}>
          <option value="">Inventory Item</option>
          {inventoryItems.map((item) => <option key={item.id} value={item.id}>{item.item_code} - {item.item_name}</option>)}
        </select>
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Item Name" value={poForm.item_name} onChange={(e) => setPoForm({ ...poForm, item_name: e.target.value })} required />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="number" placeholder="Order Qty" value={poForm.order_quantity} onChange={(e) => setPoForm({ ...poForm, order_quantity: Number(e.target.value) })} required />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Unit" value={poForm.unit} onChange={(e) => setPoForm({ ...poForm, unit: e.target.value })} required />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="date" value={poForm.expected_delivery_date} onChange={(e) => setPoForm({ ...poForm, expected_delivery_date: e.target.value })} required />
        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={poForm.status} onChange={(e) => setPoForm({ ...poForm, status: e.target.value })}>
          <option>Open</option>
          <option>Partial</option>
          <option>Received</option>
          <option>Cancelled</option>
        </select>
        <button type="submit" className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3">Create PO</button>
      </form>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
          <h3 className="text-2xl font-semibold mb-4">Suppliers</h3>
          <div className="overflow-x-auto rounded-xl border border-slate-800">
            <table className="w-full min-w-[760px] text-left text-sm">
              <thead className="text-slate-400 border-b border-slate-800">
                <tr>
                  <th className="py-3 px-4">Code</th>
                  <th className="py-3 px-4">Name</th>
                  <th className="py-3 px-4">Contact</th>
                  <th className="py-3 px-4">Category</th>
                  <th className="py-3 px-4">Status</th>
                  <th className="py-3 px-4">Actions</th>
                </tr>
              </thead>
              <tbody>
                {suppliers.map((row) => (
                  <tr key={row.id} className="border-b border-slate-800">
                    <td className="py-3 px-4 font-semibold">{row.supplier_code}</td>
                    <td className="py-3 px-4">{row.supplier_name}</td>
                    <td className="py-3 px-4">{row.contact_person || "-"}</td>
                    <td className="py-3 px-4">{row.category || "-"}</td>
                    <td className="py-3 px-4">
                      <select className="bg-slate-950 border border-slate-700 rounded-lg px-2 py-1" value={row.status} onChange={(e) => updateSupplier(row.id, e.target.value)}>
                        <option>Active</option>
                        <option>On Hold</option>
                        <option>Inactive</option>
                      </select>
                    </td>
                    <td className="py-3 px-4">
                      <button onClick={() => deleteSupplier?.(row.id)} className="text-red-400 border border-red-500/40 rounded-lg px-3 py-1 hover:bg-red-500/10">Delete</button>
                    </td>
                  </tr>
                ))}
                {suppliers.length === 0 && <tr><td colSpan={6} className="py-6 px-4 text-slate-400">No suppliers yet.</td></tr>}
              </tbody>
            </table>
          </div>
        </div>

        <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
          <h3 className="text-2xl font-semibold mb-4">Purchase Orders</h3>
          <div className="overflow-x-auto rounded-xl border border-slate-800">
            <table className="w-full min-w-[900px] text-left text-sm">
              <thead className="text-slate-400 border-b border-slate-800">
                <tr>
                  <th className="py-3 px-4">PO</th>
                  <th className="py-3 px-4">Supplier</th>
                  <th className="py-3 px-4">Item</th>
                  <th className="py-3 px-4">Ordered</th>
                  <th className="py-3 px-4">Received</th>
                  <th className="py-3 px-4">Expected</th>
                  <th className="py-3 px-4">Status</th>
                  <th className="py-3 px-4">Actions</th>
                </tr>
              </thead>
              <tbody>
                {purchaseOrders.map((row) => {
                  const progress = row.order_quantity > 0 ? Math.min(Math.round((row.received_quantity / row.order_quantity) * 100), 100) : 0;
                  return (
                    <tr key={row.id} className="border-b border-slate-800">
                      <td className="py-3 px-4 font-semibold">{row.po_no}</td>
                      <td className="py-3 px-4">{getSupplierName(row.supplier_id)}</td>
                      <td className="py-3 px-4">{row.item_name}</td>
                      <td className="py-3 px-4">{row.order_quantity} {row.unit}</td>
                      <td className="py-3 px-4">
                        <input className="w-24 bg-slate-950 border border-slate-700 rounded-lg px-2 py-1" type="number" defaultValue={row.received_quantity} onBlur={(e) => updatePurchaseOrder(row.id, Number(e.target.value), undefined)} />
                        <p className="text-xs text-slate-500">{progress}%</p>
                      </td>
                      <td className="py-3 px-4">{row.expected_delivery_date}</td>
                      <td className="py-3 px-4">
                        <select className={`rounded-full px-3 py-1 text-xs border bg-slate-950 ${statusStyle(row.status)}`} value={row.status} onChange={(e) => updatePurchaseOrder(row.id, row.received_quantity, e.target.value)}>
                          <option>Open</option>
                          <option>Partial</option>
                          <option>Received</option>
                          <option>Cancelled</option>
                        </select>
                      </td>
                      <td className="py-3 px-4">
                        <button onClick={() => deletePurchaseOrder?.(row.id)} className="text-red-400 border border-red-500/40 rounded-lg px-3 py-1 hover:bg-red-500/10">Delete</button>
                      </td>
                    </tr>
                  );
                })}
                {purchaseOrders.length === 0 && <tr><td colSpan={8} className="py-6 px-4 text-slate-400">No purchase orders yet.</td></tr>}
              </tbody>
            </table>
          </div>
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
