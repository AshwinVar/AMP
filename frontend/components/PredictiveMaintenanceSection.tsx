import type { PredictiveRisk } from "../lib/phase10-types";

function riskStyle(level: string) {
  switch (level) {
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

export default function PredictiveMaintenanceSection({
  risks,
}: {
  risks: PredictiveRisk[];
}) {
  const criticalCount = risks.filter((risk) => risk.risk_level === "Critical").length;
  const highCount = risks.filter((risk) => risk.risk_level === "High").length;
  const avgRisk =
    risks.length > 0
      ? Math.round(risks.reduce((sum, risk) => sum + risk.risk_score, 0) / risks.length)
      : 0;

  return (
    <section className="mt-8 space-y-6">
      <div>
        <h2 className="text-3xl font-bold">Predictive Maintenance</h2>
        <p className="text-slate-400 mt-2">
          Failure-risk scoring based on downtime, state changes, utilization, quality loss and production load.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Kpi title="Average Risk" value={`${avgRisk}%`} />
        <Kpi title="Critical Machines" value={criticalCount} />
        <Kpi title="High Risk Machines" value={highCount} />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
        {risks.map((risk) => (
          <div
            key={risk.machine_id}
            className="rounded-2xl bg-slate-900 border border-slate-800 p-5"
          >
            <div className="flex flex-col md:flex-row md:items-start md:justify-between gap-3">
              <div>
                <h3 className="text-2xl font-semibold">{risk.machine_name}</h3>
                <p className="text-sm text-slate-400 mt-1">
                  {risk.status} | Utilization {risk.utilization}%
                </p>
              </div>

              <span className={`rounded-full border px-3 py-1 text-sm font-semibold ${riskStyle(risk.risk_level)}`}>
                {risk.risk_level} | {risk.risk_score}%
              </span>
            </div>

            <div className="mt-5 w-full h-3 rounded-full bg-slate-800">
              <div
                className="h-3 rounded-full bg-white"
                style={{ width: `${risk.risk_score}%` }}
              />
            </div>

            <div className="mt-5 grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
              <Metric label="Downtime" value={`${risk.downtime_minutes}m`} />
              <Metric label="Events" value={risk.downtime_events} />
              <Metric label="Breakdowns" value={risk.breakdown_events} />
              <Metric label="Reject Rate" value={`${risk.reject_rate}%`} />
            </div>

            <div className="mt-5 rounded-xl bg-slate-950 border border-slate-800 p-4">
              <p className="text-sm text-slate-400">Risk Drivers</p>
              <ul className="mt-2 list-disc list-inside text-sm text-slate-200 space-y-1">
                {risk.reasons.map((reason, index) => (
                  <li key={index}>{reason}</li>
                ))}
              </ul>
            </div>

            <div className="mt-4 rounded-xl bg-slate-950 border border-slate-800 p-4">
              <p className="text-sm text-slate-400">Recommended Action</p>
              <p className="mt-1 text-sm font-semibold text-white">
                {risk.recommendation}
              </p>
            </div>
          </div>
        ))}

        {risks.length === 0 && (
          <div className="rounded-2xl bg-slate-900 border border-slate-800 p-6 text-slate-400">
            No predictive maintenance data yet.
          </div>
        )}
      </div>
    </section>
  );
}

function Kpi({ title, value }: { title: string; value: string | number }) {
  return (
    <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
      <p className="text-sm text-slate-400">{title}</p>
      <h3 className="text-3xl font-bold mt-2">{value}</h3>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-950 p-3">
      <p className="text-xs text-slate-500">{label}</p>
      <p className="text-lg font-bold mt-1">{value}</p>
    </div>
  );
}
