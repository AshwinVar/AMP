"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import { formatFactValue, provenanceTone, stateNotice, type Fact, type ProposableKind } from "../lib/evidence";
import { money } from "../lib/money";
import ProposeActionButton from "./ProposeActionButton";

// Mirrors ai/risk_radar.py (ADR-0026).
type Risk = {
  key: string; title: string; detail: string; likelihood: string; rule: string; horizon: string;
  module: string; view: string; facts: Fact[];
  impact_units: number | null; impact_money: number | null; currency: string | null;
  // ADR-0039. `action` is what to do about it, always a sentence — this card
  // used to end in a deep link, which made a list of eight things about to go
  // wrong read as an alarm panel rather than an advisor. `propose` is the
  // stricter thing: a draft AMP can actually carry out. Most risks have none,
  // and that is correct — "chase the customer" is not AMP's to do.
  action?: string | null;
  propose?: { kind: ProposableKind; machine_id: number } | null;
};
type Radar = {
  generated_at: string; state: string; headline: string;
  measured_rate_per_day: number | null; risks: Risk[]; note: string;
};

const LIKELIHOOD_CLASS: Record<string, string> = {
  LIKELY: "text-red-300 border-red-500/40",
  POSSIBLE: "text-amber-300 border-amber-500/40",
  WATCH: "text-slate-400 border-slate-600",
};

const PROV_CLASS: Record<string, string> = {
  measured: "text-emerald-300 border-emerald-500/40",
  derived: "text-sky-300 border-sky-500/40",
  correlation: "text-amber-300 border-amber-500/40",
  rule: "text-violet-300 border-violet-500/40",
  model: "text-fuchsia-300 border-fuchsia-500/40",
  unknown: "text-slate-400 border-slate-600",
};

/** What a risk would cost, as this card may state it. */
export function riskSize(r: Pick<Risk, "impact_money" | "impact_units">): string | null {
  if (r.impact_money != null) return money(r.impact_money);
  if (r.impact_units != null) return `${r.impact_units.toLocaleString()} units`;
  return null;
}

// The Production Risk Radar (ADR-0026): what is likely to become a problem, each
// with the rule and the measurement that produced it. No probabilities: AMP has
// no calibrated forecast, and a number would imply one.
export default function RiskRadarSection({ onOpen }: { onOpen?: (viewKey: string) => void }) {
  const [r, setR] = useState<Radar | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setR(await apiGet<Radar>("/risk-radar"));
    } catch {
      // Stay quiet rather than break the page.
    }
  }, []);
  useEffect(() => {
    let alive = true;
    const tick = () => { if (alive) load(); };
    const first = setTimeout(tick, 0);
    const id = setInterval(tick, 120000);
    return () => { alive = false; clearTimeout(first); clearInterval(id); };
  }, [load]);

  if (!r) return null;
  const notice = stateNotice(r.state);
  return (
    <section className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h2 className="text-xl font-bold">Risk radar</h2>
          <p className="text-slate-300 text-sm mt-1">{r.headline}</p>
        </div>
        {r.measured_rate_per_day != null && (
          <span className="text-[11px] text-slate-500">
            measured output {r.measured_rate_per_day.toLocaleString()} units/day
          </span>
        )}
      </div>
      {notice && <p role="status" className="text-amber-300/90 text-xs mt-2">{r.state} · {notice}</p>}

      <ul className="mt-4 space-y-2">
        {r.risks.map((risk) => (
          <li key={risk.key} className="rounded-xl bg-slate-950/60 border border-slate-800 p-3">
            <div className="flex items-start justify-between gap-2 flex-wrap">
              <div>
                <p className="text-sm font-semibold">{risk.title}</p>
                <p className="text-xs text-slate-400">{risk.detail}</p>
                {/* The rule, always visible: a likelihood word with no rule behind
                    it is the thing this card exists not to be. */}
                <p className="text-xs text-slate-500 mt-1">Rule: {risk.rule}</p>
              </div>
              <div className="text-right shrink-0">
                <span className={`text-[10px] uppercase tracking-wide rounded-full px-2 py-0.5 border ${LIKELIHOOD_CLASS[risk.likelihood] ?? LIKELIHOOD_CLASS.WATCH}`}>
                  {risk.likelihood}
                </span>
                {riskSize(risk) && <p className="text-sm font-semibold tabular-nums mt-1">{riskSize(risk)}</p>}
                <p className="text-[10px] text-slate-500">{risk.horizon}</p>
              </div>
            </div>
            {risk.action && (
              <p className="text-sm text-slate-200 mt-2">
                <span className="text-[10px] uppercase tracking-wide text-indigo-300/80 mr-2">Do</span>
                {risk.action}
              </p>
            )}
            {risk.propose && (
              <ProposeActionButton
                kind={risk.propose.kind}
                machineId={risk.propose.machine_id}
                label="Propose a maintenance task"
                onOpen={onOpen}
                compact
              />
            )}
            <div className="flex gap-3 mt-2">
              {risk.facts.length > 0 && (
                <button
                  type="button"
                  aria-expanded={open === risk.key}
                  onClick={() => setOpen(open === risk.key ? null : risk.key)}
                  className="text-xs text-slate-400 hover:text-white"
                >
                  {open === risk.key ? "Hide" : "Show"} evidence ({risk.facts.length})
                </button>
              )}
              {onOpen && (
                <button type="button" onClick={() => onOpen(risk.view)} className="text-xs text-indigo-300 hover:text-indigo-200">
                  Open →
                </button>
              )}
            </div>
            {open === risk.key && (
              <ul className="mt-2 space-y-1">
                {risk.facts.map((f) => (
                  <li key={f.key} className="text-xs flex flex-wrap items-baseline gap-x-2">
                    <span className="text-slate-400">{f.label}</span>
                    <span className="text-white font-semibold tabular-nums">{formatFactValue(f)}</span>
                    <span className={`text-[10px] uppercase tracking-wide rounded-full px-2 py-0.5 border ${PROV_CLASS[provenanceTone(f.provenance)]}`}>
                      {f.provenance}
                    </span>
                    {f.detail && <span className="text-slate-500">· {f.detail}</span>}
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
      {r.risks.length === 0 && <p className="text-slate-400 text-sm mt-2">Nothing on the radar right now.</p>}
      <p className="text-[11px] text-slate-500 mt-3">{r.note}</p>
    </section>
  );
}
