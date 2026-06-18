import type { WorkOrder } from "../lib/phase9-types";
import type { ProductionPlan } from "../lib/phase11-types";
import type { QualityAnalytics, QualityInspection } from "../lib/phase14-types";

type Machine = {
  id: number;
  name: string;
  status: string;
  utilization: number;
  downtime: string;
};

function statusStyle(status: string) {
  switch (status) {
    case "Closed":
      return "border-green-500/40 bg-green-500/10 text-green-300";
    case "In Review":
      return "border-blue-500/40 bg-blue-500/10 text-blue-300";
    case "Rejected":
      return "border-red-500/40 bg-red-500/10 text-red-300";
    default:
      return "border-yellow-500/40 bg-yellow-500/10 text-yellow-300";
  }
}

export default function QualitySection({
  machines,
  workOrders,
  productionPlans,
  inspections,
  analytics,
  form,
  setForm,
  createInspection,
  updateInspection,
  deleteInspection,
  generateDefectEscalations,
  getMachineName,
}: {
  machines: Machine[];
  workOrders: WorkOrder[];
  productionPlans: ProductionPlan[];
  inspections: QualityInspection[];
  analytics: QualityAnalytics | null;
  form: {
    inspection_no: string;
    work_order_id: string;
    production_plan_id: string;
    machine_id: string;
    inspector: string;
    inspected_quantity: number;
    passed_quantity: number;
    failed_quantity: number;
    defect_category: string;
    rework_quantity: number;
    scrap_quantity: number;
    status: string;
    notes: string;
  };
  setForm: (value: any) => void;
  createInspection: (e: React.FormEvent) => void;
  updateInspection: (
    id: number,
    passed: number,
    failed: number,
    status?: string,
    defectCategory?: string,
    rework?: number,
    scrap?: number,
    notes?: string
  ) => void;
  deleteInspection?: (id: number) => void;
  generateDefectEscalations: () => void;
  getMachineName: (id: number) => string;
}) {
  return (
    <section className="mt-8 space-y-6">
      <div className="flex flex-col md:flex-row md:items-end md:justify-between gap-4">
        <div>
          <h2 className="text-3xl font-bold">Quality Control</h2>
          <p className="text-slate-400 mt-2">
            Record inspections, defect categories, pass/fail quantities, rework and scrap.
          </p>
        </div>

        <button
          type="button"
          onClick={generateDefectEscalations}
          className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3"
        >
          Generate Defect Escalations
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 xl:grid-cols-8 gap-4">
        <Kpi title="Inspections" value={analytics?.total_inspections ?? 0} />
        <Kpi title="Inspected" value={analytics?.inspected_quantity ?? 0} />
        <Kpi title="Passed" value={analytics?.passed_quantity ?? 0} />
        <Kpi title="Failed" value={analytics?.failed_quantity ?? 0} />
        <Kpi title="Pass Rate" value={`${analytics?.pass_rate ?? 0}%`} />
        <Kpi title="Fail Rate" value={`${analytics?.fail_rate ?? 0}%`} />
        <Kpi title="Rework" value={analytics?.rework_quantity ?? 0} />
        <Kpi title="Scrap" value={analytics?.scrap_quantity ?? 0} />
      </div>

      <form
        onSubmit={createInspection}
        className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-1 md:grid-cols-3 xl:grid-cols-8 gap-4"
      >
        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          placeholder="Inspection No"
          value={form.inspection_no}
          onChange={(e) => setForm({ ...form, inspection_no: e.target.value })}
          required
        />

        <select
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          value={form.work_order_id}
          onChange={(e) => setForm({ ...form, work_order_id: e.target.value })}
        >
          <option value="">Work Order</option>
          {workOrders.map((wo) => (
            <option key={wo.id} value={wo.id}>
              {wo.work_order_no}
            </option>
          ))}
        </select>

        <select
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          value={form.production_plan_id}
          onChange={(e) => setForm({ ...form, production_plan_id: e.target.value })}
        >
          <option value="">Production Plan</option>
          {productionPlans.map((plan) => (
            <option key={plan.id} value={plan.id}>
              {plan.plan_no}
            </option>
          ))}
        </select>

        <select
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          value={form.machine_id}
          onChange={(e) => setForm({ ...form, machine_id: e.target.value })}
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
          placeholder="Inspector"
          value={form.inspector}
          onChange={(e) => setForm({ ...form, inspector: e.target.value })}
          required
        />

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          type="number"
          placeholder="Inspected Qty"
          value={form.inspected_quantity}
          onChange={(e) =>
            setForm({ ...form, inspected_quantity: Number(e.target.value) })
          }
          required
        />

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          type="number"
          placeholder="Passed Qty"
          value={form.passed_quantity}
          onChange={(e) =>
            setForm({ ...form, passed_quantity: Number(e.target.value) })
          }
        />

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          type="number"
          placeholder="Failed Qty"
          value={form.failed_quantity}
          onChange={(e) =>
            setForm({ ...form, failed_quantity: Number(e.target.value) })
          }
        />

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          placeholder="Defect Category"
          value={form.defect_category}
          onChange={(e) => setForm({ ...form, defect_category: e.target.value })}
        />

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          type="number"
          placeholder="Rework Qty"
          value={form.rework_quantity}
          onChange={(e) =>
            setForm({ ...form, rework_quantity: Number(e.target.value) })
          }
        />

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          type="number"
          placeholder="Scrap Qty"
          value={form.scrap_quantity}
          onChange={(e) =>
            setForm({ ...form, scrap_quantity: Number(e.target.value) })
          }
        />

        <select
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          value={form.status}
          onChange={(e) => setForm({ ...form, status: e.target.value })}
        >
          <option>Open</option>
          <option>In Review</option>
          <option>Closed</option>
          <option>Rejected</option>
        </select>

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3 xl:col-span-3"
          placeholder="Notes"
          value={form.notes}
          onChange={(e) => setForm({ ...form, notes: e.target.value })}
        />

        <button
          type="submit"
          className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3"
        >
          Save Inspection
        </button>
      </form>

      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-2xl font-semibold mb-4">Inspection Records</h3>

        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full min-w-[1250px] text-left text-sm">
            <thead className="text-slate-400 border-b border-slate-800">
              <tr>
                <th className="py-3 px-4">Inspection</th>
                <th className="py-3 px-4">Machine</th>
                <th className="py-3 px-4">Inspector</th>
                <th className="py-3 px-4">Inspected</th>
                <th className="py-3 px-4">Passed</th>
                <th className="py-3 px-4">Failed</th>
                <th className="py-3 px-4">Defect</th>
                <th className="py-3 px-4">Rework</th>
                <th className="py-3 px-4">Scrap</th>
                <th className="py-3 px-4">Status</th>
                <th className="py-3 px-4">Actions</th>
              </tr>
            </thead>

            <tbody>
              {inspections.map((row) => (
                <tr key={row.id} className="border-b border-slate-800">
                  <td className="py-3 px-4">
                    <p className="font-semibold">{row.inspection_no}</p>
                    <p className="text-xs text-slate-500">{row.notes || "-"}</p>
                  </td>

                  <td className="py-3 px-4">
                    {row.machine_id ? getMachineName(row.machine_id) : "-"}
                  </td>

                  <td className="py-3 px-4">{row.inspector}</td>
                  <td className="py-3 px-4">{row.inspected_quantity}</td>

                  <td className="py-3 px-4">
                    <input
                      className="w-20 bg-slate-950 border border-slate-700 rounded-lg px-2 py-1"
                      type="number"
                      defaultValue={row.passed_quantity}
                      onBlur={(e) =>
                        updateInspection(
                          row.id,
                          Number(e.target.value),
                          row.failed_quantity,
                          row.status,
                          row.defect_category || "",
                          row.rework_quantity,
                          row.scrap_quantity,
                          row.notes || ""
                        )
                      }
                    />
                  </td>

                  <td className="py-3 px-4">
                    <input
                      className="w-20 bg-slate-950 border border-slate-700 rounded-lg px-2 py-1"
                      type="number"
                      defaultValue={row.failed_quantity}
                      onBlur={(e) =>
                        updateInspection(
                          row.id,
                          row.passed_quantity,
                          Number(e.target.value),
                          row.status,
                          row.defect_category || "",
                          row.rework_quantity,
                          row.scrap_quantity,
                          row.notes || ""
                        )
                      }
                    />
                  </td>

                  <td className="py-3 px-4">{row.defect_category || "-"}</td>
                  <td className="py-3 px-4">{row.rework_quantity}</td>
                  <td className="py-3 px-4">{row.scrap_quantity}</td>

                  <td className="py-3 px-4">
                    <select
                      className={`rounded-full px-3 py-1 text-xs border bg-slate-950 ${statusStyle(row.status)}`}
                      value={row.status}
                      onChange={(e) =>
                        updateInspection(
                          row.id,
                          row.passed_quantity,
                          row.failed_quantity,
                          e.target.value,
                          row.defect_category || "",
                          row.rework_quantity,
                          row.scrap_quantity,
                          row.notes || ""
                        )
                      }
                    >
                      <option>Open</option>
                      <option>In Review</option>
                      <option>Closed</option>
                      <option>Rejected</option>
                    </select>
                  </td>

                  <td className="py-3 px-4">
                    <button
                      onClick={() => deleteInspection?.(row.id)}
                      className="text-red-400 border border-red-500/40 rounded-lg px-3 py-1 hover:bg-red-500/10"
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}

              {inspections.length === 0 && (
                <tr>
                  <td colSpan={11} className="py-6 px-4 text-slate-400">
                    No quality inspections yet.
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
