"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import { formatFactValue, provenanceTone, stateNotice, type Fact } from "../lib/evidence";
import { CURRENCY, money } from "../lib/money";

// Mirrors ai/command_centre.py (ADR-0024).
type Why = { label: string; text: string; facts: Fact[] };
type Problem = {
  key: string; title: string; detail: string; module: string; view: string;
  impact_units: number | null; impact_money: number | null; currency: string | null;
  rank_basis: string; state: string; facts: Fact[]; why: Why[];
};
type Action = { key: string; title: string; detail: string; who: string; module: string; view: string; state: string };
type Centre = {
  generated_at: string; days: number; state: string; headline: string;
  position: {
    state: string; oee: number | null; coverage_phrase: string;
    machines: { total: number; running: number; down: number; down_names: string[]; maintenance: number; idle: number };
    output: { good: number; total: number; good_rate: number; runs: number; days: number };
    plan: { state: string; planned_units: number; actual_units: number; attainment_rate: number | null; behind: number; missed: number };
    // The same fleet figure the pulse header shows (ai/twin.fleet_health).
    // `avg_health` is null — never 0 — when no machine has a reading, because
    // 0 is the worst score there is and a real reading on that scale.
    // Optional so a payload from a server that predates this block renders
    // rather than throwing — during a rolling deploy the browser can hold the
    // new bundle and still be answered by the old backend.
    health?: {
      machines: number; measured: number; avg_health: number | null; needs_attention: number;
      worst: { machine_id: number; name: string; health_score: number; health_band: string; health_measured: boolean } | null;
    } | null;
    facts: Fact[];
  };
  problems: Problem[];
  overlap_note: string;
  cost: { state: string; priced: boolean; loss_cost: number | null; lost_units: number | null; downtime_minutes: number; rejected_units: number; facts: Fact[] };
  actions: Action[];
};

const TONE_CLASS: Record<string, string> = {
  measured: "text-emerald-300 border-emerald-500/40",
  derived: "text-sky-300 border-sky-500/40",
  correlation: "text-amber-300 border-amber-500/40",
  rule: "text-violet-300 border-violet-500/40",
  model: "text-fuchsia-300 border-fuchsia-500/40",
  unknown: "text-slate-400 border-slate-600",
};

/** A loss as this card states it: money when the company set a unit value, good units otherwise. */
export function impactLabel(p: Pick<Problem, "impact_money" | "impact_units">): string {
  if (p.impact_money != null) return money(p.impact_money);
  if (p.impact_units != null) return `${p.impact_units.toLocaleString()} good units`;
  return "not measured";
}

function Facts({ facts }: { facts: Fact[] }) {
  return (
    <ul className="mt-2 space-y-1">
      {facts.map((f) => (
        <li key={f.key} className="text-xs flex flex-wrap items-baseline gap-x-2">
          <span className="text-slate-400">{f.label}</span>
          <span className="text-white font-semibold tabular-nums">{formatFactValue(f)}</span>
          <span className={`text-[10px] uppercase tracking-wide rounded-full px-2 py-0.5 border ${TONE_CLASS[provenanceTone(f.provenance)]}`}>
            {f.provenance}
          </span>
          {f.detail && <span className="text-slate-500">· {f.detail}</span>}
        </li>
      ))}
    </ul>
  );
}

