import type { FactoryCommandCenter, FactoryLayoutNode } from "../lib/phase16-types";

type Machine = {
  id: number;
  name: string;
  status: string;
  utilization: number;
  downtime: string;
};

function statusStyle(status: string) {
  switch (status) {
    case "Running":
      return "border-green-500/60 bg-green-500/20 text-green-300";
    case "Breakdown":
      return "border-red-500/60 bg-red-500/20 text-red-300";
    case "Maintenance":
      return "border-blue-500/60 bg-blue-500/20 text-blue-300";
    case "Idle":
      return "border-yellow-500/60 bg-yellow-500/20 text-yellow-300";
    default:
      return "border-slate-500/60 bg-slate-500/20 text-slate-300";
  }
}

export default function DigitalTwinSection({
  machines,
  nodes,
  commandCenter,
  form,
  setForm,
  createNode,
  updateNode,
  deleteNode,
  autoGenerateLayout,
}: {
  machines: Machine[];
  nodes: FactoryLayoutNode[];
  commandCenter: FactoryCommandCenter | null;
  form: {
    machine_id: string;
    node_name: string;
    node_type: string;
    x_position: number;
    y_position: number;
    width: number;
    height: number;
    zone: string;
  };
  setForm: (value: any) => void;
  createNode: (e: React.FormEvent) => void;
  updateNode: (id: number, x: number, y: number, zone: string) => void;
  deleteNode?: (id: number) => void;
  autoGenerateLayout: () => void;
}) {
  function machineForNode(node: FactoryLayoutNode) {
    return machines.find((machine) => machine.id === node.machine_id);
  }

  function statusForNode(node: FactoryLayoutNode) {
    return machineForNode(node)?.status || "Area";
  }

  return (
    <section className="mt-8 space-y-6">
      <div className="flex flex-col md:flex-row md:items-end md:justify-between gap-4">
        <div>
          <h2 className="text-3xl font-bold">Digital Twin Command Center</h2>
          <p className="text-slate-400 mt-2">
            Live factory floor visualization with machine status, hotspots and command KPIs.
          </p>
        </div>

        <button
          type="button"
          onClick={autoGenerateLayout}
          className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3"
        >
          Auto Generate Layout
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 xl:grid-cols-10 gap-4">
        <Kpi title="Machines" value={commandCenter?.machines ?? 0} />
        <Kpi title="Running" value={commandCenter?.running ?? 0} />
        <Kpi title="Breakdown" value={commandCenter?.breakdown ?? 0} />
        <Kpi title="Idle" value={commandCenter?.idle ?? 0} />
        <Kpi title="Maintenance" value={commandCenter?.maintenance ?? 0} />
        <Kpi title="Downtime" value={`${commandCenter?.total_downtime_minutes ?? 0}m`} />
        <Kpi title="Work Orders" value={commandCenter?.active_work_orders ?? 0} />
        <Kpi title="Behind Plans" value={commandCenter?.behind_plans ?? 0} />
        <Kpi title="Escalations" value={commandCenter?.open_escalations ?? 0} />
        <Kpi title="Quality Fail" value={`${commandCenter?.quality_fail_rate ?? 0}%`} />
      </div>

      <form
        onSubmit={createNode}
        className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-1 md:grid-cols-4 xl:grid-cols-8 gap-4"
      >
        <select
          className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
          value={form.machine_id}
          onChange={(e) => {
            const machine = machines.find((m) => String(m.id) === e.target.value);
            setForm({
              ...form,
              machine_id: e.target.value,
              node_name: machine ? machine.name : form.node_name,
            });
          }}
        >
          <option value="">No Machine</option>
          {machines.map((machine) => (
            <option key={machine.id} value={machine.id}>{machine.name}</option>
          ))}
        </select>

        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Node name" value={form.node_name} onChange={(e) => setForm({ ...form, node_name: e.target.value })} required />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Zone" value={form.zone} onChange={(e) => setForm({ ...form, zone: e.target.value })} />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="number" placeholder="X" value={form.x_position} onChange={(e) => setForm({ ...form, x_position: Number(e.target.value) })} />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="number" placeholder="Y" value={form.y_position} onChange={(e) => setForm({ ...form, y_position: Number(e.target.value) })} />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="number" placeholder="Width" value={form.width} onChange={(e) => setForm({ ...form, width: Number(e.target.value) })} />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="number" placeholder="Height" value={form.height} onChange={(e) => setForm({ ...form, height: Number(e.target.value) })} />

        <button type="submit" className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3">
          Add Node
        </button>
      </form>

      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <div className="flex items-center justify-between gap-4 mb-4">
          <h3 className="text-2xl font-semibold">Factory Floor Live Map</h3>
          <p className="text-sm text-slate-400">Canvas: 1200 x 640</p>
        </div>

        <div className="relative w-full h-[640px] bg-slate-950 border border-slate-800 rounded-2xl overflow-auto">
          <div className="relative w-[1200px] h-[640px] bg-[radial-gradient(circle_at_1px_1px,#334155_1px,transparent_0)] [background-size:24px_24px]">
            {nodes.map((node) => {
              const machine = machineForNode(node);
              const status = statusForNode(node);

              return (
                <div
                  key={node.id}
                  className={`absolute rounded-2xl border p-3 shadow-xl ${statusStyle(status)}`}
                  style={{
                    left: node.x_position,
                    top: node.y_position,
                    width: node.width,
                    height: node.height,
                  }}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <p className="font-bold">{node.node_name}</p>
                      <p className="text-xs opacity-80">{node.zone}</p>
                    </div>
                    <button onClick={() => deleteNode?.(node.id)} className="text-xs border border-red-400/50 rounded-lg px-2 py-1 text-red-300">
                      X
                    </button>
                  </div>

                  <div className="mt-3 text-xs space-y-1">
                    <p>Status: {status}</p>
                    <p>Util: {machine?.utilization ?? 0}%</p>
                    <p>Downtime: {machine?.downtime ?? "-"}</p>
                  </div>
                </div>
              );
            })}

            {nodes.length === 0 && (
              <div className="absolute inset-0 flex items-center justify-center text-slate-500">
                No layout nodes yet. Click Auto Generate Layout.
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-2xl font-semibold mb-4">Layout Node Controls</h3>
        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full min-w-[900px] text-left text-sm">
            <thead className="text-slate-400 border-b border-slate-800">
              <tr>
                <th className="py-3 px-4">Node</th>
                <th className="py-3 px-4">Machine</th>
                <th className="py-3 px-4">Zone</th>
                <th className="py-3 px-4">X</th>
                <th className="py-3 px-4">Y</th>
                <th className="py-3 px-4">Status</th>
                <th className="py-3 px-4">Update</th>
              </tr>
            </thead>

            <tbody>
              {nodes.map((node) => (
                <tr key={node.id} className="border-b border-slate-800">
                  <td className="py-3 px-4 font-semibold">{node.node_name}</td>
                  <td className="py-3 px-4">{machineForNode(node)?.name || "-"}</td>
                  <td className="py-3 px-4">{node.zone}</td>
                  <td className="py-3 px-4">{node.x_position}</td>
                  <td className="py-3 px-4">{node.y_position}</td>
                  <td className="py-3 px-4">{statusForNode(node)}</td>
                  <td className="py-3 px-4">
                    <button
                      onClick={() => updateNode(node.id, node.x_position + 20, node.y_position, node.zone)}
                      className="rounded-lg border border-white/30 px-3 py-1 hover:bg-white/10"
                    >
                      Move +X
                    </button>
                  </td>
                </tr>
              ))}

              {nodes.length === 0 && (
                <tr>
                  <td colSpan={7} className="py-6 px-4 text-slate-400">No layout nodes yet.</td>
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
      <h3 className="text-xl font-bold mt-2">{value}</h3>
    </div>
  );
}
