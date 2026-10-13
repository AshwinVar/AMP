/**
 * Gateway health, as `ai/gateway_health.py` reports it.
 *
 * The lights are decided in the BACKEND, not here. Two screens render this —
 * the Machines cards and the Plant Board — and if each mapped states to colours
 * itself they would eventually disagree about what amber means, which is worse
 * than having no colour at all.
 */

export type HealthState =
  | "REPORTING"
  | "MACHINE SILENT"
  | "GATEWAY UNREACHABLE"
  | "NEVER REPORTED";

export type Light = "green" | "amber" | "red" | "grey";

export type GatewayRow = {
  gateway_id: string;
  label: string;
  site: string;
  state: HealthState;
  light: Light;
  last_seen: string | null;
  quiet_minutes: number | null;
  reason: string;
  fix: string;
};

export type MachineLink = {
  machine_id: number;
  machine: string;
  status: string;
  state: HealthState;
  light: Light;
  last_heard: string | null;
  quiet_minutes: number | null;
  gateway: string | null;
  reason: string;
  fix: string;
  /** What the LAST outage turned out to be, once the gateway came back. Null
   *  when there is nothing to say — which is also the answer for an outage that
   *  only lost the network, because then nothing was missed. */
  resolved_cause: string | null;
};

export type GatewayHealth = {
  as_of: string;
  gateways: GatewayRow[];
  machines: MachineLink[];
  reporting: number;
  total: number;
  worst: HealthState;
  light: Light;
  headline: string;
};

/** Tailwind for each light. One table, so a badge and a banner of the same
 *  colour always look the same. */
export const LIGHT_STYLE: Record<Light, string> = {
  green: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
  amber: "bg-amber-500/15 text-amber-300 border-amber-500/40",
  red: "bg-red-500/15 text-red-300 border-red-500/40",
  grey: "bg-slate-500/15 text-slate-400 border-slate-600/50",
};

/** The short words on the badge. Deliberately about the LINK, never about the
 *  machine: "offline" would read as "the press is off", which is the confusion
 *  this whole feature exists to end. */
export const LIGHT_LABEL: Record<HealthState, string> = {
  REPORTING: "Reporting",
  "MACHINE SILENT": "No signal",
  "GATEWAY UNREACHABLE": "Gateway down",
  "NEVER REPORTED": "Not wired up",
};

/** The link for one machine, or null when the backend said nothing about it. */
export function linkFor(health: GatewayHealth | null, machineId: number): MachineLink | null {
  if (!health) return null;
  return health.machines.find((m) => m.machine_id === machineId) ?? null;
}

/** Whether the banner is worth showing at all: a plant where everything reports
 *  and nothing recently broke does not need a bar across the top telling it so. */
export function worthBanner(health: GatewayHealth | null): boolean {
  if (!health) return false;
  if (health.worst !== "REPORTING") return true;
  return health.machines.some((m) => m.resolved_cause);
}
