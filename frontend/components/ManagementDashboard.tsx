import KpiCard from "./KpiCard";

export default function ManagementDashboard({
  managementSummary,
}: {
  managementSummary: any;
}) {
  if (!managementSummary) {
    return (
      <section className="rounded-2xl bg-slate-900 border border-slate-800 p-6">
        <h3 className="text-2xl font-semibold">Management Dashboard</h3>
        <p className="text-slate-400 mt-2">No management data available yet.</p>
      </section>
    );
  }

  return (
    <section className="mt-8">
      <div className="mb-5">
        <h3 className="text-2xl font-semibold">Management Dashboard</h3>
        <p className="text-slate-400 mt-1">Executive view of factory performance, production loss and key constraints.</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 xl:grid-cols-4 gap-4">
        <KpiCard title="Average OEE" value={`${managementSummary.avg_oee}%`} />
        <KpiCard title="Target Achievement" value={`${managementSummary.target_achievement}%`} />
        <KpiCard title="Top Loss Reason" value={managementSummary.top_loss_reason} small />
        <KpiCard title="Worst Machine" value={managementSummary.worst_machine} small />
        <KpiCard title="Downtime Loss" value={`£${managementSummary.estimated_loss_value}`} />
        <KpiCard title="Total Downtime" value={`${managementSummary.total_downtime_minutes}m`} />
        <KpiCard title="Actual Output" value={managementSummary.actual_output} />
        <KpiCard title="Target Output" value={managementSummary.target_output} />
      </div>
    </section>
  );
}
