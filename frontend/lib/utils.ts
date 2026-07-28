export const INPUT_CLASS =
  "bg-slate-950 border border-slate-700 rounded-xl px-4 py-3 text-white outline-none focus:border-white/60";

export const BTN_CLASS =
  "rounded-xl bg-white text-slate-950 font-semibold px-4 py-3 hover:opacity-90 transition";

// parseDurationToMinutes used to live here too, as a third copy with the same
// decimal-hour bug. It now has one home: lib/duration.ts, ported from
// backend/duration.py and pinned against it by lib/duration.test.ts.

export function calculateOEE(utilization: number) {
  return Math.round((utilization / 100) * 0.9 * 0.95 * 100);
}

export function getStatusStyle(status: string) {
  switch (status) {
    case "Running":
      return "bg-green-500/20 text-green-400 border-green-500/40";
    case "Idle":
      return "bg-yellow-500/20 text-yellow-400 border-yellow-500/40";
    case "Breakdown":
      return "bg-red-500/20 text-red-400 border-red-500/40";
    case "Maintenance":
      return "bg-blue-500/20 text-blue-400 border-blue-500/40";
    default:
      return "bg-gray-500/20 text-gray-400 border-gray-500/40";
  }
}
