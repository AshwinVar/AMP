import type { Role } from "../lib/types";

export default function Sidebar({
  activeView,
  setActiveView,
  username,
  role,
}: {
  activeView: string;
  setActiveView: (value: string) => void;
  username: string;
  role: Role | "";
}
) 

{
  const isAdmin = role === "Admin";
  const canManageShifts = role === "Admin" || role === "Supervisor";
  const canDownloadReports = role === "Admin" || role === "Supervisor";

  const sidebarItems = [
    { key: "timeline", label: "Timeline" },
    { key: "management", label: "Management" },
    { key: "overview", label: "Overview" },
    { key: "machines", label: "Machines" },
    { key: "downtime", label: "Downtime" },
    { key: "production", label: "Production/OEE" },
    ...(canManageShifts ? [{ key: "shifts", label: "Shifts" }] : []),
    { key: "analytics", label: "Analytics" },
    { key: "alerts", label: "Alerts" },
    ...(canDownloadReports ? [{ key: "reports", label: "Reports" }] : []),
    ...(isAdmin ? [{ key: "users", label: "Users" }] : []),
  ];

  return (
    <aside className="hidden md:flex w-72 bg-slate-900 border-r border-slate-800 p-5 flex-col">
      <div className="mb-8">
        <p className="text-xs text-slate-400">MES Lite SaaS MVP</p>
        <h1 className="text-2xl font-bold mt-1">FlowMES</h1>
        <p className="text-xs text-slate-500 mt-2">Factory command centre</p>
      </div>

      <nav className="space-y-2 flex-1">
        {sidebarItems.map((item) => (
          <button
            key={item.key}
            onClick={() => setActiveView(item.key)}
            className={`w-full text-left px-4 py-3 rounded-xl text-sm ${
              activeView === item.key
                ? "bg-white text-slate-950 font-semibold"
                : "text-slate-300 hover:bg-slate-800"
            }`}
          >
            {item.label}
          </button>
        ))}
      </nav>

      <div className="border-t border-slate-800 pt-4 text-sm text-slate-400">
        <p className="font-medium text-white">{username}</p>
        <p className="text-slate-500">{role}</p>
      </div>
    </aside>
  );
}
