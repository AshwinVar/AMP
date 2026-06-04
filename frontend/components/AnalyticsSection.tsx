import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  LineChart,
  Line,
  PieChart,
  Pie,
  Cell,
  Legend,
} from "recharts";

const PIE_COLORS = ["#f8fafc", "#38bdf8", "#f97316", "#ef4444", "#22c55e", "#a855f7"];

export default function AnalyticsSection({
  downtimeReasonChartData,
  downtimeTrendData,
  shiftChartData,
  oeeChartData,
}: {
  downtimeReasonChartData: { reason: string; count: number }[];
  downtimeTrendData: { event: number; minutes: number }[];
  shiftChartData: { shift: string; target: number; actual: number }[];
  oeeChartData: { record: number; oee: number; availability: number; performance: number; quality: number }[];
}) {
  const latestOee = oeeChartData.slice(-40);
  const latestDowntime = downtimeTrendData.slice(-40);

  return (
    <section className="mt-8 grid grid-cols-1 xl:grid-cols-2 gap-6">
      <ChartCard title="OEE Trend">
        <ResponsiveContainer width="100%" height={320}>
          <LineChart data={latestOee}>
            <XAxis dataKey="record" stroke="#94a3b8" />
            <YAxis stroke="#94a3b8" />
            <Tooltip contentStyle={{ backgroundColor: "#020617", border: "1px solid #334155", color: "#ffffff" }} />
            <Line type="monotone" dataKey="oee" stroke="#ffffff" strokeWidth={3} dot={false} />
            <Line type="monotone" dataKey="availability" stroke="#38bdf8" strokeWidth={2} dot={false} />
            <Line type="monotone" dataKey="quality" stroke="#22c55e" strokeWidth={2} dot={false} />
          </LineChart>
        </ResponsiveContainer>
      </ChartCard>

      <ChartCard title="Downtime Reason Pareto">
        <ResponsiveContainer width="100%" height={320}>
          <BarChart data={downtimeReasonChartData}>
            <XAxis dataKey="reason" stroke="#94a3b8" />
            <YAxis stroke="#94a3b8" />
            <Tooltip contentStyle={{ backgroundColor: "#020617", border: "1px solid #334155", color: "#ffffff" }} />
            <Bar dataKey="count" fill="#ffffff" radius={[8, 8, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </ChartCard>

      <ChartCard title="Downtime Trend">
        <ResponsiveContainer width="100%" height={320}>
          <LineChart data={latestDowntime}>
            <XAxis dataKey="event" stroke="#94a3b8" />
            <YAxis stroke="#94a3b8" />
            <Tooltip contentStyle={{ backgroundColor: "#020617", border: "1px solid #334155", color: "#ffffff" }} />
            <Line type="monotone" dataKey="minutes" stroke="#ffffff" strokeWidth={3} dot={false} />
          </LineChart>
        </ResponsiveContainer>
      </ChartCard>

      <ChartCard title="Reason Distribution">
        <ResponsiveContainer width="100%" height={320}>
          <PieChart>
            <Pie data={downtimeReasonChartData} dataKey="count" nameKey="reason" outerRadius={100} label>
              {downtimeReasonChartData.map((_, index) => (
                <Cell key={index} fill={PIE_COLORS[index % PIE_COLORS.length]} />
              ))}
            </Pie>
            <Legend />
            <Tooltip contentStyle={{ backgroundColor: "#020617", border: "1px solid #334155", color: "#ffffff" }} />
          </PieChart>
        </ResponsiveContainer>
      </ChartCard>

      <ChartCard title="Target vs Actual">
        <ResponsiveContainer width="100%" height={320}>
          <BarChart data={shiftChartData.slice(0, 20)}>
            <XAxis dataKey="shift" stroke="#94a3b8" />
            <YAxis stroke="#94a3b8" />
            <Tooltip contentStyle={{ backgroundColor: "#020617", border: "1px solid #334155", color: "#ffffff" }} />
            <Bar dataKey="target" fill="#64748b" />
            <Bar dataKey="actual" fill="#ffffff" />
          </BarChart>
        </ResponsiveContainer>
      </ChartCard>
    </section>
  );
}

function ChartCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5 min-w-0">
      <h3 className="text-2xl font-semibold mb-4">{title}</h3>
      <div className="w-full min-w-0 overflow-hidden">{children}</div>
    </div>
  );
}
