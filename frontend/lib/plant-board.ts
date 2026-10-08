// The plant board's contract with the backend, and the few decisions worth
// testing away from React.
//
// Mirrors backend/ai/plant_board.py. The customer asked for one tab of pictures:
// production against target by the hour, raw material consumed, the rupee rate a
// shift is running at, power, and packing. Two of those five have no source on
// their floor, and this file is where that stays honest — an UnavailableSeries
// carries no points at all, so there is nothing a chart could draw as zero.

/** A series AMP cannot draw, and why. Never zeros — see plant_board.py. */
import { parseApiDate } from "./apiDate";

export type UnavailableSeries = {
  available: false;
  reason: string;
  fix: string;
  points: [];
};

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
  total: number;
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
  power: UnavailableSeries;
  packing: UnavailableSeries;
};

export type PlantBoardMonth = {
  year: number;
  month: number;
  itemwise_production: { part: string; total: number; good: number }[];
  rm_consumption: { material: string; kg: number }[];
  shift_rate_by_machine: { machine: string; revenue: number; rate_per_hour: number }[];
  total_rate_per_hour: number;
  total_revenue: number;
  power: UnavailableSeries;
  packing: UnavailableSeries;
};

/** One bucket of a period series. `kg` and `revenue` are null when nothing in
 *  the whole window has a part spec (or a price): there is no weight to report,
 *  and a flat line at zero would claim there was. */
export type PeriodPoint = {
  start: string;
  parts: number;
  good: number;
  unassigned: number;
  kg: number | null;
  revenue: number | null;
};

/** Material received in the window. `kg` is filled in ONLY where the inventory
 *  item is counted in kilograms — a receipt booked in bags is a count, and
 *  adding it to a weight would print a number that is not a weight. */
export type RmAdded = {
  material: string;
  quantity: number;
  unit: string;
  kg: number | null;
};

/** Added against consumed for one material. Either side may be null: the two
 *  come from different vocabularies (the spec's material, the storekeeper's
 *  item name) and "not received" and "received under another name" are
 *  different facts this join cannot tell apart. */
export type RmBalanceRow = {
  material: string;
  consumed_kg: number | null;
  added_kg: number | null;
  added_qty: number | null;
  added_unit: string | null;
};

export type PlantBoardPeriod = {
  from: string;
  to: string;
  bucket: Bucket;
  hours: number;
  series: PeriodPoint[];
  itemwise_production: { part: string; total: number; good: number }[];
  rm_consumption: { material: string; kg: number }[];
  rm_added: RmAdded[];
  rm_balance: RmBalanceRow[];
  shift_rate_by_machine: { machine: string; revenue: number; rate_per_hour: number }[];
  total_rate_per_hour: number;
  total_revenue: number;
  power: UnavailableSeries;
  packing: UnavailableSeries;
};

export type Bucket = "hour" | "day" | "month";

/** The windows the board offers, each relative to the date in the picker — so
 *  "week" means the week containing that day, not only the current one. */
export type Preset = "day" | "week" | "month" | "year" | "custom";

export const PRESETS: { key: Preset; label: string }[] = [
  { key: "day", label: "Day" },
  { key: "week", label: "Week" },
  { key: "month", label: "Month" },
  { key: "year", label: "Year" },
  { key: "custom", label: "Custom" },
];

const MONTH_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MONTH_LONG = ["January", "February", "March", "April", "May", "June", "July",
                    "August", "September", "October", "November", "December"];

/**
 * A calendar day as a LOCAL midnight Date, through the one sanctioned parser.
 *
 * lib/apiDate.ts owns this because the platform reads a bare date string as UTC
 * midnight, which is the previous day for every viewer west of Greenwich —
 * lib/date-parsing.test.ts fails the build on any other way of doing it.
 */
export function localDate(iso: string): Date {
  const d = parseApiDate(iso);
  if (!d) throw new Error("not a calendar day: " + iso);
  return d;
}

export function isoOf(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
}

