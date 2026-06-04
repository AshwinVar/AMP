export default function ShiftKPIs({
  shiftKpis,
}: {
  shiftKpis: any[];
}) {
  const visibleRows = shiftKpis.slice(0, 20);

  return (
    <section className="mt-8 rounded-2xl bg-slate-900 border border-slate-800 p-5">
      <div className="mb-4">
        <h3 className="text-2xl font-semibold">Shift KPI Dashboard</h3>
        <p className="text-sm text-slate-400 mt-1">Shift-level target, output and efficiency comparison.</p>
      </div>

      <div className="overflow-x-auto rounded-xl border border-slate-800">
        <table className="w-full min-w-[720px] text-left text-sm">
          <thead className="bg-slate-900 text-slate-400 border-b border-slate-800">
            <tr>
              <th className="py-3 px-4">Shift</th>
              <th className="py-3 px-4">Target</th>
              <th className="py-3 px-4">Actual</th>
              <th className="py-3 px-4">Efficiency</th>
              <th className="py-3 px-4">Gap</th>
            </tr>
          </thead>

          <tbody>
            {visibleRows.map((shift, index) => (
              <tr key={`${shift.shift_name}-${index}`} className="border-b border-slate-800">
                <td className="py-3 px-4 font-medium">{shift.shift_name}</td>
                <td className="py-3 px-4">{shift.target_output}</td>
                <td className="py-3 px-4">{shift.actual_output}</td>
                <td className={`py-3 px-4 font-bold ${shift.efficiency < 70 ? "text-red-400" : shift.efficiency < 90 ? "text-yellow-300" : "text-green-400"}`}>
                  {shift.efficiency}%
                </td>
                <td className="py-3 px-4">{shift.gap}</td>
              </tr>
            ))}

            {visibleRows.length === 0 && (
              <tr>
                <td colSpan={5} className="py-6 px-4 text-slate-400">No shift KPI data yet.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
