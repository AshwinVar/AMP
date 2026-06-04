import type { WorkOrder } from "../lib/phase9-types";
import type { ProductionPlan } from "../lib/phase11-types";
import type { OperatorAnalytics, OperatorJobExecution } from "../lib/mega-pack3-types";

type Machine = { id: number; name: string; status: string; utilization: number; downtime: string; };

export default function OperatorTerminalSection({ machines, workOrders, productionPlans, executions, analytics, form, setForm, createExecution, updateExecution, deleteExecution, getMachineName }: {
  machines: Machine[]; workOrders: WorkOrder[]; productionPlans: ProductionPlan[]; executions: OperatorJobExecution[]; analytics: OperatorAnalytics | null; form:any; setForm:(v:any)=>void; createExecution:(e:React.FormEvent)=>void; updateExecution:(id:number,status:string,good:number,reject:number)=>void; deleteExecution:(id:number)=>void; getMachineName:(id:number)=>string;
}) {
  return <section className="mt-8 space-y-6">
    <div><h2 className="text-3xl font-bold">Operator Terminal</h2><p className="text-slate-400 mt-2">Tablet-ready shopfloor job execution screen.</p></div>
    <div className="grid grid-cols-1 md:grid-cols-6 gap-4"><Kpi title="Jobs" value={analytics?.total_jobs ?? 0}/><Kpi title="Started" value={analytics?.started ?? 0}/><Kpi title="Paused" value={analytics?.paused ?? 0}/><Kpi title="Completed" value={analytics?.completed ?? 0}/><Kpi title="Good" value={analytics?.good_count ?? 0}/><Kpi title="Quality" value={`${analytics?.quality_rate ?? 0}%`}/></div>
    <form onSubmit={createExecution} className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-1 md:grid-cols-4 xl:grid-cols-8 gap-4">
      <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Execution No" value={form.execution_no} onChange={(e)=>setForm({...form,execution_no:e.target.value})} required/>
      <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Operator" value={form.operator_name} onChange={(e)=>setForm({...form,operator_name:e.target.value})} required/>
      <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.machine_id} onChange={(e)=>setForm({...form,machine_id:e.target.value})} required><option value="">Machine</option>{machines.map(m=><option key={m.id} value={m.id}>{m.name}</option>)}</select>
      <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.work_order_id} onChange={(e)=>setForm({...form,work_order_id:e.target.value})}><option value="">Work Order</option>{workOrders.map(w=><option key={w.id} value={w.id}>{w.work_order_no}</option>)}</select>
      <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.production_plan_id} onChange={(e)=>setForm({...form,production_plan_id:e.target.value})}><option value="">Plan</option>{productionPlans.map(p=><option key={p.id} value={p.id}>{p.plan_no}</option>)}</select>
      <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="number" placeholder="Good" value={form.good_count} onChange={(e)=>setForm({...form,good_count:Number(e.target.value)})}/>
      <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="number" placeholder="Reject" value={form.rejected_count} onChange={(e)=>setForm({...form,rejected_count:Number(e.target.value)})}/>
      <button className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3">Start Job</button>
    </form>
    <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">{executions.map(row=><div key={row.id} className="rounded-2xl bg-slate-900 border border-slate-800 p-5"><div className="flex items-start justify-between"><div><p className="text-sm text-slate-400">{row.execution_no}</p><h3 className="text-xl font-bold">{getMachineName(row.machine_id)}</h3><p className="text-slate-400">Operator: {row.operator_name}</p></div><select className="bg-slate-950 border border-slate-700 rounded-lg px-3 py-2" value={row.job_status} onChange={(e)=>updateExecution(row.id,e.target.value,row.good_count,row.rejected_count)}><option>Started</option><option>Paused</option><option>Completed</option></select></div><div className="mt-4 grid grid-cols-2 gap-3"><input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="number" defaultValue={row.good_count} onBlur={(e)=>updateExecution(row.id,row.job_status,Number(e.target.value),row.rejected_count)}/><input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="number" defaultValue={row.rejected_count} onBlur={(e)=>updateExecution(row.id,row.job_status,row.good_count,Number(e.target.value))}/></div><button onClick={()=>deleteExecution(row.id)} className="mt-4 text-red-400 border border-red-500/40 rounded-lg px-3 py-1">Delete</button></div>)}</div>
  </section>;
}
function Kpi({title,value}:{title:string;value:string|number}){return <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5"><p className="text-slate-400 text-sm">{title}</p><h3 className="text-2xl font-bold mt-2">{value}</h3></div>;}
