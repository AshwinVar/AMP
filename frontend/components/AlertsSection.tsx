export default function AlertsSection({
  alerts,
}: {
  alerts: { type: string; severity: string; message: string; machine?: string }[];
}) {
  const visibleAlerts = alerts.slice(0, 20);

  return (
    <section className="mt-8 rounded-2xl bg-slate-900 border border-slate-800 p-6">
      <div className="mb-6">
        <h3 className="text-2xl font-semibold">Factory Alerts</h3>
        <p className="text-slate-400 mt-2">Deduplicated live exception monitoring for breakdowns, low utilization, low OEE and quality loss.</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {visibleAlerts.map((alert, index) => (
          <div key={`${alert.type}-${alert.machine || index}-${index}`} className={`rounded-2xl p-4 border ${alert.severity === "High" ? "border-red-500/30 bg-red-500/10" : "border-yellow-500/30 bg-yellow-500/10"}`}>
            <div className="flex items-center justify-between gap-3">
              <p className="text-sm font-semibold text-white">{alert.type}</p>
              <span className={`text-xs px-2 py-1 rounded-full border ${alert.severity === "High" ? "border-red-500/40 text-red-300" : "border-yellow-500/40 text-yellow-300"}`}>{alert.severity}</span>
            </div>

            {alert.machine && <p className="text-xs text-slate-500 mt-2">Machine: {alert.machine}</p>}
            <p className="text-sm text-slate-300 mt-3">{alert.message}</p>
          </div>
        ))}

        {visibleAlerts.length === 0 && (
          <div className="col-span-full rounded-2xl border border-slate-800 p-6 text-slate-400">No active factory alerts.</div>
        )}
      </div>
    </section>
  );
}
