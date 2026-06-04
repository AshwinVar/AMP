import type { AuditLog, FinalExecutiveSummary, ReportRequest, SystemHealth } from "../lib/phase27-types";

export default function EnterprisePolishSection({ auditLogs, reports, health, summary, reportForm, setReportForm, createReport }: {
  auditLogs: AuditLog[];
  reports: ReportRequest[];
  health: SystemHealth | null;
  summary: FinalExecutiveSummary | null;
  reportForm: any;
  setReportForm: (v: any) => void;
  createReport: (e: React.FormEvent) => void;
}) {
  return (
    <section className="mt-8 space-y-6">
      <div>
        <h2 className="text-3xl font-bold">Enterprise Polish</h2>
        <p className="text-slate-400 mt-2">System health, reports, audit trail and final executive readiness.</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 xl:grid-cols-8 gap-4">
        <Kpi title="API" value={health?.api_status ?? "-"} />
        <Kpi title="DB" value={health?.database_status ?? "-"} />
        <Kpi title="Machines" value={summary?.machine_count ?? 0} />
        <Kpi title="Running" value={summary?.running_machines ?? 0} />
        <Kpi title="Quality" value={`${summary?.quality_rate ?? 0}%`} />
        <Kpi title="Dispatch" value={`${summary?.dispatch_rate ?? 0}%`} />
        <Kpi title="Low Stock" value={summary?.low_stock_items ?? 0} />
        <Kpi title="Cost" value={`£${summary?.total_cost ?? 0}`} />
      </div>

      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-2xl font-semibold mb-4">Enabled Modules</h3>
        <div className="flex flex-wrap gap-2">
          {(health?.modules_enabled ?? []).map((module) => (
            <span key={module} className="rounded-full border border-slate-700 bg-slate-950 px-3 py-1 text-sm text-slate-300">{module}</span>
          ))}
        </div>
      </div>

      <form onSubmit={createReport} className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-1 md:grid-cols-5 gap-4">
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Report No" value={reportForm.report_no} onChange={(e) => setReportForm({ ...reportForm, report_no: e.target.value })} required />
        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={reportForm.report_type} onChange={(e) => setReportForm({ ...reportForm, report_type: e.target.value })}>
          <option>Executive Summary</option>
          <option>OEE Report</option>
          <option>Quality Report</option>
          <option>Inventory Report</option>
          <option>Compliance Report</option>
        </select>
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Requested By" value={reportForm.requested_by} onChange={(e) => setReportForm({ ...reportForm, requested_by: e.target.value })} />
        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={reportForm.format} onChange={(e) => setReportForm({ ...reportForm, format: e.target.value })}>
          <option>PDF</option>
          <option>Excel</option>
          <option>CSV</option>
        </select>
        <button className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3">Log Report</button>
      </form>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <Table title="Report Requests" headers={["Report", "Type", "By", "Format", "Status"]}>
          {reports.map((row) => (
            <tr key={row.id} className="border-b border-slate-800">
              <td className="py-3 px-4 font-semibold">{row.report_no}</td>
              <td className="py-3 px-4">{row.report_type}</td>
              <td className="py-3 px-4">{row.requested_by}</td>
              <td className="py-3 px-4">{row.format}</td>
              <td className="py-3 px-4">{row.status}</td>
            </tr>
          ))}
        </Table>

        <Table title="Audit Logs" headers={["Actor", "Action", "Entity", "Details"]}>
          {auditLogs.map((row) => (
            <tr key={row.id} className="border-b border-slate-800">
              <td className="py-3 px-4">{row.actor}</td>
              <td className="py-3 px-4 font-semibold">{row.action}</td>
              <td className="py-3 px-4">{row.entity_type ?? "-"}</td>
              <td className="py-3 px-4 text-slate-400">{row.details ?? "-"}</td>
            </tr>
          ))}
        </Table>
      </div>
    </section>
  );
}

function Kpi({ title, value }: { title: string; value: string | number }) {
  return <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5"><p className="text-slate-400 text-sm">{title}</p><h3 className="text-xl font-bold mt-2">{value}</h3></div>;
}

function Table({ title, headers, children }: { title: string; headers: string[]; children: React.ReactNode }) {
  return <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5 overflow-x-auto"><h3 className="text-2xl font-semibold mb-4">{title}</h3><table className="w-full min-w-[650px] text-left text-sm"><thead className="text-slate-400 border-b border-slate-800"><tr>{headers.map((h) => <th key={h} className="py-3 px-4">{h}</th>)}</tr></thead><tbody>{children}</tbody></table></div>;
}
