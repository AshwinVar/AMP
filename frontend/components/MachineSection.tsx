import type { Machine } from "../lib/types";
import { BTN_CLASS, INPUT_CLASS, calculateOEE, getStatusStyle } from "../lib/utils";

export default function MachineSection({
  canManageMachines,
  canUpdateStatus,
  machines,
  machineFilter,
  setMachineFilter,
  name,
  setName,
  status,
  setStatus,
  utilization,
  setUtilization,
  downtime,
  setDowntime,
  addMachine,
  deleteMachine,
  updateMachineStatus,
}: {
  canManageMachines: boolean;
  canUpdateStatus: boolean;
  machines: Machine[];
  machineFilter: string;
  setMachineFilter: (value: string) => void;
  name: string;
  setName: (value: string) => void;
  status: string;
  setStatus: (value: string) => void;
  utilization: number;
  setUtilization: (value: number) => void;
  downtime: string;
  setDowntime: (value: string) => void;
  addMachine: (e: React.FormEvent) => void;
  deleteMachine: (id: number) => void;
  updateMachineStatus: (id: number, status: string) => void;
}) {
  const filteredMachines =
    machineFilter === "All"
      ? machines
      : machines.filter((machine) => machine.status === machineFilter);

  return (
    <section>
      {canManageMachines && (
        <form
          onSubmit={addMachine}
          className="mb-8 rounded-2xl bg-slate-900 border border-slate-800 p-5"
        >
          <div className="mb-5">
            <h3 className="text-2xl font-semibold">Add Machine</h3>
            <p className="text-slate-400 text-sm mt-1">
              Create factory assets and assign operational status.
            </p>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-5 gap-4">
            <input className={INPUT_CLASS} placeholder="Machine name" value={name} onChange={(e) => setName(e.target.value)} required />
            <select className={INPUT_CLASS} value={status} onChange={(e) => setStatus(e.target.value)}>
              <option>Running</option>
              <option>Idle</option>
              <option>Breakdown</option>
              <option>Maintenance</option>
            </select>
            <input className={INPUT_CLASS} type="number" placeholder="Utilization" value={utilization} onChange={(e) => setUtilization(Number(e.target.value))} min={0} max={100} required />
            <input className={INPUT_CLASS} placeholder="Downtime e.g. 0 min" value={downtime} onChange={(e) => setDowntime(e.target.value)} required />
            <button type="submit" className={BTN_CLASS}>Add Machine</button>
          </div>
        </form>
      )}

      <div className="mb-4 flex flex-col md:flex-row md:items-center md:justify-between gap-3">
        <h3 className="text-2xl font-semibold">Machine Status</h3>

        <select className={INPUT_CLASS} value={machineFilter} onChange={(e) => setMachineFilter(e.target.value)}>
          <option>All</option>
          <option>Running</option>
          <option>Idle</option>
          <option>Breakdown</option>
          <option>Maintenance</option>
        </select>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
        {filteredMachines.map((machine) => (
          <div key={machine.id} className="rounded-2xl bg-slate-900 border border-slate-800 p-5 hover:border-slate-600 transition">
            <div className="flex items-center justify-between">
              <h4 className="text-xl font-semibold">{machine.name}</h4>
              <span className={`text-xs px-3 py-1 rounded-full border ${getStatusStyle(machine.status)}`}>
                {machine.status}
              </span>
            </div>

            <div className="mt-6">
              <p className="text-sm text-slate-400">Utilization</p>
              <div className="w-full bg-slate-800 rounded-full h-3 mt-2 overflow-hidden">
                <div className="bg-white h-3 rounded-full transition-all" style={{ width: `${machine.utilization}%` }} />
              </div>
              <p className="text-sm mt-2">{machine.utilization}%</p>

              <p className="text-sm text-slate-400 mt-3">Fallback OEE</p>
              <p className="text-lg font-semibold mt-1">{calculateOEE(machine.utilization)}%</p>
            </div>

            <div className="mt-5">
              <p className="text-sm text-slate-400">Downtime Today</p>
              <p className="text-lg font-semibold mt-1">{machine.downtime}</p>
            </div>

            {canUpdateStatus && (
              <select className={`${INPUT_CLASS} mt-5 w-full`} value={machine.status} onChange={(e) => updateMachineStatus(machine.id, e.target.value)}>
                <option>Running</option>
                <option>Idle</option>
                <option>Breakdown</option>
                <option>Maintenance</option>
              </select>
            )}

            {canManageMachines && (
              <button onClick={() => deleteMachine(machine.id)} className="mt-4 w-full rounded-xl border border-red-500/40 text-red-400 py-2 text-sm hover:bg-red-500/10">
                Delete Machine
              </button>
            )}
          </div>
        ))}

        {filteredMachines.length === 0 && (
          <div className="col-span-full rounded-2xl border border-dashed border-slate-700 p-8 text-slate-400">
            No machines found for this filter.
          </div>
        )}
      </div>
    </section>
  );
}
