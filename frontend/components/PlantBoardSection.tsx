"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  Cell,
  CartesianGrid,
  Legend,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { apiGet, errorDetail } from "../lib/api";
import { money } from "../lib/money";
import {
  HOUR_FILL,
  HOUR_LABEL,
  hasTarget,
  hourFromClick,
  machineFromClick,
  hourLabel,
  type MeasuredSeries,
  missingLinks,
  plantHourly,
  powerMode,
  type Series,
  shiftLabel,
  type HourStatus,
  type PlantBoardDay,
  type PlantBoardMonth,
  dayOnlyMachines,
  todayLocalIso,
  type UnavailableSeries,
} from "../lib/plant-board";
import {
  type Cell as CsvCell,
  downloadCsv,
  exportFilename,
  toCsv,
} from "../lib/chart-export";
import PartMasterCard from "./PartMasterCard";

/**
 * THE PLANT BOARD — one tab, every picture the customer asked for.
 *
 * Shrinidhi Plastics runs about fourteen injection moulding machines with no
 * operators on the floor at night and no network to any of them. What they asked
 * for was not a report: it was a wall of charts they could point at, where
 * anything on a chart can be clicked to ask why.
 *
 * TWO OF THE FIVE SERIES HAVE NO SOURCE, AND THIS SAYS SO. Power needs an energy
 * meter nobody has fitted; packing needs somebody to record packed quantities and
 * nothing does. The backend sends those as an unavailable series with no points,
 * and this renders them as a stated gap with the fix next to it. The alternative
 * — a flat line at zero — is a chart that lies while looking like every other
 * chart on the page, and it is exactly the kind of thing a plant discovers three
 * months later when it matters.
 *
 * NOTHING HERE INVENTS A TARGET. An hour is green only against a cycle time
 * somebody entered. With no spec the bars are still drawn, because the shot count
 * is real, but they are slate and the target line is absent.
 */

// ── Recharts hands a tooltip value as a union it cannot narrow ────────
//
// `Formatter<ValueType, NameType>` admits strings, arrays and undefined, so a
// `(v: number) => string` is not assignable. These take `unknown` and narrow,
// which is honest about what arrives and keeps a stray non-number from
// rendering as "NaN" in front of a customer.
function numberOf(v: unknown): number {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

const fmtCount = (v: unknown) => numberOf(v).toLocaleString();
// Recharts 3 types a tick/label formatter's argument as ReactNode, not
// number, so hourLabel cannot be passed directly (same trap as fmtCount).
const fmtHour = (v: unknown) => hourLabel(numberOf(v));
const fmtKg = (v: unknown) => numberOf(v).toFixed(3) + " kg";
const fmtMoney = (v: unknown) => money(Math.round(numberOf(v)));

/**
 * Download this chart's data. On EVERY chart, for EVERY user.
 *
 * Deliberately not admin-gated. The person who needs yesterday's hourly
 * production in a spreadsheet is the supervisor who has to explain it, not the
 * administrator, and an export behind a role is an export nobody uses.
 *
 * It exports the rows the chart DREW, so a gap in the chart is a gap in the
 * file — see lib/chart-export.ts for why that matters more than it sounds.
 */
function ExportButton({
  chart,
  on,
  headers,
  rows,
}: {
  chart: string;
  on: string;
  headers: string[];
  rows: Record<string, CsvCell>[];
}) {
  const [said, setSaid] = useState("");
  if (!rows.length) return null;
  return (
    <button
      type="button"
      onClick={() => {
        const ok = downloadCsv(exportFilename(chart, on), toCsv(headers, rows));
        // A download that silently did not happen is worse than an error: the
        // person waits, then looks in a Downloads folder that has nothing.
        setSaid(ok ? "Saved" : "Could not save");
        setTimeout(() => setSaid(""), 2500);
      }}
      className="shrink-0 rounded-lg border border-slate-700 px-2.5 py-1 text-xs
                 text-slate-300 hover:border-slate-500 hover:text-slate-100"
      title={`Download the ${rows.length} rows behind this chart as a CSV`}
    >
      {said || "Export"}
    </button>
  );
}

function Card({
  title,
  hint,
  children,
  right,
  exportAs,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
  right?: React.ReactNode;
  exportAs?: { on: string; headers: string[]; rows: Record<string, CsvCell>[] };
}) {
  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="text-sm font-semibold uppercase tracking-wide text-slate-300">{title}</h3>
          {hint ? <p className="mt-1 text-xs text-slate-500">{hint}</p> : null}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {right}
          {exportAs ? (
            <ExportButton
              chart={title}
              on={exportAs.on}
              headers={exportAs.headers}
              rows={exportAs.rows}
            />
          ) : null}
        </div>
      </div>
      <div className="mt-4">{children}</div>
    </div>
  );
}

