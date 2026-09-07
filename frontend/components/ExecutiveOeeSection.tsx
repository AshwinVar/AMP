import {
  Bar,
  BarChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { ExecutiveOee } from "../lib/phase15-types";

/**
 * Was there anything to measure? `/analytics/executive-oee` answers this in
 * `has_data`, computed by `oee_contract.is_measurable` — the one rule all four
 * backend call sites use since #558. Before this, the dashboard never asked.
 */
function measured(data: ExecutiveOee | null): boolean {
  return data != null && data.has_data;
}

/**
 * One of the four POOLED plant figures, or an honest placeholder.
 *
 * Three states, not two, because three things are actually different:
 *
 *   no payload yet   "—"        nothing has been asked, let alone answered
 *   nothing to measure "Not run" the week happened and the plant did not run
 *   measured          "58%"      including a real, terrible 0%
 *
 * A plant that DID run and produced nothing usable has an OEE of 0%, and that is
 * exactly when the number matters most — so this cannot simply hide the card.
 * What it must not do is print that same 0% for a shutdown week, a bank holiday,
 * or a tenant's first morning, which is what `?? 0` did.
 *
 * Only the four pooled ratios go through here. Target, Actual, Breakdowns and
 * Achievement answer different questions with their own data presence —
 * Achievement especially, being actual-over-target, has a 0% that can be true.
 */
function pooled(data: ExecutiveOee | null, value: number | undefined): string {
  if (data == null) return "—";
  if (!data.has_data) return "Not run";
  return `${value ?? 0}%`;
}

function oeeStyle(value: number) {
  if (value >= 85) return "border-green-500/40 bg-green-500/10 text-green-300";
  if (value >= 65) return "border-yellow-500/40 bg-yellow-500/10 text-yellow-300";
  return "border-red-500/40 bg-red-500/10 text-red-300";
}

export default function ExecutiveOeeSection({ data }: { data: ExecutiveOee | null }) {
  const machineRows = data?.machine_ranking ?? [];
  const downtimeRows = data?.downtime_pareto ?? [];
  const shiftRows = data?.shift_oee ?? [];
  const qualityRows = data?.quality_trend ?? [];

  return (
    <section className="mt-8 space-y-6">
      <div>
        <h2 className="text-3xl font-bold">Executive OEE Dashboard</h2>
        <p className="text-slate-400 mt-2">
          Plant-level availability, performance, quality and OEE intelligence for management review.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 xl:grid-cols-9 gap-4">
        {/* pooled(): 0% and "did not run" are different answers, and this card
            used to give the same one to both. See the helper above. `highlight`
            is dropped when there is nothing to measure — colouring an unmeasured
            week red is the loudest way to state a number you do not have. */}
        <Kpi title="Plant OEE" value={pooled(data, data?.plant_oee)}
             highlight={measured(data) ? data?.plant_oee ?? 0 : undefined} />
        <Kpi title="Availability" value={pooled(data, data?.plant_availability)} />
        <Kpi title="Performance" value={pooled(data, data?.plant_performance)} />
        <Kpi title="Quality" value={pooled(data, data?.plant_quality)} />
        <Kpi title="Target" value={data?.production_target ?? 0} />
        <Kpi title="Actual" value={data?.production_actual ?? 0} />
        <Kpi title="Achievement" value={`${data?.production_achievement ?? 0}%`} />
        <Kpi title="Breakdowns" value={data?.breakdown_machines ?? 0} />
        {/* A machine whose gateway dropped is not producing either, and this is
            the management view of whether the plant is producing. It was shown
            here as neither running nor broken, i.e. not at all. */}
        <Kpi title="Offline" value={data?.offline_machines ?? 0} />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <ChartCard title="Machine OEE Ranking">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={machineRows}>
              <XAxis dataKey="machine_name" stroke="#94a3b8" />
              <YAxis stroke="#94a3b8" />
              <Tooltip
                contentStyle={{
                  backgroundColor: "#020617",
                  border: "1px solid #334155",
                  color: "#ffffff",
                }}
              />
              <Bar dataKey="oee" fill="#ffffff" radius={[8, 8, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>

        <ChartCard title="Downtime Pareto">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={downtimeRows}>
              <XAxis dataKey="reason" stroke="#94a3b8" />
              <YAxis stroke="#94a3b8" />
              <Tooltip
                contentStyle={{
                  backgroundColor: "#020617",
                  border: "1px solid #334155",
                  color: "#ffffff",
                }}
              />
              <Bar dataKey="minutes" fill="#ffffff" radius={[8, 8, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>

        <ChartCard title="Shift Production Efficiency">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={shiftRows}>
              <XAxis dataKey="shift_name" stroke="#94a3b8" />
              <YAxis stroke="#94a3b8" />
              <Tooltip
                contentStyle={{
                  backgroundColor: "#020617",
                  border: "1px solid #334155",
                  color: "#ffffff",
                }}
              />
              <Bar dataKey="efficiency" fill="#ffffff" radius={[8, 8, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>

        <ChartCard title="Quality Defect Trend">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={qualityRows}>
              <XAxis dataKey="defect" stroke="#94a3b8" />
              <YAxis stroke="#94a3b8" />
              <Tooltip
                contentStyle={{
                  backgroundColor: "#020617",
                  border: "1px solid #334155",
                  color: "#ffffff",
                }}
              />
              <Bar dataKey="failed_quantity" fill="#ffffff" radius={[8, 8, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>
      </div>

      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-2xl font-semibold mb-4">Machine Executive Ranking</h3>

        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full min-w-[1050px] text-left text-sm">
            <thead className="text-slate-400 border-b border-slate-800">
              <tr>
                <th className="py-3 px-4">Machine</th>
                <th className="py-3 px-4">Status</th>
                <th className="py-3 px-4">Availability</th>
                <th className="py-3 px-4">Performance</th>
                <th className="py-3 px-4">Quality</th>
                <th className="py-3 px-4">OEE</th>
                <th className="py-3 px-4">Downtime</th>
                <th className="py-3 px-4">Good</th>
                <th className="py-3 px-4">Rejects</th>
              </tr>
            </thead>

            <tbody>
              {machineRows.map((row) => (
                <tr key={row.machine_id} className="border-b border-slate-800">
                  <td className="py-3 px-4 font-semibold">{row.machine_name}</td>
                  <td className="py-3 px-4">{row.status}</td>
                  <td className="py-3 px-4">{row.availability}%</td>
                  <td className="py-3 px-4">{row.performance}%</td>
                  <td className="py-3 px-4">{row.quality}%</td>
                  <td className="py-3 px-4">
                    <span className={`rounded-full px-3 py-1 text-xs border ${oeeStyle(row.oee)}`}>
                      {row.oee}%
                    </span>
                  </td>
                  <td className="py-3 px-4">{row.downtime_minutes}m</td>
                  <td className="py-3 px-4">{row.good_count}</td>
                  <td className="py-3 px-4">{row.rejected_count}</td>
                </tr>
              ))}

              {machineRows.length === 0 && (
                <tr>
                  <td colSpan={9} className="py-6 px-4 text-slate-400">
                    No OEE data yet.
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
  highlight,
}: {
  title: string;
  value: string | number;
  highlight?: number;
}) {
  return (
    <div className={`rounded-2xl bg-slate-900 border p-5 ${
      highlight !== undefined ? oeeStyle(highlight) : "border-slate-800"
    }`}>
      <p className="text-sm opacity-80">{title}</p>
      <h3 className="text-2xl font-bold mt-2">{value}</h3>
    </div>
  );
}

function ChartCard({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5 min-w-0">
      <h3 className="text-xl font-semibold mb-4">{title}</h3>
      <div className="h-80 w-full min-w-0 overflow-hidden">{children}</div>
    </div>
  );
}
