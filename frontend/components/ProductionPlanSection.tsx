import type { ProductionPlan, ProductionPlanAnalytics } from "../lib/phase11-types";
import type { WorkOrder } from "../lib/phase9-types";

type Machine = {
  id: number;
  name: string;
  status: string;
  utilization: number;
  downtime: string;
};

function statusStyle(status: string) {
  switch (status) {
    case "Completed":
      return "border-green-500/40 bg-green-500/10 text-green-300";
    case "Running":
      return "border-blue-500/40 bg-blue-500/10 text-blue-300";
    case "Behind":
      return "border-red-500/40 bg-red-500/10 text-red-300";
    case "Planned":
      return "border-yellow-500/40 bg-yellow-500/10 text-yellow-300";
    default:
      return "border-slate-500/40 bg-slate-500/10 text-slate-300";
  }
}

export default function ProductionPlanSection({
  machines,
  workOrders,
  plans,
  analytics,
  form,
  setForm,
  createPlan,
  updatePlan,
  deletePlan,
  getMachineName,
}: {
  machines: Machine[];
  workOrders: WorkOrder[];
  plans: ProductionPlan[];
  analytics: ProductionPlanAnalytics | null;
  form: {
    plan_no: string;
    work_order_id: string;
    machine_id: string;
    planned_quantity: number;
    actual_quantity: number;
    plan_date: string;
    shift_name: string;
    status: string;
  };
  setForm: (value: any) => void;
  createPlan: (e: React.FormEvent) => void;
  updatePlan: (id: number, actualQuantity: number, status?: string) => void;
  deletePlan?: (id: number) => void;
  getMachineName: (id: number) => string;
}) {
  return (
    <section className="mt-8 space-y-6">
      <div>
        <h2 className="text-3xl font-bold">Production Plan</h2>
        <p className="text-slate-400 mt-2">
          Daily production planning, target vs actual tracking and behind-schedule visibility.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 xl:grid-cols-8 gap-4">
        <Kpi title="Plans" value={analytics?.total_plans ?? 0} />
        <Kpi title="Planned Qty" value={analytics?.planned_quantity ?? 0} />
        <Kpi title="Actual Qty" value={analytics?.actual_quantity ?? 0} />
        <Kpi title="Achievement" value={`${analytics?.achievement ?? 0}%`} />
        <Kpi title="Planned" value={analytics?.planned ?? 0} />
        <Kpi title="Running" value={analytics?.running ?? 0} />
        <Kpi title="Completed" value={analytics?.completed ?? 0} />
        <Kpi title="Behind" value={analytics?.behind ?? 0} />
      </div>

      <form
        onSubmit={createPlan}
        className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-1 md:grid-cols-3 xl:grid-cols-8 gap-4"
      >
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Plan No" value={form.plan_no} onChange={(e) => setForm({ ...form, plan_no: e.target.value })} required />

        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.work_order_id} onChange={(e) => setForm({ ...form, work_order_id: e.target.value })} required>
          <option value="">Work Order</option>
          {workOrders.map((wo) => (
            <option key={wo.id} value={wo.id}>{wo.work_order_no}</option>
          ))}
        </select>

        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.machine_id} onChange={(e) => setForm({ ...form, machine_id: e.target.value })} required>
          <option value="">Machine</option>
          {machines.map((machine) => (
            <option key={machine.id} value={machine.id}>{machine.name}</option>
          ))}
        </select>

        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="number" placeholder="Planned Qty" value={form.planned_quantity} onChange={(e) => setForm({ ...form, planned_quantity: Number(e.target.value) })} required />

        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="date" value={form.plan_date} onChange={(e) => setForm({ ...form, plan_date: e.target.value })} required />

        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Shift" value={form.shift_name} onChange={(e) => setForm({ ...form, shift_name: e.target.value })} required />

        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value })}>
          <option>Planned</option>
          <option>Running</option>
          <option>Completed</option>
          <option>Behind</option>
        </select>

        <button type="submit" className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3">Create Plan</button>
      </form>

      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-2xl font-semibold mb-4">Production Schedule</h3>

        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full min-w-[1100px] text-left text-sm">
            <thead className="text-slate-400 border-b border-slate-800">
              <tr>
                <th className="py-3 px-4">Plan</th>
                <th className="py-3 px-4">WO</th>
                <th className="py-3 px-4">Machine</th>
                <th className="py-3 px-4">Date</th>
                <th className="py-3 px-4">Shift</th>
                <th className="py-3 px-4">Target</th>
                <th className="py-3 px-4">Actual</th>
                <th className="py-3 px-4">Progress</th>
                <th className="py-3 px-4">Status</th>
                <th className="py-3 px-4">Actions</th>
              </tr>
            </thead>

            <tbody>
              {plans.map((plan) => {
                const workOrder = workOrders.find((wo) => wo.id === plan.work_order_id);
                const progress = plan.planned_quantity > 0 ? Math.min(Math.round((plan.actual_quantity / plan.planned_quantity) * 100), 100) : 0;

                return (
                  <tr key={plan.id} className="border-b border-slate-800">
                    <td className="py-3 px-4 font-semibold">{plan.plan_no}</td>
                    <td className="py-3 px-4">{workOrder?.work_order_no ?? plan.work_order_id}</td>
                    <td className="py-3 px-4">{getMachineName(plan.machine_id)}</td>
                    <td className="py-3 px-4">{plan.plan_date}</td>
                    <td className="py-3 px-4">{plan.shift_name}</td>
                    <td className="py-3 px-4">{plan.planned_quantity}</td>
                    <td className="py-3 px-4">
                      <input className="w-24 bg-slate-950 border border-slate-700 rounded-lg px-2 py-1" type="number" defaultValue={plan.actual_quantity} onBlur={(e) => updatePlan(plan.id, Number(e.target.value), undefined)} />
                    </td>
                    <td className="py-3 px-4">
                      <div className="w-32 bg-slate-800 h-2 rounded-full">
                        <div className="bg-white h-2 rounded-full" style={{ width: `${progress}%` }} />
                      </div>
                      <p className="text-xs text-slate-400 mt-1">{progress}%</p>
                    </td>
                    <td className="py-3 px-4">
                      <select className={`rounded-full px-3 py-1 text-xs border bg-slate-950 ${statusStyle(plan.status)}`} value={plan.status} onChange={(e) => updatePlan(plan.id, plan.actual_quantity, e.target.value)}>
                        <option>Planned</option>
                        <option>Running</option>
                        <option>Completed</option>
                        <option>Behind</option>
                      </select>
                    </td>
                    <td className="py-3 px-4">
                      <button onClick={() => deletePlan?.(plan.id)} className="text-red-400 border border-red-500/40 rounded-lg px-3 py-1 hover:bg-red-500/10">
                        Delete
                      </button>
                    </td>
                  </tr>
                );
              })}

              {plans.length === 0 && (
                <tr>
                  <td colSpan={10} className="py-6 px-4 text-slate-400">No production plans yet.</td>
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