/**
 * A chart AMP cannot draw. Deliberately not chart-shaped: a reader skimming the
 * page must not mistake it for a measurement, and the fix is the useful part.
 */
function NoSource({ title, series }: { title: string; series: UnavailableSeries }) {
  return (
    <Card title={title} hint="Not measured">
      <div className="rounded-xl border border-dashed border-slate-700 bg-slate-950/40 p-6">
        <p className="text-sm text-slate-300">{series.reason}</p>
        <p className="mt-3 text-xs uppercase tracking-wide text-slate-500">To measure it</p>
        <p className="mt-1 text-sm text-slate-400">{series.fix}</p>
        <p className="mt-4 text-xs text-slate-600">
          No figure is shown rather than a zero — a zero here would be
          indistinguishable from a plant that consumed nothing.
        </p>
      </div>
    </Card>
  );
}

/**
 * Electricity, drawn only to the resolution it was measured at.
 *
 * Three states, because there are three (see `powerMode`). The middle one is
 * the one that did not exist until machines started arriving by photograph: a
 * meter that reports a day's total and no hours. Its number is real and its
 * chart is not, so the number is shown and the chart is not drawn.
 */
function PowerCard({ series }: { series: Series }) {
  const mode = powerMode(series);
  if (mode === "unavailable") {
    return <NoSource title="Power consumption" series={series as UnavailableSeries} />;
  }
  const measured = series as MeasuredSeries;
  const total = `${fmtCount(measured.total)} ${measured.unit}`;

  if (mode === "day-only") {
    return (
      <Card title="Power consumption" hint="Measured per day">
        <p className="text-3xl font-semibold text-emerald-300">{total}</p>
        <div className="mt-4 rounded-xl border border-dashed border-slate-700 bg-slate-950/40 p-4">
          <p className="text-sm text-slate-300">
            This came off the controller&apos;s month page, which records a day
            at a time. The day&apos;s figure is the machine&apos;s own; the hours
            inside it were never stored.
          </p>
          <p className="mt-3 text-xs uppercase tracking-wide text-slate-500">
            To see it by the hour
          </p>
          <p className="mt-1 text-sm text-slate-400">
            Press EXPORT DATA on the machine&apos;s HOUR PROD. page with a USB
            stick fitted, or connect the machine to AMP.
          </p>
        </div>
      </Card>
    );
  }

  return (
    <Card
      title="Power consumption"
      hint={total}
      exportAs={{
        on: "day",
        headers: ["hour", "kwh"],
        rows: (measured.points ?? []).map((pt) => ({
          hour: hourLabel(pt.hour),
          kwh: pt.kwh,
        })),
      }}
    >
      <ResponsiveContainer width="100%" height={200}>
        <BarChart data={measured.points ?? []}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
          <XAxis dataKey="hour" stroke="#64748b" fontSize={10} tickFormatter={fmtHour} />
          <YAxis stroke="#64748b" fontSize={10} />
          <Tooltip
            contentStyle={{ background: "#0f172a", border: "1px solid #334155" }}
            labelFormatter={fmtHour}
            formatter={fmtCount}
          />
          {/* An hour with no measurement is null and leaves a gap. Recharts
              draws no bar for null, which is the point — a zero bar would say
              the machine ran that hour and drew nothing. */}
          <Bar dataKey="kwh" name={measured.unit} fill="#34d399" />
        </BarChart>
      </ResponsiveContainer>
    </Card>
  );
}