/** Calendar-correct day arithmetic: month lengths and leap years are the
 *  platform's problem, not ours, so this goes through a Date and mutates it. */
export function shiftDays(iso: string, days: number): string {
  const d = localDate(iso);
  d.setDate(d.getDate() + days);
  return isoOf(d);
}

const pad2 = (n: number) => String(n).padStart(2, "0");

/** The first of the month an ISO day falls in, by string surgery — no instant
 *  is involved in "which month is this", so none is constructed. */
function firstOfMonth(iso: string): string {
  return iso.slice(0, 8) + "01";
}

function firstOfNextMonth(iso: string): string {
  const y = Number(iso.slice(0, 4));
  const m = Number(iso.slice(5, 7));
  return (m === 12 ? y + 1 : y) + "-" + pad2(m === 12 ? 1 : m + 1) + "-01";
}

/**
 * The [start, end) a preset means around an anchor day, as YYYY-MM-DD.
 *
 * `end` is EXCLUSIVE, matching the route, so one day is start=D end=D+1 and no
 * record can land in two windows. The week starts on Monday, which is how a
 * shop floor counts one.
 */
export function periodWindow(preset: Preset, anchor: string): { start: string; end: string } {
  if (preset === "week") {
    const back = (localDate(anchor).getDay() + 6) % 7;      // Monday = 0
    const from = shiftDays(anchor, -back);
    return { start: from, end: shiftDays(from, 7) };
  }
  if (preset === "month") {
    return { start: firstOfMonth(anchor), end: firstOfNextMonth(anchor) };
  }
  if (preset === "year") {
    const y = Number(anchor.slice(0, 4));
    return { start: y + "-01-01", end: y + 1 + "-01-01" };
  }
  return { start: anchor, end: shiftDays(anchor, 1) };
}

/**
 * What a bucket is called on an axis, read as TEXT.
 *
 * These are the PLANT's own wall-clock buckets: ai/plant_board.py floors a
 * naive `created_at`, so "2026-10-07T14:00:00" means 2pm on the floor. Parsing
 * it as an instant and re-rendering it in the viewer's zone would slide every
 * label by the UTC offset — five and a half hours for the plant this was built
 * for, which would put the night shift in the afternoon.
 */
export function bucketTick(iso: string, bucket: Bucket): string {
  const [datePart, timePart = ""] = iso.split("T");
  const [, m, d] = datePart.split("-");
  if (bucket === "hour") return (timePart.slice(0, 2) || "00") + ":00";
  if (bucket === "day") return String(Number(d));
  return MONTH_SHORT[Number(m) - 1] ?? datePart;
}

/** The window in words, for the card hints: "7 Oct 2026", "October 2026",
 *  "2026", or a plain from-to. `end` is exclusive, so the last day it covers
 *  is end - 1, and printing `end` would claim a day the window excludes. */
export function periodLabel(start: string, end: string): string {
  const last = shiftDays(end, -1);
  const pretty = (iso: string) =>
    Number(iso.slice(8, 10)) + " " + MONTH_SHORT[Number(iso.slice(5, 7)) - 1] + " " + iso.slice(0, 4);

  if (start === last) return pretty(start);
  if (start === firstOfMonth(start) && end === firstOfNextMonth(start)) {
    return MONTH_LONG[Number(start.slice(5, 7)) - 1] + " " + start.slice(0, 4);
  }
  if (start.endsWith("-01-01") && end === Number(start.slice(0, 4)) + 1 + "-01-01") {
    return start.slice(0, 4);
  }
  return pretty(start) + " – " + pretty(last);
}

/** Total kilograms received, or null when nothing received is counted in
 *  kilograms. Never 0: "nothing arrived" and "what arrived was booked in bags"
 *  are different facts. */
export function addedKgTotal(rows: RmAdded[]): number | null {
  const kg = rows.filter((r) => r.kg !== null);
  if (!kg.length) return null;
  return Math.round(kg.reduce((s, r) => s + (r.kg ?? 0), 0) * 1000) / 1000;
}

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
