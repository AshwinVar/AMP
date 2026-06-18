import type { WorkOrder } from "../lib/phase9-types";
import type { ProductionPlan } from "../lib/phase11-types";
import type { ProductionSchedule, ScheduleAnalytics } from "../lib/mega-pack1-types";

type Machine = { id: number; name: string; status: string; utilization: number; downtime: string; };

export default function SchedulingSection({ machines, workOrders, productionPlans, schedules, analytics, form, setForm, createSchedule, updateSchedule, deleteSchedule, getMachineName }: {
  machines: Machine[]; workOrders: WorkOrder[]; productionPlans: ProductionPlan[]; schedules: ProductionSchedule[]; analytics: ScheduleAnalytics | null; form: any; setForm: (value:any)=>void; createSchedule: (e:React.FormEvent)=>void; updateSchedule: (id:number,status:string,priority?:string)=>void; deleteSchedule?:(id:number)=>void; getMachineName:(id:number)=>string;
}) {
  return (
    <section className="mt-8 space-y-6">
      <div><h2 className="text-3xl font-bold">Smart Production Scheduling</h2><p className="text-slate-400 mt-2">Capacity planning, machine allocation, shift loading and bottleneck visibility.</p></div>
      <div className="grid grid-cols-1 md:grid-cols-4 xl:grid-cols-7 gap-4"><Kpi title="Schedules" value={analytics?.total_schedules ?? 0}/><Kpi title="Scheduled" value={analytics?.scheduled ?? 0}/><Kpi title="Running" value={analytics?.running ?? 0}/><Kpi title="Completed" value={analytics?.completed ?? 0}/><Kpi title="Delayed" value={analytics?.delayed ?? 0}/><Kpi title="Qty" value={analytics?.total_quantity ?? 0}/><Kpi title="Load" value={`${analytics?.total_minutes ?? 0}m`}/></div>
      <form onSubmit={createSchedule} className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-1 md:grid-cols-4 xl:grid-cols-9 gap-4">
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Schedule No" value={form.schedule_no} onChange={(e)=>setForm({...form,schedule_no:e.target.value})} required/>
        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.work_order_id} onChange={(e)=>setForm({...form,work_order_id:e.target.value})}><option value="">Work Order</option>{workOrders.map(w=><option key={w.id} value={w.id}>{w.work_order_no}</option>)}</select>
        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.production_plan_id} onChange={(e)=>setForm({...form,production_plan_id:e.target.value})}><option value="">Production Plan</option>{productionPlans.map(p=><option key={p.id} value={p.id}>{p.plan_no}</option>)}</select>
        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.machine_id} onChange={(e)=>setForm({...form,machine_id:e.target.value})} required><option value="">Machine</option>{machines.map(m=><option key={m.id} value={m.id}>{m.name}</option>)}</select>
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Shift" value={form.shift_name} onChange={(e)=>setForm({...form,shift_name:e.target.value})} required/>
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="date" value={form.scheduled_date} onChange={(e)=>setForm({...form,scheduled_date:e.target.value})} required/>
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="number" placeholder="Qty" value={form.planned_quantity} onChange={(e)=>setForm({...form,planned_quantity:Number(e.target.value)})} required/>
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="number" placeholder="Minutes" value={form.estimated_minutes} onChange={(e)=>setForm({...form,estimated_minutes:Number(e.target.value)})}/>
        <button type="submit" className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3">Schedule</button>
      </form>
      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5 overflow-x-auto"><table className="w-full min-w-[1100px] text-left text-sm"><thead className="text-slate-400 border-b border-slate-800"><tr>{["Schedule","Machine","Shift","Date","Priority","Qty","Minutes","Status","Actions"].map(h=><th key={h} className="py-3 px-4">{h}</th>)}</tr></thead><tbody>{schedules.map(row=><tr key={row.id} className="border-b border-slate-800"><td className="py-3 px-4 font-semibold">{row.schedule_no}</td><td className="py-3 px-4">{getMachineName(row.machine_id)}</td><td className="py-3 px-4">{row.shift_name}</td><td className="py-3 px-4">{row.scheduled_date}</td><td className="py-3 px-4"><select className="bg-slate-950 border border-slate-700 rounded-lg px-2 py-1" value={row.priority} onChange={(e)=>updateSchedule(row.id,row.status,e.target.value)}><option>Critical</option><option>High</option><option>Medium</option><option>Low</option></select></td><td className="py-3 px-4">{row.planned_quantity}</td><td className="py-3 px-4">{row.estimated_minutes}</td><td className="py-3 px-4"><select className="bg-slate-950 border border-slate-700 rounded-lg px-2 py-1" value={row.status} onChange={(e)=>updateSchedule(row.id,e.target.value,row.priority)}><option>Scheduled</option><option>Running</option><option>Completed</option><option>Delayed</option></select></td><td className="py-3 px-4"><button onClick={()=>deleteSchedule?.(row.id)} className="text-red-400 border border-red-500/40 rounded-lg px-3 py-1">Delete</button></td></tr>)}</tbody></table></div>
    </section>
  );
}
function Kpi({title,value}:{title:string;value:string|number}){return <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5"><p className="text-slate-400 text-sm">{title}</p><h3 className="text-2xl font-bold mt-2">{value}</h3></div>;}
