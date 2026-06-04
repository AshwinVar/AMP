import type { IoTCommandCenter, IoTTelemetry } from "../lib/mega-pack2-types";

type Machine = { id: number; name: string; status: string; utilization: number; downtime: string; };

export default function IoTCommandSection({ machines, telemetry, command, form, setForm, createTelemetry }: {
  machines: Machine[]; telemetry: IoTTelemetry[]; command: IoTCommandCenter | null; form: any; setForm: (v:any)=>void; createTelemetry: (e:React.FormEvent)=>void;
}) {
  return (
    <section className="mt-8 space-y-6">
      <div><h2 className="text-3xl font-bold">IoT Command Center</h2><p className="text-slate-400 mt-2">PLC/MQTT-ready telemetry layer and live machine signals.</p></div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4"><Kpi title="Machines" value={command?.machines ?? 0}/><Kpi title="Signals" value={command?.signals ?? 0}/><Kpi title="Live Machines" value={command?.live_machines ?? 0}/></div>
      <form onSubmit={createTelemetry} className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-1 md:grid-cols-6 gap-4">
        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.machine_id} onChange={(e)=>setForm({...form,machine_id:e.target.value})} required><option value="">Machine</option>{machines.map(m=><option key={m.id} value={m.id}>{m.name}</option>)}</select>
        <select className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" value={form.signal_name} onChange={(e)=>setForm({...form,signal_name:e.target.value})}><option>status</option><option>utilization</option><option>temperature</option><option>cycle_count</option><option>load</option></select>
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Value" value={form.signal_value} onChange={(e)=>setForm({...form,signal_value:e.target.value})} required/>
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" type="number" placeholder="Numeric" value={form.numeric_value} onChange={(e)=>setForm({...form,numeric_value:Number(e.target.value)})}/>
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3" placeholder="Unit" value={form.unit} onChange={(e)=>setForm({...form,unit:e.target.value})}/>
        <button className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3">Post Signal</button>
      </form>
      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5 overflow-x-auto"><h3 className="text-2xl font-semibold mb-4">Latest Signals</h3><table className="w-full min-w-[850px] text-left text-sm"><thead className="text-slate-400 border-b border-slate-800"><tr>{["Machine","Signal","Value","Numeric","Unit","Source"].map(h=><th key={h} className="py-3 px-4">{h}</th>)}</tr></thead><tbody>{(command?.latest_signals ?? []).map((row,idx)=><tr key={idx} className="border-b border-slate-800"><td className="py-3 px-4 font-semibold">{row.machine_name}</td><td className="py-3 px-4">{row.signal_name}</td><td className="py-3 px-4">{row.signal_value}</td><td className="py-3 px-4">{row.numeric_value}</td><td className="py-3 px-4">{row.unit ?? "-"}</td><td className="py-3 px-4">{row.source}</td></tr>)}</tbody></table></div>
    </section>
  );
}
function Kpi({title,value}:{title:string;value:string|number}){return <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5"><p className="text-slate-400 text-sm">{title}</p><h3 className="text-2xl font-bold mt-2">{value}</h3></div>;}
