import type { MachineEvent } from "../lib/phase8-types";

function statusStyle(status: string) {
  switch (status) {
    case "Running":
      return "border-green-500/40 bg-green-500/10 text-green-300";
    case "Idle":
      return "border-yellow-500/40 bg-yellow-500/10 text-yellow-300";
    case "Breakdown":
      return "border-red-500/40 bg-red-500/10 text-red-300";
    case "Maintenance":
      return "border-blue-500/40 bg-blue-500/10 text-blue-300";
    default:
      return "border-slate-500/40 bg-slate-500/10 text-slate-300";
  }
}

export default function MachineTimeline({ events }: { events: MachineEvent[] }) {
  const visibleEvents = events.slice(0, 80);

  return (
    <section className="mt-8 rounded-2xl bg-slate-900 border border-slate-800 p-5">
      <div className="mb-5">
        <h3 className="text-2xl font-semibold">Machine Timeline</h3>
        <p className="text-sm text-slate-400 mt-1">
          Live machine state transition history from MQTT events.
        </p>
      </div>

      <div className="max-h-[680px] overflow-y-auto space-y-3 pr-2">
        {visibleEvents.map((event) => (
          <div key={event.id} className="rounded-2xl border border-slate-800 bg-slate-950 p-4">
            <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-3">
              <div>
                <p className="font-semibold text-white">{event.machine_name}</p>
                <p className="text-xs text-slate-500 mt-1">
                  {event.created_at ? new Date(event.created_at).toLocaleString() : "Live event"}
                </p>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <span className={`px-3 py-1 rounded-full text-xs border ${statusStyle(event.old_status || "Unknown")}`}>
                  {event.old_status || "Unknown"}
                </span>
                <span className="text-slate-500">→</span>
                <span className={`px-3 py-1 rounded-full text-xs border ${statusStyle(event.new_status)}`}>
                  {event.new_status}
                </span>
                <span className="px-3 py-1 rounded-full text-xs border border-slate-700 text-slate-300">
                  {event.utilization}%
                </span>
                <span className="px-3 py-1 rounded-full text-xs border border-slate-700 text-slate-400">
                  {event.source}
                </span>
              </div>
            </div>
          </div>
        ))}

        {visibleEvents.length === 0 && (
          <div className="rounded-2xl border border-slate-800 p-6 text-slate-400">
            No machine timeline events yet. Wait for MQTT status changes.
          </div>
        )}
      </div>
    </section>
  );
}
