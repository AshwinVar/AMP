import type { CostingAnalytics, CostRecord } from "../lib/mega-pack3-types";

export default function CostingSection({ costs, analytics, form, setForm, createCost, updateCost, deleteCost }: {
  costs: CostRecord[]; analytics: CostingAnalytics | null; form: any; setForm:(v:any)=>void; createCost:(e:React.FormEvent)=>void; updateCost:(id:number,amount:number)=>void; deleteCost?:(id:number)=>void;
}) {
  return <section className="mt-8 space-y-6">
    <div><h2 className="text-3xl font-bold">ERP-lite Costing</h2><p className="text-slate-400 mt-2">Production costing, material costing, supplier spend and profitability base layer.</p></div>
    <div className="grid grid-cols-1 md:grid-cols-5 gap-4"><Kpi title="Cost Records" value={analytics?.total_cost_records ?? 0}/><Kpi title="Manual Cost" value={`£${analytics?.manual_cost_total ?? 0}`}/><Kpi title="Good Units" value={analytics?.production_units ?? 0}/><Kpi title="Cost / Unit" value={analytics?.cost_per_good_unit != null ? `£${analytics.cost_per_good_unit.toFixed(2)}` : "—"}/><Kpi title="Receipt Units" value={analytics?.supplier_receipt_units ?? 0}/></div>
    <form onSubmit={createCost} className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-1 md:grid-cols-6 gap-4">
      <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Cost No" value={form.cost_no} onChange={(e)=>setForm({...form,cost_no:e.target.value})} required/>
      <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.cost_type} onChange={(e)=>setForm({...form,cost_type:e.target.value})}><option>Material</option><option>Labour</option><option>Machine</option><option>Overhead</option><option>Supplier</option></select>
      <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Description" value={form.description} onChange={(e)=>setForm({...form,description:e.target.value})} required/>
      <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="number" placeholder="Amount" value={form.amount} onChange={(e)=>setForm({...form,amount:Number(e.target.value)})}/>
      <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Department" value={form.department} onChange={(e)=>setForm({...form,department:e.target.value})}/>
      <button className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3">Add Cost</button>
    </form>
    <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5 overflow-x-auto"><table className="w-full min-w-[850px] text-left text-sm"><thead className="text-slate-400 border-b border-slate-800"><tr>{["Cost No","Type","Description","Amount","Department","Actions"].map(h=><th key={h} className="py-3 px-4">{h}</th>)}</tr></thead><tbody>{costs.map(row=><tr key={row.id} className="border-b border-slate-800"><td className="py-3 px-4 font-semibold">{row.cost_no}</td><td className="py-3 px-4">{row.cost_type}</td><td className="py-3 px-4">{row.description}</td><td className="py-3 px-4"><input className="w-24 bg-slate-950 border border-slate-700 rounded-lg px-2 py-1" type="number" defaultValue={row.amount} onBlur={(e)=>updateCost(row.id,Number(e.target.value))}/></td><td className="py-3 px-4">{row.department ?? "-"}</td><td className="py-3 px-4"><button onClick={()=>deleteCost?.(row.id)} className="text-red-400 border border-red-500/40 rounded-lg px-3 py-1">Delete</button></td></tr>)}</tbody></table></div>
  </section>;
}
function Kpi({title,value}:{title:string;value:string|number}){return <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5"><p className="text-slate-400 text-sm">{title}</p><h3 className="text-2xl font-bold mt-2">{value}</h3></div>;}
