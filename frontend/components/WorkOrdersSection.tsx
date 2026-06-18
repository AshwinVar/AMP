import type { WorkOrder, WorkOrderAnalytics } from "../lib/phase9-types";

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
    case "Delayed":
      return "border-red-500/40 bg-red-500/10 text-red-300";
    case "Planned":
      return "border-yellow-500/40 bg-yellow-500/10 text-yellow-300";
    default:
      return "border-slate-500/40 bg-slate-500/10 text-slate-300";
  }
}

export default function WorkOrdersSection({
  machines,
  workOrders,
  analytics,
  form,
  setForm,
  createWorkOrder,
  updateWorkOrder,
  deleteWorkOrder,
  getMachineName,
}: {
  machines: Machine[];
  workOrders: WorkOrder[];
  analytics: WorkOrderAnalytics | null;
  form: {
    work_order_no: string;
    part_number: string;
    batch_number: string;
    machine_id: string;
    target_quantity: number;
    actual_quantity: number;
    status: string;
  };
  setForm: (value: any) => void;
  createWorkOrder: (e: React.FormEvent) => void;
  updateWorkOrder: (id: number, actualQuantity: number, status?: string) => void;
  deleteWorkOrder?: (id: number) => void;
  getMachineName: (id: number) => string;
}) {
  return (
    <section className="mt-8 space-y-6">
      <div>
        <h2 className="text-3xl font-bold">Work Orders</h2>
        <p className="text-slate-400 mt-2">
          Assign production jobs to machines and track planned vs actual output.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 xl:grid-cols-7 gap-4">
        <Kpi title="Total WO" value={analytics?.total_work_orders ?? 0} />
        <Kpi title="Planned" value={analytics?.planned ?? 0} />
        <Kpi title="Running" value={analytics?.running ?? 0} />
        <Kpi title="Completed" value={analytics?.completed ?? 0} />
        <Kpi title="Delayed" value={analytics?.delayed ?? 0} />
        <Kpi title="Target" value={analytics?.total_target ?? 0} />
        <Kpi title="Achievement" value={`${analytics?.achievement ?? 0}%`} />
      </div>

      <form
        onSubmit={createWorkOrder}
        className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-1 md:grid-cols-3 xl:grid-cols-7 gap-4"
      >
        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          placeholder="WO No"
          value={form.work_order_no}
          onChange={(e) => setForm({ ...form, work_order_no: e.target.value })}
          required
        />

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          placeholder="Part Number"
          value={form.part_number}
          onChange={(e) => setForm({ ...form, part_number: e.target.value })}
          required
        />

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          placeholder="Batch Number"
          value={form.batch_number}
          onChange={(e) => setForm({ ...form, batch_number: e.target.value })}
          required
        />

        <select
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          value={form.machine_id}
          onChange={(e) => setForm({ ...form, machine_id: e.target.value })}
          required
        >
          <option value="">Machine</option>
          {machines.map((machine) => (
            <option key={machine.id} value={machine.id}>
              {machine.name}
            </option>
          ))}
        </select>

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          type="number"
          placeholder="Target Qty"
          value={form.target_quantity}
          onChange={(e) =>
            setForm({ ...form, target_quantity: Number(e.target.value) })
          }
          required
        />

        <select
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          value={form.status}
          onChange={(e) => setForm({ ...form, status: e.target.value })}
        >
          <option>Planned</option>
          <option>Running</option>
          <option>Completed</option>
          <option>Delayed</option>
        </select>

        <button
          type="submit"
          className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3"
        >
          Create WO
        </button>
      </form>

      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-2xl font-semibold mb-4">Active Work Orders</h3>

        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full min-w-[980px] text-left text-sm">
            <thead className="text-slate-400 border-b border-slate-800">
              <tr>
                <th className="py-3 px-4">WO</th>
                <th className="py-3 px-4">Part</th>
                <th className="py-3 px-4">Batch</th>
                <th className="py-3 px-4">Machine</th>
                <th className="py-3 px-4">Target</th>
                <th className="py-3 px-4">Actual</th>
                <th className="py-3 px-4">Progress</th>
                <th className="py-3 px-4">Status</th>
                <th className="py-3 px-4">Actions</th>
              </tr>
            </thead>

            <tbody>
              {workOrders.map((wo) => {
                const progress =
                  wo.target_quantity > 0
                    ? Math.min(
                        Math.round(
                          (wo.actual_quantity / wo.target_quantity) * 100
                        ),
                        100
                      )
                    : 0;

                return (
                  <tr key={wo.id} className="border-b border-slate-800">
                    <td className="py-3 px-4 font-semibold">
                      {wo.work_order_no}
                    </td>
                    <td className="py-3 px-4">{wo.part_number}</td>
                    <td className="py-3 px-4">{wo.batch_number}</td>
                    <td className="py-3 px-4">
                      {getMachineName(wo.machine_id)}
                    </td>
                    <td className="py-3 px-4">{wo.target_quantity}</td>
                    <td className="py-3 px-4">
                      <input
                        className="w-24 bg-slate-950 border border-slate-700 rounded-lg px-2 py-1"
                        type="number"
                        defaultValue={wo.actual_quantity}
                        onBlur={(e) =>
                          updateWorkOrder(
                            wo.id,
                            Number(e.target.value),
                            undefined
                          )
                        }
                      />
                    </td>
                    <td className="py-3 px-4">
                      <div className="w-32 bg-slate-800 h-2 rounded-full">
                        <div
                          className="bg-white h-2 rounded-full"
                          style={{ width: `${progress}%` }}
                        />
                      </div>
                      <p className="text-xs text-slate-400 mt-1">
                        {progress}%
                      </p>
                    </td>
                    <td className="py-3 px-4">
                      <select
                        className={`rounded-full px-3 py-1 text-xs border bg-slate-950 ${statusStyle(
                          wo.status
                        )}`}
                        value={wo.status}
                        onChange={(e) =>
                          updateWorkOrder(
                            wo.id,
                            wo.actual_quantity,
                            e.target.value
                          )
                        }
                      >
                        <option>Planned</option>
                        <option>Running</option>
                        <option>Completed</option>
                        <option>Delayed</option>
                      </select>
                    </td>
                    <td className="py-3 px-4">
                      <button
                        onClick={() => deleteWorkOrder?.(wo.id)}
                        className="text-red-400 border border-red-500/40 rounded-lg px-3 py-1 hover:bg-red-500/10"
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                );
              })}

              {workOrders.length === 0 && (
                <tr>
                  <td colSpan={9} className="py-6 px-4 text-slate-400">
                    No work orders yet.
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

function Kpi({
  title,
  value,
}: {
  title: string;
  value: string | number;
}) {
  return (
    <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
      <p className="text-slate-400 text-sm">{title}</p>
      <h3 className="text-2xl font-bold mt-2">{value}</h3>
    </div>
  );
}
