import type { AIInsights, AIRecommendation } from "../lib/mega-pack2-types";

function severityStyle(severity:string){ if(severity==="Critical") return "border-red-500/40 bg-red-500/10 text-red-300"; if(severity==="High") return "border-orange-500/40 bg-orange-500/10 text-orange-300"; if(severity==="Medium") return "border-yellow-500/40 bg-yellow-500/10 text-yellow-300"; return "border-green-500/40 bg-green-500/10 text-green-300"; }

export default function AIInsightsSection({ recommendations, insights, generateRecommendations, updateRecommendation }: {
  recommendations: AIRecommendation[]; insights: AIInsights | null; generateRecommendations:()=>void; updateRecommendation:(id:number,status:string)=>void;
}) {
  return (
    <section className="mt-8 space-y-6">
      <div className="flex flex-col md:flex-row md:items-end md:justify-between gap-4"><div><h2 className="text-3xl font-bold">AI Predictive Intelligence</h2><p className="text-slate-400 mt-2">Predictive maintenance, delay risk, inventory forecasting and quality recommendations.</p></div><button onClick={generateRecommendations} className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3">Generate AI Recommendations</button></div>
      <div className="grid grid-cols-1 md:grid-cols-4 xl:grid-cols-8 gap-4"><Kpi title="Total" value={insights?.total ?? 0}/><Kpi title="Open" value={insights?.open ?? 0}/><Kpi title="Acknowledged" value={insights?.acknowledged ?? 0}/><Kpi title="Closed" value={insights?.closed ?? 0}/><Kpi title="Critical" value={insights?.critical ?? 0}/><Kpi title="High" value={insights?.high ?? 0}/><Kpi title="Medium" value={insights?.medium ?? 0}/><Kpi title="Low" value={insights?.low ?? 0}/></div>
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">{recommendations.map(row=><div key={row.id} className="rounded-2xl bg-slate-900 border border-slate-800 p-5"><div className="flex items-start justify-between gap-3"><div><p className="text-sm text-slate-400">{row.recommendation_type}</p><h3 className="text-xl font-bold mt-1">{row.title}</h3></div><span className={`rounded-full px-3 py-1 text-xs border ${severityStyle(row.severity)}`}>{row.severity}</span></div><p className="text-slate-300 mt-4">{row.message}</p><div className="mt-4 flex items-center justify-between"><p className="text-sm text-slate-400">Confidence: {row.confidence}%</p><select className="bg-slate-950 border border-slate-700 rounded-lg px-3 py-2" value={row.status} onChange={(e)=>updateRecommendation(row.id,e.target.value)}><option>Open</option><option>Acknowledged</option><option>Closed</option></select></div></div>)}</div>
    </section>
  );
}
function Kpi({title,value}:{title:string;value:string|number}){return <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5"><p className="text-slate-400 text-sm">{title}</p><h3 className="text-2xl font-bold mt-2">{value}</h3></div>;}
