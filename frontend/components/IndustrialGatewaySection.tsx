export default function IndustrialGatewaySection() {
  return (
    <section className="mt-8 rounded-2xl bg-slate-900 border border-slate-800 p-6">
      <h2 className="text-3xl font-bold">Industrial Gateway</h2>
      <p className="text-slate-400 mt-2">
        Phase 30 backend routes and PLC simulator are included. Wire this component into dashboard/page.tsx after confirming Swagger endpoints.
      </p>
      <div className="mt-4 grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
          <p className="text-slate-400">Devices</p>
          <h3 className="text-2xl font-bold">/industrial/devices</h3>
        </div>
        <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
          <p className="text-slate-400">Signals</p>
          <h3 className="text-2xl font-bold">/industrial/signals</h3>
        </div>
        <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
          <p className="text-slate-400">Mappings</p>
          <h3 className="text-2xl font-bold">/industrial/mappings</h3>
        </div>
      </div>
    </section>
  );
}
