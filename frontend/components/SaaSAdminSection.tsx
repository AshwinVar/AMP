import type { CompanyTenant, SaaSAnalytics } from "../lib/mega-pack3-types";

export default function SaaSAdminSection({ tenants, analytics, form, setForm, createTenant, updateTenant, deleteTenant, provisionAdmin, adminCreds, clearAdminCreds }: {
  tenants: CompanyTenant[]; analytics: SaaSAnalytics | null; form: any; setForm: (v:any)=>void; createTenant:(e:React.FormEvent)=>void; updateTenant:(id:number,status:string)=>void; deleteTenant?:(id:number)=>void;
  provisionAdmin?:(id:number)=>void; adminCreds?:{username:string; temporary_password:string; company_code:string} | null; clearAdminCreds?:()=>void;
}) {
  return <section className="mt-8 space-y-6">
    <div><h2 className="text-3xl font-bold">SaaS Admin</h2><p className="text-slate-400 mt-2">Companies, subscriptions, seats and MRR tracking.</p></div>
    {adminCreds && (
      <div className="rounded-2xl border border-emerald-500/40 bg-emerald-500/10 p-5 flex flex-wrap items-center gap-4">
        <div>
          <p className="font-semibold text-emerald-300">Admin login created for {adminCreds.company_code}</p>
          <p className="text-sm text-slate-300 mt-1">Username <code className="bg-slate-950 px-2 py-0.5 rounded">{adminCreds.username}</code> · Temporary password <code className="bg-slate-950 px-2 py-0.5 rounded">{adminCreds.temporary_password}</code></p>
          <p className="text-xs text-amber-300 mt-1">Shown once — copy it now and share securely. The customer should change it after first login.</p>
        </div>
        <button onClick={() => navigator.clipboard?.writeText(`${adminCreds.username} / ${adminCreds.temporary_password}`)} className="rounded-lg border border-emerald-500/40 px-3 py-1 text-sm text-emerald-300">Copy</button>
        <button onClick={clearAdminCreds} className="rounded-lg border border-slate-600 px-3 py-1 text-sm text-slate-300">Dismiss</button>
      </div>
    )}
    <div className="grid grid-cols-1 md:grid-cols-4 xl:grid-cols-7 gap-4"><Kpi title="Tenants" value={analytics?.total_tenants ?? 0}/><Kpi title="Trial" value={analytics?.trial ?? 0}/><Kpi title="Active" value={analytics?.active ?? 0}/><Kpi title="Past Due" value={analytics?.past_due ?? 0}/><Kpi title="Cancelled" value={analytics?.cancelled ?? 0}/><Kpi title="MRR" value={`£${analytics?.monthly_recurring_revenue ?? 0}`}/><Kpi title="Seats" value={analytics?.total_seats ?? 0}/></div>
    <form onSubmit={createTenant} className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-1 md:grid-cols-4 xl:grid-cols-8 gap-4">
      <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Company Code" value={form.company_code} onChange={(e)=>setForm({...form,company_code:e.target.value})} required/>
      <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Company Name" value={form.company_name} onChange={(e)=>setForm({...form,company_name:e.target.value})} required/>
      <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Industry" value={form.industry} onChange={(e)=>setForm({...form,industry:e.target.value})}/>
      <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.plan_name} onChange={(e)=>setForm({...form,plan_name:e.target.value})}><option>Starter</option><option>Professional</option><option>Enterprise</option></select>
      <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.subscription_status} onChange={(e)=>setForm({...form,subscription_status:e.target.value})}><option>Trial</option><option>Active</option><option>Past Due</option><option>Cancelled</option></select>
      <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="number" placeholder="Seats" value={form.seats} onChange={(e)=>setForm({...form,seats:Number(e.target.value)})}/>
      <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="number" placeholder="Monthly Fee" value={form.monthly_fee} onChange={(e)=>setForm({...form,monthly_fee:Number(e.target.value)})}/>
      <button className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3">Add Tenant</button>
    </form>
    <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5 overflow-x-auto"><table className="w-full min-w-[900px] text-left text-sm"><thead className="text-slate-400 border-b border-slate-800"><tr>{["Code","Company","Industry","Plan","Status","Seats","Fee","Actions"].map(h=><th key={h} className="py-3 px-4">{h}</th>)}</tr></thead><tbody>{tenants.map(row=><tr key={row.id} className="border-b border-slate-800"><td className="py-3 px-4 font-semibold">{row.company_code}</td><td className="py-3 px-4">{row.company_name}</td><td className="py-3 px-4">{row.industry ?? "-"}</td><td className="py-3 px-4">{row.plan_name}</td><td className="py-3 px-4"><select className="bg-slate-950 border border-slate-700 rounded-lg px-2 py-1" value={row.subscription_status} onChange={(e)=>updateTenant(row.id,e.target.value)}><option>Trial</option><option>Active</option><option>Past Due</option><option>Cancelled</option></select>{row.subscription_status === "Trial" && row.trial_days_left != null && <div className={`text-xs mt-1 ${row.trial_days_left <= 5 ? "text-amber-400" : "text-slate-400"}`}>{row.trial_days_left > 0 ? `${row.trial_days_left}d left` : "expired — logins blocked"}</div>}</td><td className="py-3 px-4">{row.seats}</td><td className="py-3 px-4">£{row.monthly_fee}</td><td className="py-3 px-4 whitespace-nowrap"><button onClick={()=>provisionAdmin?.(row.id)} className="text-emerald-300 border border-emerald-500/40 rounded-lg px-3 py-1 mr-2">Create admin</button><button onClick={()=>deleteTenant?.(row.id)} className="text-red-400 border border-red-500/40 rounded-lg px-3 py-1">Delete</button></td></tr>)}</tbody></table></div>
  </section>;
}
function Kpi({title,value}:{title:string;value:string|number}){return <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5"><p className="text-slate-400 text-sm">{title}</p><h3 className="text-2xl font-bold mt-2">{value}</h3></div>;}
