import type { WorkOrder } from "../lib/phase9-types";
import type { ProductionPlan } from "../lib/phase11-types";
import type { CustomerOrder, CustomerOrderAnalytics } from "../lib/phase17-types";

function statusStyle(status: string) {
  switch (status) {
    case "Dispatched":
      return "border-green-500/40 bg-green-500/10 text-green-300";
    case "Partial":
      return "border-blue-500/40 bg-blue-500/10 text-blue-300";
    case "Cancelled":
      return "border-slate-500/40 bg-slate-500/10 text-slate-300";
    default:
      return "border-yellow-500/40 bg-yellow-500/10 text-yellow-300";
  }
}

function priorityStyle(priority: string) {
  switch (priority) {
    case "Critical":
      return "border-red-500/40 bg-red-500/10 text-red-300";
    case "High":
      return "border-orange-500/40 bg-orange-500/10 text-orange-300";
    case "Medium":
      return "border-yellow-500/40 bg-yellow-500/10 text-yellow-300";
    default:
      return "border-green-500/40 bg-green-500/10 text-green-300";
  }
}

export default function OrdersDispatchSection({
  workOrders,
  productionPlans,
  orders,
  analytics,
  form,
  setForm,
  createOrder,
  updateOrder,
  deleteOrder,
  generateLateOrderEscalations,
}: {
  workOrders: WorkOrder[];
  productionPlans: ProductionPlan[];
  orders: CustomerOrder[];
  analytics: CustomerOrderAnalytics | null;
  form: {
    order_no: string;
    customer_name: string;
    product_name: string;
    linked_work_order_id: string;
    linked_production_plan_id: string;
    order_quantity: number;
    dispatched_quantity: number;
    priority: string;
    due_date: string;
    status: string;
    notes: string;
  };
  setForm: (value: any) => void;
  createOrder: (e: React.FormEvent) => void;
  updateOrder: (id: number, dispatchedQty: number, status?: string, priority?: string) => void;
  deleteOrder: (id: number) => void;
  generateLateOrderEscalations: () => void;
}) {
  function getWorkOrderName(id?: number | null) {
    if (!id) return "-";
    return workOrders.find((wo) => wo.id === id)?.work_order_no || `WO ${id}`;
  }

  function getPlanName(id?: number | null) {
    if (!id) return "-";
    return productionPlans.find((plan) => plan.id === id)?.plan_no || `Plan ${id}`;
  }

  return (
    <section className="mt-8 space-y-6">
      <div className="flex flex-col md:flex-row md:items-end md:justify-between gap-4">
        <div>
          <h2 className="text-3xl font-bold">Orders & Dispatch</h2>
          <p className="text-slate-400 mt-2">
            Track customer demand, order priority, linked production and dispatch completion.
          </p>
        </div>

        <button
          type="button"
          onClick={generateLateOrderEscalations}
          className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3"
        >
          Generate Late Order Escalations
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 xl:grid-cols-9 gap-4">
        <Kpi title="Orders" value={analytics?.total_orders ?? 0} />
        <Kpi title="Pending" value={analytics?.pending ?? 0} />
        <Kpi title="Partial" value={analytics?.partial ?? 0} />
        <Kpi title="Dispatched" value={analytics?.dispatched ?? 0} />
        <Kpi title="Late" value={analytics?.late ?? 0} />
        <Kpi title="Order Qty" value={analytics?.total_order_qty ?? 0} />
        <Kpi title="Dispatch Qty" value={analytics?.total_dispatched_qty ?? 0} />
        <Kpi title="Dispatch Rate" value={`${analytics?.dispatch_rate ?? 0}%`} />
        <Kpi title="Cancelled" value={analytics?.cancelled ?? 0} />
      </div>

      <form
        onSubmit={createOrder}
        className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-1 md:grid-cols-3 xl:grid-cols-9 gap-4"
      >
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Order No" value={form.order_no} onChange={(e) => setForm({ ...form, order_no: e.target.value })} required />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Customer" value={form.customer_name} onChange={(e) => setForm({ ...form, customer_name: e.target.value })} required />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Product" value={form.product_name} onChange={(e) => setForm({ ...form, product_name: e.target.value })} required />

        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.linked_work_order_id} onChange={(e) => setForm({ ...form, linked_work_order_id: e.target.value })}>
          <option value="">Work Order</option>
          {workOrders.map((wo) => <option key={wo.id} value={wo.id}>{wo.work_order_no}</option>)}
        </select>

        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.linked_production_plan_id} onChange={(e) => setForm({ ...form, linked_production_plan_id: e.target.value })}>
          <option value="">Production Plan</option>
          {productionPlans.map((plan) => <option key={plan.id} value={plan.id}>{plan.plan_no}</option>)}
        </select>

        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="number" placeholder="Order Qty" value={form.order_quantity} onChange={(e) => setForm({ ...form, order_quantity: Number(e.target.value) })} required />

        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })}>
          <option>Critical</option>
          <option>High</option>
          <option>Medium</option>
          <option>Low</option>
        </select>

        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="date" value={form.due_date} onChange={(e) => setForm({ ...form, due_date: e.target.value })} required />

        <button type="submit" className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3">
          Create Order
        </button>
      </form>

      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-2xl font-semibold mb-4">Customer Orders</h3>

        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full min-w-[1250px] text-left text-sm">
            <thead className="text-slate-400 border-b border-slate-800">
              <tr>
                <th className="py-3 px-4">Order</th>
                <th className="py-3 px-4">Customer</th>
                <th className="py-3 px-4">Product</th>
                <th className="py-3 px-4">WO</th>
                <th className="py-3 px-4">Plan</th>
                <th className="py-3 px-4">Qty</th>
                <th className="py-3 px-4">Dispatched</th>
                <th className="py-3 px-4">Progress</th>
                <th className="py-3 px-4">Priority</th>
                <th className="py-3 px-4">Due</th>
                <th className="py-3 px-4">Status</th>
                <th className="py-3 px-4">Actions</th>
              </tr>
            </thead>

            <tbody>
              {orders.map((row) => {
                const progress = row.order_quantity > 0
                  ? Math.min(Math.round((row.dispatched_quantity / row.order_quantity) * 100), 100)
                  : 0;

                return (
                  <tr key={row.id} className="border-b border-slate-800">
                    <td className="py-3 px-4 font-semibold">{row.order_no}</td>
                    <td className="py-3 px-4">{row.customer_name}</td>
                    <td className="py-3 px-4">{row.product_name}</td>
                    <td className="py-3 px-4">{getWorkOrderName(row.linked_work_order_id)}</td>
                    <td className="py-3 px-4">{getPlanName(row.linked_production_plan_id)}</td>
                    <td className="py-3 px-4">{row.order_quantity}</td>
                    <td className="py-3 px-4">
                      <input
                        className="w-24 bg-slate-950 border border-slate-700 rounded-lg px-2 py-1"
                        type="number"
                        defaultValue={row.dispatched_quantity}
                        onBlur={(e) => updateOrder(row.id, Number(e.target.value), undefined, row.priority)}
                      />
                    </td>
                    <td className="py-3 px-4">
                      <div className="w-32 bg-slate-800 h-2 rounded-full">
                        <div className="bg-white h-2 rounded-full" style={{ width: `${progress}%` }} />
                      </div>
                      <p className="text-xs text-slate-400 mt-1">{progress}%</p>
                    </td>
                    <td className="py-3 px-4">
                      <select
                        className={`rounded-full px-3 py-1 text-xs border bg-slate-950 ${priorityStyle(row.priority)}`}
                        value={row.priority}
                        onChange={(e) => updateOrder(row.id, row.dispatched_quantity, row.status, e.target.value)}
                      >
                        <option>Critical</option>
                        <option>High</option>
                        <option>Medium</option>
                        <option>Low</option>
                      </select>
                    </td>
                    <td className="py-3 px-4">{row.due_date}</td>
                    <td className="py-3 px-4">
                      <select
                        className={`rounded-full px-3 py-1 text-xs border bg-slate-950 ${statusStyle(row.status)}`}
                        value={row.status}
                        onChange={(e) => updateOrder(row.id, row.dispatched_quantity, e.target.value, row.priority)}
                      >
                        <option>Pending</option>
                        <option>Partial</option>
                        <option>Dispatched</option>
                        <option>Cancelled</option>
                      </select>
                    </td>
                    <td className="py-3 px-4">
                      <button onClick={() => deleteOrder(row.id)} className="text-red-400 border border-red-500/40 rounded-lg px-3 py-1 hover:bg-red-500/10">
                        Delete
                      </button>
                    </td>
                  </tr>
                );
              })}

              {orders.length === 0 && (
                <tr>
                  <td colSpan={12} className="py-6 px-4 text-slate-400">
                    No customer orders yet.
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
