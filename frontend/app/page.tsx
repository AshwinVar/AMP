const FEATURES = [
  { icon: "▦", title: "Real-time machine monitoring", body: "Live downtime, utilisation and OEE from your shop floor — see a stoppage the moment it happens, not at month-end." },
  { icon: "▥", title: "Smart inventory", body: "4-bucket stock (physical · reserved · available), item aliases, proforma reservation, tax-invoice deduction and free-spares tracking." },
  { icon: "▣", title: "Work orders & BOM", body: "Plan and track production. Completing a work order auto-deducts raw material and adds finished goods via the bill of materials." },
  { icon: "✓", title: "Quality & maintenance", body: "Log inspections against work orders and schedule preventive maintenance — full traceability for every batch and machine." },
  { icon: "◔", title: "Roles & multi-company", body: "Admin, Supervisor and Operator access. Each company's data is isolated and enforced on the server, not just hidden." },
  { icon: "↻", title: "Tally-friendly", body: "Import your existing item master from Tally in seconds. Keep Tally for accounts; FlowMES owns the shop-floor truth." },
];

const PLANS = [
  {
    name: "Starter", price: "₹7,999", period: "/ plant / month",
    tagline: "Core MES for a single plant",
    features: ["Machine monitoring & downtime", "OEE & shift performance", "Up to 5 users", "Email support"],
    cta: "Start with Starter", highlight: false,
  },
  {
    name: "Growth", price: "₹14,999", period: "/ plant / month",
    tagline: "Production + inventory, end to end",
    features: ["Everything in Starter", "Work orders & production planning", "Smart inventory & purchasing", "Quality & maintenance", "Up to 15 users", "Priority support"],
    cta: "Choose Growth", highlight: true,
  },
  {
    name: "Enterprise", price: "Custom", period: "",
    tagline: "Multi-plant & advanced",
    features: ["Everything in Growth", "IoT command & AI insights", "Executive OEE dashboards", "Multiple plants", "On-premise option", "Dedicated onboarding"],
    cta: "Talk to us", highlight: false,
  },
];

