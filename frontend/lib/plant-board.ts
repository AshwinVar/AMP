// The plant board's contract with the backend, and the few decisions worth
// testing away from React.
//
// Mirrors backend/ai/plant_board.py. The customer asked for one tab of pictures:
// production against target by the hour, raw material consumed, the rupee rate a
// shift is running at, power, and packing. Two of those five have no source on
// their floor, and this file is where that stays honest — an UnavailableSeries
// carries no points at all, so there is nothing a chart could draw as zero.

/** A series AMP cannot draw, and why. Never zeros — see plant_board.py. */
export type UnavailableSeries = {
  available: false;
  reason: string;
  fix: string;
  points: [];
};

/**
 * A series AMP CAN draw, in whichever of its two measured states is true.
 *
 * `hours_known: false` is the one worth explaining. A controller's MONTH page
 * gives a day's kilowatt-hours with no hour in it. The meter exists and it
 * reported — what is missing is the hour axis, not the measurement. So the
 * total is real and `points` must not be drawn: a day's 90 kWh bucketed by
 * hour lands entirely on midnight and reads as a plant that ran for an hour.
 */
export type MeasuredSeries = {
  available: true;
  unit: string;
  total: number;
  points: { hour: number; kwh: number | null }[] | null;
  hours_known: boolean;
};

export type Series = UnavailableSeries | MeasuredSeries;

export type HourStatus = "ok" | "low" | "unrated";

export type ProductionPoint = {
  hour: number;
  parts: number;
  ideal: number;
  status: HourStatus;
};

export type MachineProduction = {
  machine_id: number;
  machine: string;
  part: string | null;
  part_code: string | null;
  tool: string | null;
  ideal_per_hour: number;
  average_per_hour: number;
  /** The day's output from EVERY source, hourly or not. */
  total: number;
  /** True when the hours below account for `total`. False means the day came
   *  from a source with no hour in it, so the bars are not the day. */
  hours_known: boolean;
  /** What the hourly points add up to. Equals `total` when hours_known. */
  hourly_total: number;
  points: ProductionPoint[];
};

export type MachineRm = {
  machine_id: number;
  machine: string;
  material: string | null;
  kg_total: number;
  points: { hour: number; kg: number }[];
};

export type ShiftBlock = {
  shift: number;
  from_hour: number;
  to_hour: number;
  parts: number;
  revenue: number;
  rate_per_hour: number;
};

export type MachineShiftRate = {
  machine_id: number;
  machine: string;
  priced: boolean;
  points: ShiftBlock[];
};

export type PlantBoardDay = {
  date: string;
  acceptable_fraction: number;
  machines: { id: number; name: string; status: string }[];
  production: MachineProduction[];
  rm_status: MachineRm[];
  shift_rate: MachineShiftRate[];
  power: Series;
  packing: UnavailableSeries;
};

export type MonthDay = {
  day: string;
  parts: number;
  /** null when no part spec declares a weight — never 0 for "unknown". */
  kg: number | null;
  revenue: number | null;
};

export type PlantBoardMonth = {
  year: number;
  month: number;
  /** Every day that produced anything. A day with no record is ABSENT. */
  daily: MonthDay[];
  itemwise_production: { part: string; total: number; good: number }[];
  rm_consumption: { material: string; kg: number }[];
  shift_rate_by_machine: { machine: string; revenue: number; rate_per_hour: number }[];
  total_rate_per_hour: number;
  total_revenue: number;
  power: Series;
  packing: UnavailableSeries;
};

export type PartSpec = {
  id: number;
  part_code: string;
  part_name: string;
  material: string;
  part_weight_g: number;
  cavities: number;
  active_cavities: number;
  ideal_cycle_time_s: number;
  price_per_piece: number;
  effective_from: string | null;
  ideal_parts_per_hour: number;
};

export type ToolAssignment = {
  id: number;
  tool_no: string;
  name: string;
  machine_id: number | null;
  machine: string | null;
  part_code: string | null;
  cavities: number | null;
  status: string;
  cycles_total: number;
  cycles_since_service: number;
  service_interval_cycles: number | null;
};

// ── The three colours, and why there are three ────────────────────────
//
// An hour that beat its target and an hour that had no target are not the same
// fact, and must not share a colour. The backend decides which an hour is
// (plant_board.py); this only paints it. "unrated" is slate on purpose: it reads
// as absent, not as a pass.
export const HOUR_FILL: Record<HourStatus, string> = {
  ok: "#34d399",
  low: "#f87171",
  unrated: "#64748b",
};

export const HOUR_LABEL: Record<HourStatus, string> = {
  ok: "At or above target",
  low: "Below target",
  unrated: "No target set",
};

/** "09:00", from the hour index the backend sends. */
export function hourLabel(hour: number): string {
  return `${String(hour).padStart(2, "0")}:00`;
}

/**
 * Today as the plant's calendar sees it, "YYYY-MM-DD".
 *
 * Read off the local calendar fields (getFullYear / getMonth / getDate) rather
 * than subtracting the timezone offset from an instant and slicing its ISO
 * string. That older trick is a different thing wearing the same clothes: it
 * builds an instant whose UTC fields happen to read as local, which is an hour
 * out either side of a DST change — the class of bug lib/apiDate.ts exists for.
 * India has no DST, but this board is not an India-only screen, and a date
 * picker that quietly lands on yesterday is a bug nobody reports clearly.
 */
