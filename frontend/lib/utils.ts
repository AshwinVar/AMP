export const INPUT_CLASS =
  "bg-slate-950 border border-slate-700 rounded-xl px-4 py-3 text-white outline-none focus:border-white/60";

// The primary button. `disabled:opacity-50` is part of it because every button
// wearing this is a submit button, and every submit button in this app is
// disabled while its request is in flight (#385's double-submit guard) — a
// button that looks identical while it is refusing clicks is what makes people
// click it again.
export const BTN_CLASS =
  "rounded-xl bg-white text-slate-950 font-semibold px-4 py-3 hover:opacity-90 transition disabled:opacity-50";

// parseDurationToMinutes used to live here too, as a third copy with the same
// decimal-hour bug. It now has one home: lib/duration.ts, ported from
// backend/duration.py and pinned against it by lib/duration.test.ts.

// calculateOEE lived here too — the same utilization-only estimate, unused.
// It now has one home, lib/oee.ts, as `fallbackOee`: a labelled last resort for
// machines with no production data, not something to print as "OEE".

/**
 * The colour a machine state reads as. ONE definition, repo-wide.
 *
 * This is deliberately a HUE and not a class string. Five places painted
 * machine status and all five agreed on the meaning — Running is green,
 * Breakdown is red — while disagreeing on the treatment: a dashboard pill
 * (`bg-x-500/20 text-x-400`), a twin node (`border-x-500/60 text-x-300`), a
 * timeline chip (`bg-x-500/10`), a health dot (just `bg-x-400`). Collapsing
 * them to one string would have changed how four screens look.
 *
 * The part that must not drift is the MEANING. That lives here. Each caller
 * keeps its own hue→class table, because Tailwind needs whole class names
 * present in the source to emit them, and because "how saturated is a timeline
 * chip" is genuinely a local decision.
 *
 * NOTE ON "Offline". backend/machine_status.py lists it in
 * VALID_MACHINE_STATUSES, and both ai/briefing.py and ai/assistant.py count it
 * as hard-down alongside Breakdown. It has no case here, so an offline machine
 * renders neutral — "I do not recognise this" rather than "this machine is
 * down". Neutral is safe (it never claims the machine is fine) and picking
 * between "red, like Breakdown" and "its own hue, because down-but-not-broken
 * is different" is a product decision, not one to make in a refactor. When it
 * is made, it is one line in this function and every screen follows.
 */
export type StatusHue = "green" | "yellow" | "red" | "blue" | "neutral";

export function statusHue(status: string): StatusHue {
  switch (status) {
    case "Running":
      return "green";
    case "Idle":
      return "yellow";
    case "Breakdown":
      return "red";
    case "Maintenance":
      return "blue";
    default:
      return "neutral";
  }
}

/** The dashboard's status pill. Also used by the machine cards. */
const PILL: Record<StatusHue, string> = {
  green:   "bg-green-500/20 text-green-400 border-green-500/40",
  yellow:  "bg-yellow-500/20 text-yellow-400 border-yellow-500/40",
  red:     "bg-red-500/20 text-red-400 border-red-500/40",
  blue:    "bg-blue-500/20 text-blue-400 border-blue-500/40",
  neutral: "bg-gray-500/20 text-gray-400 border-gray-500/40",
};

export function getStatusStyle(status: string) {
  return PILL[statusHue(status)];
}
