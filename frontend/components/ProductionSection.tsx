import type { Machine, OeeRow, ProductionRecord } from "../lib/types";
import { BTN_CLASS, INPUT_CLASS } from "../lib/utils";

export default function ProductionSection({
  machines,
  records,
  oeeRows,
  productionForm,
  setProductionForm,
  addProductionRecord,
}: {
  machines: Machine[];
  records: ProductionRecord[];
  oeeRows: OeeRow[];
  productionForm: {
    machine_id: string;
    planned_minutes: number;
    runtime_minutes: number;
    ideal_cycle_time_seconds: number;
    total_count: number;
    good_count: number;
    rejected_count: number;
  };
  setProductionForm: (value: any) => void;
  addProductionRecord: (e: React.FormEvent) => void;
}) {
  const visibleOeeRows = oeeRows.slice(0, 30);

  return (
    <section className="mt-8 grid grid-cols-1 xl:grid-cols-3 gap-6">
      <form onSubmit={addProductionRecord} className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-2xl font-semibold mb-2">Production / OEE Entry</h3>
        <p className="text-sm text-slate-400 mb-4">OEE = Availability × Performance × Quality</p>

        <div className="space-y-4">
          <select className={`${INPUT_CLASS} w-full`} value={productionForm.machine_id} onChange={(e) => setProductionForm({ ...productionForm, machine_id: e.target.value })} required>
            <option value="">Select machine</option>
            {machines.map((machine) => (
              <option key={machine.id} value={machine.id}>{machine.name}</option>
            ))}
          </select>

          <input className={`${INPUT_CLASS} w-full`} type="number" placeholder="Planned minutes" value={productionForm.planned_minutes} onChange={(e) => setProductionForm({ ...productionForm, planned_minutes: Number(e.target.value) })} required />
          <input className={`${INPUT_CLASS} w-full`} type="number" placeholder="Runtime minutes" value={productionForm.runtime_minutes} onChange={(e) => setProductionForm({ ...productionForm, runtime_minutes: Number(e.target.value) })} required />
          <input className={`${INPUT_CLASS} w-full`} type="number" placeholder="Ideal cycle time seconds" value={productionForm.ideal_cycle_time_seconds} onChange={(e) => setProductionForm({ ...productionForm, ideal_cycle_time_seconds: Number(e.target.value) })} required />
          <input className={`${INPUT_CLASS} w-full`} type="number" placeholder="Total count" value={productionForm.total_count} onChange={(e) => setProductionForm({ ...productionForm, total_count: Number(e.target.value) })} required />
          <input className={`${INPUT_CLASS} w-full`} type="number" placeholder="Good count" value={productionForm.good_count} onChange={(e) => setProductionForm({ ...productionForm, good_count: Number(e.target.value) })} required />
          <input className={`${INPUT_CLASS} w-full`} type="number" placeholder="Rejected count" value={productionForm.rejected_count} onChange={(e) => setProductionForm({ ...productionForm, rejected_count: Number(e.target.value) })} required />

          <button type="submit" className={`${BTN_CLASS} w-full`}>Save Production Record</button>
        </div>
      </form>

      <div className="xl:col-span-2 rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <div className="mb-4">
          <h3 className="text-2xl font-semibold">OEE Records</h3>
          <p className="text-sm text-slate-400 mt-1">Showing latest {visibleOeeRows.length} OEE records.</p>
        </div>

        <div className="max-h-[620px] overflow-y-auto overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full min-w-[760px] text-left text-sm">
            <thead className="sticky top-0 bg-slate-900 text-slate-400 border-b border-slate-800">
              <tr>
                <th className="py-3 px-4">Machine</th>
                <th className="py-3 px-4">Availability</th>
                <th className="py-3 px-4">Performance</th>
                <th className="py-3 px-4">Quality</th>
                <th className="py-3 px-4">OEE</th>
              </tr>
            </thead>
            <tbody>
              {visibleOeeRows.map((row) => (
                <tr key={row.id} className="border-b border-slate-800">
                  <td className="py-3 px-4 font-medium">{row.machine_name}</td>
                  <td className="py-3 px-4">{row.availability}%</td>
                  <td className="py-3 px-4">{row.performance}%</td>
                  <td className="py-3 px-4">{row.quality}%</td>
                  <td className={`py-3 px-4 font-bold ${row.oee < 60 ? "text-red-400" : row.oee < 75 ? "text-yellow-300" : "text-green-400"}`}>{row.oee}%</td>
                </tr>
              ))}

              {visibleOeeRows.length === 0 && (
                <tr>
                  <td colSpan={5} className="py-6 px-4 text-slate-400">No OEE records yet.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