export function todayLocalIso(now: Date = new Date()): string {
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

/** "06:00–14:00", the block a shift rate is an average over. */
export function shiftLabel(block: Pick<ShiftBlock, "from_hour" | "to_hour">): string {
  return `${hourLabel(block.from_hour)}–${hourLabel(block.to_hour % 24)}`;
}

/**
 * The hour a chart click landed on, read from the clicked DATUM.
 *
 * NOT FROM AN INDEX, and that was learned the hard way. Two index-shaped
 * sources both looked right and both were wrong:
 *
 *   * the chart's `activeTooltipIndex` is null on a click, and `Number(null)`
 *     is 0 — so clicking the 12:00 bar opened "00:00", a plant that made
 *     nothing, rather than reporting a missed click;
 *   * the Bar's own `onClick` index counts the bars it RENDERED, not the data
 *     points — with an idle night the 12:00 bar is the 5th drawn, so it
 *     reported 04:00.
 *
 * Both are off-by-something in a way that still produces a plausible hour, which
 * is the worst kind of wrong: the drill-in shows real figures for the wrong hour
 * and nothing looks broken. The datum knows which hour it is; ask it.
 */
export function hourFromClick(entry: unknown): number | null {
  const node = entry as { hour?: unknown; payload?: { hour?: unknown } } | null;
  const raw = node?.hour ?? node?.payload?.hour;
  return typeof raw === "number" && Number.isInteger(raw) && raw >= 0 && raw < 24
    ? raw
    : null;
}

/**
 * Is there a target to compare against at all?
 *
 * A machine with no part spec still COUNTS parts — the controller's number is
 * real — it just has nothing to call good or bad. The chart draws the bars and
 * omits the target line, rather than drawing a target of zero, which every hour
 * would then clear.
 */
export function hasTarget(series: Pick<MachineProduction, "ideal_per_hour">): boolean {
  return series.ideal_per_hour > 0;
}

/**
 * What this machine is missing before its figures mean anything, in the order a
 * person would fix it. Empty when the chain is complete.
 *
 * The chain is: a tool is fitted -> the tool names a part -> that part has a
 * spec -> the spec carries a price. Each link missing removes a different
 * figure, and saying "no data" for all four would send somebody hunting.
 */
export function missingLinks(
  prod: Pick<MachineProduction, "tool" | "part" | "ideal_per_hour">,
  priced: boolean,
): string[] {
  const gaps: string[] = [];
  if (!prod.tool) gaps.push("no mould assigned to this machine");
  else if (!prod.part) gaps.push("the fitted mould does not say which part it makes");
  else if (!prod.ideal_per_hour) gaps.push("the part has no cycle time, so there is no hourly target");
  if (prod.part && !priced) gaps.push("the part has no price, so no rupee figure is shown");
  return gaps;
}

/**
 * Sum a day's parts across machines for one hour — the plant-wide bar.
 *
 * Totals the machines rather than averaging them: the question "how many parts
 * did this plant make at 10am" has one answer, and an average of per-machine
 * rates is not it.
 */
export function plantHourly(production: MachineProduction[]): { hour: number; parts: number; ideal: number }[] {
  const hours = production[0]?.points.length ?? 0;
  return Array.from({ length: hours }, (_, h) => ({
    hour: h,
    parts: production.reduce((sum, m) => sum + (m.points[h]?.parts ?? 0), 0),
    // Only machines that HAVE a target contribute to the plant target, so a
    // plant with half its moulds unspecified is not measured against half a bar.
    ideal: production.reduce((sum, m) => sum + (hasTarget(m) ? m.ideal_per_hour : 0), 0),
  }));
}

/**
 * What the power card should do with a series — the three states, named.
 *
 * Collapsing these to two is how the card lies, in one direction or the other:
 * "day-only" rendered as "unavailable" tells a moulder his machine has no meter
 * while AMP holds the kilowatt-hours that machine measured; "day-only" rendered
 * as "hourly" draws a day's consumption as a single bar at midnight.
 */
export function powerMode(series: Series): "unavailable" | "hourly" | "day-only" {
  if (!series.available) return "unavailable";
  return series.hours_known && series.points !== null ? "hourly" : "day-only";
}

/**
 * The machines whose day is known but whose hours are not.
 *
 * These are the presses read off a MONTH page: the day's figure is real, the
 * 24 bars under it are all zero and mean nothing. The board names them rather
 * than drawing flat lines, because a flat line is indistinguishable from a
 * machine that stood idle all day.
 */
export function dayOnlyMachines(production: MachineProduction[]): string[] {
  return production.filter((m) => !m.hours_known && m.total > 0).map((m) => m.machine);
}

/**
 * The machine_id behind a clicked bar, or null.
 *
 * The same trap as `hourFromClick`, and it cost a day the first time: Recharts
 * hands the handler either the datum or a wrapper with `payload`, and a missed
 * click hands it null. `Number(null)` is 0, and 0 is a plausible-looking id —
 * so a click that resolved to nothing used to drill into whichever machine
 * happened to be first. Null has to mean null.
 */
export function machineFromClick(entry: unknown): number | null {
  const node = entry as
    | { machine_id?: unknown; payload?: { machine_id?: unknown } }
    | null;
  const raw = node?.machine_id ?? node?.payload?.machine_id;
  return typeof raw === "number" && Number.isInteger(raw) && raw > 0 ? raw : null;
}