// The Factory Command Centre (ADR-0024): where the plant is, what is wrong ranked
// by what it cost, why, what it is costing, and what to do next — with the
// evidence behind every figure one tap away.
export default function CommandCentreSection({ onOpen }: { onOpen?: (viewKey: string) => void }) {
  const [c, setC] = useState<Centre | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setC(await apiGet<Centre>("/command-centre"));
    } catch {
      // A glanceable hero: stay quiet rather than break the page.
    }
  }, []);
  useEffect(() => {
    // The first load is deferred rather than called in the effect body: a
    // synchronous setState there cascades a render (react-hooks/set-state-in-effect),
    // and the card has nothing to show until the fetch returns anyway.
    let alive = true;
    const tick = () => { if (alive) load(); };
    const first = setTimeout(tick, 0);
    const id = setInterval(tick, 60000);
    return () => { alive = false; clearTimeout(first); clearInterval(id); };
  }, [load]);

  if (!c) return null;
  const notice = stateNotice(c.state);
  const p = c.position;
  return (
    <section className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h2 className="text-xl font-bold">Command Centre</h2>
          <p className="text-slate-300 text-sm mt-1">{c.headline}</p>
        </div>
        <span className="text-[11px] text-slate-500">last {c.days} days</span>
      </div>
      {notice && <p role="status" className="text-amber-300/90 text-xs mt-2">{c.state} · {notice}</p>}

      <div className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-5 gap-3 mt-4">
        <div className="rounded-xl bg-slate-950/60 border border-slate-800 p-3">
          <p className="text-[11px] text-slate-500">Plant OEE</p>
          <p className="text-lg font-semibold tabular-nums">{p.oee == null ? "not measured" : `${p.oee}%`}</p>
          {p.coverage_phrase && <p className="text-[11px] text-amber-300/80">{p.coverage_phrase}</p>}
        </div>
        <div className="rounded-xl bg-slate-950/60 border border-slate-800 p-3">
          <p className="text-[11px] text-slate-500">Against plan</p>
          <p className="text-lg font-semibold tabular-nums">
            {p.plan.attainment_rate == null ? "no plan set" : `${p.plan.attainment_rate}%`}
          </p>
          {p.plan.attainment_rate != null && (
            <p className="text-[11px] text-slate-500 tabular-nums">
              {p.plan.actual_units.toLocaleString()} of {p.plan.planned_units.toLocaleString()} units
            </p>
          )}
        </div>
        <div className="rounded-xl bg-slate-950/60 border border-slate-800 p-3">
          <p className="text-[11px] text-slate-500">Machines</p>
          <p className="text-lg font-semibold tabular-nums">{p.machines.running}/{p.machines.total} running</p>
          {p.machines.down > 0 && <p className="text-[11px] text-red-400">{p.machines.down_names.join(", ")} down</p>}
        </div>
        {/* Machine health, which this card did not show at all: the score, the
            band and the eleven-rule explanation existed on /machine-health while
            the owner's home screen counted running-vs-total and nothing else.
            "not measured" rather than a 0, and the coverage is stated whenever
            it is partial, because a machine with nothing recorded scores 100 by
            absence and is deliberately left out of the average. */}
        {p.health && (
          <div className="rounded-xl bg-slate-950/60 border border-slate-800 p-3">
            <p className="text-[11px] text-slate-500">Machine health</p>
            <p className="text-lg font-semibold tabular-nums">
              {p.health.avg_health == null ? "not measured" : `${p.health.avg_health}/100`}
            </p>
            {p.health.avg_health == null ? (
              <p className="text-[11px] text-amber-300/80">no machine has a reading yet</p>
            ) : (
              <>
                {p.health.measured < p.health.machines && (
                  <p className="text-[11px] text-amber-300/80">
                    from {p.health.measured} of {p.health.machines} machines
                  </p>
                )}
                {p.health.worst && p.health.worst.health_measured && (
                  <p className="text-[11px] text-slate-500">
                    lowest {p.health.worst.name} {p.health.worst.health_score}/100
                  </p>
                )}
              </>
            )}
          </div>
        )}
        <div className="rounded-xl bg-slate-950/60 border border-slate-800 p-3">
          <p className="text-[11px] text-slate-500">Cost of losses</p>
          <p className="text-lg font-semibold tabular-nums">
            {c.cost.priced && c.cost.loss_cost != null
              ? money(c.cost.loss_cost)
              : c.cost.lost_units != null ? `${c.cost.lost_units.toLocaleString()} units` : "not measured"}
          </p>
          {!c.cost.priced && (
            <p className="text-[11px] text-amber-300/80">no unit value set, so no {CURRENCY} figure</p>
          )}
        </div>
      </div>

      <h3 className="text-sm font-semibold mt-5">What is wrong</h3>
      {c.problems.length === 0 && (
        <p className="text-slate-400 text-sm mt-1">Nothing needs attention right now.</p>
      )}
      <ul className="mt-2 space-y-2">
        {c.problems.map((problem) => (
          <li key={problem.key} className="rounded-xl bg-slate-950/60 border border-slate-800 p-3">
            <div className="flex items-start justify-between gap-2 flex-wrap">
              <div>
                <p className="text-sm font-semibold">{problem.title}</p>
                <p className="text-xs text-slate-400">{problem.detail}</p>
              </div>
              <div className="text-right shrink-0">
                <p className="text-sm font-semibold tabular-nums">{impactLabel(problem)}</p>
                <p className="text-[10px] uppercase tracking-wide text-slate-500">{problem.rank_basis}</p>
              </div>
            </div>
            {problem.why.map((w, i) => (
              <p key={i} className="text-xs text-slate-400 mt-2">
                <span className="text-[10px] uppercase tracking-wide rounded-full px-2 py-0.5 border border-slate-700 text-slate-300 mr-2">
                  {w.label}
                </span>
                {w.text}
              </p>
            ))}
            <div className="flex gap-3 mt-2">
              <button
                type="button"
                aria-expanded={open === problem.key}
                onClick={() => setOpen(open === problem.key ? null : problem.key)}
                className="text-xs text-slate-400 hover:text-white"
              >
                {open === problem.key ? "Hide" : "Show"} evidence ({problem.facts.length})
              </button>
              {onOpen && (
                <button type="button" onClick={() => onOpen(problem.view)} className="text-xs text-indigo-300 hover:text-indigo-200">
                  Open →
                </button>
              )}
            </div>
            {open === problem.key && <Facts facts={problem.facts} />}
          </li>
        ))}
      </ul>
      {c.problems.length > 1 && <p className="text-[11px] text-slate-500 mt-2">{c.overlap_note}</p>}

      <h3 className="text-sm font-semibold mt-5">What to do next</h3>
      <ul className="mt-2 space-y-2">
        {c.actions.map((a) => (
          <li key={a.key} className="rounded-xl bg-slate-950/60 border border-slate-800 p-3 flex items-start justify-between gap-2 flex-wrap">
            <div>
              <p className="text-sm">{a.title}</p>
              <p className="text-xs text-slate-400">{a.detail}</p>
              <p className="text-[11px] text-slate-500 mt-1">{a.who}</p>
            </div>
            <div className="text-right shrink-0">
              <span className="text-[10px] uppercase tracking-wide rounded-full px-2 py-0.5 border border-slate-700 text-slate-400">
                {a.state}
              </span>
              {onOpen && (
                <button type="button" onClick={() => onOpen(a.view)} className="block mt-2 text-xs text-indigo-300 hover:text-indigo-200">
                  Open →
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
