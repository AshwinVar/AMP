import { BTN_CLASS } from "../lib/utils";

export default function ReportsSection({
  canDownloadReports,
  downloadReport,
}: {
  canDownloadReports: boolean;
  downloadReport: (path: string, filename: string) => void;
}) {
  return (
    <section className="mt-8 rounded-2xl bg-slate-900 border border-slate-800 p-6">
      <h3 className="text-2xl font-semibold mb-2">Reports Export</h3>

      <p className="text-slate-400 mb-6">
        Export downtime, shift, OEE and daily factory reports.
      </p>

      {canDownloadReports ? (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
          <button onClick={() => downloadReport("/reports/downtime.csv", "downtime_report.csv")} className={BTN_CLASS}>
            Downtime CSV
          </button>

          <button onClick={() => downloadReport("/reports/shifts.csv", "shift_report.csv")} className={BTN_CLASS}>
            Shift CSV
          </button>

          <button onClick={() => downloadReport("/reports/oee.csv", "oee_report.csv")} className={BTN_CLASS}>
            OEE CSV
          </button>

          <button onClick={() => downloadReport("/reports/daily-summary.txt", "daily_summary_report.txt")} className={BTN_CLASS}>
            Daily Summary
          </button>
        </div>
      ) : (
        <div className="rounded-xl border border-yellow-500/30 bg-yellow-500/10 p-4 text-yellow-300">
          You do not have permission to download reports.
        </div>
      )}
    </section>
  );
}
