import type { MaintenanceAnalytics, MaintenanceTask } from "../lib/mega-pack1-types";

type Machine = { id: number; name: string; status: string; utilization: number; downtime: string; };

export default function MaintenanceSection({ machines, tasks, analytics, form, setForm, createTask, updateTask, deleteTask, generateOverdueEscalations, getMachineName }: {
  machines: Machine[]; tasks: MaintenanceTask[]; analytics: MaintenanceAnalytics | null; form: any; setForm: (value: any) => void; createTask: (e: React.FormEvent) => void; updateTask: (id: number, status: string, downtime?: number) => void; deleteTask: (id: number) => void; generateOverdueEscalations: () => void; getMachineName: (id: number) => string;
}) {
  return (
    <section className="mt-8 space-y-6">
      <div className="flex flex-col md:flex-row md:items-end md:justify-between gap-4">
        <div><h2 className="text-3xl font-bold">Maintenance / CMMS</h2><p className="text-slate-400 mt-2">Preventive maintenance, breakdown tasks, service history and MTTR tracking.</p></div>
        <button onClick={generateOverdueEscalations} className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3">Generate Overdue Escalations</button>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-4 xl:grid-cols-8 gap-4">
        <Kpi title="Tasks" value={analytics?.total_tasks ?? 0}/><Kpi title="Open" value={analytics?.open ?? 0}/><Kpi title="In Progress" value={analytics?.in_progress ?? 0}/><Kpi title="Completed" value={analytics?.completed ?? 0}/><Kpi title="Overdue" value={analytics?.overdue ?? 0}/><Kpi title="PM" value={analytics?.preventive ?? 0}/><Kpi title="Breakdown" value={analytics?.breakdown ?? 0}/><Kpi title="Avg Repair" value={`${analytics?.avg_repair_minutes ?? 0}m`}/>
      </div>
      <form onSubmit={createTask} className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-1 md:grid-cols-4 xl:grid-cols-8 gap-4">
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Task No" value={form.task_no} onChange={(e) => setForm({...form, task_no:e.target.value})} required/>
        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.machine_id} onChange={(e)=>setForm({...form,machine_id:e.target.value})} required><option value="">Machine</option>{machines.map(m=><option key={m.id} value={m.id}>{m.name}</option>)}</select>
        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.task_type} onChange={(e)=>setForm({...form,task_type:e.target.value})}><option>Preventive</option><option>Breakdown</option><option>Calibration</option><option>Inspection</option></select>
        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.priority} onChange={(e)=>setForm({...form,priority:e.target.value})}><option>Critical</option><option>High</option><option>Medium</option><option>Low</option></select>
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Assigned To" value={form.assigned_to} onChange={(e)=>setForm({...form,assigned_to:e.target.value})} required/>
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="date" value={form.planned_date} onChange={(e)=>setForm({...form,planned_date:e.target.value})} required/>
        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.status} onChange={(e)=>setForm({...form,status:e.target.value})}><option>Open</option><option>In Progress</option><option>Completed</option></select>
        <button type="submit" className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3">Add Task</button>
      </form>
      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5 overflow-x-auto"><table className="w-full min-w-[1050px] text-left text-sm"><thead className="text-slate-400 border-b border-slate-800"><tr>{["Task","Machine","Type","Priority","Assigned","Planned","Downtime","Status","Actions"].map(h=><th key={h} className="py-3 px-4">{h}</th>)}</tr></thead><tbody>{tasks.map(row=><tr key={row.id} className="border-b border-slate-800"><td className="py-3 px-4 font-semibold">{row.task_no}</td><td className="py-3 px-4">{getMachineName(row.machine_id)}</td><td className="py-3 px-4">{row.task_type}</td><td className="py-3 px-4">{row.priority}</td><td className="py-3 px-4">{row.assigned_to}</td><td className="py-3 px-4">{row.planned_date}</td><td className="py-3 px-4"><input className="w-20 bg-slate-950 border border-slate-700 rounded-lg px-2 py-1" type="number" defaultValue={row.downtime_minutes} onBlur={(e)=>updateTask(row.id,row.status,Number(e.target.value))}/></td><td className="py-3 px-4"><select className="bg-slate-950 border border-slate-700 rounded-lg px-2 py-1" value={row.status} onChange={(e)=>updateTask(row.id,e.target.value,row.downtime_minutes)}><option>Open</option><option>In Progress</option><option>Completed</option></select></td><td className="py-3 px-4"><button onClick={()=>deleteTask(row.id)} className="text-red-400 border border-red-500/40 rounded-lg px-3 py-1">Delete</button></td></tr>)}</tbody></table></div>
    </section>
  );
}
function Kpi({title,value}:{title:string;value:string|number}){return <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5"><p className="text-slate-400 text-sm">{title}</p><h3 className="text-2xl font-bold mt-2">{value}</h3></div>;}
