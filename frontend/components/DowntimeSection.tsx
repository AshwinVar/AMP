import type { DowntimeLog, Machine } from "../lib/types";
import { BTN_CLASS, INPUT_CLASS } from "../lib/utils";

export default function DowntimeSection({
  canLogDowntime,
  machines,
  downtimeLogs,
  selectedMachineId,
  setSelectedMachineId,
  reason,
  setReason,
  duration,
  setDuration,
  notes,
  setNotes,
  addDowntimeLog,
  getMachineName,
}: {
  canLogDowntime: boolean;
  machines: Machine[];
  downtimeLogs: DowntimeLog[];
  selectedMachineId: string;
  setSelectedMachineId: (value: string) => void;
  reason: string;
  setReason: (value: string) => void;
  duration: string;
  setDuration: (value: string) => void;
  notes: string;
  setNotes: (value: string) => void;
  addDowntimeLog: (e: React.FormEvent) => void;
  getMachineName: (id: number) => string;
}) {
  const visibleLogs = downtimeLogs.slice(0, 30);

  return (
    <section className="mt-8 grid grid-cols-1 xl:grid-cols-3 gap-6">
      {canLogDowntime && (
        <form onSubmit={addDowntimeLog} className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
          <h3 className="text-2xl font-semibold mb-4">Log Downtime</h3>
          <div className="space-y-4">
            <select className={`${INPUT_CLASS} w-full`} value={selectedMachineId} onChange={(e) => setSelectedMachineId(e.target.value)} required>
              <option value="">Select machine</option>
              {machines.map((machine) => (
                <option key={machine.id} value={machine.id}>{machine.name}</option>
              ))}
            </select>

            <select className={`${INPUT_CLASS} w-full`} value={reason} onChange={(e) => setReason(e.target.value)}>
              <option>Material Shortage</option>
              <option>Operator Unavailable</option>
              <option>Breakdown</option>
              <option>Maintenance</option>
              <option>Quality Issue</option>
              <option>Tool Change</option>
            </select>

            <input className={`${INPUT_CLASS} w-full`} placeholder="Duration e.g. 25 min" value={duration} onChange={(e) => setDuration(e.target.value)} required />

            <textarea className={`${INPUT_CLASS} w-full min-h-28`} placeholder="Operator notes" value={notes} onChange={(e) => setNotes(e.target.value)} />

            <button type="submit" className={`${BTN_CLASS} w-full`}>Save Downtime Log</button>
          </div>
        </form>
      )}

      <div className={`${canLogDowntime ? "xl:col-span-2" : "xl:col-span-3"} rounded-2xl bg-slate-900 border border-slate-800 p-5`}>
        <div className="mb-4">
          <h3 className="text-2xl font-semibold">Recent Downtime Events</h3>
          <p className="text-sm text-slate-400 mt-1">Showing latest {visibleLogs.length} live downtime events.</p>
        </div>

        <div className="max-h-[620px] overflow-y-auto overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full min-w-[760px] text-left text-sm">
            <thead className="sticky top-0 bg-slate-900 text-slate-400 border-b border-slate-800">
              <tr>
                <th className="py-3 px-4 w-[170px]">Machine</th>
                <th className="py-3 px-4 w-[190px]">Reason</th>
                <th className="py-3 px-4 w-[120px]">Duration</th>
                <th className="py-3 px-4">Notes</th>
              </tr>
            </thead>

            <tbody>
              {visibleLogs.map((log) => (
                <tr key={log.id} className="border-b border-slate-800">
                  <td className="py-3 px-4 font-medium">{getMachineName(log.machine_id)}</td>
                  <td className="py-3 px-4">{log.reason}</td>
                  <td className="py-3 px-4 font-semibold">{log.duration}</td>
                  <td className="py-3 px-4 text-slate-400">{log.notes || "-"}</td>
                </tr>
              ))}

              {visibleLogs.length === 0 && (
                <tr>
                  <td colSpan={4} className="py-6 px-4 text-slate-400">No downtime logs yet.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
