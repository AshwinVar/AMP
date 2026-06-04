import type { Escalation, EscalationAnalytics } from "../lib/phase12-types";

type Machine = {
  id: number;
  name: string;
  status: string;
  utilization: number;
  downtime: string;
};

function severityStyle(severity: string) {
  switch (severity) {
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

function statusStyle(status: string) {
  switch (status) {
    case "Resolved":
      return "border-green-500/40 bg-green-500/10 text-green-300";
    case "In Progress":
      return "border-blue-500/40 bg-blue-500/10 text-blue-300";
    default:
      return "border-red-500/40 bg-red-500/10 text-red-300";
  }
}

export default function EscalationSection({
  machines,
  escalations,
  analytics,
  form,
  setForm,
  createEscalation,
  updateEscalation,
  deleteEscalation,
  generateFromSmartAlerts,
  getMachineName,
}: {
  machines: Machine[];
  escalations: Escalation[];
  analytics: EscalationAnalytics | null;
  form: {
    machine_id: string;
    title: string;
    severity: string;
    owner: string;
    department: string;
    status: string;
    source: string;
    notes: string;
  };
  setForm: (value: any) => void;
  createEscalation: (e: React.FormEvent) => void;
  updateEscalation: (
    id: number,
    status: string,
    owner?: string,
    department?: string,
    resolutionNotes?: string
  ) => void;
  deleteEscalation: (id: number) => void;
  generateFromSmartAlerts: () => void;
  getMachineName: (id: number) => string;
}) {
  return (
    <section className="mt-8 space-y-6">
      <div className="flex flex-col md:flex-row md:items-end md:justify-between gap-4">
        <div>
          <h2 className="text-3xl font-bold">Escalation Center</h2>
          <p className="text-slate-400 mt-2">
            Convert machine issues into accountable maintenance and operations actions.
          </p>
        </div>

        <button
          type="button"
          onClick={generateFromSmartAlerts}
          className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3"
        >
          Generate From Smart Alerts
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 xl:grid-cols-8 gap-4">
        <Kpi title="Total" value={analytics?.total ?? 0} />
        <Kpi title="Open" value={analytics?.open ?? 0} />
        <Kpi title="In Progress" value={analytics?.in_progress ?? 0} />
        <Kpi title="Resolved" value={analytics?.resolved ?? 0} />
        <Kpi title="Critical" value={analytics?.critical ?? 0} />
        <Kpi title="High" value={analytics?.high ?? 0} />
        <Kpi title="Medium" value={analytics?.medium ?? 0} />
        <Kpi title="Low" value={analytics?.low ?? 0} />
      </div>

      <form
        onSubmit={createEscalation}
        className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-1 md:grid-cols-3 xl:grid-cols-8 gap-4"
      >
        <select
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          value={form.machine_id}
          onChange={(e) => setForm({ ...form, machine_id: e.target.value })}
        >
          <option value="">Machine optional</option>
          {machines.map((machine) => (
            <option key={machine.id} value={machine.id}>
              {machine.name}
            </option>
          ))}
        </select>

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3 xl:col-span-2"
          placeholder="Issue title"
          value={form.title}
          onChange={(e) => setForm({ ...form, title: e.target.value })}
          required
        />

        <select
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          value={form.severity}
          onChange={(e) => setForm({ ...form, severity: e.target.value })}
        >
          <option>Critical</option>
          <option>High</option>
          <option>Medium</option>
          <option>Low</option>
        </select>

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          placeholder="Owner"
          value={form.owner}
          onChange={(e) => setForm({ ...form, owner: e.target.value })}
          required
        />

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          placeholder="Department"
          value={form.department}
          onChange={(e) => setForm({ ...form, department: e.target.value })}
          required
        />

        <input
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          placeholder="Notes"
          value={form.notes}
          onChange={(e) => setForm({ ...form, notes: e.target.value })}
        />

        <button
          type="submit"
          className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3"
        >
          Create
        </button>
      </form>

      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-2xl font-semibold mb-4">Active Escalations</h3>

        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full min-w-[1150px] text-left text-sm">
            <thead className="text-slate-400 border-b border-slate-800">
              <tr>
                <th className="py-3 px-4">Issue</th>
                <th className="py-3 px-4">Machine</th>
                <th className="py-3 px-4">Severity</th>
                <th className="py-3 px-4">Owner</th>
                <th className="py-3 px-4">Department</th>
                <th className="py-3 px-4">Status</th>
                <th className="py-3 px-4">Source</th>
                <th className="py-3 px-4">Resolution</th>
                <th className="py-3 px-4">Actions</th>
              </tr>
            </thead>

            <tbody>
              {escalations.map((row) => (
                <tr key={row.id} className="border-b border-slate-800">
                  <td className="py-3 px-4">
                    <p className="font-semibold">{row.title}</p>
                    <p className="text-xs text-slate-500">{row.notes || "-"}</p>
                  </td>

                  <td className="py-3 px-4">
                    {row.machine_id ? getMachineName(row.machine_id) : "-"}
                  </td>

                  <td className="py-3 px-4">
                    <span className={`rounded-full px-3 py-1 text-xs border ${severityStyle(row.severity)}`}>
                      {row.severity}
                    </span>
                  </td>

                  <td className="py-3 px-4">
                    <input
                      className="w-32 bg-slate-950 border border-slate-700 rounded-lg px-2 py-1"
                      defaultValue={row.owner}
                      onBlur={(e) =>
                        updateEscalation(
                          row.id,
                          row.status,
                          e.target.value,
                          row.department,
                          row.resolution_notes || ""
                        )
                      }
                    />
                  </td>

                  <td className="py-3 px-4">
                    <input
                      className="w-32 bg-slate-950 border border-slate-700 rounded-lg px-2 py-1"
                      defaultValue={row.department}
                      onBlur={(e) =>
                        updateEscalation(
                          row.id,
                          row.status,
                          row.owner,
                          e.target.value,
                          row.resolution_notes || ""
                        )
                      }
                    />
                  </td>

                  <td className="py-3 px-4">
                    <select
                      className={`rounded-full px-3 py-1 text-xs border bg-slate-950 ${statusStyle(row.status)}`}
                      value={row.status}
                      onChange={(e) =>
                        updateEscalation(
                          row.id,
                          e.target.value,
                          row.owner,
                          row.department,
                          row.resolution_notes || ""
                        )
                      }
                    >
                      <option>Open</option>
                      <option>In Progress</option>
                      <option>Resolved</option>
                    </select>
                  </td>

                  <td className="py-3 px-4">{row.source}</td>

                  <td className="py-3 px-4">
                    <input
                      className="w-52 bg-slate-950 border border-slate-700 rounded-lg px-2 py-1"
                      placeholder="Resolution notes"
                      defaultValue={row.resolution_notes || ""}
                      onBlur={(e) =>
                        updateEscalation(
                          row.id,
                          row.status,
                          row.owner,
                          row.department,
                          e.target.value
                        )
                      }
                    />
                  </td>

                  <td className="py-3 px-4">
                    <button
                      onClick={() => deleteEscalation(row.id)}
                      className="text-red-400 border border-red-500/40 rounded-lg px-3 py-1 hover:bg-red-500/10"
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}

              {escalations.length === 0 && (
                <tr>
                  <td colSpan={9} className="py-6 px-4 text-slate-400">
                    No escalations yet.
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
