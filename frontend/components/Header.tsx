export default function Header({
  username,
  role,
  logout,
}: {
  username: string;
  role: string;
  logout: () => void;
}) {
  return (
    <header className="mb-8 flex flex-col md:flex-row md:items-center md:justify-between gap-4">
      <div>
        <p className="text-sm text-slate-400">
          Factory Operations Command Center
        </p>
        <h2 className="text-4xl font-bold mt-2">
          FlowMES Dashboard
        </h2>
        <p className="text-slate-400 mt-2">
          Real-time downtime, production, OEE, quality, shift performance and alerts.
        </p>
        <p className="text-slate-500 mt-2 text-sm">
          Logged in as: {username} | Role: {role}
        </p>
      </div>

      <button
        onClick={logout}
        className="rounded-xl border border-red-500/30 bg-red-500/10 px-5 py-3 text-red-400 hover:bg-red-500/20"
      >
        Logout
      </button>
    </header>
  );
}