export default function LandingPage() {
  return (
    <main className="min-h-screen bg-slate-950 text-white">
      {/* Nav */}
      <header className="max-w-6xl mx-auto px-6 py-5 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <div className="w-9 h-9 rounded-xl bg-white text-slate-950 flex items-center justify-center font-bold">⌁</div>
          <span className="text-xl font-bold">FlowMES</span>
        </div>
        <nav className="flex items-center gap-6 text-sm">
          <a href="#features" className="text-slate-300 hover:text-white hidden sm:inline">Features</a>
          <a href="#pricing" className="text-slate-300 hover:text-white hidden sm:inline">Pricing</a>
          <a href="/login" className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-2 hover:opacity-90">Sign in</a>
        </nav>
      </header>

      {/* Hero */}
      <section className="max-w-4xl mx-auto px-6 pt-16 pb-20 text-center">
        <p className="text-indigo-400 text-sm font-semibold tracking-wider uppercase">Manufacturing Execution System for SMEs</p>
        <h1 className="text-4xl md:text-6xl font-bold mt-4 leading-tight">
          Run your factory floor<br />from one live dashboard
        </h1>
        <p className="text-slate-400 text-lg mt-6 max-w-2xl mx-auto">
          FlowMES gives Indian SME manufacturers real-time machine monitoring, smart inventory, and production tracking —
          the visibility large plants pay crores for, built for the way you actually work.
        </p>
        <div className="flex flex-col sm:flex-row gap-3 justify-center mt-9">
          <a href="mailto:hello@flowmes.in?subject=FlowMES%20demo%20request" className="rounded-xl bg-white text-slate-950 font-semibold px-6 py-3 hover:opacity-90">Book a demo</a>
          <a href="/login" className="rounded-xl border border-slate-700 text-white font-semibold px-6 py-3 hover:bg-slate-900">Sign in</a>
        </div>
        <p className="text-slate-500 text-sm mt-6">No spreadsheets. No WhatsApp updates. No guesswork.</p>
      </section>

      {/* Features */}
      <section id="features" className="max-w-6xl mx-auto px-6 py-16 border-t border-slate-900">
        <h2 className="text-3xl font-bold text-center">Everything your plant needs</h2>
        <p className="text-slate-400 text-center mt-3">One system for machines, materials, production and quality.</p>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5 mt-12">
          {FEATURES.map((f) => (
            <div key={f.title} className="rounded-2xl bg-slate-900 border border-slate-800 p-6">
              <div className="text-2xl mb-3">{f.icon}</div>
              <h3 className="text-lg font-semibold">{f.title}</h3>
              <p className="text-slate-400 text-sm mt-2 leading-relaxed">{f.body}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Pricing */}
      <section id="pricing" className="max-w-6xl mx-auto px-6 py-16 border-t border-slate-900">
        <h2 className="text-3xl font-bold text-center">Simple, plant-based pricing</h2>
        <p className="text-slate-400 text-center mt-3">Start with one plant. Scale when you're ready. Cancel anytime.</p>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-5 mt-12">
          {PLANS.map((p) => (
            <div key={p.name} className={`rounded-2xl p-7 border ${p.highlight ? "border-indigo-500 bg-slate-900" : "border-slate-800 bg-slate-900"}`}>
              {p.highlight && <span className="inline-block rounded-full bg-indigo-500/20 text-indigo-300 text-xs font-semibold px-3 py-1 mb-3">Most popular</span>}
              <h3 className="text-xl font-bold">{p.name}</h3>
              <p className="text-slate-400 text-sm mt-1">{p.tagline}</p>
              <div className="mt-5 flex items-baseline gap-1">
                <span className="text-4xl font-bold">{p.price}</span>
                <span className="text-slate-400 text-sm">{p.period}</span>
              </div>
              <ul className="mt-6 space-y-2.5">
                {p.features.map((feat) => (
                  <li key={feat} className="flex items-start gap-2 text-sm text-slate-300">
                    <span className="text-green-400 mt-0.5">✓</span>{feat}
                  </li>
                ))}
              </ul>
              <a href={`mailto:hello@flowmes.in?subject=FlowMES%20-%20${p.name}%20plan`} className={`block text-center mt-7 rounded-xl font-semibold px-4 py-3 ${p.highlight ? "bg-white text-slate-950 hover:opacity-90" : "border border-slate-700 text-white hover:bg-slate-800"}`}>
                {p.cta}
              </a>
            </div>
          ))}
        </div>
        <p className="text-slate-500 text-sm text-center mt-8">Prices exclusive of GST. Annual plans available. On-premise deployment for Enterprise.</p>
      </section>

      {/* CTA */}
      <section className="max-w-4xl mx-auto px-6 py-20 text-center border-t border-slate-900">
        <h2 className="text-3xl md:text-4xl font-bold">See FlowMES on your own factory data</h2>
        <p className="text-slate-400 mt-4">Book a 30-minute demo. We'll import a sample of your items and show you the difference live.</p>
        <a href="mailto:hello@flowmes.in?subject=FlowMES%20demo%20request" className="inline-block mt-8 rounded-xl bg-white text-slate-950 font-semibold px-7 py-3 hover:opacity-90">Book a demo</a>
      </section>

      {/* Footer */}
      <footer className="border-t border-slate-900">
        <div className="max-w-6xl mx-auto px-6 py-8 flex flex-col sm:flex-row items-center justify-between gap-4 text-sm text-slate-500">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-lg bg-slate-800 flex items-center justify-center">⌁</div>
            <span>FlowMES — Manufacturing Execution System</span>
          </div>
          <div className="flex gap-6">
            <a href="#features" className="hover:text-white">Features</a>
            <a href="#pricing" className="hover:text-white">Pricing</a>
            <a href="/login" className="hover:text-white">Sign in</a>
          </div>
        </div>
      </footer>
    </main>
  );
}
