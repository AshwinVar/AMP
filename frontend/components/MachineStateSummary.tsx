import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from "recharts";

import type { MachineStateSummary as MachineStateSummaryType } from "../lib/phase8-types";

export default function MachineStateSummary({
  data,
}: {
  data: MachineStateSummaryType[];
}) {
  return (
    <section className="mt-8 rounded-2xl bg-slate-900 border border-slate-800 p-5">
      <div className="mb-5">
        <h3 className="text-2xl font-semibold">Machine State Summary</h3>
        <p className="text-sm text-slate-400 mt-1">
          Frequency of state transitions by machine.
        </p>
      </div>

      <div className="h-[360px] min-w-0">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data}>
            <XAxis dataKey="machine_name" stroke="#94a3b8" />
            <YAxis stroke="#94a3b8" />
            <Tooltip
              contentStyle={{
                backgroundColor: "#020617",
                border: "1px solid #334155",
                color: "#ffffff",
              }}
            />
            <Legend />
            <Bar dataKey="Running" stackId="a" fill="#22c55e" />
            <Bar dataKey="Idle" stackId="a" fill="#facc15" />
            <Bar dataKey="Breakdown" stackId="a" fill="#ef4444" />
            <Bar dataKey="Maintenance" stackId="a" fill="#3b82f6" />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </section>
  );
}
