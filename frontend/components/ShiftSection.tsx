import type { Shift } from "../lib/types";
import { BTN_CLASS, INPUT_CLASS } from "../lib/utils";

export default function ShiftSection({
  shifts,
  shiftName,
  setShiftName,
  targetOutput,
  setTargetOutput,
  actualOutput,
  setActualOutput,
  addShift,
}: {
  shifts: Shift[];
  shiftName: string;
  setShiftName: (value: string) => void;
  targetOutput: number;
  setTargetOutput: (value: number) => void;
  actualOutput: number;
  setActualOutput: (value: number) => void;
  addShift: (e: React.FormEvent) => void;
}) {
  return (
    <section className="mt-8 grid grid-cols-1 xl:grid-cols-3 gap-6">
      <form onSubmit={addShift} className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-2xl font-semibold mb-4">Shift Performance Entry</h3>

        <div className="space-y-4">
          <input className={`${INPUT_CLASS} w-full`} placeholder="Shift Name" value={shiftName} onChange={(e) => setShiftName(e.target.value)} required />
          <input className={`${INPUT_CLASS} w-full`} type="number" placeholder="Target Output" value={targetOutput} onChange={(e) => setTargetOutput(Number(e.target.value))} required />
          <input className={`${INPUT_CLASS} w-full`} type="number" placeholder="Actual Output" value={actualOutput} onChange={(e) => setActualOutput(Number(e.target.value))} required />
          <button type="submit" className={`${BTN_CLASS} w-full`}>Save Shift Data</button>
        </div>
      </form>

      <div className="xl:col-span-2 rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-2xl font-semibold mb-4">Shift Performance</h3>

        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="text-slate-400 border-b border-slate-800">
              <tr>
                <th className="py-3">Shift</th>
                <th>Target</th>
                <th>Actual</th>
                <th>Efficiency</th>
              </tr>
            </thead>

            <tbody>
              {shifts.map((shift) => {
                const efficiency =
                  shift.target_output > 0
                    ? Math.round((shift.actual_output / shift.target_output) * 100)
                    : 0;

                return (
                  <tr key={shift.id} className="border-b border-slate-800">
                    <td className="py-3 font-medium">{shift.shift_name}</td>
                    <td>{shift.target_output}</td>
                    <td>{shift.actual_output}</td>
                    <td>{efficiency}%</td>
                  </tr>
                );
              })}

              {shifts.length === 0 && (
                <tr>
                  <td colSpan={4} className="py-6 text-slate-400">No shift data yet.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
