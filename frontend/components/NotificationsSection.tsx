import type { NotificationItem } from "../lib/phase27-types";

function severityStyle(severity: string) {
  if (severity === "Critical") return "border-red-500/40 bg-red-500/10 text-red-300";
  if (severity === "Warning") return "border-yellow-500/40 bg-yellow-500/10 text-yellow-300";
  return "border-blue-500/40 bg-blue-500/10 text-blue-300";
}

export default function NotificationsSection({ notifications, generateNotifications, updateNotification }: {
  notifications: NotificationItem[];
  generateNotifications: () => void;
  updateNotification: (id: number, status: string) => void;
}) {
  return (
    <section className="mt-8 space-y-6">
      <div className="flex flex-col md:flex-row md:items-end md:justify-between gap-4">
        <div>
          <h2 className="text-3xl font-bold">Notification Center</h2>
          <p className="text-slate-400 mt-2">Enterprise alerts generated from machines, inventory and escalation risks.</p>
        </div>
        <button onClick={generateNotifications} className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3">Generate Notifications</button>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        {notifications.map((row) => (
          <div key={row.id} className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-sm text-slate-400">{row.notification_type}</p>
                <h3 className="text-xl font-bold mt-1">{row.title}</h3>
              </div>
              <span className={`rounded-full px-3 py-1 text-xs border ${severityStyle(row.severity)}`}>{row.severity}</span>
            </div>
            <p className="text-slate-300 mt-4">{row.message}</p>
            <div className="mt-4 flex items-center justify-between">
              <p className="text-sm text-slate-400">{row.created_at ?? "-"}</p>
              <select className="bg-slate-950 border border-slate-700 rounded-lg px-3 py-2" value={row.status} onChange={(e) => updateNotification(row.id, e.target.value)}>
                <option>Unread</option>
                <option>Read</option>
              </select>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