export default function PlantBoardSection({ isAdmin }: { isAdmin: boolean }) {
  const [on, setOn] = useState(todayLocalIso());
  const [day, setDay] = useState<PlantBoardDay | null>(null);
  const [month, setMonth] = useState<PlantBoardMonth | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [machineId, setMachineId] = useState<number | null>(null);
  const [hour, setHour] = useState<number | null>(null);
  const [period, setPeriod] = useState<"hourly" | "daily" | "monthly">("hourly");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    const [y, m] = on.split("-");
    try {
      const [d, mo] = await Promise.all([
        apiGet<PlantBoardDay>("/analytics/plant-board?on=" + on),
        apiGet<PlantBoardMonth>("/analytics/plant-board/month?year=" + y + "&month=" + Number(m)),
      ]);
      setDay(d);
      setMonth(mo);
    } catch (e) {
      setError(errorDetail(e));
    } finally {
      setLoading(false);
    }
  }, [on]);

  useEffect(() => {
    // Deferred by a tick, and guarded for unmount, the way RootCauseSection
    // does it: calling load() in the effect body sets state synchronously
    // during the effect, which react-hooks/set-state-in-effect flags and which
    // costs a cascading render on every mount.
    let alive = true;
    const first = setTimeout(() => {
      if (alive) void load();
    }, 0);
    return () => {
      alive = false;
      clearTimeout(first);
    };
  }, [load]);

  const production = useMemo(() => day?.production ?? [], [day]);
  const selected = useMemo(
    () => production.find((p) => p.machine_id === machineId) ?? null,
    [production, machineId],
  );
  const selectedRm = day?.rm_status.find((r) => r.machine_id === machineId) ?? null;
  const selectedShift = day?.shift_rate.find((s) => s.machine_id === machineId) ?? null;

  const plant = useMemo(() => plantHourly(production), [production]);
  const plantTarget = plant[0]?.ideal ?? 0;
  const dayOnly = useMemo(() => dayOnlyMachines(production), [production]);

  // Who was making what at the clicked hour. This is the drill-in: a bar on the
  // plant chart is a question, and this is the answer.
  const atHour = useMemo(() => {
    if (hour == null) return [];
    return production
      .map((p) => ({
        machine: p.machine,
        machine_id: p.machine_id,
        part: p.part,
        parts: p.points[hour]?.parts ?? 0,
        ideal: p.ideal_per_hour,
        status: p.points[hour]?.status ?? "unrated",
      }))
      .sort((a, b) => b.parts - a.parts);
  }, [production, hour]);

  const plantRevenue = useMemo(
    () => (day?.shift_rate ?? []).reduce(
      (sum, s) => sum + s.points.reduce((t, b) => t + b.revenue, 0), 0),
    [day],
  );
  const anyPriced = (day?.shift_rate ?? []).some((s) => s.priced);
  const plantKg = useMemo(
    () => (day?.rm_status ?? []).reduce((sum, r) => sum + r.kg_total, 0),
    [day],
  );

  return (
    <section className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold text-white">Plant Board</h2>
          <p className="mt-1 text-sm text-slate-400">
            Every figure below is counted by a machine or derived from a part
            specification somebody entered. Click any bar to see which machine it
            came from.
          </p>
        </div>
        <div className="flex items-end gap-3">
          {/* PERIOD. Three resolutions, and which ones are HONEST depends on
              the data: a machine read off a month page has a real day and no
              hours, so "Hourly" would draw it as zero. See `dayOnly`. */}
          <div className="text-xs text-slate-400">
            <span className="block mb-1">Show</span>
            <div className="flex overflow-hidden rounded-lg border border-slate-700">
              {(["hourly", "daily", "monthly"] as const).map((p) => (
                <button
                  key={p}
                  type="button"
                  onClick={() => setPeriod(p)}
                  className={
                    "px-3 py-2 text-sm capitalize " +
                    (period === p
                      ? "bg-slate-700 text-white"
                      : "bg-slate-950 text-slate-400 hover:text-slate-200")
                  }
                >
                  {p}
                </button>
              ))}
            </div>
          </div>
          <label className="text-xs text-slate-400">
            <span className="block mb-1">
              {period === "monthly" || period === "daily" ? "Month of" : "Day"}
            </span>
            <input
              type="date"
              value={on}
              max={todayLocalIso()}
              onChange={(e) => {
                setOn(e.target.value);
                setHour(null);
              }}
              className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white"
            />
          </label>
          <button
            type="button"
            onClick={() => void load()}
            className="rounded-lg border border-slate-700 bg-slate-800 px-4 py-2 text-sm text-slate-200 hover:bg-slate-700"
          >
            Refresh
          </button>
        </div>
      </div>

      {error ? (
        <div className="rounded-xl border border-red-500/40 bg-red-500/10 p-4 text-sm text-red-200">
          {error}
          <button
            type="button"
            onClick={() => void load()}
            className="ml-3 underline hover:no-underline"
          >
            Try again
          </button>
        </div>
      ) : null}

      {loading && !day ? (
        <p className="text-sm text-slate-500">Reading the day…</p>
      ) : null}

      {day && day.machines.length === 0 ? (
        <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-8 text-center">
          <p className="text-sm text-slate-300">No machines in this workspace yet.</p>
          <p className="mt-1 text-xs text-slate-500">
            Add a machine, fit a mould to it, and give the part a specification —
            then every chart on this page fills in.
          </p>
        </div>
      ) : null}

      {day && day.machines.length > 0 ? (
        <>
          {/* ── The day in three numbers ─────────────────────────── */}
          <div className="grid gap-4 sm:grid-cols-3">
            <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
              <p className="text-xs uppercase tracking-wide text-slate-500">Parts made</p>
              <p className="mt-2 text-2xl font-semibold text-white">
                {production.reduce((s, p) => s + p.total, 0).toLocaleString()}
              </p>
              <p className="mt-1 text-xs text-slate-500">
                {day.machines.length} machine{day.machines.length === 1 ? "" : "s"}
              </p>
            </div>
            <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
              <p className="text-xs uppercase tracking-wide text-slate-500">Material consumed</p>
              <p className="mt-2 text-2xl font-semibold text-white">
                {plantKg > 0 ? plantKg.toFixed(2) + " kg" : "—"}
              </p>
              <p className="mt-1 text-xs text-slate-500">
                {plantKg > 0 ? "From part weight × parts" : "Needs a part weight"}
              </p>
            </div>
            <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
              <p className="text-xs uppercase tracking-wide text-slate-500">Value produced</p>
              <p className="mt-2 text-2xl font-semibold text-white">
                {anyPriced ? money(Math.round(plantRevenue)) : "—"}
              </p>
              <p className="mt-1 text-xs text-slate-500">
                {anyPriced ? "At the price in force today" : "No part is priced yet"}
              </p>
            </div>
          </div>

          {/* ── Production against target, by the hour ────────────── */}
          <Card
            title="Production per hour — whole plant"
            hint={
              plantTarget > 0
                ? "The dashed line is the plant's target for an hour, from the cycle times entered. Click a bar for the machine-by-machine split."
                : "No cycle time has been entered for any fitted mould, so there is no target to draw. The bars are real counts."
            }
            exportAs={{
              on: day.date,
              headers: ["hour", "parts", "target_per_hour"],
              rows: plant.map((p) => ({
                hour: hourLabel(p.hour),
                parts: p.parts,
                target_per_hour: p.ideal > 0 ? p.ideal : null,
              })),
            }}
          >
            <ResponsiveContainer width="100%" height={260}>
              <BarChart
                data={plant.map((p) => ({ ...p, label: hourLabel(p.hour) }))}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                <XAxis dataKey="label" stroke="#64748b" fontSize={11} interval={1} />
                <YAxis stroke="#64748b" fontSize={11} />
                <Tooltip
                  contentStyle={{ background: "#0f172a", border: "1px solid #334155" }}
                  formatter={fmtCount}
                />
                <Bar
                  dataKey="parts"
                  name="Parts"
                  cursor="pointer"
                  onClick={(entry: unknown) => {
                    const h = hourFromClick(entry);
                    if (h != null) setHour(h === hour ? null : h);
                  }}
                >
                  {plant.map((p) => (
                    <Cell
                      key={p.hour}
                      fill={
                        plantTarget === 0
                          ? HOUR_FILL.unrated
                          : p.parts >= plantTarget * (day.acceptable_fraction ?? 0.8)
                            ? HOUR_FILL.ok
                            : HOUR_FILL.low
                      }
                      stroke={hour === p.hour ? "#e2e8f0" : undefined}
                      strokeWidth={hour === p.hour ? 2 : 0}
                    />
                  ))}
                </Bar>
                {plantTarget > 0 ? (
                  <ReferenceLine
                    y={plantTarget}
                    stroke="#38bdf8"
                    strokeDasharray="6 4"
                    label={{ value: "Target", fill: "#38bdf8", fontSize: 11, position: "right" }}
                  />
                ) : null}
              </BarChart>
            </ResponsiveContainer>

            {/* A machine read off a MONTH page has a real day and no hours. Its
                24 zero bars are already excluded from the plant total above, so
                without this line the plant would simply look quieter than it
                was, with nothing on screen to say why. */}
            {dayOnly.length ? (
              <p className="mt-3 rounded-lg border border-amber-900/50 bg-amber-950/20 px-3 py-2 text-xs text-amber-200/80">
                These bars leave out {dayOnly.join(", ")}: {dayOnly.length === 1 ? "its" : "their"}{" "}
                output was read off a month page, which records a day at a time.{" "}
                {dayOnly.length === 1 ? "That day is" : "Those days are"} counted in the
                machine table below, not in the hours here.
              </p>
            ) : null}

            <div className="mt-3 flex flex-wrap gap-4 text-xs text-slate-400">
              {(["ok", "low", "unrated"] as const).map((k) => (
                <span key={k} className="flex items-center gap-2">
                  <span
                    className="inline-block h-3 w-3 rounded"
                    style={{ background: HOUR_FILL[k] }}
                  />
                  {HOUR_LABEL[k]}
                </span>
              ))}
            </div>

            {hour != null ? (
              <div className="mt-4 rounded-xl border border-slate-700 bg-slate-950/60 p-4">
                <div className="flex items-center justify-between">
                  <h4 className="text-sm font-semibold text-white">
                    {hourLabel(hour)} — machine by machine
                  </h4>
                  <button
                    type="button"
                    onClick={() => setHour(null)}
                    className="text-xs text-slate-400 underline hover:no-underline"
                  >
                    Close
                  </button>
                </div>
                {/* Made against target, machine by machine, for the hour that
                    was clicked. Horizontal so the machine names stay readable
                    down the side at fourteen presses, and the two bars are
                    paired rather than stacked: the question is "did this one
                    hit its number", which is a comparison, not a total.
                    Clicking a bar still opens that machine's own day. */}
                <ResponsiveContainer width="100%" height={Math.max(160, atHour.length * 26)}>
                  <BarChart data={atHour} layout="vertical">
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                    <XAxis type="number" stroke="#64748b" fontSize={10} />
                    <YAxis
                      type="category"
                      dataKey="machine"
                      stroke="#64748b"
                      fontSize={10}
                      width={110}
                    />
                    <Tooltip
                      contentStyle={{ background: "#0f172a", border: "1px solid #334155" }}
                      formatter={fmtCount}
                    />
                    <Legend wrapperStyle={{ fontSize: 11 }} />
                    <Bar
                      dataKey="parts"
                      name="Made"
                      cursor="pointer"
                      onClick={(entry: unknown) => {
                        const id = (entry as { machine_id?: number } | null)?.machine_id;
                        if (typeof id === "number") setMachineId(id);
                      }}
                    >
                      {atHour.map((r) => (
                        <Cell key={r.machine_id} fill={HOUR_FILL[r.status as HourStatus]} />
                      ))}
                    </Bar>
                    <Bar dataKey="ideal" name="Target" fill="#334155" />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            ) : null}
          </Card>

          {/* ── Per-machine, as BARS. The way into one machine's day. ─ */}
          <Card
            title="Parts today, by machine"
            hint="Click a bar for that machine's hourly production, material and shift rate."
            exportAs={{
              on: day.date,
              headers: ["machine", "parts_today", "target_per_hour",
                        "average_per_hour", "mould", "part", "hours_known"],
              rows: production.map((p) => ({
                machine: p.machine,
                parts_today: p.total,
                target_per_hour: hasTarget(p) ? p.ideal_per_hour : null,
                average_per_hour: p.average_per_hour,
                mould: p.tool,
                part: p.part,
                hours_known: p.hours_known,
              })),
            }}
          >
            <ResponsiveContainer width="100%" height={Math.max(180, production.length * 30)}>
              <BarChart
                data={production.map((p) => ({
                  machine: p.machine,
                  parts: p.total,
                  machine_id: p.machine_id,
                }))}
                layout="vertical"
                margin={{ left: 8, right: 24, top: 4, bottom: 18 }}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                <XAxis
                  type="number"
                  stroke="#64748b"
                  fontSize={11}
                  label={{ value: "Parts made today", position: "insideBottom",
                           offset: -8, fill: "#64748b", fontSize: 11 }}
                />
                <YAxis type="category" dataKey="machine" stroke="#64748b"
                       fontSize={11} width={78} />
                <Tooltip
                  contentStyle={{ background: "#0f172a", border: "1px solid #334155" }}
                  formatter={fmtCount}
                />
                <Bar dataKey="parts" name="Parts" cursor="pointer"
                     onClick={(entry: unknown) => {
                       const id = machineFromClick(entry);
                       if (id != null) setMachineId(machineId === id ? null : id);
                     }}>
                  {production.map((p) => (
                    <Cell
                      key={p.machine_id}
                      fill={
                        !hasTarget(p)
                          ? HOUR_FILL.unrated
                          : p.average_per_hour >= p.ideal_per_hour * (day.acceptable_fraction ?? 0.8)
                            ? HOUR_FILL.ok
                            : HOUR_FILL.low
                      }
                      stroke={machineId === p.machine_id ? "#e2e8f0" : undefined}
                      strokeWidth={machineId === p.machine_id ? 2 : 0}
                    />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </Card>

          {/* ── The same machines as a table, for the detail a bar cannot
                carry: the mould, the part, and what each one still needs. ── */}
          <Card
            title="By machine — detail"
            hint="The same machines. A bar shows how much; this shows what of, and what is missing."
          >
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs uppercase tracking-wide text-slate-500">
                    <th className="py-2">Machine</th>
                    <th className="py-2">Mould</th>
                    <th className="py-2">Part</th>
                    <th className="py-2 text-right">Parts today</th>
                    <th className="py-2 text-right">Target / hr</th>
                    <th className="py-2 text-right">Avg / hr</th>
                    <th className="py-2">Needs</th>
                  </tr>
                </thead>
                <tbody>
                  {production.map((p) => {
                    const priced = day.shift_rate.find(
                      (s) => s.machine_id === p.machine_id)?.priced ?? false;
                    const gaps = missingLinks(p, priced);
                    return (
                      <tr
                        key={p.machine_id}
                        onClick={() =>
                          setMachineId(machineId === p.machine_id ? null : p.machine_id)}
                        className={
                          "cursor-pointer border-t border-slate-800 hover:bg-slate-800/50 " +
                          (machineId === p.machine_id ? "bg-slate-800/60" : "")
                        }
                      >
                        <td className="py-2 font-medium text-slate-100">{p.machine}</td>
                        <td className="py-2 text-slate-400">{p.tool ?? "—"}</td>
                        <td className="py-2 text-slate-400">{p.part ?? "—"}</td>
                        <td className="py-2 text-right text-slate-100">
                          {p.total.toLocaleString()}
                        </td>
                        <td className="py-2 text-right text-slate-400">
                          {hasTarget(p) ? p.ideal_per_hour.toLocaleString() : "not set"}
                        </td>
                        <td className="py-2 text-right text-slate-400">
                          {p.average_per_hour.toLocaleString()}
                        </td>
                        <td className="py-2 text-xs text-amber-300/80">
                          {gaps.length ? gaps[0] : ""}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Card>

          {/* ── One machine, three series ─────────────────────────── */}
          {selected ? (
            <div className="grid gap-4 lg:grid-cols-3">
              <Card
                title={selected.machine + " — parts per hour"}
                hint={selected.part ? "Making " + selected.part : "No part assigned"}
                exportAs={{
                  on: day.date + "-" + selected.machine,
                  headers: ["hour", "parts", "target_per_hour", "status"],
                  rows: selected.points.map((p) => ({
                    hour: hourLabel(p.hour),
                    parts: p.parts,
                    target_per_hour: p.ideal > 0 ? p.ideal : null,
                    status: p.status,
                  })),
                }}
                right={
                  <button
                    type="button"
                    onClick={() => setMachineId(null)}
                    className="text-xs text-slate-400 underline hover:no-underline"
                  >
                    Clear
                  </button>
                }
              >
                <ResponsiveContainer width="100%" height={200}>
                  <BarChart data={selected.points.map((p) => ({ ...p, label: hourLabel(p.hour) }))}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                    <XAxis dataKey="label" stroke="#64748b" fontSize={10} interval={3} />
                    <YAxis stroke="#64748b" fontSize={10} />
                    <Tooltip
                      contentStyle={{ background: "#0f172a", border: "1px solid #334155" }}
                      formatter={fmtCount}
                    />
                    <Bar dataKey="parts" name="Parts">
                      {selected.points.map((p) => (
                        <Cell key={p.hour} fill={HOUR_FILL[p.status]} />
                      ))}
                    </Bar>
                    {hasTarget(selected) ? (
                      <ReferenceLine
                        y={selected.ideal_per_hour}
                        stroke="#38bdf8"
                        strokeDasharray="6 4"
                      />
                    ) : null}
                  </BarChart>
                </ResponsiveContainer>
              </Card>

              <Card
                title="Material consumed"
                exportAs={
                  selectedRm
                    ? {
                        on: day.date + "-" + selected.machine,
                        headers: ["hour", "kg"],
                        rows: selectedRm.points.map((p) => ({
                          hour: hourLabel(p.hour),
                          kg: p.kg,
                        })),
                      }
                    : undefined
                }
                hint={
                  selectedRm?.material
                    ? selectedRm.material + " — " + selectedRm.kg_total.toFixed(2) + " kg today"
                    : "No material known: the part has no specification"
                }
              >
                {selectedRm && selectedRm.kg_total > 0 ? (
                  <ResponsiveContainer width="100%" height={200}>
                    {/* Bars, not a line. Material is consumed BY THE HOUR — each
                        hour is its own quantity, not a reading on a continuous
                        curve, and a line between 09:00 and 11:00 draws a slope
                        through an idle 10:00 that nothing consumed. */}
                    <BarChart
                      data={selectedRm.points.map((p) => ({ ...p, label: hourLabel(p.hour) }))}
                    >
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                      <XAxis dataKey="label" stroke="#64748b" fontSize={10} interval={3} />
                      <YAxis stroke="#64748b" fontSize={10} unit=" kg" />
                      <Tooltip
                        contentStyle={{ background: "#0f172a", border: "1px solid #334155" }}
                        formatter={fmtKg}
                      />
                      <Bar dataKey="kg" name="kg" fill="#a78bfa" />
                    </BarChart>
                  </ResponsiveContainer>
                ) : (
                  <p className="py-12 text-center text-sm text-slate-500">
                    Enter a part weight for {selected.part ?? "this machine's part"} and this fills in.
                  </p>
                )}
              </Card>

              <Card
                title="Shift rate"
                exportAs={
                  selectedShift
                    ? {
                        on: day.date + "-" + selected.machine,
                        headers: ["shift", "from_hour", "to_hour", "parts",
                                  "revenue", "rate_per_hour"],
                        rows: selectedShift.points.map((b) => ({
                          shift: b.shift,
                          from_hour: hourLabel(b.from_hour),
                          to_hour: hourLabel(b.to_hour),
                          parts: b.parts,
                          revenue: selectedShift.priced ? b.revenue : null,
                          rate_per_hour: selectedShift.priced ? b.rate_per_hour : null,
                        })),
                      }
                    : undefined
                }
                hint={
                  selectedShift?.priced
                    ? "Value produced per hour, averaged over each 8-hour block"
                    : "The part has no price, so there is no rate to show"
                }
              >
                {selectedShift?.priced ? (
                  <ResponsiveContainer width="100%" height={200}>
                    <BarChart
                      data={selectedShift.points.map((b) => ({
                        ...b,
                        label: shiftLabel(b),
                      }))}
                    >
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                      <XAxis dataKey="label" stroke="#64748b" fontSize={10} />
                      <YAxis stroke="#64748b" fontSize={10} />
                      <Tooltip
                        contentStyle={{ background: "#0f172a", border: "1px solid #334155" }}
                        formatter={fmtMoney}
                      />
                      <Bar dataKey="rate_per_hour" name="Per hour" fill="#fbbf24" />
                    </BarChart>
                  </ResponsiveContainer>
                ) : (
                  <p className="py-12 text-center text-sm text-slate-500">
                    Enter a price per piece and this fills in.
                  </p>
                )}
              </Card>
            </div>
          ) : null}

          {/* ── Power, in whichever of its three states is true ────── */}
          <div className="grid gap-4 lg:grid-cols-2">
            <PowerCard series={day.power} />
            <NoSource title="Packing" series={day.packing} />
          </div>

          {/* ── DAILY: the month, day by day. The middle resolution, and
                the only one a machine read off a MONTH page can answer. ── */}
          {period === "daily" && month ? (
            <Card
              title="Production per day — whole plant"
              hint={
                (month.daily?.length ?? 0) > 0
                  ? "Every day this month that produced anything. A day the plant did not run is absent, not zero."
                  : "Nothing recorded this month."
              }
              exportAs={{
                on: month.year + "-" + String(month.month).padStart(2, "0"),
                headers: ["day", "parts", "kg", "revenue"],
                rows: (month.daily ?? []).map((d) => ({
                  day: d.day,
                  parts: d.parts,
                  kg: d.kg,
                  revenue: d.revenue,
                })),
              }}
            >
              {(month.daily?.length ?? 0) > 0 ? (
                <ResponsiveContainer width="100%" height={260}>
                  <BarChart data={month.daily ?? []}
                            margin={{ left: 8, right: 16, top: 4, bottom: 20 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                    <XAxis
                      dataKey="day"
                      stroke="#64748b"
                      fontSize={10}
                      tickFormatter={(v: unknown) => String(v).slice(8)}
                      label={{ value: "Day of month", position: "insideBottom",
                               offset: -10, fill: "#64748b", fontSize: 11 }}
                    />
                    <YAxis stroke="#64748b" fontSize={11}
                           label={{ value: "Parts", angle: -90, position: "insideLeft",
                                    fill: "#64748b", fontSize: 11 }} />
                    <Tooltip
                      contentStyle={{ background: "#0f172a", border: "1px solid #334155" }}
                      formatter={fmtCount}
                    />
                    <Bar dataKey="parts" name="Parts" fill={HOUR_FILL.ok} />
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <p className="py-12 text-center text-sm text-slate-500">
                  No production recorded this month.
                </p>
              )}
            </Card>
          ) : null}

          {/* ── The month ─────────────────────────────────────────── */}
          {month ? (
            <div className="grid gap-4 lg:grid-cols-3">
              <Card title="Item-wise production this month">
                {month.itemwise_production.length ? (
                  <ResponsiveContainer width="100%" height={220}>
                    <BarChart data={month.itemwise_production} layout="vertical">
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                      <XAxis type="number" stroke="#64748b" fontSize={10} />
                      <YAxis
                        type="category"
                        dataKey="part"
                        stroke="#64748b"
                        fontSize={10}
                        width={110}
                      />
                      <Tooltip
                        contentStyle={{ background: "#0f172a", border: "1px solid #334155" }}
                        formatter={fmtCount}
                      />
                      <Legend wrapperStyle={{ fontSize: 11 }} />
                      <Bar dataKey="good" name="Good" fill="#34d399" />
                      <Bar dataKey="total" name="Total" fill="#475569" />
                    </BarChart>
                  </ResponsiveContainer>
                ) : (
                  <p className="py-12 text-center text-sm text-slate-500">
                    Nothing produced this month yet.
                  </p>
                )}
              </Card>

              <Card title="Raw material consumed this month">
                {month.rm_consumption.length ? (
                  <ResponsiveContainer width="100%" height={220}>
                    {/* Horizontal, because a material name is a word and a
                        vertical axis of them turns into unreadable 45-degree
                        labels the moment a plant runs more than three grades. */}
                    <BarChart data={month.rm_consumption} layout="vertical">
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                      <XAxis type="number" stroke="#64748b" fontSize={10} unit=" kg" />
                      <YAxis
                        type="category"
                        dataKey="material"
                        stroke="#64748b"
                        fontSize={10}
                        width={110}
                      />
                      <Tooltip
                        contentStyle={{ background: "#0f172a", border: "1px solid #334155" }}
                        formatter={fmtKg}
                      />
                      <Bar dataKey="kg" name="kg" fill="#a78bfa" />
                    </BarChart>
                  </ResponsiveContainer>
                ) : (
                  <p className="py-12 text-center text-sm text-slate-500">
                    Needs a part weight against the parts being made.
                  </p>
                )}
              </Card>

              <Card
                title="Shift rate by machine, this month"
                hint={
                  month.total_revenue > 0
                    ? "Plant total " + money(Math.round(month.total_revenue))
                    : "No priced part has produced anything this month"
                }
              >
                {month.shift_rate_by_machine.length ? (
                  <ResponsiveContainer width="100%" height={220}>
                    {/* VALUE ONLY, not value and rate on one pair of axes. The
                        month's value is six figures and its per-hour rate is
                        two or three; drawn together the rate bar is a line of
                        pixels against the axis and reads as zero. The rate is
                        in the tooltip, where it keeps its own magnitude. */}
                    <BarChart data={month.shift_rate_by_machine} layout="vertical">
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                      <XAxis type="number" stroke="#64748b" fontSize={10} />
                      <YAxis
                        type="category"
                        dataKey="machine"
                        stroke="#64748b"
                        fontSize={10}
                        width={110}
                      />
                      <Tooltip
                        contentStyle={{ background: "#0f172a", border: "1px solid #334155" }}
                        formatter={(v: unknown, name: unknown) =>
                          fmtMoney(v) + (String(name).includes("hour") ? "/hr" : "")}
                      />
                      <Bar dataKey="revenue" name="Value this month" fill="#fbbf24" />
                    </BarChart>
                  </ResponsiveContainer>
                ) : (
                  <p className="py-12 text-center text-sm text-slate-500">
                    Enter a price per piece for the parts being made.
                  </p>
                )}
              </Card>
            </div>
          ) : null}
        </>
      ) : null}

      {/* ── What no machine can tell AMP: a person enters it ────── */}
      <PartMasterCard isAdmin={isAdmin} onSaved={() => void load()} />
    </section>
  );
}
