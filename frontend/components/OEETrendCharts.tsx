import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  BarChart,
  Bar,
} from "recharts";

export default function OEETrendCharts({
  oeeTrends,
}: {
  oeeTrends: any[];
}) {
  const latest = oeeTrends.slice(-60);

  return (
    <section className="mt-8 grid grid-cols-1 xl:grid-cols-2 gap-6">
      <ChartCard title="OEE Intelligence Trend">
        <ResponsiveContainer width="100%" height={320}>
          <LineChart data={latest}>
            <XAxis dataKey="record" stroke="#94a3b8" />
            <YAxis stroke="#94a3b8" />
            <Tooltip contentStyle={{ backgroundColor: "#020617", border: "1px solid #334155", color: "#ffffff" }} />
            <Line type="monotone" dataKey="oee" stroke="#ffffff" strokeWidth={3} dot={false} />
            <Line type="monotone" dataKey="availability" stroke="#38bdf8" strokeWidth={2} dot={false} />
            <Line type="monotone" dataKey="performance" stroke="#f97316" strokeWidth={2} dot={false} />
            <Line type="monotone" dataKey="quality" stroke="#22c55e" strokeWidth={2} dot={false} />
          </LineChart>
        </ResponsiveContainer>
      </ChartCard>

      <ChartCard title="Good vs Rejected Output">
        <ResponsiveContainer width="100%" height={320}>
          <BarChart data={latest}>
            <XAxis dataKey="record" stroke="#94a3b8" />
            <YAxis stroke="#94a3b8" />
            <Tooltip contentStyle={{ backgroundColor: "#020617", border: "1px solid #334155", color: "#ffffff" }} />
            <Bar dataKey="good_count" fill="#ffffff" />
            <Bar dataKey="rejected_count" fill="#ef4444" />
          </BarChart>
        </ResponsiveContainer>
      </ChartCard>
    </section>
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
      <h3 className="text-2xl font-semibold mb-4">{title}</h3>
      <div className="w-full min-w-0 overflow-hidden">{children}</div>
    </div>
  );
}
